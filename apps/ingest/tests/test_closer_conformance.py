"""AC7.6: the server path and the in-process Pipecat adapter derive the same turn facts.

**Server path:** the real Pipecat 1.5 capture is sent over HTTP to a real server
(``POST /v1/traces``); its maintenance loop's closer turns the call into revision 1.
**In-process path:** the same spans, as the OpenTelemetry SDK hands them to a span
processor (``ReadableSpan``), go straight into ``PipecatAdapter.consume_span`` on a
recorder with the same capture policy. ``adapter_conformance`` compares the turn
facts both bundles project.
"""

from __future__ import annotations

import time

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from adapter_conformance import assert_capture_conforms, assert_same_turn_facts
from captured_otlp import captured_requests
from earshot.adapters.pipecat import PipecatAdapter
from earshot.api import create_app
from earshot.call_capture import any_value, spans_from_request
from earshot.call_materializer import capture_policy
from earshot.clock import ManualClock
from earshot.codec import decode_incident_protobuf
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.storage_calls import CallTimings
from otlp_support import post_otlp, serve
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration


def _readable(span) -> ReadableSpan:
    """A captured span as the SDK's span processors receive it in-process."""

    raw = span.span
    trace_id = int.from_bytes(raw.trace_id, "big")

    def context(span_id: bytes) -> SpanContext:
        return SpanContext(
            trace_id, int.from_bytes(span_id, "big"), is_remote=False, trace_flags=TraceFlags(1)
        )

    def attributes(items) -> dict:
        return {item.key: any_value(item.value) for item in items}

    return ReadableSpan(
        name=raw.name,
        context=context(raw.span_id),
        parent=context(raw.parent_span_id) if raw.parent_span_id else None,
        resource=Resource(attributes(span.resource.attributes)),
        attributes=attributes(raw.attributes),
        events=[
            Event(event.name, attributes(event.attributes), event.time_unix_nano)
            for event in raw.events
        ],
        kind=SpanKind(raw.kind - 1),
        status=Status(StatusCode(raw.status.code)),
        start_time=raw.start_time_unix_nano,
        end_time=raw.end_time_unix_nano,
        instrumentation_scope=InstrumentationScope(span.scope.name, span.scope.version or None),
    )


def _in_process_bundle():
    spans = [span for request in captured_requests() for span in spans_from_request(request)]
    recorder = IncidentRecorder(
        session_id="in-process",
        bundle_id="in-process-call",
        clock=ManualClock(wall=min(span.span.start_time_unix_nano for span in spans)),
        config=RecorderConfig(clock_domain_id="in-process", capture_policy=capture_policy("full")),
    )
    adapter = PipecatAdapter(recorder)
    for span in spans:  # in export order, as a span processor sees them end
        adapter.consume_span(_readable(span))
    return recorder.close()


def test_the_server_path_derives_the_turn_facts_the_in_process_adapter_derives(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    app = create_app(store=store, call_timings=CallTimings(end_grace_seconds=0.05))
    with serve(app) as port:
        for request in captured_requests():
            assert post_otlp(port, request.SerializeToString()).status == 200
        deadline = time.monotonic() + 15
        while not catalog_rows(tmp_path, "SELECT 1 FROM call_revisions"):
            assert time.monotonic() < deadline, "the server's closer made no revision"
            time.sleep(0.05)
    [(call_id, bundle_id)] = catalog_rows(tmp_path, "SELECT call_id, bundle_id FROM call_revisions")
    served = decode_incident_protobuf(store.get_artifact(bundle_id)[1])

    in_process = assert_capture_conforms(_in_process_bundle, require_omission=False)

    assert_same_turn_facts(served, in_process)
    assert len(store.list_turn_facts(session_id=call_id)) == 3
