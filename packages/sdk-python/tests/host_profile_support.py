"""Calls in the host span profile (``call_span_facts``), as a host's exporter sends them.

``host_request`` builds one OTLP request under the ``earshot.host`` scope: a ``call``
span with each ``turn`` as its child, and each turn's ``user_speech``,
``agent_speech`` and stage spans as the turn's children. Span IDs are fixed by
position (the call is 1; turn ``n`` is ``0x100 * n`` and its children follow), so a
repeat delivery writes the same spans. The session key is on the resource.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

HOST_SCOPE = "earshot.host"
HOST_SESSION_KEY = "host-session-0001"
TRACE = 0x48


@dataclass(frozen=True)
class HostStage:
    name: str  # stt, llm or tts
    start: int
    end: int
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HostTurn:
    turn_id: str
    start: int
    end: int
    user_speech: tuple[tuple[int, int], ...] = ()
    agent_speech: tuple[int, int] | None = None
    stages: tuple[HostStage, ...] = ()
    reported_latency_ms: float | None = None
    timeline_continuous: bool | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


def attribute(key: str, value: Any) -> KeyValue:
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _span(
    span_id: int, name: str, start: int, end: int, parent: int | None, attributes: Mapping
) -> Span:
    return Span(
        trace_id=TRACE.to_bytes(16, "big"),
        span_id=span_id.to_bytes(8, "big"),
        parent_span_id=b"" if parent is None else parent.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        attributes=[attribute(key, value) for key, value in attributes.items()],
    )


def host_spans(
    turns: Sequence[HostTurn], *, ended: bool = True, call: Mapping[str, Any] | None = None
) -> list[Span]:
    start = min(turn.start for turn in turns)
    end = max(turn.end for turn in turns)
    spans = [
        _span(
            1,
            "call",
            start,
            end,
            None,
            {**({"earshot.call.ended": True} if ended else {}), **(call or {})},
        )
    ]
    for number, turn in enumerate(turns, start=1):
        turn_span = 0x100 * number
        turn_attributes: dict[str, Any] = {
            "earshot.turn.id": turn.turn_id,
            "earshot.turn.index": number,
            **turn.attributes,
        }
        if turn.reported_latency_ms is not None:
            turn_attributes["earshot.turn.reported_latency_ms"] = turn.reported_latency_ms
        if turn.timeline_continuous is not None:
            turn_attributes["earshot.turn.timeline_continuous"] = turn.timeline_continuous
        spans.append(_span(turn_span, "turn", turn.start, turn.end, 1, turn_attributes))
        child = turn_span
        own = {"earshot.turn.id": turn.turn_id}
        for speech_start, speech_end in turn.user_speech:
            child += 1
            spans.append(_span(child, "user_speech", speech_start, speech_end, turn_span, own))
        if turn.agent_speech is not None:
            child += 1
            spans.append(_span(child, "agent_speech", *turn.agent_speech, turn_span, own))
        for stage in turn.stages:
            child += 1
            spans.append(
                _span(
                    child,
                    stage.name,
                    stage.start,
                    stage.end,
                    turn_span,
                    {**own, **stage.attributes},
                )
            )
    return spans


def host_request(
    turns: Sequence[HostTurn],
    *,
    ended: bool = True,
    session_key: str = HOST_SESSION_KEY,
    call: Mapping[str, Any] | None = None,
) -> ExportTraceServiceRequest:
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[attribute("earshot.session.id", session_key)]),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name=HOST_SCOPE, version="0.1.0"),
                        spans=host_spans(turns, ended=ended, call=call),
                    )
                ],
            )
        ]
    )
