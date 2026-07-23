from __future__ import annotations

import asyncio
import builtins
import gzip
import os
import select
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from earshot.adapters import LiveKitAdapter, PipecatAdapter, routing
from earshot.adapters.base import AdapterDependencyError, seconds_to_nano
from earshot.clock import ManualClock
from earshot.context import is_instrumentation_suppressed
from earshot.contract import ErrorRecord, Evidence
from earshot.exporter import (
    BoundedAsyncExporter,
    ExportDiagnostic,
    ExportItem,
    HttpExportTransport,
    PermanentExportError,
    RetryableExportError,
)
from earshot.privacy import CaptureClass, CapturePolicy
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident
from incident_factory import ROOT_SPAN_ID, SECRET_SENTINEL, TRACE_ID, point

pytestmark = pytest.mark.unit


class RecordingTransport:
    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def send(self, item: ExportItem) -> None:
        self.items.append(item)


class BlockingTransport:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.items: list[ExportItem] = []

    def send(self, item: ExportItem) -> None:
        self.items.append(item)
        self.started.set()
        assert self.release.wait(5)


class FlakyTransport:
    def __init__(self, failures: int, *, permanent: bool = False) -> None:
        self.failures = failures
        self.permanent = permanent
        self.attempts = 0

    def send(self, _item: ExportItem) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            if self.permanent:
                raise PermanentExportError("rejected")
            raise OSError("temporary")


def test_exporter_rejects_invalid_capacity_and_attempt_count() -> None:
    transport = RecordingTransport()
    with pytest.raises(ValueError, match="capacity"):
        BoundedAsyncExporter(transport, capacity=0)
    with pytest.raises(ValueError, match="max_attempts"):
        BoundedAsyncExporter(transport, max_attempts=0)
    with pytest.raises(ValueError, match="base_backoff"):
        BoundedAsyncExporter(transport, base_backoff=-0.1)
    with pytest.raises(ValueError, match="total_attempt_deadline"):
        BoundedAsyncExporter(transport, total_attempt_deadline=0)


def test_submit_is_nonblocking_and_queue_overflow_is_observable() -> None:
    transport = BlockingTransport()
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(transport, capacity=1, diagnostic=diagnostics.append)
    try:
        assert exporter.submit(ExportItem("one", b"1"))
        assert transport.started.wait(2)
        assert exporter.submit(ExportItem("two", b"2"))
        assert not exporter.submit(ExportItem("three", b"3"))
        assert diagnostics[-1].code == "exporter.queue_full"
        assert diagnostics[-1].bundle_id == "three"
    finally:
        transport.release.set()
        assert exporter.shutdown()


def test_shutdown_is_fail_open_when_worker_and_queue_are_both_blocked() -> None:
    transport = BlockingTransport()
    exporter = BoundedAsyncExporter(transport, capacity=1)
    assert exporter.submit(ExportItem("one", b"1"))
    assert transport.started.wait(2)
    assert exporter.submit(ExportItem("two", b"2"))

    # Shutdown is allowed to time out, but it must never leak queue.Full into an
    # application/context-manager cleanup path.
    assert not exporter.shutdown(timeout=0.02)
    transport.release.set()
    exporter._worker.join(2)
    assert exporter.shutdown()
    assert [item.bundle_id for item in transport.items] == ["one", "two"]


def test_exporter_retries_bounded_number_of_times_without_duplicate_success() -> None:
    transport = FlakyTransport(failures=2)
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(
        transport,
        max_attempts=4,
        base_backoff=0,
        diagnostic=diagnostics.append,
    )
    assert exporter.submit(ExportItem("bundle", b"payload"))
    assert exporter.flush(timeout=2)
    assert exporter.shutdown()
    assert transport.attempts == 3
    assert [(item.attempt, item.retryable) for item in diagnostics] == [(1, True), (2, True)]
    status = exporter.status()
    assert status.retried == 2
    assert status.last_success_at_unix_nano is not None
    assert status.last_failure == "transport.failure"
    assert status.in_flight_bytes == 0
    assert status.oldest_age_seconds is None


def test_permanent_export_rejection_is_not_retried() -> None:
    transport = FlakyTransport(failures=10, permanent=True)
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(
        transport, max_attempts=5, base_backoff=0, diagnostic=diagnostics.append
    )
    assert exporter.submit(ExportItem("bundle", b"payload"))
    assert exporter.flush(timeout=2)
    exporter.shutdown()
    assert transport.attempts == 1
    assert diagnostics == [ExportDiagnostic("exporter.rejected", "bundle", 1, False)]


def test_transport_failure_never_escapes_worker_and_exhaustion_is_reported() -> None:
    transport = FlakyTransport(failures=10)
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(
        transport, max_attempts=2, base_backoff=0, diagnostic=diagnostics.append
    )
    assert exporter.submit(ExportItem("bundle", b"payload"))
    assert exporter.flush(timeout=2)
    assert exporter.shutdown()
    assert diagnostics[-1] == ExportDiagnostic("exporter.failed", "bundle", 2, False)


def test_http_transport_posts_canonical_content_type_auth_and_idempotency(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["suppressed"] = is_instrumentation_suppressed()
        return Response()

    transport = HttpExportTransport("http://localhost:4319/", token="token", timeout=2.5)
    monkeypatch.setattr(transport._opener, "open", urlopen)
    item = ExportItem("bundle-id", b"payload")
    transport.send(item)
    request = captured["request"]
    assert request.full_url == "http://localhost:4319/v1/incidents"
    assert request.data == b"payload"
    assert request.get_header("Content-type") == item.content_type
    assert request.get_header("Authorization") == "Bearer token"
    assert request.get_header("Idempotency-key") == "bundle-id"
    assert captured["timeout"] == 2.5
    assert captured["suppressed"] is True


def test_http_transport_gzips_large_payload_without_changing_identity(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    transport = HttpExportTransport(
        "http://localhost:4319",
        compression_threshold_bytes=8,
    )

    def open_request(request, **_kwargs):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(transport._opener, "open", open_request)
    item = ExportItem("stable-bundle-id", b"canonical-payload")

    transport.send(item)

    request = captured["request"]
    assert request.get_header("Content-encoding") == "gzip"
    assert request.get_header("Idempotency-key") == item.bundle_id
    assert request.get_header("Content-length") == str(len(request.data))
    assert gzip.decompress(request.data) == item.payload


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:password@localhost:4319",
        "http://localhost:4319?token=secret",
        "http://localhost:4319#fragment",
    ],
)
def test_http_transport_rejects_ambiguous_or_credential_bearing_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"userinfo|query|fragment"):
        HttpExportTransport(endpoint)


def test_http_transport_repr_never_contains_bearer_token() -> None:
    transport = HttpExportTransport("http://localhost:4319", token=SECRET_SENTINEL)
    assert not hasattr(transport, "token")
    assert SECRET_SENTINEL not in repr(transport)


def test_http_transport_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout"):
        HttpExportTransport("http://localhost:4319", timeout=0)
    with pytest.raises(ValueError, match="compression_threshold_bytes"):
        HttpExportTransport("http://localhost:4319", compression_threshold_bytes=0)


def test_export_queue_is_bounded_by_payload_bytes() -> None:
    transport = BlockingTransport()
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(
        transport,
        capacity=10,
        max_queue_bytes=9,
        diagnostic=diagnostics.append,
    )
    try:
        assert exporter.submit(ExportItem("one", b"1234"))
        assert transport.started.wait(2)
        assert exporter.submit(ExportItem("two", b"12345"))
        assert not exporter.submit(ExportItem("three", b"1"))
        status = exporter.status()
        assert status.accepted == 2
        assert status.dropped == 1
        assert status.queued_bytes == 5
        assert status.in_flight_bytes == 4
        assert status.high_water_bytes == 9
        assert status.oldest_age_seconds is not None
        assert status.oldest_age_seconds >= 0
        assert status.overflow == 1
        assert diagnostics[-1].code == "exporter.queue_bytes_full"
    finally:
        transport.release.set()
        assert exporter.shutdown()


