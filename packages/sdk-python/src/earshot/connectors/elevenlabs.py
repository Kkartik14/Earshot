"""ElevenLabs Agents: post-call webhooks as call evidence.

Tested on a real conversation (``fixtures/captured/elevenlabs``) wrapped in the
documented webhook envelope and signed with a test secret; the webhook envelope,
``call_initiation_failure`` and the traces-only OTel shape are from the docs.

- **Auth:** ``ElevenLabs-Signature: t=<s>,v0=<hex>[,v0=…]``, HMAC-SHA256 of
  ``"<t>." + body`` with the webhook secret; within 1800 s.
- **Consumed:** ``post_call_transcription``, ``post_call_transcription_otel`` and
  ``call_initiation_failure``. ``post_call_audio`` (base64 MP3, often over the body
  cap) is authenticated as it streams, up to 64 MiB, and ignored: the audio comes
  from the conversations API instead.
- **Call:** ``metadata.start_time_unix_secs`` plus ``call_duration_secs``; agent,
  status, ``termination_reason``.
- **Transcript:** ``time_in_call_secs`` is a turn's start in whole seconds and there
  is no end, so every line is ``start_only`` (never an end taken from the next line).
- **Latency:** an agent turn's ``conversation_turn_metrics`` (seconds): LLM and TTS
  time to first byte and ASR latency as stages; ``convai_ttf_audio_since_silence``
  (time to first audio after the caller's silence) as the turn's reported latency.
- **Cost:** ``metadata.cost``, in credits. **Analysis:** summary and success.
- **Recording:** one mixed MP3 from ``GET /v1/convai/conversations/{id}/audio`` when
  ``has_audio``; it needs an API key (``api_key_ref``), and without one there is none.

Both transcription events describe the same conversation and write the same spans.
The OTel event is translated from its conversation object when it carries one, and
otherwise from its ``elevenlabs.conversation`` and ``elevenlabs.recv.*`` spans.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..versions import ELEVENLABS_NORMALIZER_VERSION
from .base import (
    ProviderAdapter,
    call_window,
    event_name,
    items,
    mapping,
    present,
    provider_id,
    required_mapping,
    transcript_line,
    whole_number,
)
from .evidence import (
    AGENT_ID,
    AGENT_NAME,
    ANALYSIS,
    ANALYSIS_SUCCESSFUL,
    ANALYSIS_SUMMARY,
    CALL,
    CALL_ENDED_REASON,
    CALL_STATUS,
    COST,
    COST_AMOUNT,
    COST_SOURCE,
    COST_UNIT,
    LATENCY_LLM_MS,
    LATENCY_STT_MS,
    LATENCY_TTS_MS,
    LATENCY_TURN_MS,
    TURN,
    TURN_INDEX,
    AttributeValue,
    Evidence,
    EvidenceSpan,
    RecordingSource,
    bounded_text,
    number,
)
from .signatures import HmacVerifier, header_values, within_window
from .types import DeliveryPayloadError, DeliveryTrustError

ADAPTER_VERSION = ELEVENLABS_NORMALIZER_VERSION
TRANSCRIPTION = "post_call_transcription"
TRANSCRIPTION_OTEL = "post_call_transcription_otel"
INITIATION_FAILURE = "call_initiation_failure"
AUDIO = "post_call_audio"
SIGNATURE_WINDOW_SECONDS = 1800
MAX_STREAMED_BYTES = 64 * 1024 * 1024
_SECOND = 1_000_000_000
_SPEAKERS = {"agent": "agent", "user": "user"}
_TURN_METRICS = {
    "convai_ttf_audio_since_silence": LATENCY_TURN_MS,
    "convai_llm_service_ttfb": LATENCY_LLM_MS,
    "convai_tts_service_ttfb": LATENCY_TTS_MS,
    "convai_turn_asr_latency": LATENCY_STT_MS,
}
_FAILED_STATUSES = frozenset({"failed"})


class ElevenLabsConnectorAdapter(ProviderAdapter):
    provider = "elevenlabs"
    translator_version = ADAPTER_VERSION
    consumed_events = frozenset({TRANSCRIPTION, TRANSCRIPTION_OTEL, INITIATION_FAILURE})
    streamed_events = frozenset({AUDIO})
    serves_recordings_by_id = True

    @property
    def max_streamed_bytes(self) -> int:
        return MAX_STREAMED_BYTES

    def verifier(self, headers, secrets, *, now):
        values = header_values(headers, b"elevenlabs-signature")
        if len(values) != 1:
            raise DeliveryTrustError
        try:
            pieces = [piece.strip() for piece in values[0].decode("ascii").split(",")]
        except UnicodeDecodeError:
            raise DeliveryTrustError from None
        timestamps = [piece[2:] for piece in pieces if piece.startswith("t=")]
        signatures = tuple(piece[3:] for piece in pieces if piece.startswith("v0="))
        if (
            len(timestamps) != 1
            or not timestamps[0].isdigit()
            or len(timestamps[0]) > 16
            or not signatures
            or any(len(value) != 64 for value in signatures)
        ):
            raise DeliveryTrustError
        within_window(int(timestamps[0]), now, SIGNATURE_WINDOW_SECONDS)
        return HmacVerifier(secrets, signatures, prefix=timestamps[0].encode("ascii") + b".")

    def event_type(self, payload: Mapping[str, Any]) -> str:
        return event_name(payload.get("type"))

    def translate(self, event_type: str, payload: Mapping[str, Any]) -> Evidence:
        data = required_mapping(payload.get("data"))
        conversation_id = provider_id(data.get("conversation_id"))
        if event_type == INITIATION_FAILURE:
            spans = _initiation_failure(data, whole_number(payload.get("event_timestamp")))
            return Evidence(self.provider, conversation_id, spans)
        if event_type == TRANSCRIPTION_OTEL and "transcript" not in data:
            return Evidence(self.provider, conversation_id, _from_traces(data))
        metadata = mapping(data.get("metadata"))
        start_seconds = whole_number(metadata.get("start_time_unix_secs"))
        if start_seconds is None:
            raise DeliveryPayloadError
        duration = number(metadata.get("call_duration_secs")) or 0.0
        start, end = call_window(
            start_seconds * _SECOND, start_seconds * _SECOND + int(duration * _SECOND)
        )
        status = bounded_text(data.get("status"))
        analysis = mapping(data.get("analysis"))
        spans = [
            _call(
                start,
                end,
                agent_id=data.get("agent_id"),
                agent_name=data.get("agent_name"),
                status=status,
                reason=metadata.get("termination_reason"),
            ),
            *_lines_and_turns(data.get("transcript"), start),
            *_analysis_and_cost(
                start,
                end,
                summary=analysis.get("transcript_summary"),
                successful=analysis.get("call_successful"),
                cost=metadata.get("cost"),
            ),
        ]
        recordings = (
            (
                RecordingSource(
                    track="mixed",
                    speaker="mixed",
                    content_type="audio/mpeg",
                    started_at_unix_nano=start,
                    provider_api=True,
                ),
            )
            if data.get("has_audio") is True
            else ()
        )
        return Evidence(self.provider, conversation_id, tuple(spans), recordings)


def _call(
    start: int,
    end: int,
    *,
    agent_id: object,
    agent_name: object,
    status: str | None,
    reason: object,
    error: bool = False,
) -> EvidenceSpan:
    return EvidenceSpan(
        CALL,
        0,
        start,
        end,
        present(
            {
                AGENT_ID: bounded_text(agent_id),
                AGENT_NAME: bounded_text(agent_name),
                CALL_STATUS: status,
                CALL_ENDED_REASON: bounded_text(reason),
            }
        ),
        error=error or status in _FAILED_STATUSES,
    )


def _lines_and_turns(transcript: object, call_start: int) -> list[EvidenceSpan]:
    spans = []
    for ordinal, raw in enumerate(items(transcript)):
        entry = mapping(raw)
        speaker = _SPEAKERS.get(entry.get("role"))  # type: ignore[arg-type]
        offset = number(entry.get("time_in_call_secs"))
        if speaker is None or offset is None:
            continue
        at = call_start + int(offset * _SECOND)
        interrupted = entry.get("interrupted")
        line = transcript_line(
            ordinal,
            speaker=speaker,
            text=entry.get("message"),
            start=at,
            end=at,
            timing="start_only",
            interrupted=interrupted if isinstance(interrupted, bool) else False,
        )
        if line is not None:
            spans.append(line)
        if speaker != "agent":
            continue
        turn_metrics = mapping(entry.get("conversation_turn_metrics"))
        metrics = mapping(turn_metrics.get("metrics", turn_metrics))
        values: dict[str, AttributeValue] = {}
        for name, key in _TURN_METRICS.items():
            seconds = number(mapping(metrics.get(name)).get("elapsed_time"))
            if seconds is not None:
                values[key] = seconds * 1_000
        if values:
            spans.append(EvidenceSpan(TURN, ordinal, at, at, {TURN_INDEX: ordinal, **values}))
    return spans


def _analysis_and_cost(
    start: int, end: int, *, summary: object, successful: object, cost: object
) -> list[EvidenceSpan]:
    spans = []
    analysed = present(
        {
            ANALYSIS_SUMMARY: bounded_text(summary, maximum=64 * 1024),
            ANALYSIS_SUCCESSFUL: bounded_text(successful, maximum=64),
        }
    )
    if analysed:
        spans.append(EvidenceSpan(ANALYSIS, 0, start, end, analysed))
    amount = number(cost)
    if amount is not None:
        spans.append(
            EvidenceSpan(
                COST,
                0,
                start,
                end,
                {COST_AMOUNT: amount, COST_UNIT: "credit", COST_SOURCE: "data.metadata.cost"},
            )
        )
    return spans


def _initiation_failure(data: Mapping[str, Any], event_seconds: int | None) -> tuple:
    if event_seconds is None:
        raise DeliveryPayloadError
    at = event_seconds * _SECOND
    call_window(at, at)
    return (
        _call(
            at,
            at,
            agent_id=data.get("agent_id"),
            agent_name=None,
            status="initiation_failed",
            reason=data.get("failure_reason"),
            error=True,
        ),
    )


def _from_traces(data: Mapping[str, Any]) -> tuple[EvidenceSpan, ...]:
    """The OTel event's own spans (OTLP/JSON) as the facts the transcription event
    gives: the conversation span is the call, each ``elevenlabs.recv.*`` span a line."""

    spans = [
        mapping(span)
        for resource in items(required_mapping(data.get("otlp_traces")).get("resourceSpans"))
        for scope in items(mapping(resource).get("scopeSpans"))
        for span in items(mapping(scope).get("spans"))
    ]
    conversations = [span for span in spans if span.get("name") == "elevenlabs.conversation"]
    if len(conversations) != 1:
        raise DeliveryPayloadError
    [conversation] = conversations
    facts = _otlp_attributes(conversation.get("attributes"))
    start, end = call_window(
        _otlp_time(conversation.get("startTimeUnixNano")),
        _otlp_time(conversation.get("endTimeUnixNano")),
    )
    evidence = [
        _call(
            start,
            end,
            agent_id=facts.get("elevenlabs.agent_id", data.get("agent_id")),
            agent_name=facts.get("elevenlabs.agent_name"),
            status=bounded_text(facts.get("elevenlabs.status")),
            reason=facts.get("elevenlabs.termination_reason"),
        ),
        *_analysis_and_cost(
            start,
            end,
            summary=facts.get("elevenlabs.analysis.transcript_summary"),
            successful=facts.get("elevenlabs.analysis.call_successful"),
            cost=facts.get("elevenlabs.cost"),
        ),
    ]
    for span in spans:
        speaker = _TRACE_SPEAKERS.get(span.get("name"))  # type: ignore[arg-type]
        at = _otlp_time(span.get("startTimeUnixNano"))
        values = _otlp_attributes(span.get("attributes"))
        index = values.get("elevenlabs.turn.index")
        if speaker is None or at is None or not isinstance(index, (str, int)):
            continue
        ordinal = whole_number(int(index)) if str(index).isdigit() else None
        if ordinal is None:
            continue
        line = transcript_line(
            ordinal,
            speaker=speaker,
            text=values.get(f"elevenlabs.{speaker}.text"),
            start=at,
            end=at,
            timing="start_only",
            interrupted=values.get("elevenlabs.interrupted") is True,
        )
        if line is not None:
            evidence.append(line)
    return tuple(evidence)


_TRACE_SPEAKERS = {
    "elevenlabs.recv.agent_response": "agent",
    "elevenlabs.recv.user_transcript": "user",
}


def _otlp_time(value: object) -> int | None:
    """An OTLP/JSON time (a decimal string, or a number) as nanoseconds."""

    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        return int(value)
    return whole_number(value)


def _otlp_attributes(values: object) -> dict[str, object]:
    """OTLP/JSON attributes (``[{key, value: {stringValue|intValue|…}}]``) as a dict
    of their scalar values; the first of a repeated key wins."""

    found: dict[str, object] = {}
    for raw in items(values):
        item = mapping(raw)
        key, value = item.get("key"), mapping(item.get("value"))
        if not isinstance(key, str) or key in found:
            continue
        for kind in ("stringValue", "boolValue", "doubleValue"):
            if kind in value:
                found[key] = value[kind]
                break
        else:
            if "intValue" in value:
                raw_int = value["intValue"]
                found[key] = int(raw_int) if str(raw_int).lstrip("-").isdigit() else None
    return found
