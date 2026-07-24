"""Golden and hardening tests for the OTLP / OpenInference exporters + push client."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.codec import decode_incident_json
from earshot.contract import ClockDomain, IncidentBundle, TimePoint
from earshot.exporters import (
    OtlpHttpExporter,
    langfuse_exporter,
    phoenix_exporter,
    span_count,
    to_openinference,
    to_otlp,
)
from earshot.exporters.push import serialize_document
from incident_factory import make_valid_bundle

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
    # Scopes live inside whichever resource declared them, so look across resources.
    scope_spans = [
        entry
        for resource_spans in to_otlp(bundle)["resourceSpans"]
        for entry in resource_spans["scopeSpans"]
    ]
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
# The projection must not manufacture trace topology
#
# A lossy OTLP projection is expected; an *authoritative-looking* one is a defect.
# These tests pin the four ways the projection could invent structure the incident
# never contained: epoch timestamps, duplicate span identity, one incident split
# across synthetic traces, and spans reassigned to a service they never declared.
# --------------------------------------------------------------------------- #
# 2000-01-01T00:00:00Z. Any emitted OTLP unix-nano below this is not a wall clock:
# it is a monotonic reading written into an epoch field.
_YEAR_2000_UNIX_NANO = 946_684_800_000_000_000

_BROWSER_CLOCK = ClockDomain(
    clock_domain_id="browser-clock",
    kind="browser_monotonic",
    observer="browser",
)

_PROJECTION_FIXTURES = [
    "tool_timeout_retry",
    "webrtc_degradation",
    "full_barge_in_chain",
    "llm_delay",
    "render_delay",
    "telephony_handoff",
]


def _monotonic_only(nano: int) -> TimePoint:
    """A reading from an uncalibrated browser clock: no unix time exists for it."""

    return TimePoint(monotonic_time_nano=str(nano), clock_domain_id="browser-clock")


def _patch_profile(bundle: IncidentBundle, **updates: object) -> IncidentBundle:
    profile = bundle.profile
    return bundle.model_copy(update={"profile": profile.model_copy(update=updates)})


def _with_browser_clock(bundle: IncidentBundle, **updates: object) -> IncidentBundle:
    return _patch_profile(
        bundle, clock_domains=(*bundle.profile.clock_domains, _BROWSER_CLOCK), **updates
    )


def _strip_otel_identity(bundle: IncidentBundle) -> IncidentBundle:
    """An incident the source never traced: no trace/span ids anywhere."""

    profile = bundle.profile
    return _patch_profile(
        bundle,
        operations=tuple(
            operation.model_copy(update={"trace_id": None, "span_id": None, "parent_span_id": None})
            for operation in profile.operations
        ),
        events=tuple(
            event.model_copy(update={"trace_id": None, "span_id": None}) for event in profile.events
        ),
    )


def _identities(document: dict) -> list[tuple[str, str]]:
    return [(span["traceId"], span["spanId"]) for span in _spans(document)]


def _service_by_span_name(document: dict) -> dict[str, str]:
    services: dict[str, str] = {}
    for resource_spans in document["resourceSpans"]:
        attributes = {a["key"]: a["value"] for a in resource_spans["resource"]["attributes"]}
        service = attributes["service.name"]["stringValue"]
        for scope_spans in resource_spans["scopeSpans"]:
            for span in scope_spans["spans"]:
                services[span["name"]] = service
    return services


# --- (a) monotonic readings must never land in a unix-epoch field ----------- #
@pytest.mark.parametrize("project", [to_otlp, to_openinference])
@pytest.mark.parametrize("name", _PROJECTION_FIXTURES)
def test_every_emitted_time_field_holds_a_real_wall_clock(name: str, project) -> None:
    for span in _spans(project(_load(name))):
        assert int(span["startTimeUnixNano"]) >= _YEAR_2000_UNIX_NANO
        if "endTimeUnixNano" in span:
            assert int(span["endTimeUnixNano"]) >= _YEAR_2000_UNIX_NANO
        for event in span.get("events", []):
            assert int(event["timeUnixNano"]) >= _YEAR_2000_UNIX_NANO


def test_monotonic_only_operation_is_omitted_not_dated_to_1970() -> None:
    # A browser-clock render whose endpoints carry only performance.now()-style
    # readings has no server unix time at all; writing 1.5s-since-boot into
    # startTimeUnixNano would place the span in January 1970.
    bundle = make_valid_bundle()
    operations = list(bundle.profile.operations)
    operations[-1] = operations[-1].model_copy(
        update={
            "started_at": _monotonic_only(1_500_000_000),
            "ended_at": _monotonic_only(1_700_000_000),
        }
    )
    document = to_otlp(_with_browser_clock(bundle, operations=tuple(operations)))

    for span in _spans(document):
        assert int(span["startTimeUnixNano"]) >= _YEAR_2000_UNIX_NANO
    assert "render" not in {span["name"] for span in _spans(document)}
    resource = _resource_attributes(document)
    assert int(resource["earshot.projection.omitted_record_count"]["intValue"]) >= 1
    details = resource["earshot.projection.details"]["arrayValue"]["values"]
    assert any("monotonic" in value["stringValue"] for value in details)


def test_monotonic_only_end_is_omitted_rather_than_backdated() -> None:
    bundle = make_valid_bundle()
    operations = list(bundle.profile.operations)
    operations[-1] = operations[-1].model_copy(update={"ended_at": _monotonic_only(1_900_000_000)})
    document = to_otlp(_with_browser_clock(bundle, operations=tuple(operations)))

    render = next(span for span in _spans(document) if span["name"] == "render")
    assert int(render["startTimeUnixNano"]) >= _YEAR_2000_UNIX_NANO
    # The real start survives; the unknowable end is absent, not invented.
    assert "endTimeUnixNano" not in render


def test_monotonic_only_event_is_omitted_not_dated_to_1970() -> None:
    bundle = make_valid_bundle()
    events = list(bundle.profile.events)
    events[0] = events[0].model_copy(update={"time": _monotonic_only(900_000_000)})
    document = to_otlp(_with_browser_clock(bundle, events=tuple(events)))

    assert "earshot.speech.ended" not in {span["name"] for span in _spans(document)}
    for span in _spans(document):
        assert int(span["startTimeUnixNano"]) >= _YEAR_2000_UNIX_NANO


def test_the_emitted_time_basis_is_declared_on_every_span() -> None:
    document = to_otlp(_load("tool_timeout_retry"))
    for span in _spans(document):
        basis = _span_attributes(span)["earshot.projection.time_basis"]["stringValue"]
        assert basis in {"source_wall", "observed_wall"}


# --- (b) one logical entity -> exactly one OTLP entity ---------------------- #
@pytest.mark.parametrize("project", [to_otlp, to_openinference])
@pytest.mark.parametrize("name", _PROJECTION_FIXTURES)
def test_span_identity_is_unique_within_a_document(name: str, project) -> None:
    identities = _identities(project(_load(name)))
    assert len(identities) == len(set(identities))


def test_two_events_recorded_on_one_source_span_get_distinct_identities() -> None:
    # The classic OTLP ingest shape: two span events lifted off the same span, each
    # carrying that span's trace/span id. Reusing it as their own span id emits two
    # spans with one identity.
    bundle = make_valid_bundle()
    base = bundle.profile.events[0]
    twins = tuple(
        base.model_copy(
            update={
                "event_id": f"evt-twin-{suffix}",
                "operation_id": None,
                "trace_id": "b" * 32,
                "span_id": "9" * 16,
            }
        )
        for suffix in ("a", "b")
    )
    document = to_otlp(_patch_profile(bundle, events=twins))

    identities = _identities(document)
    assert len(identities) == len(set(identities))
    twin_spans = [span for span in _spans(document) if span["name"] == base.event_name]
    assert len(twin_spans) == 2
    # The recorded span they were observed on stays their parent -- the truthful
    # relationship -- and is never reused as their own identity.
    assert {span["parentSpanId"] for span in twin_spans} == {"9" * 16}
    assert {span["traceId"] for span in twin_spans} == {"b" * 32}


def test_an_event_carrying_an_operations_span_identity_becomes_that_spans_event() -> None:
    bundle = make_valid_bundle()
    llm = next(op for op in bundle.profile.operations if op.operation_name == "llm")
    ghost = bundle.profile.events[0].model_copy(
        update={
            "event_id": "evt-ghost",
            "operation_id": None,
            "trace_id": llm.trace_id,
            "span_id": llm.span_id,
        }
    )
    document = to_otlp(_patch_profile(bundle, events=(ghost,)))

    identities = _identities(document)
    assert len(identities) == len(set(identities))
    llm_span = next(span for span in _spans(document) if span["name"] == "llm")
    hosted = [
        {a["key"]: a["value"] for a in event["attributes"]}["earshot.event.id"]["stringValue"]
        for event in llm_span.get("events", [])
    ]
    assert hosted == ["evt-ghost"]
    # ... and it is not also emitted as a second span.
    assert sum(1 for span in _spans(document) if span["name"] == ghost.event_name) == 0


# --- (c) one incident -> one trace ------------------------------------------ #
@pytest.mark.parametrize("project", [to_otlp, to_openinference])
@pytest.mark.parametrize("name", _PROJECTION_FIXTURES)
def test_one_incident_projects_into_one_trace(name: str, project) -> None:
    bundle = _load(name)
    document = project(bundle)
    recorded = {
        record.trace_id
        for record in (*bundle.profile.operations, *bundle.profile.events)
        if record.trace_id is not None
    }
    trace_ids = {span["traceId"] for span in _spans(document)}
    # The projection never adds a trace the incident did not already contain.
    assert len(trace_ids) <= max(len(recorded), 1)


def test_an_identity_less_incident_maps_to_exactly_one_trace() -> None:
    document = to_otlp(_strip_otel_identity(make_valid_bundle()))
    trace_ids = {span["traceId"] for span in _spans(document)}
    assert len(_spans(document)) > 1
    assert len(trace_ids) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", next(iter(trace_ids)))


def test_the_synthetic_incident_trace_id_is_deterministic_and_incident_scoped() -> None:
    def trace_of(bundle: IncidentBundle) -> str:
        trace_ids = {span["traceId"] for span in _spans(to_otlp(bundle))}
        assert len(trace_ids) == 1
        return trace_ids.pop()

    first = trace_of(_strip_otel_identity(make_valid_bundle()))
    again = trace_of(_strip_otel_identity(make_valid_bundle()))
    other = trace_of(_strip_otel_identity(make_valid_bundle(bundle_id="bundle-2")))
    assert first == again  # no clock, no randomness
    assert first != other  # one incident, one trace -- and only that incident's


def test_records_without_a_trace_context_join_the_recorded_trace() -> None:
    # full_barge_in_chain records operations on one real trace plus standalone
    # interruption events and quality samples that carry no trace context.
    bundle = _load("full_barge_in_chain")
    recorded = {
        operation.trace_id
        for operation in bundle.profile.operations
        if operation.trace_id is not None
    }
    assert len(recorded) == 1
    document = to_otlp(bundle)
    assert {span["traceId"] for span in _spans(document)} == recorded
    assert "earshot.session" in {span["name"] for span in _spans(document)}


# --- (d) distinct resources stay distinct ----------------------------------- #
def test_distinct_service_resources_are_not_merged_first_wins() -> None:
    bundle = make_valid_bundle()
    operations = list(bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={"resource": {"service.name": "voice-api", "host.name": "api-1"}}
    )
    operations[-1] = operations[-1].model_copy(
        update={"resource": {"service.name": "browser-client", "host.name": "laptop"}}
    )
    document = to_otlp(_patch_profile(bundle, operations=tuple(operations)))

    services = _service_by_span_name(document)
    assert services["turn_detection"] == "voice-api"
    assert services["render"] == "browser-client"
    assert span_count(document) == len(_spans(document))


def test_an_undeclared_resource_is_not_reassigned_to_a_declared_service() -> None:
    bundle = make_valid_bundle()
    operations = list(bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={"resource": {"service.name": "voice-api"}})
    document = to_otlp(_patch_profile(bundle, operations=tuple(operations)))

    services = _service_by_span_name(document)
    assert services["turn_detection"] == "voice-api"
    # op-llm declared no resource: it keeps the producer identity rather than being
    # silently filed under a service it never claimed.
    assert services["llm"] == "earshot-tests"


def test_incident_facts_are_repeated_on_every_resource() -> None:
    bundle = make_valid_bundle()
    operations = list(bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={"resource": {"service.name": "voice-api"}})
    document = to_otlp(_patch_profile(bundle, operations=tuple(operations)))

    assert len(document["resourceSpans"]) == 2
    for resource_spans in document["resourceSpans"]:
        attributes = {a["key"]: a["value"] for a in resource_spans["resource"]["attributes"]}
        assert attributes["earshot.bundle.id"] == {"stringValue": "bundle-1"}
        assert attributes["earshot.session.id"] == {"stringValue": "session-1"}
        assert attributes["earshot.projection.lossy"] == {"boolValue": True}


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