@pytest.mark.parametrize("status", [400, 409, 422])
def test_http_transport_classifies_client_rejection_as_permanent(monkeypatch, status: int) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://localhost", status, "bad", {}, None)

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    with pytest.raises(PermanentExportError):
        transport.send(ExportItem("bundle", b"payload"))


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_http_transport_leaves_retryable_http_failures_retryable(monkeypatch, status: int) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://localhost", status, "retry", {}, None)

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    with pytest.raises(RetryableExportError):
        transport.send(ExportItem("bundle", b"payload"))


def test_http_transport_preserves_retry_after_hint(monkeypatch) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost",
            429,
            "retry",
            {"Retry-After": "3.5"},
            None,
        )

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    with pytest.raises(RetryableExportError) as raised:
        transport.send(ExportItem("bundle", b"payload"))
    assert raised.value.retry_after == 3.5


def test_http_transport_accepts_http_date_retry_after(monkeypatch) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost",
            503,
            "retry",
            {"Retry-After": "Thu, 01 Jan 1970 00:00:05 GMT"},
            None,
        )

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    monkeypatch.setattr("earshot.exporter.time.time", lambda: 2.0)
    with pytest.raises(RetryableExportError) as raised:
        transport.send(ExportItem("bundle", b"payload"))
    assert raised.value.retry_after == 3.0


def test_exporter_uses_jitter_and_retry_after_without_blocking_submit(monkeypatch) -> None:
    class RetryAfterTransport:
        attempts = 0

        def send(self, _item) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RetryableExportError("busy", retry_after=0.02)

    transport = RetryAfterTransport()
    monkeypatch.setattr("earshot.exporter.random.uniform", lambda low, high: high)
    exporter = BoundedAsyncExporter(
        transport,
        max_attempts=2,
        base_backoff=0.1,
        jitter_ratio=0.25,
    )
    started = time.monotonic()
    assert exporter.submit(ExportItem("bundle", b"payload"))
    assert exporter.flush(timeout=2)
    assert exporter.shutdown()
    assert transport.attempts == 2
    assert time.monotonic() - started >= 0.02


def test_shutdown_interrupts_retry_after_backoff_and_accounts_failure() -> None:
    attempted = threading.Event()

    class RetryAfterTransport:
        def send(self, _item) -> None:
            attempted.set()
            raise RetryableExportError("busy", retry_after=30.0)

    exporter = BoundedAsyncExporter(
        RetryAfterTransport(), max_attempts=3, total_attempt_deadline=60
    )
    assert exporter.submit(ExportItem("bundle", b"payload"))
    assert attempted.wait(2)

    started = time.monotonic()
    assert exporter.shutdown(timeout=0.5)

    assert time.monotonic() - started < 0.5
    status = exporter.status()
    assert status.failed == 1
    assert status.retried == 1
    assert status.last_failure == "exporter.shutdown_during_retry"


def test_total_attempt_deadline_stops_retrying_before_next_backoff() -> None:
    transport = FlakyTransport(failures=100)
    exporter = BoundedAsyncExporter(
        transport,
        max_attempts=10,
        base_backoff=0.03,
        jitter_ratio=0,
        total_attempt_deadline=0.05,
    )
    assert exporter.submit(ExportItem("deadline", b"payload"))

    assert exporter.flush(timeout=1)
    assert exporter.shutdown()

    status = exporter.status()
    assert transport.attempts == 2
    assert status.failed == 1
    assert status.retried == 1
    assert status.last_failure == "exporter.attempt_deadline_exceeded"


def test_http_transport_rejects_unexpected_success_status(monkeypatch) -> None:
    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="unexpected ingest status"):
        transport.send(ExportItem("bundle", b"payload"))


def test_shutdown_and_submit_after_shutdown_are_idempotent() -> None:
    diagnostics: list[ExportDiagnostic] = []
    exporter = BoundedAsyncExporter(RecordingTransport(), diagnostic=diagnostics.append)
    assert exporter.shutdown()
    assert exporter.shutdown()
    assert not exporter.submit(ExportItem("late", b"payload"))
    assert diagnostics[-1].code == "exporter.closed"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_exporter_restarts_its_worker_after_fork() -> None:
    exporter = BoundedAsyncExporter(RecordingTransport(), base_backoff=0)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in the parent
        try:
            os.close(read_fd)
            submitted = exporter.submit(ExportItem("child", b"payload"))
            flushed = exporter.flush(timeout=2)
            child_status = exporter.status()
            result = f"{submitted},{flushed},{child_status.pid},{child_status.sent}"
            os.write(write_fd, result.encode())
            exporter.shutdown(timeout=1)
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        readable, _, _ = select.select([read_fd], [], [], 5)
        assert readable, "forked exporter child did not make progress"
        submitted, flushed, reported_pid, sent = os.read(read_fd, 256).decode().split(",")
        assert (submitted, flushed) == ("True", "True")
        assert int(reported_pid) == child_pid
        assert sent == "1"
        waited_pid, wait_status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(wait_status) == 0
    finally:
        os.close(read_fd)
        exporter.shutdown()


def test_recorder_uses_injected_monotonic_clock_and_closes_once() -> None:
    clock = ManualClock(wall=1_800_000_000_000_000_000, monotonic=50_000)
    recorder = IncidentRecorder(
        session_id="session-recorder",
        bundle_id="bundle-recorder",
        config=RecorderConfig(clock_domain_id="manual-clock"),
        clock=clock,
    )
    with recorder.operation("llm", operation_id="op-manual"):
        clock.advance(250_000_000)
    first = recorder.close()
    second = recorder.close("failed")
    assert first == second
    assert first is not second
    assert first.profile.session.status == "completed"
    operation = first.profile.operations[0]
    assert operation.started_at.monotonic_time_nano == "0"
    assert operation.ended_at is not None
    assert operation.ended_at.monotonic_time_nano == "250000000"
    assert validate_incident(first).ok


def test_manual_operations_share_one_session_trace_without_reusing_spans() -> None:
    recorder = IncidentRecorder(session_id="manual-trace")
    with recorder.operation("stt"):
        pass
    with recorder.operation("llm"):
        pass

    first, second = recorder.close().profile.operations
    assert first.trace_id == second.trace_id
    assert first.span_id != second.span_id


def test_context_manager_marks_session_failed_and_reraises_without_message_leak() -> None:
    recorder = IncidentRecorder()
    secret = "context-secret"
    with pytest.raises(ValueError, match=secret), recorder, recorder.operation("tool"):
        raise ValueError(secret)
    bundle = recorder.close()
    assert bundle.profile.session.status == "failed"
    assert bundle.profile.manifest.finality == "final"
    assert bundle.profile.manifest.completeness == "incomplete"
    assert bundle.profile.operations[0].status == "error"
    assert bundle.profile.operations[0].error is not None
    assert bundle.profile.operations[0].error.message is None
    assert secret not in str(bundle.model_dump(mode="python"))


def test_cancellation_closes_recorder_once_and_preserves_only_exception_type() -> None:
    recorder = IncidentRecorder()
    with pytest.raises(asyncio.CancelledError), recorder, recorder.operation("agent"):
        raise asyncio.CancelledError("sensitive cancellation detail")
    bundle = recorder.close()
    assert bundle.profile.session.status == "failed"
    assert bundle.profile.operations[0].status == "error"
    assert bundle.profile.operations[0].error is not None
    assert bundle.profile.operations[0].error.code == "CancelledError"
    assert bundle.profile.operations[0].error.message is None
    assert "sensitive cancellation detail" not in str(bundle.model_dump(mode="python"))


def test_closed_recorder_rejects_new_facts() -> None:
    recorder = IncidentRecorder()
    recorder.close()
    with pytest.raises(RuntimeError, match="closed"):
        recorder.record_event("late")


