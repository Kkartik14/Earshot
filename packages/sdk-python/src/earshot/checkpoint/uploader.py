"""Ship an open session's journal frames to a backend, off the voice path.

The journal on disk is the source of truth; this only forwards bytes that are
already there. That ordering is the whole design: the uploader tails the file
from its own daemon thread, so a slow or unreachable backend can never add
latency to a voice callback, and a failed upload degrades to "local journal
only" rather than to lost evidence.

It is fail-open in the strict sense. Any error — network, HTTP, malformed
response — stops the uploader permanently and records why. It never retries into
a hot loop, never buffers without a bound, and never raises into its caller.

Every size bound it obeys comes from :mod:`earshot.checkpoint.limits`, which is
also what the ingest API reads. A frame the journal permits but the wire cannot
carry is refused *here*, by name and sequence, rather than posted into a
guaranteed rejection: see ``STATE_UNDELIVERABLE``.
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

from .limits import (
    DEFAULT_MAX_BATCH_BYTES,
    MAX_CHECKPOINT_BATCH_BYTES,
    MAX_CHECKPOINT_FRAME_BYTES,
)

CHECKPOINT_MEDIA_TYPE = "application/vnd.earshot.checkpoint+frames"

DEFAULT_BATCH_INTERVAL_MS = 500

# Following the journal, and expected to keep following it.
STATE_READY = "ready"
# Something outside this process refused or broke. Local journalling continues.
STATE_DEGRADED = "degraded"
# A frame this journal holds is larger than any upload may carry. It cannot be
# skipped — the server refuses a batch that skips a sequence, and a client must
# never choose where the server's evidence stops — so remote coverage of this
# session ends here, at a named sequence, and says so.
STATE_UNDELIVERABLE = "undeliverable_frame"

DIAGNOSTIC_UPLOAD_FAILED = "checkpoint.upload_failed"
DIAGNOSTIC_FRAME_UNDELIVERABLE = "checkpoint.frame_undeliverable"


@dataclass(frozen=True)
class UploaderStatus:
    """What this uploader has done, and what it has stopped doing.

    ``undeliverable_sequence`` and ``undeliverable_bytes`` are set only in
    ``STATE_UNDELIVERABLE``, and they name the exact frame that ended remote
    coverage. A caller can therefore tell "the backend is unreachable" from
    "this session holds a record too large to stream", which are different
    problems with different answers.
    """

    state: str
    uploaded_bytes: int
    batches: int
    last_failure: str | None
    undeliverable_sequence: int | None = None
    undeliverable_bytes: int | None = None


class CheckpointFrameUndeliverable(Exception):
    """One whole frame exceeds every bound an upload is permitted to carry."""

    def __init__(self, sequence: int, size: int) -> None:
        super().__init__(
            f"journal frame {sequence} is {size} bytes, above the "
            f"{MAX_CHECKPOINT_FRAME_BYTES}-byte checkpoint upload bound"
        )
        self.sequence = sequence
        self.size = size


@dataclass(frozen=True, slots=True)
class _BatchPlan:
    """Where the next batch ends, or which frame makes there be no next batch."""

    end: int
    undeliverable: CheckpointFrameUndeliverable | None = None


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

    ``max_batch_bytes`` is a cadence, not a licence: it decides how many frames
    travel per request and is itself clamped to
    :data:`~earshot.checkpoint.limits.MAX_CHECKPOINT_BATCH_BYTES`, which is what
    ingest accepts. One whole frame is always sent alone rather than stranded
    below that bound, and a frame above it ends remote coverage explicitly
    instead of being posted into a certain rejection.
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
        if max_batch_bytes > MAX_CHECKPOINT_BATCH_BYTES:
            # A budget above the wire bound would build batches the server is
            # required to refuse, so it is a misconfiguration, not a preference.
            raise ValueError(
                f"max_batch_bytes must not exceed {MAX_CHECKPOINT_BATCH_BYTES}, "
                "the largest body checkpoint ingest accepts"
            )
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
        self._state = STATE_READY
        self._last_failure: str | None = None
        self._undeliverable: CheckpointFrameUndeliverable | None = None
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
            undeliverable = self._undeliverable
            return UploaderStatus(
                state=self._state,
                uploaded_bytes=self._uploaded,
                batches=self._batches,
                last_failure=self._last_failure,
                undeliverable_sequence=None if undeliverable is None else undeliverable.sequence,
                undeliverable_bytes=None if undeliverable is None else undeliverable.size,
            )

    def flush(self) -> bool:
        """Send whatever whole frames the journal has gained. Never raises."""

        with self._lock:
            if self._state != STATE_READY:
                return False
            offset = self._offset
        try:
            data = self.journal.read_bytes()
        except OSError as error:
            self._fail(error)
            return False
        plan = _plan_batch(data, offset, self._max_batch_bytes)
        if plan.end <= offset:
            if plan.undeliverable is not None:
                self._stop_at_undeliverable(plan.undeliverable)
            return False
        batch = data[offset : plan.end]
        try:
            self._post(batch)
        except Exception as error:
            self._fail(error)
            return False
        with self._lock:
            self._offset = plan.end
            self._uploaded += len(batch)
            self._batches += 1
        return True

    # -------------------------------------------------------------- internal

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self.flush() and self._state != STATE_READY:
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
            if self._state != STATE_READY:
                return
            self._state = STATE_DEGRADED
            self._last_failure = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            with contextlib.suppress(Exception):
                error.close()
        self._emit(DIAGNOSTIC_UPLOAD_FAILED)

    def _stop_at_undeliverable(self, error: CheckpointFrameUndeliverable) -> None:
        """Declare the frame that ends remote coverage, then stop for good.

        A distinct state from ``degraded`` because it is a distinct fact: no
        retry, no backend, and no configuration on this side will ever deliver
        this frame, so reporting it as a transport failure would invite exactly
        the retry that cannot work.
        """

        with self._lock:
            if self._state != STATE_READY:
                return
            self._state = STATE_UNDELIVERABLE
            self._undeliverable = error
            self._last_failure = type(error).__name__
        self._emit(DIAGNOSTIC_FRAME_UNDELIVERABLE)

    def _emit(self, reason: str) -> None:
        diagnostic = self._diagnostic
        if diagnostic is None:
            return
        from ..exporter import ExportDiagnostic  # lazy: keeps the import graph acyclic

        with contextlib.suppress(Exception):
            diagnostic(ExportDiagnostic(reason, self.session_id))


def _plan_batch(data: bytes, offset: int, budget: int) -> _BatchPlan:
    """Walk whole frames from ``offset`` and say where the next batch ends.

    A partially written trailing frame is left for the next pass. The server
    refuses a torn batch whole, so cutting mid-frame would throw away a batch
    that the very next read would have completed.

    Two bounds are honoured and they are not the same bound. ``budget`` stops
    the batch growing; a single frame that exceeds it is still sent alone,
    because a frame is indivisible and stranding it would stall the stream
    silently. :data:`MAX_CHECKPOINT_FRAME_BYTES` is what the wire accepts at
    all; a frame above it is reported rather than sent, and only once everything
    ahead of it has been delivered.
    """

    from .framing import CHECKSUM_SIZE, HEADER_SIZE, SEPARATOR

    end = offset
    while end + HEADER_SIZE <= len(data):
        if data[end] != SEPARATOR:
            break
        length = int.from_bytes(data[end + 5 : end + HEADER_SIZE], "big")
        frame_end = end + HEADER_SIZE + length + CHECKSUM_SIZE
        size = frame_end - end
        if size > MAX_CHECKPOINT_FRAME_BYTES:
            # Known from the length prefix alone, so a frame that can never be
            # delivered is declared without waiting for the rest of it to land.
            if end > offset:
                break  # deliver what precedes it first, then say so
            sequence = int.from_bytes(data[end + 1 : end + 5], "big")
            return _BatchPlan(end, CheckpointFrameUndeliverable(sequence, size))
        if frame_end > len(data):
            break
        if frame_end - offset > budget and end > offset:
            break
        end = frame_end
    return _BatchPlan(end)


__all__ = [
    "CHECKPOINT_MEDIA_TYPE",
    "DEFAULT_BATCH_INTERVAL_MS",
    "DEFAULT_MAX_BATCH_BYTES",
    "DIAGNOSTIC_FRAME_UNDELIVERABLE",
    "DIAGNOSTIC_UPLOAD_FAILED",
    "STATE_DEGRADED",
    "STATE_READY",
    "STATE_UNDELIVERABLE",
    "CheckpointFrameUndeliverable",
    "CheckpointUploader",
    "UploaderStatus",
]
