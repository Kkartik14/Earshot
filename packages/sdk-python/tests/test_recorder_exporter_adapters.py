from __future__ import annotations

import asyncio
import builtins
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from earshot.adapters import LiveKitAdapter, PipecatAdapter
from earshot.adapters.base import AdapterDependencyError, seconds_to_nano
from earshot.clock import ManualClock
from earshot.contract import ErrorRecord, Evidence
from earshot.exporter import (
    BoundedAsyncExporter,
    ExportDiagnostic,
    ExportItem,
    HttpExportTransport,
    PermanentExportError,
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


@pytest.mark.parametrize("status", [400, 409, 422])
def test_http_transport_classifies_client_rejection_as_permanent(monkeypatch, status: int) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://localhost", status, "bad", {}, None)

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    with pytest.raises(PermanentExportError):
        transport.send(ExportItem("bundle", b"payload"))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_transport_leaves_retryable_http_failures_retryable(monkeypatch, status: int) -> None:
    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://localhost", status, "retry", {}, None)

    transport = HttpExportTransport("http://localhost")
    monkeypatch.setattr(transport._opener, "open", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        transport.send(ExportItem("bundle", b"payload"))


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


def test_context_manager_marks_session_failed_and_reraises_without_message_leak() -> None:
    recorder = IncidentRecorder()
    secret = "context-secret"
    with pytest.raises(ValueError, match=secret), recorder, recorder.operation("tool"):
        raise ValueError(secret)
    bundle = recorder.close()
    assert bundle.profile.session.status == "failed"
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
                "turn.number": 1,
                "turn.was_interrupted": True,
                "turn.ended_by_conversation_end": True,
            },
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.operations[0].operation_name == "framework_operation"
    assert bundle.profile.events == ()
    render = next(item for item in bundle.profile.coverage if item.signal == "client.render")
    assert render.reason == "server_cannot_observe_client_render"
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


def test_pipecat_current_otel_keys_preserve_turn_ttfb_and_interruption() -> None:
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
    assert bundle.profile.events[0].event_name == "earshot.interruption.accepted"
    assert bundle.profile.events[0].turn_id == "7"
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


def test_pipecat_attach_uses_existing_provider_without_replacing_it(monkeypatch) -> None:
    recorder = _pipecat_recorder()
    adapter = PipecatAdapter(recorder)
    sentinel = object()
    monkeypatch.setattr(adapter, "create_span_processor", lambda: sentinel)

    class Provider:
        def __init__(self) -> None:
            self.processors = []

        def add_span_processor(self, processor) -> None:
            self.processors.append(processor)

    provider = Provider()
    assert adapter.attach(provider) is sentinel
    assert provider.processors == [sentinel]
    with pytest.raises(TypeError, match="does not support"):
        adapter.attach(object())


def test_pipecat_optional_span_processor_has_actionable_dependency_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry.sdk.trace"):
            raise ImportError("forced missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_otel)
    adapter = PipecatAdapter(_pipecat_recorder())
    with pytest.raises(AdapterDependencyError, match="opentelemetry-sdk"):
        adapter.create_span_processor()


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

    pipecat = PipecatAdapter(_pipecat_recorder())
    processor = pipecat.create_span_processor()
    pipecat.recorder.close()
    processor.on_end(SimpleNamespace())


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
