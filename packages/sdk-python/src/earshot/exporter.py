"""Fail-open, bounded incident export primitives.

Voice callbacks enqueue already-sanitized bytes with ``put_nowait``. Network I/O,
retry, and backoff happen only on the worker thread. Queue overflow is observable
through diagnostics and never blocks or raises into the voice loop.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import ipaddress
import json
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .context import suppress_instrumentation

INCIDENT_PROTOBUF = "application/vnd.earshot.incident+protobuf"
INCIDENT_JSON = "application/vnd.earshot.incident+json"

# Envelope-encryption constants for the durable spool at rest. A single AES-256
# key protects every record; each record carries its own random 96-bit nonce.
_SPOOL_KEY_BYTES = 32
_SPOOL_NONCE_BYTES = 12
_SPOOL_FORMAT_AESGCM_V1 = "aesgcm-v1"


def _import_aesgcm() -> type:
    """Return the AES-GCM primitive, importing ``cryptography`` lazily.

    Isolated in its own function so the base install never imports the optional
    dependency, and so tests can simulate the "requested but not installed"
    condition by patching this symbol to raise ``ImportError``.
    """

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM


def _coerce_spool_key(value: bytes | str) -> bytes:
    """Normalize a configured spool key to raw 32 AES-256 bytes.

    A ``bytes`` value of length 32 is treated as a raw key; any other ``bytes``
    value and every ``str`` value is interpreted as base64 that must decode to
    exactly 32 bytes (a 32-byte key is 44 base64 characters).
    """

    if isinstance(value, str):
        candidate = value.strip().encode("ascii")
    else:
        value = bytes(value)
        if len(value) == _SPOOL_KEY_BYTES:
            return value
        candidate = value.strip()
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except ValueError:
        raise ValueError(
            "spool key must be 32 raw bytes or a base64 encoding of 32 bytes"
        ) from None
    if len(decoded) != _SPOOL_KEY_BYTES:
        raise ValueError("spool key must decode to 32 bytes for AES-256")
    return decoded


def _read_spool_key_file(path: Path) -> bytes:
    """Load a spool key from ``EARSHOT_SPOOL_KEY_FILE`` (raw/base64, mode 0600)."""

    if path.is_symlink():
        raise ValueError("EARSHOT_SPOOL_KEY_FILE must not be a symbolic link")
    if not path.is_file():
        raise ValueError("EARSHOT_SPOOL_KEY_FILE must reference a regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError(
            "EARSHOT_SPOOL_KEY_FILE must not be accessible by group or other users (chmod 600)"
        )
    return _coerce_spool_key(path.read_bytes())


def _resolve_spool_key(explicit: bytes | str | None) -> bytes | None:
    """Resolve the at-rest spool key by precedence, or ``None`` for plaintext.

    Precedence: explicit ``spool_key`` argument, then ``EARSHOT_SPOOL_KEY``
    (base64), then ``EARSHOT_SPOOL_KEY_FILE``. When nothing is configured the
    spool stays plaintext and behavior is unchanged.
    """

    if explicit is not None:
        return _coerce_spool_key(explicit)
    inline = os.environ.get("EARSHOT_SPOOL_KEY")
    if inline:
        return _coerce_spool_key(inline)
    key_file = os.environ.get("EARSHOT_SPOOL_KEY_FILE")
    if key_file:
        return _read_spool_key_file(Path(key_file))
    return None


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


@dataclass(frozen=True)
class ExporterStatus:
    state: str
    pid: int
    accepted: int
    sent: int
    dropped: int
    failed: int
    rejected: int
    pending: int
    queued_bytes: int
    in_flight_bytes: int = 0
    high_water_bytes: int = 0
    oldest_age_seconds: float | None = None
    retried: int = 0
    overflow: int = 0
    last_success_at_unix_nano: int | None = None
    last_failure: str | None = None
    spool_depth: int = 0
    spool_bytes: int = 0
    replayed: int = 0
    abandoned: int = 0
    retained_rejections: int = 0
    spool_fingerprint: str | None = None
    spool_root_fingerprint: str | None = None


@dataclass(frozen=True)
class _PendingExport:
    item: ExportItem
    enqueued_at: float


class ExportTransport(Protocol):
    def send(self, item: ExportItem) -> None: ...


class HttpExportTransport:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        project_id: str | None = None,
        timeout: float = 10.0,
        compression_threshold_bytes: int | None = 16 * 1024,
    ):
        if timeout <= 0:
            raise ValueError("export timeout must be positive")
        if compression_threshold_bytes is not None and compression_threshold_bytes < 1:
            raise ValueError("compression_threshold_bytes must be positive or None")
        if project_id is not None and (not project_id or project_id != project_id.strip()):
            raise ValueError("project_id must be a non-empty trimmed string or None")
        if endpoint != endpoint.strip() or any(ord(character) < 33 for character in endpoint):
            raise ValueError("export endpoint must not contain whitespace or control characters")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("export endpoint must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("export endpoint must not contain userinfo")
        if parsed.query:
            raise ValueError("export endpoint must not contain a query")
        if parsed.fragment:
            raise ValueError("export endpoint must not contain a fragment")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("export endpoint contains an invalid port") from error
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
        self._token = token
        self.project_id = project_id
        self.timeout = timeout
        self.compression_threshold_bytes = compression_threshold_bytes
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def send(self, item: ExportItem) -> None:
        self._send(item, timeout=self.timeout)

    def send_with_timeout(self, item: ExportItem, *, timeout: float) -> None:
        """Send one attempt while respecting a smaller exporter-wide deadline."""

        if timeout <= 0:
            raise TimeoutError("export attempt deadline expired")
        self._send(item, timeout=min(self.timeout, timeout))

    def _send(self, item: ExportItem, *, timeout: float) -> None:
        headers = {
            "Content-Type": item.content_type,
            "Accept": INCIDENT_JSON,
            "Idempotency-Key": item.bundle_id,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self.project_id is not None:
            headers["X-Earshot-Project-Id"] = self.project_id
        payload = item.payload
        if (
            self.compression_threshold_bytes is not None
            and len(payload) >= self.compression_threshold_bytes
        ):
            payload = gzip.compress(payload, mtime=0)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(payload))
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with (
                suppress_instrumentation(),
                self._opener.open(request, timeout=timeout) as response,
            ):
                if response.status not in (200, 201):
                    raise RuntimeError(f"unexpected ingest status {response.status}")
        except urllib.error.HTTPError as error:
            # Validation/conflict errors are permanent. Server and rate-limit errors
            # are safe to retry because ingest is content-addressed and idempotent.
            if error.code == 408 or error.code == 429 or error.code >= 500:
                retry_after: float | None = None
                raw_retry_after = error.headers.get("Retry-After") if error.headers else None
                if raw_retry_after is not None:
                    try:
                        retry_after = max(0.0, float(raw_retry_after))
                    except ValueError:
                        with contextlib.suppress(TypeError, ValueError, OverflowError):
                            retry_at = parsedate_to_datetime(raw_retry_after).timestamp()
                            retry_after = max(0.0, retry_at - time.time())
                raise RetryableExportError(
                    f"ingest temporarily unavailable ({error.code})",
                    retry_after=retry_after,
                ) from None
            if error.code < 500:
                raise PermanentExportError(f"ingest rejected bundle ({error.code})") from None
            raise

    def __repr__(self) -> str:
        return f"HttpExportTransport(endpoint={self.endpoint!r}, timeout={self.timeout!r})"


class PermanentExportError(RuntimeError):
    pass


class RetryableExportError(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        max_queue_bytes: int = 16 * 1024 * 1024,
        max_attempts: int = 3,
        base_backoff: float = 0.1,
        jitter_ratio: float = 0.2,
        max_backoff: float = 60.0,
        total_attempt_deadline: float = 30.0,
        diagnostic: Callable[[ExportDiagnostic], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_queue_bytes < 1:
            raise ValueError("max_queue_bytes must be positive")
        if base_backoff < 0:
            raise ValueError("base_backoff cannot be negative")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if max_backoff <= 0:
            raise ValueError("max_backoff must be positive")
        if total_attempt_deadline <= 0:
            raise ValueError("total_attempt_deadline must be positive")
        self._transport = transport
        self._capacity = capacity
        self._max_queue_bytes = max_queue_bytes
        self._queue: queue.Queue[_PendingExport] = queue.Queue(maxsize=capacity)
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._jitter_ratio = jitter_ratio
        self._max_backoff = max_backoff
        self._total_attempt_deadline = total_attempt_deadline
        self._diagnostic = diagnostic or (lambda _: None)
        self._closed = False
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._queued_bytes = 0
        self._in_flight_bytes = 0
        self._in_flight_enqueued_at: float | None = None
        self._queued_times: deque[float] = deque()
        self._high_water_bytes = 0
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._overflow = 0
        self._last_success_at_unix_nano: int | None = None
        self._last_failure: str | None = None
        self._stop_requested = threading.Event()
        self._worker = threading.Thread(target=self._run, name="earshot-export", daemon=True)
        self._worker.start()

    def _ensure_pid(self) -> None:
        """Discard inherited thread state and start a usable worker after fork."""

        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        # Never acquire inherited synchronization primitives: another thread may
        # have held them at fork and that owner no longer exists in the child.
        was_closed = self._closed
        self._pid = current_pid
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=self._capacity)
        self._stop_requested = threading.Event()
        self._queued_bytes = 0
        self._in_flight_bytes = 0
        self._in_flight_enqueued_at = None
        self._queued_times = deque()
        self._high_water_bytes = 0
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._overflow = 0
        self._last_success_at_unix_nano = None
        self._last_failure = None
        self._closed = was_closed
        self._worker = threading.Thread(
            target=self._run,
            name="earshot-export",
            daemon=True,
        )
        if not was_closed:
            self._worker.start()

    def _notify(self, diagnostic: ExportDiagnostic) -> None:
        # User callbacks are outside our trust boundary and cannot become an
        # application failure path.
        with contextlib.suppress(Exception):
            self._diagnostic(diagnostic)

    def submit(self, item: ExportItem) -> bool:
        """Enqueue without waiting. Returns false when evidence was omitted."""

        self._ensure_pid()
        diagnostic: ExportDiagnostic | None = None
        enqueued_at = time.monotonic()
        with self._lock:
            if self._closed:
                self._dropped += 1
                self._last_failure = "exporter.closed"
                diagnostic = ExportDiagnostic("exporter.closed", item.bundle_id)
            elif (
                self._queued_bytes + self._in_flight_bytes + len(item.payload)
                > self._max_queue_bytes
            ):
                self._dropped += 1
                self._overflow += 1
                self._last_failure = "exporter.queue_bytes_full"
                diagnostic = ExportDiagnostic("exporter.queue_bytes_full", item.bundle_id)
            else:
                try:
                    self._queue.put_nowait(_PendingExport(item, enqueued_at))
                    self._queued_bytes += len(item.payload)
                    self._queued_times.append(enqueued_at)
                    self._high_water_bytes = max(
                        self._high_water_bytes,
                        self._queued_bytes + self._in_flight_bytes,
                    )
                    self._accepted += 1
                except queue.Full:
                    self._dropped += 1
                    self._overflow += 1
                    self._last_failure = "exporter.queue_full"
                    diagnostic = ExportDiagnostic("exporter.queue_full", item.bundle_id)
        if diagnostic is not None:
            self._notify(diagnostic)
            return False
        return True

    def flush(self, timeout: float | None = None) -> bool:
        self._ensure_pid()
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        self._ensure_pid()
        with self._lock:
            if self._closed:
                already_closed = True
            else:
                already_closed = False
                self._closed = True
                self._stop_requested.set()
        if threading.current_thread() is self._worker:
            return False
        if already_closed:
            self._worker.join(timeout=max(0.0, timeout))
            return not self._worker.is_alive()
        self.flush(timeout=max(0.0, timeout / 2))
        self._worker.join(timeout=max(0.0, timeout / 2))
        return not self._worker.is_alive()

    def _run(self) -> None:
        while True:
            if self._stop_requested.is_set() and self._queue.empty():
                return
            try:
                pending = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    self._queued_bytes -= len(pending.item.payload)
                    self._in_flight_bytes += len(pending.item.payload)
                    self._in_flight_enqueued_at = pending.enqueued_at
                    if self._queued_times:
                        self._queued_times.popleft()
                self._send_with_retry(pending.item)
            finally:
                with self._lock:
                    self._in_flight_bytes -= len(pending.item.payload)
                    self._in_flight_enqueued_at = None
                self._queue.task_done()

    def _send_with_retry(self, item: ExportItem) -> None:
        deadline = time.monotonic() + self._total_attempt_deadline
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                remaining = deadline - time.monotonic()
                deadline_sender = getattr(self._transport, "send_with_timeout", None)
                if callable(deadline_sender):
                    deadline_sender(item, timeout=remaining)
                else:
                    self._transport.send(item)
                with self._lock:
                    self._sent += 1
                    self._last_success_at_unix_nano = time.time_ns()
                return
            except PermanentExportError:
                with self._lock:
                    self._rejected += 1
                    self._last_failure = "transport.permanent_rejection"
                self._notify(ExportDiagnostic("exporter.rejected", item.bundle_id, attempt, False))
                return
            except RetryableExportError as error:
                retry_after = error.retry_after
                failure = "transport.retryable"
            except Exception:  # transport failures must never escape the worker
                failure = "transport.failure"
            retryable = attempt < self._max_attempts
            with self._lock:
                self._last_failure = failure
            if not retryable:
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, False))
                with self._lock:
                    self._failed += 1
                return
            exponential = self._base_backoff * (2 ** (attempt - 1))
            jittered = exponential * random.uniform(
                1 - self._jitter_ratio,
                1 + self._jitter_ratio,
            )
            delay = min(max(jittered, retry_after or 0.0), self._max_backoff)
            if time.monotonic() + delay >= deadline:
                with self._lock:
                    self._failed += 1
                    self._last_failure = "exporter.attempt_deadline_exceeded"
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, False))
                return
            self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, True))
            with self._lock:
                self._retried += 1
            if self._stop_requested.wait(delay):
                with self._lock:
                    self._failed += 1
                    self._last_failure = "exporter.shutdown_during_retry"
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, False))
                return

    def status(self) -> ExporterStatus:
        self._ensure_pid()
        with self._lock:
            oldest_enqueued_at = min(
                (
                    value
                    for value in (
                        self._queued_times[0] if self._queued_times else None,
                        self._in_flight_enqueued_at,
                    )
                    if value is not None
                ),
                default=None,
            )
            return ExporterStatus(
                state=(
                    "closing"
                    if self._closed and self._worker.is_alive()
                    else "closed"
                    if self._closed
                    else "running"
                ),
                pid=self._pid,
                accepted=self._accepted,
                sent=self._sent,
                dropped=self._dropped,
                failed=self._failed,
                rejected=self._rejected,
                pending=self._queue.unfinished_tasks,
                queued_bytes=self._queued_bytes,
                in_flight_bytes=self._in_flight_bytes,
                high_water_bytes=self._high_water_bytes,
                oldest_age_seconds=(
                    max(0.0, time.monotonic() - oldest_enqueued_at)
                    if oldest_enqueued_at is not None
                    else None
                ),
                retried=self._retried,
                overflow=self._overflow,
                last_success_at_unix_nano=self._last_success_at_unix_nano,
                last_failure=self._last_failure,
            )

    def __enter__(self) -> BoundedAsyncExporter:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()


class SynchronousExporter:
    """Deliver on the caller thread within a bounded total retry deadline.

    ``submit()`` returns true only after the remote transport acknowledges the
    incident. It never creates a worker thread.
    """

    def __init__(
        self,
        transport: ExportTransport,
        *,
        max_attempts: int = 3,
        base_backoff: float = 0.1,
        jitter_ratio: float = 0.2,
        max_backoff: float = 60.0,
        max_elapsed: float = 10.0,
        diagnostic: Callable[[ExportDiagnostic], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_backoff < 0:
            raise ValueError("base_backoff cannot be negative")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if max_backoff <= 0 or max_elapsed <= 0:
            raise ValueError("retry deadlines must be positive")
        self._transport = transport
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._jitter_ratio = jitter_ratio
        self._max_backoff = max_backoff
        self._max_elapsed = max_elapsed
        self._diagnostic = diagnostic or (lambda _: None)
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._closed = False
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._last_success_at_unix_nano: int | None = None
        self._last_failure: str | None = None

    def _ensure_pid(self) -> None:
        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        was_closed = self._closed
        self._pid = current_pid
        self._lock = threading.Lock()
        self._closed = was_closed
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._last_success_at_unix_nano = None
        self._last_failure = None

    def _notify(self, diagnostic: ExportDiagnostic) -> None:
        with contextlib.suppress(Exception):
            self._diagnostic(diagnostic)

    def submit(self, item: ExportItem) -> bool:
        self._ensure_pid()
        with self._lock:
            if self._closed:
                self._dropped += 1
                self._last_failure = "exporter.closed"
                closed = True
            else:
                closed = False
        if closed:
            self._notify(ExportDiagnostic("exporter.closed", item.bundle_id))
            return False

        deadline = time.monotonic() + self._max_elapsed
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("export deadline expired")
                deadline_sender = getattr(self._transport, "send_with_timeout", None)
                if callable(deadline_sender):
                    deadline_sender(item, timeout=remaining)
                else:
                    self._transport.send(item)
                with self._lock:
                    self._accepted += 1
                    self._sent += 1
                    self._last_success_at_unix_nano = time.time_ns()
                return True
            except PermanentExportError:
                with self._lock:
                    self._rejected += 1
                    self._last_failure = "transport.permanent_rejection"
                self._notify(ExportDiagnostic("exporter.rejected", item.bundle_id, attempt, False))
                return False
            except RetryableExportError as error:
                retry_after = error.retry_after
                failure = "transport.retryable"
            except Exception:
                failure = "transport.failure"

            remaining = deadline - time.monotonic()
            retryable = attempt < self._max_attempts and remaining > 0
            with self._lock:
                self._last_failure = failure
            if not retryable:
                with self._lock:
                    self._failed += 1
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, False))
                return False
            exponential = self._base_backoff * (2 ** (attempt - 1))
            jittered = exponential * random.uniform(
                1 - self._jitter_ratio,
                1 + self._jitter_ratio,
            )
            delay = min(max(jittered, retry_after or 0.0), self._max_backoff)
            if delay >= remaining:
                with self._lock:
                    self._failed += 1
                    self._last_failure = "exporter.attempt_deadline_exceeded"
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, False))
                return False
            self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, True))
            with self._lock:
                self._retried += 1
            time.sleep(delay)
        return False  # pragma: no cover - the bounded loop always returns

    def flush(self, timeout: float | None = None) -> bool:
        del timeout
        self._ensure_pid()
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        del timeout
        self._ensure_pid()
        with self._lock:
            self._closed = True
        return True

    def status(self) -> ExporterStatus:
        self._ensure_pid()
        with self._lock:
            return ExporterStatus(
                state="closed" if self._closed else "running",
                pid=self._pid,
                accepted=self._accepted,
                sent=self._sent,
                dropped=self._dropped,
                failed=self._failed,
                rejected=self._rejected,
                pending=0,
                queued_bytes=0,
                retried=self._retried,
                last_success_at_unix_nano=self._last_success_at_unix_nano,
                last_failure=self._last_failure,
            )


class DurableExporter:
    """Atomically commit incidents to a private disk spool before return.

    Choosing this exporter and an explicit ``spool_dir`` is the local-storage
    opt-in. ``submit()`` means the exact payload and idempotency key were fsynced
    locally; remote acknowledgement is reported later by ``status().sent``.

    When a spool key is configured (``spool_key`` argument, ``EARSHOT_SPOOL_KEY``,
    or ``EARSHOT_SPOOL_KEY_FILE``) each record's payload is envelope-encrypted with
    AES-256-GCM before the fsync/atomic-replace, and decrypted on the delivery
    path. With no key configured the spool stays plaintext and behavior is
    unchanged. Encryption is key-based erasure ("crypto-shredding"): destroying
    the key permanently renders existing encrypted records undecryptable, and they
    quarantine on read rather than crashing or delivering.

    Note: live active-session crash journaling (a resumable tail of the in-flight
    session) is intentionally out of scope here; it needs a live protocol and is
    tracked as future work.
    """

    _VERSION = 1

    def __init__(
        self,
        transport: ExportTransport,
        *,
        spool_dir: Path,
        destination_fingerprint: str | None = None,
        max_spool_items: int = 1024,
        max_spool_bytes: int = 256 * 1024 * 1024,
        permanent_rejection_policy: str = "retain",
        spool_key: bytes | str | None = None,
        max_attempts: int = 3,
        base_backoff: float = 0.1,
        jitter_ratio: float = 0.2,
        max_backoff: float = 60.0,
        max_elapsed: float = 30.0,
        diagnostic: Callable[[ExportDiagnostic], None] | None = None,
    ) -> None:
        if max_spool_items < 1 or max_spool_bytes < 1:
            raise ValueError("spool item and byte caps must be positive")
        if permanent_rejection_policy not in {"retain", "delete"}:
            raise ValueError("permanent_rejection_policy must be retain or delete")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_backoff < 0:
            raise ValueError("base_backoff cannot be negative")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if max_backoff <= 0 or max_elapsed <= 0:
            raise ValueError("retry deadlines must be positive")
        self._transport = transport
        self._spool_dir = Path(spool_dir)
        self._spool_root_fingerprint = hashlib.sha256(
            str(self._spool_dir.resolve()).encode()
        ).hexdigest()
        self._destination_fingerprint = (
            destination_fingerprint or hashlib.sha256(b"earshot.standalone").hexdigest()
        )
        if len(self._destination_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self._destination_fingerprint
        ):
            raise ValueError("destination_fingerprint must be 64 lowercase hexadecimal characters")
        self._max_spool_items = max_spool_items
        self._max_spool_bytes = max_spool_bytes
        self._permanent_rejection_policy = permanent_rejection_policy
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._jitter_ratio = jitter_ratio
        self._max_backoff = max_backoff
        self._max_elapsed = max_elapsed
        self._diagnostic = diagnostic or (lambda _: None)
        # Fail closed: if a key is configured but the optional dependency is
        # absent, refuse to construct rather than silently spooling plaintext.
        spool_key_bytes = _resolve_spool_key(spool_key)
        if spool_key_bytes is None:
            self._cipher = None
        else:
            try:
                aesgcm = _import_aesgcm()
            except ImportError as error:
                raise RuntimeError(
                    "spool encryption is configured (spool_key / EARSHOT_SPOOL_KEY / "
                    "EARSHOT_SPOOL_KEY_FILE) but the 'cryptography' package is not installed; "
                    "install earshot-observability[spool-encryption]"
                ) from error
            self._cipher = aesgcm(spool_key_bytes)
        self._prepare_private_directory(self._spool_dir)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()
        self._pid = os.getpid()
        self._closed = False
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._overflow = 0
        self._replayed = 0
        self._in_flight_files: set[str] = set()
        self._new_records: set[str] = set()
        self._retry_cycles: dict[str, int] = {}
        self._retry_not_before: dict[str, float] = {}
        _, _, initial_spool_bytes, _, _, _ = self._disk_status()
        self._high_water_bytes = initial_spool_bytes
        self._last_success_at_unix_nano: int | None = None
        self._last_failure: str | None = None
        self._recover_temporary_files()
        self._worker = threading.Thread(
            target=self._run,
            name="earshot-durable-export",
            daemon=True,
        )
        self._worker.start()

    @staticmethod
    def _prepare_private_directory(path: Path) -> None:
        if path.exists() and path.is_symlink():
            raise ValueError("spool_dir must not be a symbolic link")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("spool_dir must be a directory")
        if path.stat().st_mode & 0o077:
            raise ValueError("spool_dir must not be accessible by group or other users")

    def _ensure_pid(self) -> None:
        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        was_closed = self._closed
        self._pid = current_pid
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()
        self._closed = was_closed
        self._accepted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._rejected = 0
        self._retried = 0
        self._overflow = 0
        self._replayed = 0
        self._in_flight_files = set()
        self._new_records = set()
        self._retry_cycles = {}
        self._retry_not_before = {}
        _, _, initial_spool_bytes, _, _, _ = self._disk_status()
        self._high_water_bytes = initial_spool_bytes
        self._last_success_at_unix_nano = None
        self._last_failure = None
        self._worker = threading.Thread(
            target=self._run,
            name="earshot-durable-export",
            daemon=True,
        )
        if not was_closed:
            self._worker.start()

    def _notify(self, diagnostic: ExportDiagnostic) -> None:
        with contextlib.suppress(Exception):
            self._diagnostic(diagnostic)

    def _spool_aad(self, bundle_id: str, content_type: str) -> bytes:
        """Associated data binding a record to its identity.

        Authenticated (not encrypted) alongside the ciphertext so a record cannot
        be replayed against a different route, bundle id, or content type: any
        tampering with these cleartext fields fails GCM tag verification on read.
        """

        return json.dumps(
            {
                "spool_format": _SPOOL_FORMAT_AESGCM_V1,
                "version": DurableExporter._VERSION,
                "destination_fingerprint": self._destination_fingerprint,
                "bundle_id": bundle_id,
                "content_type": content_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _encode(self, item: ExportItem) -> bytes:
        document = {
            "version": DurableExporter._VERSION,
            "destination_fingerprint": self._destination_fingerprint,
            "bundle_id": item.bundle_id,
            "content_type": item.content_type,
            "payload_sha256": hashlib.sha256(item.payload).hexdigest(),
        }
        if self._cipher is None:
            document["payload_base64"] = base64.b64encode(item.payload).decode("ascii")
        else:
            nonce = os.urandom(_SPOOL_NONCE_BYTES)
            aad = self._spool_aad(item.bundle_id, item.content_type)
            ciphertext = self._cipher.encrypt(nonce, item.payload, aad)
            document["spool_format"] = _SPOOL_FORMAT_AESGCM_V1
            document["nonce_b64"] = base64.b64encode(nonce).decode("ascii")
            document["ciphertext_b64"] = base64.b64encode(ciphertext).decode("ascii")
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _decode(self, data: bytes) -> ExportItem:
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("version") != DurableExporter._VERSION:
            raise ValueError("unsupported spool record")
        if document.get("destination_fingerprint") != self._destination_fingerprint:
            raise ValueError("spool record destination mismatch")
        bundle_id = document.get("bundle_id")
        content_type = document.get("content_type")
        checksum = document.get("payload_sha256")
        spool_format = document.get("spool_format")
        if spool_format is not None:
            payload = self._decrypt_record(document, bundle_id, content_type, spool_format)
        else:
            encoded = document.get("payload_base64")
            if not all(
                isinstance(value, str) for value in (bundle_id, content_type, encoded, checksum)
            ):
                raise ValueError("invalid spool record")
            payload = base64.b64decode(encoded, validate=True)
        if not isinstance(checksum, str) or hashlib.sha256(payload).hexdigest() != checksum:
            raise ValueError("spool checksum mismatch")
        return ExportItem(bundle_id=bundle_id, content_type=content_type, payload=payload)

    def _decrypt_record(
        self,
        document: dict,
        bundle_id: object,
        content_type: object,
        spool_format: object,
    ) -> bytes:
        if spool_format != _SPOOL_FORMAT_AESGCM_V1:
            raise ValueError("unsupported spool format")
        nonce_b64 = document.get("nonce_b64")
        ciphertext_b64 = document.get("ciphertext_b64")
        if not all(
            isinstance(value, str) for value in (bundle_id, content_type, nonce_b64, ciphertext_b64)
        ):
            raise ValueError("invalid spool record")
        # No key at read time means the key was never present or was destroyed
        # (crypto-shredding). Treat as unreadable/corrupt: quarantine, never crash.
        if self._cipher is None:
            raise ValueError("spool record is encrypted but no spool key is available")
        nonce = base64.b64decode(nonce_b64, validate=True)
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        aad = self._spool_aad(bundle_id, content_type)
        try:
            return self._cipher.decrypt(nonce, ciphertext, aad)
        except Exception as error:  # InvalidTag (wrong key / AAD / tamper) or malformed input
            raise ValueError("spool record failed authenticated decryption") from error

    def _spool_files(self) -> list[Path]:
        return sorted(self._spool_dir.glob(f"{self._destination_fingerprint}-*.spool"))

    def _retained_files(self) -> list[Path]:
        return sorted(self._spool_dir.glob(f"{self._destination_fingerprint}-*.rejected"))

    def _disk_status(self) -> tuple[int, int, int, int, int, float | None]:
        active = self._spool_files()
        recognized: list[Path] = []
        candidates = [
            *self._spool_dir.glob("*.spool"),
            *self._spool_dir.glob("*.rejected"),
            *self._spool_dir.glob(".*.tmp"),
        ]
        quarantine = self._spool_dir / "quarantine"
        if quarantine.is_dir():
            candidates.extend(quarantine.glob("*.corrupt"))
        for path in candidates:
            name = path.name.lstrip(".")
            fingerprint = name.split("-", 1)[0]
            if len(fingerprint) == 64 and all(
                character in "0123456789abcdef" for character in fingerprint
            ):
                recognized.append(path)
        total_bytes = 0
        oldest_modified: float | None = None
        for path in recognized:
            with contextlib.suppress(FileNotFoundError):
                file_status = path.lstat()
                total_bytes += file_status.st_size
                oldest_modified = min(oldest_modified or file_status.st_mtime, file_status.st_mtime)
        retained_count = sum(path.suffix == ".rejected" for path in recognized)
        active_names = {path.name for path in active}
        current_active_count = sum(path.name in active_names for path in recognized)
        return (
            len(active),
            len(recognized),
            total_bytes,
            len(recognized) - current_active_count,
            retained_count,
            max(0.0, time.time() - oldest_modified) if oldest_modified is not None else None,
        )

    def _fsync_directory(self) -> None:
        descriptor = os.open(self._spool_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def submit(self, item: ExportItem) -> bool:
        """Return true after an atomic local commit, before remote delivery."""

        self._ensure_pid()
        encoded = self._encode(item)
        diagnostic: ExportDiagnostic | None = None
        with self._lock:
            if self._closed:
                self._dropped += 1
                self._last_failure = "exporter.closed"
                diagnostic = ExportDiagnostic("exporter.closed", item.bundle_id)
            else:
                _, depth, used_bytes, _, _, _ = self._disk_status()
                if (
                    depth >= self._max_spool_items
                    or used_bytes + len(encoded) > self._max_spool_bytes
                ):
                    self._dropped += 1
                    self._overflow += 1
                    self._last_failure = "exporter.spool_full"
                    diagnostic = ExportDiagnostic("exporter.spool_full", item.bundle_id)
                else:
                    prefix = (
                        f"{self._destination_fingerprint}-{time.time_ns():020d}-{uuid.uuid4().hex}"
                    )
                    temporary = self._spool_dir / f".{prefix}.tmp"
                    destination = self._spool_dir / f"{prefix}.spool"
                    try:
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        descriptor = os.open(temporary, flags, 0o600)
                        with os.fdopen(descriptor, "wb", closefd=True) as handle:
                            handle.write(encoded)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.chmod(temporary, 0o600)
                        os.replace(temporary, destination)
                        self._fsync_directory()
                    except OSError:
                        with contextlib.suppress(FileNotFoundError):
                            temporary.unlink()
                        self._dropped += 1
                        self._failed += 1
                        self._last_failure = "exporter.spool_write_failed"
                        diagnostic = ExportDiagnostic(
                            "exporter.spool_write_failed",
                            item.bundle_id,
                        )
                    else:
                        self._accepted += 1
                        self._new_records.add(destination.name)
                        self._high_water_bytes = max(
                            self._high_water_bytes,
                            used_bytes + len(encoded),
                        )
        if diagnostic is not None:
            self._notify(diagnostic)
            return False
        self._wake.set()
        return True

    def _quarantine(self, path: Path) -> None:
        quarantine = self._spool_dir / "quarantine"
        self._prepare_private_directory(quarantine)
        destination = quarantine / f"{path.name.lstrip('.')}.corrupt"
        with contextlib.suppress(FileNotFoundError):
            os.replace(path, destination)
            if not destination.is_symlink():
                os.chmod(destination, 0o600)
            self._fsync_directory()
        with self._lock:
            self._new_records.discard(path.name)
            self._retry_cycles.pop(path.name, None)
            self._retry_not_before.pop(path.name, None)
            self._last_failure = "exporter.spool_corrupt"
        self._notify(ExportDiagnostic("exporter.spool_corrupt", path.name))

    def _recover_temporary_files(self) -> None:
        for path in self._spool_dir.glob(f".{self._destination_fingerprint}-*.tmp"):
            self._quarantine(path)

    def _delete(self, path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            self._fsync_directory()

    def _send_file(self, path: Path) -> bool:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("spool record is not a regular file")
            item = self._decode(path.read_bytes())
        except Exception:
            self._quarantine(path)
            return True

        deadline = time.monotonic() + self._max_elapsed
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("export deadline expired")
                deadline_sender = getattr(self._transport, "send_with_timeout", None)
                if callable(deadline_sender):
                    deadline_sender(item, timeout=remaining)
                else:
                    self._transport.send(item)
                self._delete(path)
                with self._lock:
                    self._sent += 1
                    if path.name not in self._new_records:
                        self._replayed += 1
                    self._new_records.discard(path.name)
                    self._retry_cycles.pop(path.name, None)
                    self._retry_not_before.pop(path.name, None)
                    self._last_success_at_unix_nano = time.time_ns()
                return True
            except PermanentExportError:
                with self._lock:
                    self._rejected += 1
                    self._last_failure = "transport.permanent_rejection"
                self._notify(ExportDiagnostic("exporter.rejected", item.bundle_id, attempt, False))
                if self._permanent_rejection_policy == "delete":
                    self._delete(path)
                else:
                    retained = path.with_suffix(".rejected")
                    with contextlib.suppress(FileNotFoundError):
                        os.replace(path, retained)
                        os.chmod(retained, 0o600)
                        self._fsync_directory()
                with self._lock:
                    self._new_records.discard(path.name)
                    self._retry_cycles.pop(path.name, None)
                    self._retry_not_before.pop(path.name, None)
                return True
            except RetryableExportError as error:
                retry_after = error.retry_after
                failure = "transport.retryable"
            except Exception:
                failure = "transport.failure"

            remaining = deadline - time.monotonic()
            retryable = attempt < self._max_attempts and remaining > 0
            with self._lock:
                self._last_failure = failure
            if not retryable:
                with self._lock:
                    self._retried += 1
                    self._last_failure = "transport.retryable_pending"
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, True))
                return False
            exponential = self._base_backoff * (2 ** (attempt - 1))
            jittered = exponential * random.uniform(
                1 - self._jitter_ratio,
                1 + self._jitter_ratio,
            )
            delay = min(max(jittered, retry_after or 0.0), self._max_backoff)
            if delay >= remaining:
                with self._lock:
                    self._retried += 1
                    self._last_failure = "exporter.attempt_deadline_exceeded"
                self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, True))
                return False
            self._notify(ExportDiagnostic("exporter.failed", item.bundle_id, attempt, True))
            with self._lock:
                self._retried += 1
            if self._stop_requested.wait(delay):
                return False
        return False  # pragma: no cover - the bounded loop always returns

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            files = self._spool_files()
            if not files:
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            now = time.monotonic()
            ready = [path for path in files if self._retry_not_before.get(path.name, 0.0) <= now]
            if not ready:
                next_retry = min(self._retry_not_before.get(path.name, now + 0.1) for path in files)
                self._wake.wait(max(0.0, min(0.1, next_retry - now)))
                self._wake.clear()
                continue
            made_progress = False
            for path in ready:
                if self._stop_requested.is_set():
                    return
                with self._lock:
                    self._in_flight_files.add(path.name)
                try:
                    if self._send_file(path):
                        made_progress = True
                    else:
                        cycle = self._retry_cycles.get(path.name, 0) + 1
                        self._retry_cycles[path.name] = cycle
                        cross_cycle_delay = min(
                            self._max_backoff,
                            max(0.05, self._base_backoff) * (2 ** min(cycle - 1, 20)),
                        )
                        self._retry_not_before[path.name] = time.monotonic() + cross_cycle_delay
                finally:
                    with self._lock:
                        self._in_flight_files.discard(path.name)
                    self._wake.set()
            if not made_progress:
                self._wake.wait(0.01)
                self._wake.clear()

    def flush(self, timeout: float | None = None) -> bool:
        self._ensure_pid()
        with self._lock:
            if self._closed:
                return not self._spool_files() and not self._in_flight_files
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        self._wake.set()
        while True:
            with self._lock:
                in_flight = bool(self._in_flight_files)
            if not self._spool_files() and not in_flight:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def shutdown(self, timeout: float = 5.0) -> bool:
        self._ensure_pid()
        with self._lock:
            self._closed = True
            self._stop_requested.set()
            self._wake.set()
        if threading.current_thread() is self._worker:
            return False
        self._worker.join(timeout=max(0.0, timeout))
        return not self._worker.is_alive()

    def status(self) -> ExporterStatus:
        self._ensure_pid()
        (
            _active,
            depth,
            spool_bytes,
            abandoned,
            retained_rejections,
            oldest_age_seconds,
        ) = self._disk_status()
        with self._lock:
            pending_names = {path.name for path in self._spool_files()} | self._in_flight_files
            return ExporterStatus(
                state=(
                    "closing"
                    if self._closed and self._worker.is_alive()
                    else "closed"
                    if self._closed
                    else "running"
                ),
                pid=self._pid,
                accepted=self._accepted,
                sent=self._sent,
                dropped=self._dropped,
                failed=self._failed,
                rejected=self._rejected,
                pending=len(pending_names),
                queued_bytes=spool_bytes,
                high_water_bytes=self._high_water_bytes,
                oldest_age_seconds=oldest_age_seconds,
                retried=self._retried,
                overflow=self._overflow,
                last_success_at_unix_nano=self._last_success_at_unix_nano,
                last_failure=self._last_failure,
                spool_depth=depth,
                spool_bytes=spool_bytes,
                replayed=self._replayed,
                abandoned=abandoned,
                retained_rejections=retained_rejections,
                spool_fingerprint=self._destination_fingerprint,
                spool_root_fingerprint=self._spool_root_fingerprint,
            )
