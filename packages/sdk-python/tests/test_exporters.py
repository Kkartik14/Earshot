"""Golden and hardening tests for the OTLP / OpenInference exporters + push client."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.codec import decode_incident_json
from earshot.exporters import (
    OtlpHttpExporter,
    langfuse_exporter,
    phoenix_exporter,
    span_count,
    to_openinference,
    to_otlp,
)
from earshot.exporters.push import serialize_document

ROOT = Path(__file__).resolve().parents[3]
FAULTS = ROOT / "fixtures" / "faults"
GOLDEN = ROOT / "fixtures" / "golden"


def _load(name: str, *, faults: bool = True):
    directory = FAULTS if faults else ROOT / "fixtures" / "valid"
    suffix = ".incident.json" if faults else ".json"
    return decode_incident_json((directory / f"{name}{suffix}").read_bytes())


def _spans(document: dict) -> list[dict]:
    return [
        span
        for resource_spans in document["resourceSpans"]
        for scope_spans in resource_spans["scopeSpans"]
        for span in scope_spans["spans"]
    ]


def _resource_attributes(document: dict) -> dict[str, object]:
    return {
        attribute["key"]: attribute["value"]
        for attribute in document["resourceSpans"][0]["resource"]["attributes"]
    }


def _span_attributes(span: dict) -> dict[str, object]:
    return {attribute["key"]: attribute["value"] for attribute in span["attributes"]}


# --------------------------------------------------------------------------- #
# Structure and identity preservation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("project", [to_otlp, to_openinference])
@pytest.mark.parametrize("name", ["tool_timeout_retry", "full_barge_in_chain", "llm_delay"])
def test_span_count_equals_operations_plus_standalone_events(name: str, project) -> None:
    bundle = _load(name)
    document = project(bundle)
    profile = bundle.profile
    operation_ids = {operation.operation_id for operation in profile.operations}
    standalone = sum(
        1
        for event in profile.events
        if event.operation_id is None or event.operation_id not in operation_ids
    )
    session_spans = 1 if profile.quality_samples else 0
    assert span_count(document) == len(profile.operations) + standalone + session_spans


def test_otel_identity_is_preserved_exactly() -> None:
    bundle = _load("tool_timeout_retry")
    document = to_otlp(bundle)
    spans_by_op = {}
    for span in _spans(document):
        attributes = _span_attributes(span)
        spans_by_op[attributes["earshot.operation.id"]["stringValue"]] = span

    for operation in bundle.profile.operations:
        span = spans_by_op[operation.operation_id]
        assert span["traceId"] == operation.trace_id
        assert span["spanId"] == operation.span_id
        if operation.parent_span_id is None:
            assert "parentSpanId" not in span
        else:
            assert span["parentSpanId"] == operation.parent_span_id


def test_internal_causal_links_resolve_to_target_identity() -> None:
    bundle = _load("tool_timeout_retry")
    document = to_otlp(bundle)
    retry_span = next(
        span
        for span in _spans(document)
        if _span_attributes(span)["earshot.operation.id"]["stringValue"] == "op-tool-attempt-2"
    )
    link = retry_span["links"][0]
    # op-tool-attempt-2 retries op-tool-attempt-1: the link must carry attempt-1's ids.
    assert link["traceId"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert link["spanId"] == "1111111111111111"
    link_attributes = {a["key"]: a["value"]["stringValue"] for a in link["attributes"]}
    assert link_attributes["earshot.link.type"] == "retries"


def test_status_and_raw_status_are_preserved() -> None:
    bundle = _load("tool_timeout_retry")
    document = to_otlp(bundle)
    for span in _spans(document):
        attributes = _span_attributes(span)
        operation_id = attributes["earshot.operation.id"]["stringValue"]
        raw_status = attributes["earshot.operation.status"]["stringValue"]
        if operation_id == "op-tool-attempt-1":
            assert raw_status == "timeout"
            assert span["status"] == {"code": 2, "message": "timeout"}  # ERROR
        else:
            assert raw_status == "ok"
            assert span["status"] == {"code": 1}  # OK


def test_provider_model_and_gen_ai_attributes_survive() -> None:
    bundle = _load("complete", faults=False)
    document = to_otlp(bundle)
    llm_span = next(span for span in _spans(document) if span["name"] == "llm")
    attributes = _span_attributes(llm_span)
    # gen_ai.* facts are reused verbatim, never renamed.
    assert attributes["gen_ai.request.model"] == {"stringValue": "test-model"}


def test_coverage_and_lossiness_are_attached_to_the_resource() -> None:
    bundle = _load("complete", faults=False)
    resource = _resource_attributes(to_otlp(bundle))
    # Coverage ("what was observed / not observed") survives the projection.
    assert resource["earshot.coverage.client.render"] == {"stringValue": "available"}
    # The projection honestly declares itself lossy with a note.
    assert resource["earshot.projection.lossy"] == {"boolValue": True}
    assert "stringValue" in resource["earshot.projection.note"]
    assert resource["earshot.session.id"] == {"stringValue": "session-1"}
    assert resource["earshot.bundle.id"] == {"stringValue": "bundle-1"}
    assert "service.name" in resource


def test_instrumentation_scope_is_preserved_per_source() -> None:
    bundle = _load("complete", faults=False)
    scope_spans = to_otlp(bundle)["resourceSpans"][0]["scopeSpans"]
    scope_names = {entry["scope"]["name"] for entry in scope_spans}
    # The one operation authored under a named scope keeps that scope distinct.
    assert "earshot.fixture" in scope_names
    named = next(entry for entry in scope_spans if entry["scope"]["name"] == "earshot.fixture")
    assert named["scope"]["version"] == "1.0.0"
    assert named["schemaUrl"] == "https://opentelemetry.io/schemas/1.30.0"


def test_point_operation_gets_no_fabricated_end() -> None:
    bundle = _load("complete", faults=False)
    profile = bundle.profile
    operations = list(profile.operations)
    # Turn the first operation into a point operation (no ended_at).
    operations[0] = operations[0].model_copy(update={"ended_at": None})
    point_bundle = bundle.model_copy(
        update={"profile": profile.model_copy(update={"operations": tuple(operations)})}
    )
    document = to_otlp(point_bundle)
    target = operations[0]
    span = next(
        span
        for span in _spans(document)
        if _span_attributes(span).get("earshot.operation.id", {}).get("stringValue")
        == target.operation_id
    )
    assert "startTimeUnixNano" in span
    assert "endTimeUnixNano" not in span  # no invented duration


def test_standalone_event_is_a_zero_duration_span() -> None:
    bundle = _load("complete", faults=False)
    document = to_otlp(bundle)
    event_span = next(span for span in _spans(document) if span["name"] == "earshot.speech.ended")
    assert event_span["startTimeUnixNano"] == event_span["endTimeUnixNano"]


def test_quality_measurements_ride_as_session_span_events() -> None:
    bundle = _load("webrtc_degradation")
    document = to_otlp(bundle)
    session_span = next(span for span in _spans(document) if span["name"] == "earshot.session")

    def _name(event: dict) -> str:
        attributes = {a["key"]: a["value"] for a in event["attributes"]}
        return attributes["earshot.quality.name"]["stringValue"]

    names = {_name(event) for event in session_span["events"]}
    assert names == {"jitter", "packet_loss_ratio", "round_trip_time"}


def test_openinference_span_kinds() -> None:
    bundle = _load("complete", faults=False)
    document = to_openinference(bundle)
    kinds = {
        span["name"]: _span_attributes(span)["openinference.span.kind"]["stringValue"]
        for span in _spans(document)
    }
    assert kinds["llm"] == "LLM"
    assert kinds["tts"] == "AUDIO"
    assert kinds["turn_detection"] == "CHAIN"


# --------------------------------------------------------------------------- #
# Determinism + golden pinning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("project", [to_otlp, to_openinference])
@pytest.mark.parametrize(
    "name", ["tool_timeout_retry", "webrtc_degradation", "full_barge_in_chain"]
)
def test_projection_is_deterministic(name: str, project) -> None:
    bundle = _load(name)
    first = json.dumps(project(bundle), sort_keys=True)
    second = json.dumps(project(bundle), sort_keys=True)
    assert first == second


def _dump(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_otlp_golden_is_pinned() -> None:
    bundle = _load("tool_timeout_retry")
    assert _dump(to_otlp(bundle)) == (GOLDEN / "tool_timeout_retry.otlp.json").read_text("utf-8")


def test_openinference_golden_is_pinned() -> None:
    bundle = _load("webrtc_degradation")
    expected = (GOLDEN / "webrtc_degradation.openinference.json").read_text("utf-8")
    assert _dump(to_openinference(bundle)) == expected


# --------------------------------------------------------------------------- #
# Push client helpers
# --------------------------------------------------------------------------- #
def test_push_endpoint_normalizes_to_v1_traces() -> None:
    assert OtlpHttpExporter("http://localhost:6006").endpoint == "http://localhost:6006/v1/traces"
    assert (
        OtlpHttpExporter("http://localhost:6006/v1/traces").endpoint
        == "http://localhost:6006/v1/traces"
    )


def test_phoenix_and_langfuse_helpers_build_expected_endpoints() -> None:
    assert phoenix_exporter("http://localhost:6006").endpoint == "http://localhost:6006/v1/traces"
    langfuse = langfuse_exporter("https://cloud.langfuse.com", "pk", "sk")
    assert langfuse.endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    # Basic auth over the public:secret pair.
    assert langfuse._headers["Authorization"] == "Basic cGs6c2s="


def test_push_client_rejects_unsafe_endpoints() -> None:
    with pytest.raises(ValueError):
        OtlpHttpExporter("http://example.com/v1/traces")  # non-loopback http
    with pytest.raises(ValueError):
        OtlpHttpExporter("https://user:pass@example.com/v1/traces")  # userinfo
    with pytest.raises(ValueError):
        langfuse_exporter("https://cloud.langfuse.com", "pk", "")  # missing secret


# --------------------------------------------------------------------------- #
# Mock-server push behavior
# --------------------------------------------------------------------------- #
class _MockOtlpServer(ThreadingHTTPServer):
    daemon_threads = True


class MockCollector:
    def __init__(self, *, status: int, location: str | None = None) -> None:
        self._status = status
        self._location = location
        self.requests: list[tuple[bytes, str | None]] = []
        self._lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type")
                with collector._lock:
                    collector.requests.append((body, content_type))
                self.send_response(collector._status)
                if collector._location is not None:
                    self.send_header("Location", collector._location)
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return None

        self.server = _MockOtlpServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> MockCollector:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@contextlib.contextmanager
def _document() -> Iterator[dict]:
    yield to_otlp(_load("tool_timeout_retry"))


@pytest.mark.integration
def test_push_posts_the_exact_otlp_document() -> None:
    with _document() as document, MockCollector(status=200) as collector:
        result = OtlpHttpExporter(collector.endpoint).export(document)

    assert result.ok is True
    assert result.status == 200
    assert result.spans == span_count(document)
    assert len(collector.requests) == 1
    body, content_type = collector.requests[0]
    assert content_type == "application/json"
    assert body == serialize_document(document)
    assert json.loads(body) == document


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "expected_retryable"),
    [(500, True), (503, True), (429, True), (408, True), (400, False), (401, False)],
)
def test_push_classifies_http_failures(status: int, expected_retryable: bool) -> None:
    with _document() as document, MockCollector(status=status) as collector:
        result = OtlpHttpExporter(collector.endpoint).export(document)

    assert result.ok is False
    assert result.status == status
    assert result.retryable is expected_retryable


@pytest.mark.integration
def test_push_refuses_redirects_and_never_raises() -> None:
    with (
        _document() as document,
        MockCollector(status=307, location="/somewhere-else") as collector,
    ):
        result = OtlpHttpExporter(collector.endpoint).export(document)
        # The redirect is refused: the target is never re-requested.
        assert len(collector.requests) == 1

    assert result.ok is False
    assert result.status == 307


@pytest.mark.integration
def test_push_is_fail_open_on_a_dead_endpoint() -> None:
    # Nothing is listening on this loopback port; export must return, not raise.
    result = OtlpHttpExporter("http://127.0.0.1:1", timeout=0.5).export({"resourceSpans": []})
    assert result.ok is False
    assert result.retryable is True
    assert result.error == "transport.failure"


def test_push_refuses_oversized_bodies_without_sending() -> None:
    with MockCollector(status=200) as collector:
        exporter = OtlpHttpExporter(collector.endpoint, max_body_bytes=8)
        result = exporter.export(to_otlp(_load("tool_timeout_retry")))
        assert result.ok is False
        assert result.error == "payload.too_large"
        assert collector.requests == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_export_writes_projected_document(tmp_path, capsys) -> None:
    source = FAULTS / "tool_timeout_retry.incident.json"
    out = tmp_path / "otlp.json"
    assert main(["export", str(source), "--format", "otlp", "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["format"] == "otlp"
    written = json.loads(out.read_text("utf-8"))
    assert written == to_otlp(_load("tool_timeout_retry"))


def test_cli_export_to_stdout(capsys) -> None:
    source = FAULTS / "webrtc_degradation.incident.json"
    assert main(["export", str(source), "--format", "openinference"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document == to_openinference(_load("webrtc_degradation"))


@pytest.mark.integration
def test_cli_export_push(capsys) -> None:
    source = FAULTS / "tool_timeout_retry.incident.json"
    with MockCollector(status=200) as collector:
        code = main(["export", str(source), "--format", "otlp", "--push-otlp", collector.endpoint])
        assert len(collector.requests) == 1
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["endpoint"].endswith("/v1/traces")