def test_open_event_and_span_names_cannot_smuggle_payload_into_metadata() -> None:
    secret = "CUSTOMER_EMAIL_alice@example.com"
    manual = IncidentRecorder()
    manual.record_event(secret, event_id="unsafe-name")
    manual_bundle = manual.close()
    event = manual_bundle.profile.events[0]
    assert event.event_name == "framework.event"
    assert len(event.attributes["earshot.source.name_sha256"]) == 64
    assert secret not in manual_bundle.model_dump_json()

    pipecat = PipecatAdapter(_pipecat_recorder())
    pipecat.consume_span(
        {
            "name": secret,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
        }
    )
    pipecat_bundle = pipecat.recorder.close()
    operation = pipecat_bundle.profile.operations[0]
    assert operation.operation_name == "framework_operation"
    assert operation.evidence is not None
    assert operation.evidence.source_field.startswith("sha256:")
    assert secret not in pipecat_bundle.model_dump_json()
    assert validate_incident(pipecat_bundle).ok


def test_session_status_and_coverage_reason_cannot_smuggle_free_form_payload() -> None:
    secret = "TRANSCRIPT_SECRET_SENTINEL"
    recorder = IncidentRecorder()
    recorder.record_coverage("client.render", "not_observed", secret)
    recorder.record_operation(
        operation_id="unsafe-status",
        operation_name="llm",
        status=secret,
        started_at=recorder._time(),
        ended_at=recorder._time(),
    )
    bundle = recorder.close(secret)
    assert bundle.profile.session.status == "unknown"
    assert len(bundle.profile.session.attributes["earshot.source.status_sha256"]) == 64
    assert bundle.profile.coverage[0].reason.startswith("sha256:")
    assert bundle.profile.operations[0].status == "unknown"
    assert len(bundle.profile.operations[0].attributes["earshot.source.status_sha256"]) == 64
    assert secret not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_evidence_labels_cannot_be_used_as_metadata_payload_fields() -> None:
    secret = "EVIDENCE_TRANSCRIPT_SECRET_SENTINEL"
    recorder = IncidentRecorder()
    operation = recorder.record_operation(
        operation_id="governed-evidence",
        operation_name="llm",
        status="ok",
        started_at=recorder._time(),
        evidence=Evidence(
            source=secret,
            observer=secret,
            method=secret,
            method_version=secret,
            confidence=secret,
            availability="available",
        ),
        attributes={
            "earshot.source.name_sha256": "SENTINEL-private-transcript",
            "field_key_sha256": "SENTINEL-private-transcript",
        },
    )
    bundle = recorder.close()
    assert operation.evidence is not None
    for field in ("source", "observer", "method", "method_version", "confidence"):
        assert str(getattr(operation.evidence, field)).startswith("sha256:")
    assert "earshot.source.name_sha256" not in operation.attributes
    assert "field_key_sha256" not in operation.attributes
    assert secret not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_fake_sha256_prefix_cannot_bypass_typed_label_governance() -> None:
    attacker_label = "sha256:SENTINEL-private-transcript"
    recorder = IncidentRecorder()
    operation = recorder.record_operation(
        operation_id="fake-digest-label",
        operation_name="llm",
        status=attacker_label,
        started_at=recorder._time(),
        evidence=Evidence(
            source=attacker_label,
            observer="server",
            method="native_otel",
            confidence="measured",
            availability="available",
        ),
    )
    bundle = recorder.close(attacker_label)
    assert operation.evidence is not None
    assert operation.evidence.source.startswith("sha256:")
    assert len(operation.evidence.source) == 71
    assert len(operation.attributes["earshot.source.status_sha256"]) == 64
    assert len(bundle.profile.session.attributes["earshot.source.status_sha256"]) == 64
    assert attacker_label not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_error_code_and_category_cannot_carry_free_form_payload() -> None:
    secret = "ERROR_TRANSCRIPT_SECRET_SENTINEL"
    recorder = IncidentRecorder()
    operation = recorder.record_operation(
        operation_id="governed-error",
        operation_name="tool",
        status="error",
        started_at=recorder._time(),
        error=ErrorRecord(code=secret, category=secret),
    )
    bundle = recorder.close("failed")
    assert operation.error is not None
    assert operation.error.code.startswith("sha256:")
    assert operation.error.category.startswith("sha256:")
    assert secret not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_concurrent_recorder_callbacks_do_not_cross_contaminate_or_drop_events() -> None:
    recorder = IncidentRecorder()

    def write(index: int) -> None:
        recorder.record_event("framework.event", event_id=f"event-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(100)))
    bundle = recorder.close()
    assert len(bundle.profile.events) == 100
    assert {item.event_id for item in bundle.profile.events} == {
        f"event-{index}" for index in range(100)
    }
    assert validate_incident(bundle).ok


def test_concurrent_sessions_keep_facts_strictly_isolated() -> None:
    def record(index: int):
        recorder = IncidentRecorder(session_id=f"session-{index}")
        recorder.record_event("framework.event", event_id=f"event-{index}")
        return recorder.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        bundles = list(pool.map(record, range(40)))
    assert len({item.profile.session.session_id for item in bundles}) == 40
    for index, bundle in enumerate(bundles):
        assert [item.event_id for item in bundle.profile.events] == [f"event-{index}"]
        assert bundle.profile.events[0].session_id == f"session-{index}"


def test_recorder_submits_exactly_once_to_exporter_on_repeated_close() -> None:
    class ExporterSpy:
        def __init__(self) -> None:
            self.items: list[ExportItem] = []

        def submit(self, item: ExportItem) -> bool:
            self.items.append(item)
            return True

    exporter = ExporterSpy()
    recorder = IncidentRecorder(exporter=exporter)  # type: ignore[arg-type]
    recorder.close()
    recorder.close()
    assert len(exporter.items) == 1


def _pipecat_recorder() -> IncidentRecorder:
    return IncidentRecorder(config=RecorderConfig(clock_domain_id="server-clock"))


def test_pipecat_adapter_preserves_original_otel_identity_and_parentage() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="1.5.0")
    operation_id = adapter.consume_span(
        {
            "name": "llm generation",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "parent_span_id": "f" * 16,
            "parent_scope": "external",
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-native",
            "attributes": {
                "earshot.operation.name": "llm",
                "gen_ai.request.model": "model",
            },
        }
    )
    bundle = recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.operation_id == operation_id
    assert operation.trace_id == TRACE_ID
    assert operation.span_id == ROOT_SPAN_ID
    assert operation.parent_span_id == "f" * 16
    assert operation.parent_scope == "external"
    assert operation.operation_name == "llm"
    assert validate_incident(bundle).ok


def test_pipecat_turn_lifecycle_is_not_endpointing_or_shutdown_barge_in() -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "turn",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(5).model_dump(mode="json"),
            "attributes": {
                "conversation.id": "normal-end-frame",
                "turn.number": 1,
                "turn.type": "conversation",
                "turn.duration_seconds": 0.25,
                "turn.was_interrupted": True,
            },
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.operations[0].operation_name == "framework_operation"
    assert bundle.profile.events == ()
    render = next(item for item in bundle.profile.coverage if item.signal == "client.render")
    assert render.reason == "server_cannot_observe_client_render"
    assert validate_incident(bundle).ok


