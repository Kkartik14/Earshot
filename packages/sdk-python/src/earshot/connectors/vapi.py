"""Vapi: the ``end-of-call-report`` server message as call evidence.

Verified on documented payloads only (no Vapi account): the server-message
reference, the ``Artifact`` schema of ``https://api.vapi.ai/api-json``, and
``docs.vapi.ai/assistants/retrieve-call-artifacts``.

- **Auth:** ``Authorization: Bearer <secret>`` or the legacy ``X-Vapi-Secret``,
  exactly one; no timestamp, so no signature window.
- **Consumed:** ``end-of-call-report``; every other ``message.type`` (status,
  transcript and speech updates, tool calls) is ignored.
- **Call:** ``startedAt``..``endedAt`` (ISO), the assistant ID, call type and status,
  ``endedReason``; a reason naming an error marks the call failed.
- **Transcript:** each ``bot``/``assistant`` and ``user`` message is a line. Its
  ``time``..``endTime`` (epoch milliseconds) are measured times; with only
  ``secondsFromStart`` it is ``start_only``; with neither, ``order_only``.
- **Latency:** each ``performanceMetrics.turnLatencies`` item is a turn (milliseconds:
  turn, model, voice, transcriber, endpointing). A report without
  ``performanceMetrics`` keeps its transcript and audio, and says the coverage is
  ``not_exposed``.
- **Cost:** ``cost`` (USD). **Analysis:** ``analysis.summary``, ``successEvaluation``.
- **Recordings:** the customer and assistant mono tracks (one timeline), from the
  presigned URLs (expiring at ``presignedUrlsExpiresAt``) or the recording object;
  else one mixed track. Vapi's API also serves each by call ID with a key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..versions import VAPI_NORMALIZER_VERSION
from .base import (
    ProviderAdapter,
    call_window,
    event_name,
    iso_unix_nano,
    items,
    mapping,
    present,
    provider_id,
    required_mapping,
    transcript_line,
)
from .evidence import (
    AGENT_ID,
    ANALYSIS,
    ANALYSIS_SUCCESSFUL,
    ANALYSIS_SUMMARY,
    CALL,
    CALL_ENDED_REASON,
    CALL_STATUS,
    CALL_TYPE,
    COST,
    COST_AMOUNT,
    COST_SOURCE,
    COST_UNIT,
    COVERAGE_TURN_LATENCY,
    LATENCY_ENDPOINTING_MS,
    LATENCY_LLM_MS,
    LATENCY_STT_MS,
    LATENCY_TTS_MS,
    LATENCY_TURN_MS,
    TURN,
    TURN_INDEX,
    Evidence,
    EvidenceSpan,
    RecordingSource,
    bounded_text,
    content_type_for,
    https_url,
    number,
)
from .signatures import HeaderSecretVerifier

ADAPTER_VERSION = VAPI_NORMALIZER_VERSION
END_OF_CALL_REPORT = "end-of-call-report"
_TURN_FIELDS = {
    "turnLatency": LATENCY_TURN_MS,
    "modelLatency": LATENCY_LLM_MS,
    "voiceLatency": LATENCY_TTS_MS,
    "transcriberLatency": LATENCY_STT_MS,
    "endpointingLatency": LATENCY_ENDPOINTING_MS,
}
_SPEAKERS = {"bot": "agent", "assistant": "agent", "user": "user"}
# Epoch milliseconds from 2001 on: what a message's ``time`` is when it is one.
_EPOCH_MILLISECONDS = 1_000_000_000_000
# (track, speaker, presigned field, recording.mono field)
_MONO_TRACKS = (
    ("customer", "user", "presignedCustomerUrl", "customerUrl"),
    ("assistant", "agent", "presignedAssistantUrl", "assistantUrl"),
)


class VapiConnectorAdapter(ProviderAdapter):
    provider = "vapi"
    translator_version = ADAPTER_VERSION
    consumed_events = frozenset({END_OF_CALL_REPORT})
    serves_recordings_by_id = True

    def verifier(self, headers, secrets, *, now):
        del now
        return HeaderSecretVerifier(headers, secrets, header=b"x-vapi-secret")

    def event_type(self, payload: Mapping[str, Any]) -> str:
        return event_name(required_mapping(payload.get("message")).get("type"))

    def translate(self, event_type: str, payload: Mapping[str, Any]) -> Evidence:
        del event_type
        message = required_mapping(payload.get("message"))
        call = required_mapping(message.get("call"))
        artifact = mapping(message.get("artifact"))
        call_id = provider_id(call.get("id"))
        start, end = call_window(
            iso_unix_nano(
                message.get("startedAt") or call.get("startedAt") or call.get("createdAt")
            ),
            iso_unix_nano(message.get("endedAt") or call.get("endedAt")),
        )
        ended_reason = bounded_text(message.get("endedReason") or call.get("endedReason"))
        performance = artifact.get("performanceMetrics")
        turns = _turns(performance, start, end)
        spans = [
            EvidenceSpan(
                CALL,
                0,
                start,
                end,
                present(
                    {
                        AGENT_ID: bounded_text(call.get("assistantId")),
                        CALL_TYPE: bounded_text(call.get("type")),
                        CALL_STATUS: bounded_text(call.get("status")),
                        CALL_ENDED_REASON: ended_reason,
                        COVERAGE_TURN_LATENCY: (
                            "available" if isinstance(performance, Mapping) else "not_exposed"
                        ),
                    }
                ),
                error=ended_reason is not None and "error" in ended_reason,
            ),
            *_lines(artifact.get("messages"), start),
            *turns,
        ]
        cost = number(message.get("cost", call.get("cost")))
        if cost is not None:
            spans.append(
                EvidenceSpan(
                    COST,
                    0,
                    start,
                    end,
                    {COST_AMOUNT: cost, COST_UNIT: "usd", COST_SOURCE: "message.cost"},
                )
            )
        analysis = mapping(message.get("analysis"))
        analysed = present(
            {
                ANALYSIS_SUMMARY: bounded_text(analysis.get("summary"), maximum=64 * 1024),
                ANALYSIS_SUCCESSFUL: bounded_text(analysis.get("successEvaluation"), maximum=64),
            }
        )
        if analysed:
            spans.append(EvidenceSpan(ANALYSIS, 0, start, end, analysed))
        return Evidence(
            provider=self.provider,
            provider_call_id=call_id,
            spans=tuple(spans),
            recording_sources=_recordings(artifact, start),
        )


def _lines(messages: object, call_start: int) -> list[EvidenceSpan]:
    lines = []
    for ordinal, raw in enumerate(items(messages)):
        item = mapping(raw)
        speaker = _SPEAKERS.get(item.get("role"))  # type: ignore[arg-type]
        if speaker is None:
            continue
        begin, finish = number(item.get("time")), number(item.get("endTime"))
        offset = number(item.get("secondsFromStart"))
        if begin is not None and finish is not None and _EPOCH_MILLISECONDS <= begin <= finish:
            timing, start, end = "measured", int(begin * 1_000_000), int(finish * 1_000_000)
        elif offset is not None:
            timing = "start_only"
            start = end = call_start + int(offset * 1_000_000_000)
        else:
            timing, start, end = "order_only", call_start, call_start
        line = transcript_line(
            ordinal,
            speaker=speaker,
            text=item.get("message"),
            start=start,
            end=end,
            timing=timing,
        )
        if line is not None:
            lines.append(line)
    return lines


def _turns(performance: object, start: int, end: int) -> list[EvidenceSpan]:
    turns = []
    for ordinal, raw in enumerate(items(mapping(performance).get("turnLatencies"))):
        values = present(
            {key: number(mapping(raw).get(field)) for field, key in _TURN_FIELDS.items()}
        )
        if values:
            turns.append(EvidenceSpan(TURN, ordinal, start, end, {TURN_INDEX: ordinal, **values}))
    return turns


def _recordings(artifact: Mapping[str, Any], start: int) -> tuple[RecordingSource, ...]:
    recording = mapping(artifact.get("recording"))
    mono = mapping(recording.get("mono"))
    expires = iso_unix_nano(artifact.get("presignedUrlsExpiresAt"))
    sources = []
    for track, speaker, presigned_field, mono_field in _MONO_TRACKS:
        presigned = https_url(artifact.get(presigned_field))
        url = presigned or https_url(mono.get(mono_field))
        if url is not None:
            sources.append(_source(track, speaker, url, start, expires if presigned else None))
    if sources:
        return tuple(sources)
    presigned = https_url(artifact.get("presignedMonoUrl"))
    url = presigned or https_url(mono.get("combinedUrl")) or https_url(artifact.get("recordingUrl"))
    if url is None:
        return ()
    return (_source("mixed", "mixed", url, start, expires if presigned else None),)


def _source(track: str, speaker: str, url: str, start: int, expires: int | None) -> RecordingSource:
    return RecordingSource(
        track=track,
        speaker=speaker,
        content_type=content_type_for(url, "audio/wav"),
        started_at_unix_nano=start,
        url=url,
        url_expires_unix_nano=expires,
        provider_api=True,
    )
