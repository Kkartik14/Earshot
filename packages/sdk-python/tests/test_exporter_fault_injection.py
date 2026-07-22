from __future__ import annotations

import contextlib
import gzip
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from earshot.exporter import BoundedAsyncExporter, ExportItem, HttpExportTransport

pytestmark = pytest.mark.integration


class _FaultServer(ThreadingHTTPServer):
    daemon_threads = True


class FaultCollector:
    def __init__(self, actions: list[object]) -> None:
        self.actions = actions
        self.attempts: list[tuple[str, bytes, str | None]] = []
        self.committed: dict[str, bytes] = {}
        self.commit_count = 0
        self.received = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                wire_payload = self.rfile.read(length)
                encoding = self.headers.get("Content-Encoding")
                payload = gzip.decompress(wire_payload) if encoding == "gzip" else wire_payload
                bundle_id = self.headers.get("Idempotency-Key", "")
                with collector._lock:
                    attempt_index = len(collector.attempts)
                    collector.attempts.append((bundle_id, payload, encoding))
                    action = collector.actions[min(attempt_index, len(collector.actions) - 1)]
                collector.received.set()

                if action == "block":
                    assert collector.release.wait(5)
                    collector._commit(bundle_id, payload)
                    self._respond(201)
                    return
                if action == "drop_after_commit":
                    collector._commit(bundle_id, payload)
                    self.close_connection = True
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if isinstance(action, tuple) and action[0] == "slow":
                    collector._commit(bundle_id, payload)
                    time.sleep(float(action[1]))
                    self._respond(201)
                    return

                status, retry_after = action if isinstance(action, tuple) else (action, None)
                if int(status) in {200, 201}:
                    collector._commit(bundle_id, payload)
                self._respond(int(status), retry_after=retry_after)

            def _respond(self, status: int, *, retry_after: str | None = None) -> None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.send_response(status)
                    if retry_after is not None:
                        self.send_header("Retry-After", retry_after)
                    self.end_headers()

            def log_message(self, *_args: object) -> None:
                return None

        self.server = _FaultServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _commit(self, bundle_id: str, payload: bytes) -> None:
        with self._lock:
            first_commit = bundle_id not in self.committed
            previous = self.committed.setdefault(bundle_id, payload)
            assert previous == payload
            if first_commit:
                self.commit_count += 1

    def __enter__(self) -> FaultCollector:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@contextlib.contextmanager
def exporter_for(
    collector: FaultCollector,
    *,
    timeout: float = 0.5,
    capacity: int = 8,
    max_queue_bytes: int = 1024,
    max_attempts: int = 3,
    total_attempt_deadline: float = 2,
) -> Iterator[BoundedAsyncExporter]:
    transport = HttpExportTransport(
        collector.endpoint,
        timeout=timeout,
        compression_threshold_bytes=8,
    )
    exporter = BoundedAsyncExporter(
        transport,
        capacity=capacity,
        max_queue_bytes=max_queue_bytes,
        max_attempts=max_attempts,
        base_backoff=0,
        jitter_ratio=0,
        total_attempt_deadline=total_attempt_deadline,
    )
    try:
        yield exporter
    finally:
        exporter.shutdown(timeout=2)


def test_lost_response_after_commit_retries_same_idempotent_canonical_body() -> None:
    with (
        FaultCollector(["drop_after_commit", 200]) as collector,
        exporter_for(collector) as exporter,
    ):
        item = ExportItem("lost-response", b"canonical-incident-payload")
        assert exporter.submit(item)
        assert exporter.flush(timeout=2)
        status = exporter.status()

    assert len(collector.attempts) == 2
    assert collector.commit_count == 1
    assert collector.committed == {item.bundle_id: item.payload}
    assert {(bundle_id, payload) for bundle_id, payload, _ in collector.attempts} == {
        (item.bundle_id, item.payload)
    }
    assert all(encoding == "gzip" for _, _, encoding in collector.attempts)
    assert status.sent == 1
    assert status.retried == 1
    assert status.last_success_at_unix_nano is not None
    assert status.last_failure == "transport.failure"