def test_pipecat_quality_lift_is_an_allowlist_not_a_namespace_wildcard() -> None:
    """An unknown numeric attribute must never reappear as a measurement.

    The operation sanitizer governs unknown metadata; recreating it verbatim in a
    quality sample would bypass that policy entirely.
    """
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {
                "metrics.ttfb": 0.25,
                "metrics.processing": 15551234567,
                "metrics.customer_phone": 15551234567,
                "gen_ai.usage.input_tokens": 11,
            },
        }
    )
    bundle = adapter.recorder.close()
    names = {
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert names == {"pipecat.llm.ttfb", "gen_ai.usage.input_tokens"}
    assert not any("customer_phone" in name for name in names)
    assert validate_incident(bundle).ok


def test_pipecat_stage_scoped_ttfb_keeps_native_stage_measurements_distinct() -> None:
    """Pipecat reports both stages' first-byte latency under `metrics.ttfb`.

    Analysis keys provider measurements by name, so unscoped names would let one
    stage silently overwrite the other's value and evidence.
    """
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    for stage, span_id, ttfb in (
        ("stt", "b" * 16, 0.2),
        ("llm", ROOT_SPAN_ID, 0.4),
        ("tts", "a" * 16, 0.1),
    ):
        adapter.consume_span(
            {
                "name": stage,
                "trace_id": TRACE_ID,
                "span_id": span_id,
                "status": "ok",
                "started_at": point(1).model_dump(mode="json"),
                "ended_at": point(2).model_dump(mode="json"),
                "turn_id": "turn-1",
                "attributes": {"metrics.ttfb": ttfb},
            }
        )
    bundle = adapter.recorder.close()
    measured = {
        measurement.name: measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measured == {
        "pipecat.stt.ttfb": 0.2,
        "pipecat.llm.ttfb": 0.4,
        "pipecat.tts.ttfb": 0.1,
    }
    assert validate_incident(bundle).ok


def test_pipecat_duration_too_large_for_timestamp_math_is_not_lifted() -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {"metrics.ttfb": 1e308},
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.quality_samples == ()
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("stage", "source"),
    (
        ("tool", "metrics.ttfb"),
        ("turn", "metrics.ttfb"),
        ("stt", "metrics.character_count"),
        ("llm", "metrics.character_count"),
    ),
)
def test_pipecat_vendor_metrics_are_lifted_only_from_native_emitting_stages(
    stage: str,
    source: str,
) -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": stage,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {source: 1},
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.quality_samples == ()
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("stage", "source", "raw"),
    (
        ("llm", "metrics.ttfb", -0.25),
        ("turn", "turn.user_bot_latency_seconds", -0.25),
        ("tts", "metrics.character_count", -1),
        ("tts", "metrics.character_count", 1.5),
        ("llm", "gen_ai.usage.input_tokens", -1),
        ("llm", "gen_ai.usage.input_tokens", 1.5),
    ),
)
def test_pipecat_quality_lift_rejects_values_outside_native_metric_domains(
    stage: str,
    source: str,
    raw: int | float,
) -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": stage,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {source: raw},
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.quality_samples == ()
    assert source not in bundle.profile.operations[0].attributes
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("stage", "source", "raw"),
    (
        ("llm", "gen_ai.usage.input_tokens", 9_007_199_254_740_992),
        ("tts", "metrics.character_count", 10**1000),
        ("llm", "metrics.ttfb", 10**1000),
        ("turn", "turn.user_bot_latency_seconds", 10**1000),
    ),
)
def test_pipecat_oversized_metrics_do_not_leave_partial_adapter_state(
    stage: str,
    source: str,
    raw: int,
) -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    span = {
        "name": stage,
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
        "status": "ok",
        "started_at": point(1).model_dump(mode="json"),
        "ended_at": point(2).model_dump(mode="json"),
        "turn_id": "turn-1",
        "attributes": {source: raw},
    }
    first = adapter.consume_span(span)
    second = adapter.consume_span(span)
    bundle = adapter.recorder.close()
    assert first == second
    assert len(bundle.profile.operations) == 1
    assert bundle.profile.quality_samples == ()
    assert source not in bundle.profile.operations[0].attributes
    assert validate_incident(bundle).ok


def test_pipecat_standard_usage_counters_are_lifted_only_from_llm_spans() -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "tts",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {
                "gen_ai.usage.input_tokens": 11,
                "gen_ai.usage.output_tokens": 7,
                "gen_ai.usage.cache_read.input_tokens": 3,
            },
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.quality_samples == ()
    assert validate_incident(bundle).ok


def test_pipecat_lifts_every_native_1_5_llm_usage_counter() -> None:
    expected = {
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.cache_read.input_tokens": 3,
        "gen_ai.usage.cache_creation.input_tokens": 2,
        "gen_ai.usage.reasoning_tokens": 5,
    }
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": expected,
        }
    )
    bundle = adapter.recorder.close()
    measured = {
        measurement.name: measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measured == expected
    assert validate_incident(bundle).ok


def test_pipecat_metric_samples_preserve_exact_per_measurement_provenance() -> None:
    adapter = PipecatAdapter(_pipecat_recorder(), framework_version="1.5.0")
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {
                "metrics.ttfb": 0.25,
                "gen_ai.usage.input_tokens": 11,
                "gen_ai.usage.output_tokens": 7,
                "gen_ai.usage.cache_read.input_tokens": 3,
                "gen_ai.usage.cache_creation.input_tokens": 2,
                "gen_ai.usage.reasoning_tokens": 5,
            },
        }
    )
    adapter.consume_span(
        {
            "name": "tts",
            "trace_id": TRACE_ID,
            "span_id": "b" * 16,
            "status": "ok",
            "started_at": point(2).model_dump(mode="json"),
            "ended_at": point(3).model_dump(mode="json"),
            "turn_id": "turn-1",
            "attributes": {"metrics.character_count": 17},
        }
    )
    bundle = adapter.recorder.close()
    samples = bundle.profile.quality_samples
    assert len(samples) == 7
    assert all(len(sample.measurements) == 1 for sample in samples)
    assert len({sample.sample_id for sample in samples}) == 7
    assert {
        (
            sample.measurements[0].name,
            sample.evidence.attributes["earshot.framework.metric.name"],
        )
        for sample in samples
        if sample.evidence is not None
    } == {
        ("pipecat.llm.ttfb", "metrics.ttfb"),
        ("pipecat.tts.character_count", "metrics.character_count"),
        ("gen_ai.usage.input_tokens", "gen_ai.usage.input_tokens"),
        ("gen_ai.usage.output_tokens", "gen_ai.usage.output_tokens"),
        (
            "gen_ai.usage.cache_read.input_tokens",
            "gen_ai.usage.cache_read.input_tokens",
        ),
        (
            "gen_ai.usage.cache_creation.input_tokens",
            "gen_ai.usage.cache_creation.input_tokens",
        ),
        ("gen_ai.usage.reasoning_tokens", "gen_ai.usage.reasoning_tokens"),
    }
    assert {sample.evidence.source_field for sample in samples if sample.evidence is not None} == {
        "metrics.ttfb",
        "metrics.character_count",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.reasoning_tokens",
    }
    aggregations = {
        sample.measurements[0].name: sample.measurements[0].aggregation for sample in samples
    }
    assert aggregations["pipecat.llm.ttfb"] == "instant"
    assert all(
        aggregation == "delta"
        for name, aggregation in aggregations.items()
        if name != "pipecat.llm.ttfb"
    )
    assert validate_incident(bundle).ok


def test_pipecat_same_span_callback_is_idempotent_not_a_duplicate_fact() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    span = {
        "name": "stt",
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
        "status": "ok",
        "started_at": point(1).model_dump(mode="json"),
        "ended_at": point(2).model_dump(mode="json"),
    }
    first = adapter.consume_span(span)
    second = adapter.consume_span(span)
    bundle = recorder.close()
    assert first == second
    assert len(bundle.profile.operations) == 1
    assert validate_incident(bundle).ok


