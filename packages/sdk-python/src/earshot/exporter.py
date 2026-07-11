"""Fail-open, bounded incident export primitives.

Voice callbacks enqueue already-sanitized bytes with ``put_nowait``. Network I/O,
retry, and backoff happen only on the worker thread. Queue overflow is observable
through diagnostics and never blocks or raises into the voice loop.
"""

from __future__ import annotations

import contextlib
import ipaddress
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

INCIDENT_PROTOBUF = "application/vnd.earshot.incident+protobuf"
INCIDENT_JSON = "application/vnd.earshot.incident+json"


@dataclass(frozen=True)
class ExportItem:
    bundle_id: str
    payload: bytes
    content_type: str = INCIDENT_PROTOBUF


@dataclass(frozen=True)
class ExportDiagnostic:
    code: str
    bundle_id: str
    attempt: int = 0
    retryable: bool = False


class ExportTransport(Protocol):
    def send(self, item: ExportItem) -> None: ...


class HttpExportTransport:
    def __init__(self, endpoint: str, *, token: str | None = None, timeout: float = 10.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("export endpoint must be an absolute HTTP(S) URL")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if parsed.scheme != "https" and not loopback:
            raise ValueError("non-loopback export endpoints require HTTPS")
        normalized = endpoint.rstrip("/")
        self.endpoint = (
            normalized if normalized.endswith("/v1/incidents") else normalized + "/v1/incidents"
        )
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def send(self, item: ExportItem) -> None:
        headers = {
            "Content-Type": item.content_type,
            "Accept": INCIDENT_JSON,
            "Idempotency-Key": item.bundle_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.endpoint,
            data=item.payload,
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"unexpected ingest status {response.status}")
        except urllib.error.HTTPError as error:
            # Validation/conflict errors are permanent. Server and rate-limit errors
            # are safe to retry because ingest is content-addressed and idempotent.
            if error.code < 500 and error.code != 429:
                raise PermanentExportError(f"ingest rejected bundle ({error.code})") from None
            raise


class PermanentExportError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward bearer credentials to a redirected origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del request, fp, code, msg, headers, newurl
        return None


class BoundedAsyncExporter:
    def __init__(
        self,
        transport: ExportTransport,
        *,
        capacity: int = 128,
        max_attempts: int = 3,
        base_backoff: float = 0.1,
        diagnostic: Callable[[ExportDiagnostic], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._transport = transport
        self._queue: queue.Queue[ExportItem | object] = queue.Queue(maxsize=capacity)
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._diagnostic = diagnostic or (lambda _: None)
        self._closed = False
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._worker = threading.Thread(target=self._run, name="earshot-export", daemon=True)
        self._worker.start()

    def _notify(self, diagnostic: ExportDiagnostic) -> None:
        # User callbacks are outside our trust boundary and cannot become an
        # application failure path.
        with contextlib.suppress(Exception):
            self._diagnostic(diagnostic)

    def submit(self, item: ExportItem) -> bool:
        """Enqueue without waiting. Returns false when evidence was omitted."""

        diagnostic: ExportDiagnostic | None = None
        with self._lock:
            if self._closed:
                diagnostic = ExportDiagnostic("exporter.closed", item.bundle_id)
            else:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    diagnostic = ExportDiagnostic("exporter.queue_full", item.bundle_id)
        if diagnostic is not None:
            self._notify(diagnostic)
            return False
        return True

    def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        with self._lock:
            if self._closed:
                return not self._worker.is_alive()
            self._closed = True
            self._stop_requested.set()
        self.flush(timeout=max(0.0, timeout / 2))
        self._worker.join(timeout=max(0.0, timeout / 2))
        return not self._worker.is_alive()

    def _run(self) -> None:
        while True:
            if self._stop_requested.is_set() and self._queue.empty():
                return
            try:
                queued = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                assert isinstance(queued, ExportItem)
                self._send_with_retry(queued)
            finally:
                self._queue.task_done()

    def _send_with_retry(self, item: ExportItem) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._transport.send(item)
                return
            except PermanentExportError:
                self._notify(ExportDiagnostic("exporter.rejected", item.bundle_id, attempt, False))
                return
            except Exception:  # transport failures must never escape the worker
                retryable = attempt < self._max_attempts
                self._notify(
                    ExportDiagnostic("exporter.failed", item.bundle_id, attempt, retryable)
                )
                if not retryable:
                    return
                time.sleep(self._base_backoff * (2 ** (attempt - 1)))

    def __enter__(self) -> BoundedAsyncExporter:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