@pytest.mark.parametrize(
    ("failure_status", "retry_after"),
    [(408, None), (429, "0.02"), (500, None), (503, "0")],
)
def test_retryable_http_statuses_reach_collector_once_after_retry(
    failure_status: int, retry_after: str | None
) -> None:
    with (
        FaultCollector([(failure_status, retry_after), 201]) as collector,
        exporter_for(collector) as exporter,
    ):
        started = time.monotonic()
        assert exporter.submit(ExportItem(f"retry-{failure_status}", b"retry-payload"))
        assert exporter.flush(timeout=2)
        elapsed = time.monotonic() - started
        status = exporter.status()

    assert len(collector.attempts) == 2
    assert collector.commit_count == 1
    assert status.sent == 1
    assert status.retried == 1
    if retry_after == "0.02":
        assert elapsed >= 0.02


def test_slow_responses_time_out_with_bounded_attempts_and_sanitized_failure() -> None:
    with (
        FaultCollector([("slow", 0.1)]) as collector,
        exporter_for(
            collector,
            timeout=0.02,
            max_attempts=2,
        ) as exporter,
    ):
        assert exporter.submit(ExportItem("slow-secret-id", b"secret-body"))
        assert exporter.flush(timeout=1)
        status = exporter.status()

    assert len(collector.attempts) == 2
    assert collector.commit_count == 1
    assert status.failed == 1
    assert status.retried == 1
    assert status.last_failure == "transport.failure"
    assert "secret" not in status.last_failure


def test_total_attempt_deadline_clamps_real_http_request_timeout() -> None:
    with (
        FaultCollector([("slow", 0.3)]) as collector,
        exporter_for(
            collector,
            timeout=10,
            max_attempts=2,
            total_attempt_deadline=0.05,
        ) as exporter,
    ):
        started = time.monotonic()
        assert exporter.submit(ExportItem("deadline-http", b"payload"))
        assert exporter.flush(timeout=1)
        elapsed = time.monotonic() - started
        status = exporter.status()

    assert elapsed < 0.2
    assert len(collector.attempts) == 1
    assert status.failed == 1
    assert status.retried == 0
    assert status.last_failure == "exporter.attempt_deadline_exceeded"


@pytest.mark.parametrize("failure_status", [401, 422])
def test_permanent_http_rejections_are_not_retried(failure_status: int) -> None:
    with (
        FaultCollector([failure_status]) as collector,
        exporter_for(collector) as exporter,
    ):
        assert exporter.submit(ExportItem(f"reject-{failure_status}", b"payload"))
        assert exporter.flush(timeout=1)
        status = exporter.status()

    assert len(collector.attempts) == 1
    assert collector.commit_count == 0
    assert status.rejected == 1
    assert status.retried == 0
    assert status.last_failure == "transport.permanent_rejection"


def test_queue_pressure_accounts_waiting_in_flight_high_water_age_and_overflow() -> None:
    with (
        FaultCollector(["block", 201]) as collector,
        exporter_for(
            collector,
            capacity=1,
            max_queue_bytes=24,
        ) as exporter,
    ):
        assert exporter.submit(ExportItem("in-flight", b"a" * 12))
        assert collector.received.wait(2)
        assert exporter.submit(ExportItem("waiting", b"b" * 12))
        assert not exporter.submit(ExportItem("overflow", b"c"))
        status = exporter.status()

        assert status.pending == 2
        assert status.queued_bytes == 12
        assert status.in_flight_bytes == 12
        assert status.high_water_bytes == 24
        assert status.oldest_age_seconds is not None
        assert status.overflow == 1
        assert status.dropped == 1
        collector.release.set()
        assert exporter.flush(timeout=2)

    assert [attempt[0] for attempt in collector.attempts] == ["in-flight", "waiting"]