def test_pipecat_all_identity_ledgers_stay_bounded_under_high_volume() -> None:
    with pytest.raises(ValueError, match="max_tracking_entries"):
        PipecatAdapter(_pipecat_recorder(), max_tracking_entries=0)

    source_recorder = IncidentRecorder(
        config=RecorderConfig(
            clock_domain_id="server-clock",
            max_records=20_000,
            max_capture_bytes=64 * 1024 * 1024,
        )
    )
    adapter = PipecatAdapter(source_recorder, max_tracking_entries=64)
    first_span = {
        "name": "llm",
        "trace_id": f"{1:032x}",
        "span_id": f"{1:016x}",
        "status": "ok",
        "started_at": point(1).model_dump(mode="json"),
        "ended_at": point(2).model_dump(mode="json"),
        "events": tuple(
            {
                "name": "provider.event",
                "timestamp": index + 1,
                "attributes": {},
            }
            for index in range(1_000)
        ),
    }
    adapter.consume_span(first_span)
    for index in range(1, 1_000):
        adapter.consume_span(
            {
                "name": "llm",
                "trace_id": f"{index + 1:032x}",
                "span_id": f"{index + 1:016x}",
                "status": "ok",
                "started_at": point(1).model_dump(mode="json"),
                "ended_at": point(2).model_dump(mode="json"),
                "events": (),
            }
        )

    first_interruption = {"type": "InterruptionFrame", "id": 0}
    first_event_id = adapter.consume_interruption_frame(
        first_interruption,
        observed_at=None,
        bot_was_speaking=True,
    )
    for index in range(1, 1_000):
        adapter.consume_interruption_frame(
            {"type": "InterruptionFrame", "id": index},
            observed_at=None,
            bot_was_speaking=True,
        )

    status = adapter.tracking_status()
    assert status.limit_per_ledger == 64
    assert dict(status.entries) == {
        "accepted_interruption_frames": 64,
        "interruption_frames": 64,
        "source_events": 64,
        "spans": 64,
    }
    assert status.saturated_ledgers == (
        "interruption_frames",
        "source_events",
        "spans",
    )
    assert source_recorder.status().captured_records <= 200

    captured_before_replay = source_recorder.status().captured_records
    assert adapter.consume_span(first_span) is not None
    assert (
        adapter.consume_interruption_frame(
            first_interruption,
            observed_at=None,
            bot_was_speaking=False,
        )
        == first_event_id
    )
    assert (
        adapter.consume_interruption_frame(
            {"type": "InterruptionFrame", "id": 1_001},
            observed_at=None,
            bot_was_speaking=True,
        )
        is None
    )
    assert source_recorder.status().captured_records == captured_before_replay

    bundle = source_recorder.close()
    assert len(bundle.profile.operations) == 64
    assert len(bundle.profile.events) == 128
    assert {
        (item.signal, item.availability, item.reason) for item in bundle.profile.coverage
    }.issuperset(
        {
            ("pipecat.tracking.interruption_frames", "partial", "max_tracking_entries"),
            ("pipecat.tracking.source_events", "partial", "max_tracking_entries"),
            ("pipecat.tracking.spans", "partial", "max_tracking_entries"),
        }
    )
    assert validate_incident(bundle).ok


def test_pipecat_span_tracking_bound_is_atomic_for_unique_concurrent_spans() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, max_tracking_entries=64)
    spans = [
        {
            "name": "llm",
            "trace_id": f"{index + 1:032x}",
            "span_id": f"{index + 1:016x}",
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
        }
        for index in range(1_000)
    ]

    with ThreadPoolExecutor(max_workers=32) as pool:
        operation_ids = list(pool.map(adapter.consume_span, spans))

    assert len(operation_ids) == 1_000
    status = adapter.tracking_status()
    assert dict(status.entries)["spans"] == 64
    assert status.saturated_ledgers == ("spans",)
    bundle = recorder.close()
    assert len(bundle.profile.operations) == 64
    assert validate_incident(bundle).ok


def test_pipecat_conflicting_duplicate_identity_is_rejected_without_second_fact() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    span = {
        "name": "stt",
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
        "status": "ok",
        "started_at": point(1).model_dump(mode="json"),
        "ended_at": point(2).model_dump(mode="json"),
    }
    adapter.consume_span(span)
    changed = {**span, "ended_at": point(3).model_dump(mode="json")}
    with pytest.raises(ValueError, match="conflicting duplicate"):
        adapter.consume_span(changed)
    assert len(recorder.close().profile.operations) == 1


def test_pipecat_concurrent_duplicate_callbacks_are_atomic() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    span = {
        "name": "stt",
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
        "status": "ok",
        "started_at": point(1).model_dump(mode="json"),
        "ended_at": point(2).model_dump(mode="json"),
    }
    with ThreadPoolExecutor(max_workers=16) as pool:
        operation_ids = list(pool.map(adapter.consume_span, [span] * 100))
    bundle = recorder.close()
    assert len(set(operation_ids)) == 1
    assert len(bundle.profile.operations) == 1
    assert validate_incident(bundle).ok


@pytest.mark.parametrize("name", ["llm_tool_call", "llm_tool_result"])
def test_pipecat_native_s2s_tool_spans_remain_tool_operations(name: str) -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="current")
    adapter.consume_span(
        {
            "name": name,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
        }
    )
    operation = recorder.close().profile.operations[0]
    assert operation.operation_name == "tool"
    assert operation.evidence is not None
    assert operation.evidence.source_field == name
    assert operation.attributes["earshot.framework.operation.name"] == name


def test_pipecat_current_payload_keys_follow_explicit_opt_in_classes() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.MODEL_PAYLOAD}))
    recorder = IncidentRecorder(
        config=RecorderConfig(clock_domain_id="server-clock", capture_policy=policy)
    )
    adapter = PipecatAdapter(recorder)
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "attributes": {
                "input": [{"role": "user", "content": "private input"}],
                "output": [{"role": "assistant", "content": "private output"}],
                "tools": [{"name": "lookup", "description": "private schema"}],
            },
        }
    )
    bundle = recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.capture_class == "model_payload"
    assert operation.attributes["gen_ai.input"][0]["content"] == "private input"
    assert operation.attributes["gen_ai.output.messages"][0]["content"] == "private output"
    assert operation.attributes["gen_ai.input.tools"][0]["name"] == "lookup"
    assert validate_incident(bundle).ok


def test_pipecat_span_processor_filters_unrelated_shared_provider_spans() -> None:
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "pipecat-test"})
    )
    adapter.attach(provider)
    try:
        with provider.get_tracer("application.database").start_as_current_span("sql.query"):
            pass
        with provider.get_tracer("pipecat.turn").start_as_current_span(
            "llm", attributes={"turn.number": 9, "metrics.ttfb": 0.1}
        ):
            pass
        bundle = recorder.close()
        assert len(bundle.profile.operations) == 1
        assert bundle.profile.operations[0].operation_name == "llm"
        assert bundle.profile.operations[0].instrumentation_scope_name == "pipecat.turn"
        assert validate_incident(bundle).ok
    finally:
        provider.shutdown()


def test_pipecat_native_objects_preserve_resource_scope_links_and_integer_ids() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="native")
    native_trace = int("2" * 32, 16)
    native_span = int("3" * 16, 16)
    parent_span = int("4" * 16, 16)
    linked_trace = int("5" * 32, 16)
    linked_span = int("6" * 16, 16)
    span = SimpleNamespace(
        name="tool native",
        context=SimpleNamespace(trace_id=native_trace, span_id=native_span),
        parent=SimpleNamespace(span_id=parent_span),
        status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
        start_time=1_800_000_000_000_000_000,
        end_time=1_800_000_000_100_000_000,
        attributes={"earshot.turn.id": "native-turn"},
        resource=SimpleNamespace(
            attributes={"service.name": "voice-app", "unsafe.resource": "dropped"},
            schema_url="https://opentelemetry.io/schemas/1.29.0",
        ),
        instrumentation_scope=SimpleNamespace(
            name="pipecat.telemetry",
            version="1",
            attributes={"earshot.framework.name": "pipecat"},
            schema_url="https://opentelemetry.io/schemas/1.30.0",
        ),
        links=(
            SimpleNamespace(
                context=SimpleNamespace(trace_id=linked_trace, span_id=linked_span),
                attributes={
                    "earshot.link.type": "related",
                    "earshot.link.target_scope": "external",
                },
            ),
        ),
        turn_id="native-turn",
        parent_scope="external",
    )
    adapter.consume_span(span)
    bundle = recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.trace_id == "2" * 32
    assert operation.span_id == "3" * 16
    assert operation.parent_span_id == "4" * 16
    assert operation.status == "ok"
    assert operation.resource == {"service.name": "voice-app"}
    assert operation.resource_schema_url == "https://opentelemetry.io/schemas/1.29.0"
    assert operation.instrumentation_scope_name == "pipecat.telemetry"
    assert operation.instrumentation_scope_version == "1"
    assert operation.instrumentation_scope_attributes == {"earshot.framework.name": "pipecat"}
    assert operation.schema_url == "https://opentelemetry.io/schemas/1.30.0"
    assert operation.links[0].trace_id == "5" * 32
    assert operation.links[0].span_id == "6" * 16
    assert operation.links[0].target_scope == "external"
    assert validate_incident(bundle).ok


