"""Ship an open session's journal frames to a backend, off the voice path.

The journal on disk is the source of truth; this only forwards bytes that are
already there. That ordering is the whole design: the uploader tails the file
from its own daemon thread, so a slow or unreachable backend can never add
latency to a voice callback, and a failed upload degrades to "local journal
only" rather than to lost evidence.

It is fail-open in the strict sense. Any error — network, HTTP, malformed
response — stops the uploader permanently and records why. It never retries into
a hot loop, never buffers without a bound, and never raises into its caller.
"""

from __future__ import annotations

import contextlib
import ipaddress
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

CHECKPOINT_MEDIA_TYPE = "application/vnd.earshot.checkpoint+frames"

DEFAULT_BATCH_INTERVAL_MS = 500
DEFAULT_MAX_BATCH_BYTES = 512 * 1024

DIAGNOSTIC_UPLOAD_FAILED = "checkpoint.upload_failed"


@dataclass(frozen=True)
class UploaderStatus:
    state: str
    uploaded_bytes: int
    batches: int
    last_failure: str | None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential to a redirected origin."""

    def redirect_request(self, *_: object) -> None:
        return None


class CheckpointUploader:
    """Tail one journal file and POST its new frames to a live backend.

    Batching is bounded twice over: by time (``batch_interval_ms``) and by size
    (``max_batch_bytes``). A batch is always cut at a frame boundary, because the
    server refuses a torn upload whole — which is correct, since a client must
    never get to decide where the server's evidence stops.
    """

    def __init__(
        self,
        endpoint: str,
        journal: Path | str,
        session_id: str,
        *,
        token: str | None = None,
        project_id: str | None = None,
        timeout: float = 5.0,
        batch_interval_ms: int = DEFAULT_BATCH_INTERVAL_MS,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        diagnostic: object = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("checkpoint upload timeout must be positive")
        if batch_interval_ms < 1:
            raise ValueError("batch_interval_ms must be a positive integer")
        if max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be a positive integer")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("checkpoint endpoint must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("checkpoint endpoint must not contain userinfo")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if parsed.scheme != "https" and not loopback:
            # Journal frames carry the same governed metadata an incident does.
            raise ValueError("non-loopback checkpoint endpoints require HTTPS")
        base = endpoint.rstrip("/")
        if base.endswith("/v1/live"):
            base = base[: -len("/v1/live")]
        self.endpoint = f"{base}/v1/live/sessions/{quote(session_id, safe='')}/checkpoints"
        self.journal = Path(journal)
        self.session_id = session_id
        self._token = token
        self._project_id = project_id
        self._timeout = timeout
        self._interval = batch_interval_ms / 1000.0
        self._max_batch_bytes = max_batch_bytes
        self._diagnostic = diagnostic
        self._opener = urllib.request.build_opener(_RejectRedirects())
        self._lock = threading.Lock()
        self._offset = 0
        self._uploaded = 0
        self._batches = 0
        self._degraded = False
        self._last_failure: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="earshot-checkpoint-upload",
            daemon=True,
        )
        self._thread.start()

    def close(self, *, drain: bool = True) -> None:
        """Stop uploading, optionally after one last pass over the journal."""

        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._timeout + 1.0)
        if drain:
            self.flush()

    def status(self) -> UploaderStatus:
        with self._lock:
            return UploaderStatus(
                state="degraded" if self._degraded else "ready",
                uploaded_bytes=self._uploaded,
                batches=self._batches,
                last_failure=self._last_failure,
            )

    def flush(self) -> bool:
        """Send whatever whole frames the journal has gained. Never raises."""

        with self._lock:
            if self._degraded:
                return False
            offset = self._offset
        try:
            data = self.journal.read_bytes()
        except OSError as error:
            self._fail(error)
            return False
        end = _last_frame_boundary(data, offset, self._max_batch_bytes)
        if end <= offset:
            return False
        batch = data[offset:end]
        try:
            self._post(batch)
        except Exception as error:
            self._fail(error)
            return False
        with self._lock:
            self._offset = end
            self._uploaded += len(batch)
            self._batches += 1
        return True

    # -------------------------------------------------------------- internal

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self.flush() and self._degraded:
                return

    def _post(self, batch: bytes) -> None:
        headers = {
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Content-Length": str(len(batch)),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._project_id is not None:
            headers["X-Earshot-Project-Id"] = self._project_id
        request = urllib.request.Request(
            self.endpoint,
            data=batch,
            headers=headers,
            method="POST",
        )
        with self._opener.open(request, timeout=self._timeout) as response:
            if response.status != 202:
                raise RuntimeError(f"unexpected checkpoint status {response.status}")

    def _fail(self, error: BaseException) -> None:
        with self._lock:
            if self._degraded:
                return
            self._degraded = True
            self._last_failure = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            with contextlib.suppress(Exception):
                error.close()
        diagnostic = self._diagnostic
        if diagnostic is None:
            return
        from ..exporter import ExportDiagnostic  # lazy: keeps the import graph acyclic

        with contextlib.suppress(Exception):
            diagnostic(ExportDiagnostic(DIAGNOSTIC_UPLOAD_FAILED, self.session_id))


def _last_frame_boundary(data: bytes, offset: int, budget: int) -> int:
    """Walk whole frames from ``offset`` and stop at the last complete one.

    A partially written trailing frame is left for the next pass. The server
    refuses a torn batch whole, so cutting mid-frame would throw away a batch
    that the very next read would have completed.
    """

    from .framing import CHECKSUM_SIZE, HEADER_SIZE, SEPARATOR

    end = offset
    while end + HEADER_SIZE <= len(data) and end - offset < budget:
        if data[end] != SEPARATOR:
            break
        length = int.from_bytes(data[end + 5 : end + HEADER_SIZE], "big")
        frame_end = end + HEADER_SIZE + length + CHECKSUM_SIZE
        if frame_end > len(data):
            break
        end = frame_end
    return end


__all__ = [
    "CHECKPOINT_MEDIA_TYPE",
    "DEFAULT_BATCH_INTERVAL_MS",
    "DEFAULT_MAX_BATCH_BYTES",
    "CheckpointUploader",
    "UploaderStatus",
]
