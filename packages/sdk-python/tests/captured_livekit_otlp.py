"""Read access to the real LiveKit Agents 1.8 OTLP capture (``fixtures/captured/livekit-otlp-1.8``).

Every request is returned as a fresh ``ExportTraceServiceRequest`` (tests may edit it),
in the order the real exporter sent them. ``readable_span`` turns a captured span back
into the ``ReadableSpan`` an in-process span processor receives, so one capture drives
both the server path and the in-process adapter. Importing this module needs
``opentelemetry-proto`` (the ``server`` extra); ``readable_span`` also needs
``opentelemetry-sdk``.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from earshot.call_capture import OtlpSpan, any_value, spans_from_request

LIVEKIT_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "captured" / "livekit-otlp-1.8"


@functools.cache
def livekit_manifest() -> dict[str, Any]:
    return json.loads((LIVEKIT_FIXTURE / "manifest.json").read_text(encoding="utf-8"))


@functools.cache
def _request_bytes() -> tuple[bytes, ...]:
    return tuple(
        (LIVEKIT_FIXTURE / entry["file"]).read_bytes() for entry in livekit_manifest()["requests"]
    )


def livekit_requests() -> list[ExportTraceServiceRequest]:
    """The seven requests of the real call, decoded, in arrival order."""

    return [ExportTraceServiceRequest.FromString(payload) for payload in _request_bytes()]


def livekit_spans() -> list[OtlpSpan]:
    """The capture's 58 spans, in arrival order."""

    return [span for request in livekit_requests() for span in spans_from_request(request)]


# The session key the example stamped as ``earshot.session.id`` on every span.
LIVEKIT_SESSION_KEY: str = livekit_manifest()["provenance"]["session_key"]


def readable_span(span: OtlpSpan) -> Any:
    """A captured span as the OpenTelemetry SDK hands it to a span processor in-process."""

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

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
        kind=SpanKind(max(raw.kind - 1, 0)),
        status=Status(StatusCode(raw.status.code)),
        start_time=raw.start_time_unix_nano,
        end_time=raw.end_time_unix_nano,
        instrumentation_scope=InstrumentationScope(span.scope.name, span.scope.version or None),
    )