def test_pipecat_span_outputs_retain_one_correlated_provenance_chain() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="native")
    span = SimpleNamespace(
        name="llm",
        context=SimpleNamespace(
            trace_id=int("2" * 32, 16),
            span_id=int("3" * 16, 16),
        ),
        parent=None,
        parent_scope="external",
        status="ok",
        start_time=1_800_000_000_000_000_000,
        end_time=1_800_000_000_100_000_000,
        turn_id="correlated-turn",
        attributes={"metrics.ttfb": 0.1},
        resource=SimpleNamespace(
            attributes={"service.name": "voice-app"},
            schema_url="https://opentelemetry.io/schemas/1.29.0",
        ),
        instrumentation_scope=SimpleNamespace(
            name="pipecat.telemetry",
            version="1.5.0",
            attributes={"earshot.framework.name": "pipecat"},
            schema_url="https://opentelemetry.io/schemas/1.30.0",
        ),
        links=(),
        events=(
            SimpleNamespace(
                name="provider_event",
                timestamp=1_800_000_000_050_000_000,
                attributes={},
            ),
        ),
    )
    adapter.consume_span(span)
    bundle = recorder.close()
    operation = bundle.profile.operations[0]
    event = bundle.profile.events[0]
    sample = bundle.profile.quality_samples[0]

    assert event.operation_id == operation.operation_id
    assert event.trace_id == operation.trace_id
    assert event.span_id == operation.span_id
    assert event.turn_id == operation.turn_id
    assert sample.attributes["earshot.operation.id"] == operation.operation_id
    assert sample.attributes["earshot.turn.id"] == operation.turn_id
    owner = next(
        item
        for item in bundle.profile.operations
        if item.operation_id == sample.attributes["earshot.operation.id"]
    )
    assert (owner.trace_id, owner.span_id, owner.turn_id) == (
        event.trace_id,
        event.span_id,
        event.turn_id,
    )
    for emitted in (event, sample):
        assert emitted.resource == operation.resource
        assert emitted.resource_schema_url == operation.resource_schema_url
        assert emitted.instrumentation_scope_name == operation.instrumentation_scope_name
        assert emitted.instrumentation_scope_version == operation.instrumentation_scope_version
        assert (
            emitted.instrumentation_scope_attributes == operation.instrumentation_scope_attributes
        )
        assert emitted.schema_url == operation.schema_url
    assert validate_incident(bundle).ok


def test_pipecat_source_controlled_scope_link_and_schema_labels_cannot_leak() -> None:
    adapter = PipecatAdapter(_pipecat_recorder())
    adapter.consume_span(
        SimpleNamespace(
            name="llm",
            context=SimpleNamespace(
                trace_id=int(TRACE_ID, 16),
                span_id=int(ROOT_SPAN_ID, 16),
            ),
            parent=None,
            parent_scope=SECRET_SENTINEL,
            status="ok",
            start_time=1_800_000_000_000_000_000,
            end_time=1_800_000_000_100_000_000,
            attributes={},
            resource=SimpleNamespace(
                attributes={},
                schema_url=(f"https://opentelemetry.io:{SECRET_SENTINEL}/schemas/1.30.0"),
            ),
            instrumentation_scope=SimpleNamespace(
                name=SECRET_SENTINEL,
                version=SECRET_SENTINEL,
                attributes={"vendor.scope.secret": SECRET_SENTINEL},
                schema_url=f"https://opentelemetry.io/schemas/{SECRET_SENTINEL}",
            ),
            links=(
                SimpleNamespace(
                    context=SimpleNamespace(
                        trace_id=int("5" * 32, 16),
                        span_id=int("6" * 16, 16),
                    ),
                    attributes={
                        "earshot.link.type": SECRET_SENTINEL,
                        "earshot.link.target_scope": SECRET_SENTINEL,
                    },
                ),
            ),
        )
    )
    bundle = adapter.recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.parent_scope == "unknown"
    assert operation.instrumentation_scope_name is not None
    assert operation.instrumentation_scope_name.startswith("sha256:")
    assert operation.instrumentation_scope_version is not None
    assert operation.instrumentation_scope_version.startswith("sha256:")
    assert operation.instrumentation_scope_attributes == {}
    assert operation.schema_url is None
    assert operation.resource_schema_url is None
    assert "earshot.source.schema_url_sha256" in operation.attributes
    assert "earshot.source.resource_schema_url_sha256" in operation.attributes
    assert operation.links[0].relationship.startswith("sha256:")
    assert operation.links[0].target_scope == "unknown"
    assert SECRET_SENTINEL not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


@pytest.mark.parametrize("ended_by_conversation_end", [False, True])
def test_pipecat_turn_attributes_do_not_infer_an_accepted_interruption(
    ended_by_conversation_end: bool,
) -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="current")
    adapter.consume_span(
        {
            "name": "turn",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "status": "ok",
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "attributes": {
                "conversation.id": "conversation-current",
                "turn.number": 7,
                "turn.was_interrupted": True,
                "turn.ended_by_conversation_end": ended_by_conversation_end,
                "turn.user_bot_latency_seconds": 0.375,
                "metrics.ttfb": 0.125,
            },
        }
    )
    bundle = recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.operation_name == "framework_operation"
    assert operation.turn_id == "7"
    assert operation.attributes["conversation.id"] == "conversation-current"
    assert operation.attributes["metrics.ttfb"] == 0.125
    assert operation.attributes["turn.was_interrupted"] is True
    assert operation.attributes["turn.ended_by_conversation_end"] is ended_by_conversation_end
    assert operation.attributes["turn.user_bot_latency_seconds"] == 0.375
    latency_sample = next(
        sample
        for sample in bundle.profile.quality_samples
        if sample.measurements[0].name == "pipecat.turn.user_bot_latency"
    )
    assert latency_sample.measurements[0].value == 0.375
    assert latency_sample.measurements[0].unit == "s"
    assert latency_sample.measurements[0].aggregation == "instant"
    assert latency_sample.evidence is not None
    assert latency_sample.evidence.source_field == "turn.user_bot_latency_seconds"
    assert bundle.profile.events == ()
    assert validate_incident(bundle).ok


