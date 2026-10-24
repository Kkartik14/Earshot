"""Build deterministic, synthetic Pipecat-shaped OTLP requests for tests.

The requests preserve the seven-batch, seventeen-span shape used by receiver,
storage, and call-materialization tests. All text, identifiers, timestamps, and
recording bytes are generated here. No captured call data is checked in.
"""

from __future__ import annotations

import io
import json
import wave

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from otlp_support import SECOND, START

SYNTHETIC_SESSION_KEY = "synthetic-conversation-001"
SYNTHETIC_USER_ID = "synthetic-caller-001"
SYNTHETIC_CALL_START = START
SYNTHETIC_RECORDING_DURATION_S = 2

SYNTHETIC_LINES = [
    ("user", "Hi. I would like to book a table for two people tomorrow evening."),
    ("agent", "Sure, what time would you like to come in tomorrow evening?"),
    ("user", "Seven thirty works for us."),
    ("user", "Could we sit somewhere quiet near the window?"),
    (
        "agent",
        "Sure, we've booked a quiet window seat for you at 7:30. "
        "We look forward to welcoming you tomorrow.",
    ),
    ("user", "Perfect. That is everything I needed."),
    ("user", "Thank you very much. Goodbye."),
    ("agent", "You're welcome! See you tomorrow."),
]


def _attribute(key: str, value: str | bool | int | float) -> KeyValue:
    if isinstance(value, bool):
        encoded = AnyValue(bool_value=value)
    elif isinstance(value, int):
        encoded = AnyValue(int_value=value)
    elif isinstance(value, float):
        encoded = AnyValue(double_value=value)
    else:
        encoded = AnyValue(string_value=value)
    return KeyValue(key=key, value=encoded)


def _span(
    name: str,
    span_id: int,
    second: int,
    attributes: list[KeyValue],
    *,
    parent_span_id: int | None = None,
    duration_s: int = 1,
) -> Span:
    return Span(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736.to_bytes(16, "big"),
        span_id=span_id.to_bytes(8, "big"),
        parent_span_id=(b"" if parent_span_id is None else parent_span_id.to_bytes(8, "big")),
        name=name,
        start_time_unix_nano=START + second * SECOND,
        end_time_unix_nano=START + (second + duration_s) * SECOND,
        attributes=attributes,
    )


def _request(*spans: tuple[str, Span]) -> ExportTraceServiceRequest:
    grouped: dict[str, list[Span]] = {}
    for scope, span in spans:
        grouped.setdefault(scope, []).append(span)
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(
                    attributes=[
                        _attribute("telemetry.sdk.language", "python"),
                        _attribute("service.name", "earshot-pipecat-otlp-example"),
                    ]
                ),
                scope_spans=[
                    ScopeSpans(scope=InstrumentationScope(name=scope), spans=items)
                    for scope, items in grouped.items()
                ],
            )
        ]
    )


def _stt(span_id: int, second: int, transcript: str, *, parent_span_id: int) -> tuple[str, Span]:
    return "pipecat", _span(
        "stt",
        span_id,
        second,
        [_attribute("transcript", transcript), _attribute("is_final", False)],
        parent_span_id=parent_span_id,
    )


def _llm(
    span_id: int,
    second: int,
    prompt: str,
    response: str | None = None,
    *,
    parent_span_id: int,
) -> tuple[str, Span]:
    attributes = [
        _attribute(
            "input",
            json.dumps([{"role": "user", "content": prompt}], ensure_ascii=False),
        ),
        _attribute("gen_ai.system_instructions", "Help the caller book a quiet table."),
        _attribute("param.user_turn_completion_config", "NOT_GIVEN"),
        _attribute("gen_ai.request.top_k", "NOT_GIVEN"),
        _attribute("stream", True),
        _attribute("metrics.ttfb", 0.21),
        _attribute("gen_ai.request.model", "openai/gpt-oss-20b"),
        _attribute("gen_ai.usage.output_tokens", 32),
    ]
    if response is not None:
        attributes.append(_attribute("output", response))
    return "pipecat", _span(
        "llm", span_id, second, attributes, parent_span_id=parent_span_id, duration_s=2
    )


def _tts(span_id: int, second: int, text: str, *, parent_span_id: int) -> tuple[str, Span]:
    return "pipecat", _span(
        "tts",
        span_id,
        second,
        [_attribute("text", text), _attribute("metrics.character_count", len(text))],
        parent_span_id=parent_span_id,
        duration_s=2,
    )


def _turn(
    span_id: int,
    second: int,
    number: int,
    *,
    parent_span_id: int = 16,
    duration_s: int = 3,
) -> tuple[str, Span]:
    return "pipecat.turn", _span(
        "turn",
        span_id,
        second,
        [
            _attribute("conversation.id", SYNTHETIC_SESSION_KEY),
            _attribute("turn.number", number),
            _attribute("turn.type", "voice"),
            _attribute("turn.user_bot_latency_seconds", 0.8),
            _attribute("turn.duration_seconds", float(duration_s)),
            _attribute("turn.was_interrupted", False),
        ],
        parent_span_id=parent_span_id,
        duration_s=duration_s,
    )


def synthetic_requests() -> list[ExportTraceServiceRequest]:
    """Return seven fresh requests containing the synthetic seventeen-span call."""

    lines = SYNTHETIC_LINES
    return [
        _request(_stt(1, 0, lines[0][1], parent_span_id=6)),
        _request(
            _llm(2, 1, lines[0][1], lines[1][1], parent_span_id=6),
            _tts(3, 3, lines[1][1], parent_span_id=6),
        ),
        _request(
            _stt(4, 3, lines[2][1], parent_span_id=6),
            _llm(5, 3, lines[2][1], parent_span_id=6),
            _turn(6, 0, 1, duration_s=6),
        ),
        _request(
            _stt(7, 6, lines[3][1], parent_span_id=12),
            _llm(8, 7, lines[3][1], lines[4][1], parent_span_id=12),
            _tts(9, 8, lines[4][1], parent_span_id=12),
        ),
        _request(
            _stt(10, 10, lines[5][1], parent_span_id=12),
            _llm(11, 10, lines[5][1], parent_span_id=12),
            _turn(12, 6, 2, duration_s=7),
        ),
        _request(
            _stt(13, 13, lines[6][1], parent_span_id=17),
            _llm(14, 14, lines[6][1], lines[7][1], parent_span_id=17),
            _tts(15, 15, lines[7][1], parent_span_id=17),
        ),
        _request(
            (
                "pipecat.turn",
                _span(
                    "conversation",
                    16,
                    0,
                    [
                        _attribute("earshot.session.id", SYNTHETIC_SESSION_KEY),
                        _attribute("conversation.id", SYNTHETIC_SESSION_KEY),
                        _attribute("conversation.type", "voice"),
                    ],
                    duration_s=22,
                ),
            ),
            _turn(17, 13, 3, duration_s=8),
        ),
    ]


def with_identity(
    requests: list[ExportTraceServiceRequest], user_id: str = SYNTHETIC_USER_ID
) -> list[ExportTraceServiceRequest]:
    """Add the synthetic caller identity to each STT span."""

    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    if span.name == "stt":
                        span.attributes.append(_attribute("user_id", user_id))
    return requests


def synthetic_recording_bytes(speaker: str) -> bytes:
    """Return a two-second silent WAV for one of the sample call speakers."""

    if speaker not in {"user", "agent"}:
        raise ValueError("speaker must be 'user' or 'agent'")
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\0\0" * 16_000 * SYNTHETIC_RECORDING_DURATION_S)
    return output.getvalue()