def test_pipecat_native_interruption_observer_requires_active_bot_playout() -> None:
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        InterruptionFrame,
        StartFrame,
        UserStartedSpeakingFrame,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection

    clock = ManualClock(wall=1_800_000_000_000_000_000, monotonic=1_000)
    recorder = IncidentRecorder(
        config=RecorderConfig(clock_domain_id="server-clock"),
        clock=clock,
    )
    adapter = PipecatAdapter(recorder, framework_version="1.5.0")
    observer = adapter.create_observer()
    # Pipecat's pipeline clock starts independently. Its small frame timestamps
    # must not be mislabeled as coordinates in Earshot's recorder clock.
    clock.advance(5_000_000_000)
    pipeline_started = StartFrame()
    initial_user_started = UserStartedSpeakingFrame()
    initial_turn = InterruptionFrame()
    first_bot_started = BotStartedSpeakingFrame()
    second_bot_started = BotStartedSpeakingFrame()
    first_bot_stopped = BotStoppedSpeakingFrame()
    interrupting_user_started = UserStartedSpeakingFrame()
    downstream = InterruptionFrame()
    upstream = InterruptionFrame()
    downstream.broadcast_sibling_id = upstream.id
    upstream.broadcast_sibling_id = downstream.id
    second_bot_stopped = BotStoppedSpeakingFrame()
    after_playout = InterruptionFrame()
    source = SimpleNamespace(name="source")
    destination = SimpleNamespace(name="destination")

    async def observe() -> None:
        for frame, direction, timestamp in (
            (pipeline_started, FrameDirection.DOWNSTREAM, 80),
            # Pipecat broadcasts this on an ordinary first user turn. With no
            # active bot playout it is not an accepted barge-in.
            (initial_user_started, FrameDirection.DOWNSTREAM, 85),
            (initial_turn, FrameDirection.DOWNSTREAM, 90),
            # Independent Pipecat media senders can overlap. Stopping one must
            # not clear the active state of the other.
            (first_bot_started, FrameDirection.UPSTREAM, 94),
            (second_bot_started, FrameDirection.UPSTREAM, 95),
            (first_bot_stopped, FrameDirection.UPSTREAM, 96),
            (interrupting_user_started, FrameDirection.DOWNSTREAM, 98),
            (downstream, FrameDirection.DOWNSTREAM, 100),
            (downstream, FrameDirection.DOWNSTREAM, 101),
            (upstream, FrameDirection.UPSTREAM, 102),
            (second_bot_stopped, FrameDirection.UPSTREAM, 110),
            (after_playout, FrameDirection.DOWNSTREAM, 120),
        ):
            await observer.on_push_frame(
                FramePushed(
                    source=source,
                    destination=destination,
                    frame=frame,
                    direction=direction,
                    timestamp=timestamp,
                )
            )

    asyncio.run(observe())
    bundle = recorder.close()
    assert len(bundle.profile.events) == 1
    event = bundle.profile.events[0]
    assert event.event_name == "earshot.interruption.accepted"
    assert event.turn_id == "1"
    assert event.time.monotonic_time_nano == "5000000000"
    assert event.attributes == {"earshot.metric.interruption.accepted": True}
    assert event.evidence is not None
    assert event.evidence.source == "pipecat"
    assert event.evidence.method == "native_frame_overlap_observer"
    assert event.evidence.confidence == "inferred"
    assert event.evidence.source_field == "InterruptionFrame+BotStartedSpeakingFrame"
    assert validate_incident(bundle).ok


def test_pipecat_interruption_frame_is_classified_on_first_observation() -> None:
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import BotStartedSpeakingFrame, InterruptionFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection

    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="1.5.0")
    observer = adapter.create_observer()
    interruption = InterruptionFrame()
    bot_started = BotStartedSpeakingFrame()
    source = SimpleNamespace(name="source")
    destination = SimpleNamespace(name="destination")

    async def observe() -> None:
        for frame, timestamp in ((interruption, 90), (bot_started, 95), (interruption, 100)):
            await observer.on_push_frame(
                FramePushed(
                    source=source,
                    destination=destination,
                    frame=frame,
                    direction=FrameDirection.DOWNSTREAM,
                    timestamp=timestamp,
                )
            )

    asyncio.run(observe())
    bundle = recorder.close()
    assert bundle.profile.events == ()
    assert validate_incident(bundle).ok


def test_pipecat_observer_identity_ledgers_fail_closed_at_the_bound() -> None:
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        EndFrame,
        InterruptionFrame,
        StartFrame,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection

    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder, framework_version="1.5.0", max_tracking_entries=64)
    turn_observer = adapter.create_observer()
    playout_observer = adapter.create_observer()
    source = SimpleNamespace(name="source")
    destination = SimpleNamespace(name="destination")

    async def push(observer: object, frame: object, timestamp: int) -> None:
        await observer.on_push_frame(  # type: ignore[attr-defined]
            FramePushed(
                source=source,
                destination=destination,
                frame=frame,
                direction=FrameDirection.DOWNSTREAM,
                timestamp=timestamp,
            )
        )

    async def observe() -> None:
        for index in range(65):
            await push(turn_observer, StartFrame(), index)
            await push(playout_observer, BotStartedSpeakingFrame(), index)
        # The playout observer had active bot state before saturation. Once it
        # can no longer track frame identities, End/Start must not re-enable it
        # and it must not infer an interruption from stale or new playout state.
        await push(playout_observer, EndFrame(), 98)
        await push(playout_observer, StartFrame(), 99)
        await push(playout_observer, BotStartedSpeakingFrame(), 100)
        await push(playout_observer, InterruptionFrame(), 101)

    asyncio.run(observe())
    turn_status = turn_observer.tracking_status()
    playout_status = playout_observer.tracking_status()
    assert dict(turn_status.entries) == {"playout_frames": 0, "turn_frames": 64}
    assert turn_status.saturated_ledgers == ("turn_frames",)
    assert dict(playout_status.entries) == {"playout_frames": 64, "turn_frames": 0}
    assert playout_status.saturated_ledgers == ("playout_frames",)

    bundle = recorder.close()
    assert bundle.profile.events == ()
    assert {
        (item.signal, item.availability, item.reason) for item in bundle.profile.coverage
    }.issuperset(
        {
            (
                "pipecat.tracking.observer.playout_frames",
                "partial",
                "max_tracking_entries",
            ),
            (
                "pipecat.tracking.observer.turn_frames",
                "partial",
                "max_tracking_entries",
            ),
        }
    )
    assert validate_incident(bundle).ok


def test_pipecat_preserves_policy_safe_native_span_events_without_payload_leak() -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "started_at": point(1).model_dump(mode="json"),
            "ended_at": point(2).model_dump(mode="json"),
            "events": [
                {
                    "name": "exception",
                    "timestamp": 2,
                    "attributes": {
                        "error.type": "ProviderError",
                        "exception.message": "PRIVATE_PROVIDER_MESSAGE",
                    },
                }
            ],
        }
    )
    bundle = recorder.close()
    event = bundle.profile.events[0]
    assert event.event_name == "otel.span_event"
    assert event.attributes == {
        "error.type": "ProviderError",
        "earshot.source.event.name": "exception",
    }
    assert "PRIVATE_PROVIDER_MESSAGE" not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_pipecat_attach_uses_existing_provider_without_replacing_it() -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors = []

        def add_span_processor(self, processor) -> None:
            self.processors.append(processor)

    provider = Provider()
    handle = PipecatAdapter(_pipecat_recorder()).attach(provider)
    assert isinstance(handle, routing.RoutingHandle)
    # Additive: one shared router processor, reused by later concurrent sessions.
    assert len(provider.processors) == 1
    PipecatAdapter(_pipecat_recorder()).attach(provider)
    assert len(provider.processors) == 1
    with pytest.raises(TypeError, match="does not support"):
        PipecatAdapter(_pipecat_recorder()).attach(object())


def test_recorder_bound_pipecat_span_processor_is_rejected() -> None:
    adapter = PipecatAdapter(_pipecat_recorder())
    with pytest.raises(RuntimeError, match="attach"):
        adapter.create_span_processor()


def test_pipecat_optional_attach_has_actionable_dependency_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry.sdk.trace"):
            raise ImportError("forced missing optional dependency")
        return real_import(name, *args, **kwargs)

    class Provider:
        def add_span_processor(self, processor) -> None:
            del processor

    monkeypatch.setattr(builtins, "__import__", import_without_otel)
    with pytest.raises(AdapterDependencyError, match="opentelemetry-sdk"):
        PipecatAdapter(_pipecat_recorder()).attach(Provider())


def test_pipecat_rejects_unsupported_or_missing_timestamp_shape() -> None:
    adapter = PipecatAdapter(_pipecat_recorder())
    with pytest.raises(TypeError, match="supported timestamp"):
        adapter.consume_span({"name": "llm", "started_at": "not-a-time"})


def test_livekit_adapter_keeps_vad_and_turn_detection_distinct() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder, framework_version="1.6.5")
    observed = point(200_000_000)
    adapter.consume_metric(
        {
            "metric_type": "VADMetrics",
            "speech_id": "turn-1",
            "label": "vad_primary",
            "idle_time": 0.1,
            "inference_duration_total": 0.04,
            "inference_count": 8,
            "metadata": {"model_name": "silero", "model_provider": "livekit"},
        },
        observed_at=observed,
    )
    adapter.consume_metric(
        {"metric_type": "EOUMetrics", "speech_id": "turn-1", "duration": 0.2},
        observed_at=point(500_000_000),
    )
    bundle = recorder.close()
    # VAD (a continuous background signal) is a quality sample; turn detection (a
    # discrete commitment decision) remains an operation. They stay distinct.
    assert [item.operation_name for item in bundle.profile.operations] == ["turn_detection"]
    assert [s.quality_kind for s in bundle.profile.quality_samples] == ["pipeline.metric"]
    vad = bundle.profile.quality_samples[0]
    assert vad.sample_window.start == observed
    assert vad.sample_window.end == observed
    assert vad.attributes == {
        "earshot.framework.name": "livekit",
        "earshot.framework.metric.name": "vad_primary",
        "earshot.framework.version": "1.6.5",
        "earshot.turn.id": "turn-1",
        "gen_ai.provider.name": "livekit",
        "gen_ai.request.model": "silero",
    }
    measurements = {item.name: item for item in vad.measurements}
    assert measurements["earshot.duration.inference_seconds"].aggregation == "delta"
    assert measurements["earshot.metric.inference.count"].aggregation == "delta"
    assert measurements["earshot.duration.vad.idle_seconds"].aggregation == "instant"
    assert bundle.profile.operations[0].evidence is not None
    assert bundle.profile.operations[0].evidence.confidence == "estimated"
    assert len(bundle.profile.coverage) == 1
    assert bundle.profile.coverage[0].signal == "client.render"
    assert bundle.profile.coverage[0].availability == "not_observed"
    assert validate_incident(bundle).ok


def test_livekit_vad_window_identity_deduplicates_and_detects_conflicts() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    metric = {
        "metric_type": "VADMetrics",
        "speech_id": "turn-1",
        "label": "vad",
        "inference_duration_total": 0.02,
        "inference_count": 2,
    }
    first_time = point(200_000_000)
    second_time = point(300_000_000)
    first = adapter.consume_metric(metric, observed_at=first_time)
    duplicate = adapter.consume_metric(metric, observed_at=first_time)
    second = adapter.consume_metric(metric, observed_at=second_time)
    different_dimension = adapter.consume_metric(
        {**metric, "label": "vad_backup"},
        observed_at=first_time,
    )

    assert first is not None
    assert first == duplicate
    assert second is not None and second != first
    assert different_dimension is not None and different_dimension != first
    with pytest.raises(ValueError, match="conflicting duplicate"):
        adapter.consume_metric(
            {**metric, "inference_count": 3},
            observed_at=first_time,
        )

    bundle = recorder.close()
    assert len(bundle.profile.quality_samples) == 3
    assert validate_incident(bundle).ok


def test_livekit_vad_without_measurements_returns_no_phantom_id() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    result = adapter.consume_metric(
        {"metric_type": "VADMetrics", "label": SECRET_SENTINEL},
        observed_at=point(200_000_000),
    )
    bundle = recorder.close()

    assert result is None
    assert bundle.profile.quality_samples == ()
    assert [(item.signal, item.availability) for item in bundle.profile.coverage] == [
        ("client.render", "not_observed")
    ]
    assert SECRET_SENTINEL not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_livekit_vad_free_form_metric_label_is_hashed() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    adapter.consume_metric(
        {
            "metric_type": "VADMetrics",
            "label": SECRET_SENTINEL,
            "inference_count": 2,
        },
        observed_at=point(200_000_000),
    )

    bundle = recorder.close()

    retained_label = bundle.profile.quality_samples[0].attributes["earshot.framework.metric.name"]
    assert retained_label.startswith("sha256:")
    assert SECRET_SENTINEL not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_livekit_timestamp_less_identical_vad_callbacks_collapse_by_content() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    metric = {
        "metric_type": "VADMetrics",
        "label": "vad",
        "inference_count": 2,
    }
    first = adapter.consume_metric(metric)
    second = adapter.consume_metric(metric)

    assert first is not None
    assert second == first
    assert len(recorder.close().profile.quality_samples) == 1


def test_livekit_duplicate_metric_callback_is_idempotent() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    metric = {"metric_type": "LLMMetrics", "speech_id": "turn-1", "duration": 0.1}
    first = adapter.consume_metric(metric, observed_at=point(200_000_000))
    second = adapter.consume_metric(metric, observed_at=point(200_000_000))
    assert first == second
    assert len(recorder.close().profile.operations) == 1


def test_livekit_duration_before_recorder_origin_uses_wall_time_without_zero_clamp() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    observed = point(100_000_000)
    adapter.consume_metric(
        {"metric_type": "TTSMetrics", "speech_id": "turn-1", "duration": 1.0},
        observed_at=observed,
    )
    operation = recorder.close().profile.operations[0]
    assert operation.started_at.monotonic_time_nano is None
    assert operation.started_at.source_time_unix_nano == str(
        int(observed.source_time_unix_nano) - 1_000_000_000
    )
    assert operation.started_at.source_time_unix_nano != "0"


def test_livekit_provider_timestamp_interval_has_a_comparable_server_clock() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    adapter.consume_metric(
        {
            "metric_type": "TTSMetrics",
            "speech_id": "turn-1",
            "timestamp": 1_800_000_000.0,
            "duration": 0.25,
        }
    )
    operation = recorder.close().profile.operations[0]
    assert operation.started_at.clock_domain_id == "server-clock"
    assert operation.ended_at is not None
    assert operation.ended_at.clock_domain_id == "server-clock"
    assert (
        int(operation.ended_at.source_time_unix_nano or "0")
        - int(operation.started_at.source_time_unix_nano or "0")
        == 250_000_000
    )


def test_livekit_listener_is_fail_open_when_metric_mapping_raises(monkeypatch) -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)
    listeners: dict[str, object] = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    adapter.attach_metrics_listener(Session())

    def fail(*_args, **_kwargs):
        raise TypeError("unsupported metric")

    monkeypatch.setattr(adapter, "consume_metric", fail)
    callback = listeners["metrics_collected"]
    callback({"metrics": {}})  # type: ignore[operator]
    bundle = recorder.close()
    assert bundle.profile.coverage[0].signal == "livekit.metric"
    assert bundle.profile.coverage[0].availability == "unavailable"


def test_adapter_callbacks_remain_fail_open_after_recorder_close() -> None:
    livekit = LiveKitAdapter(_pipecat_recorder())
    listeners: dict[str, object] = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    livekit.attach_metrics_listener(Session())
    livekit.recorder.close()
    listeners["metrics_collected"]({"metrics": {"type": "vad_metrics"}})  # type: ignore[operator]

    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    pipecat = PipecatAdapter(_pipecat_recorder())
    provider = sdk_trace.TracerProvider()
    handle = pipecat.attach(provider)
    pipecat.recorder.close()
    with handle.session_scope(), provider.get_tracer("pipecat").start_as_current_span(
        "llm",
        attributes={"conversation.id": "closed-recorder"},
    ):
        pass
    pipecat.detach()


def test_livekit_listener_supports_direct_two_argument_event_api() -> None:
    recorder = _pipecat_recorder()
    adapter = LiveKitAdapter(recorder)

    class Session:
        def __init__(self) -> None:
            self.listener = None

        def on(self, name: str, callback) -> None:
            assert name == "metrics_collected"
            self.listener = callback

    session = Session()
    adapter.attach_metrics_listener(session)
    assert callable(session.listener)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (0.25, 250_000_000), (-1, None), (float("nan"), None), ("1", None)],
)
def test_seconds_to_nanoseconds_rejects_invalid_provider_values(value, expected) -> None:
    assert seconds_to_nano(value) == expected


def test_importing_adapters_does_not_require_optional_framework_packages() -> None:
    # The duck-typed mapping modules should already have imported without either
    # heavy runtime being installed; this assertion also guards accidental imports.
    assert PipecatAdapter.__module__ == "earshot.adapters.pipecat"
    assert LiveKitAdapter.__module__ == "earshot.adapters.livekit"
