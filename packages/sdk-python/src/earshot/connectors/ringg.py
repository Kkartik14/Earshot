"""Ringg: the consolidated ``all_processing_completed`` event as call evidence.

Verified on documented payloads only (no Ringg account): the webhook field reference,
call-event examples and setup pages at ``docs.ringg.ai``.

- **Auth:** a static secret in a custom header: ``Authorization: Bearer <secret>``
  or ``X-Webhook-Secret: <secret>``, exactly one; Ringg signs nothing.
- **Consumed:** ``all_processing_completed``, sent once the recording, transcript
  and analyses are done; the progress events before it are ignored.
- **Call:** ``called_on`` (ISO) plus ``call_duration`` (seconds); agent, call type,
  status and ``sub_status``; ``failed`` and ``error`` mark the call failed.
- **Transcript:** ``{"bot": …}``/``{"user": …}`` turns with no timing: every line is
  ``order_only``.
- **Latency:** ``overall_latency_seconds`` and ``first_utterance_seconds`` are call
  aggregates, kept as series of one (milliseconds), never as turns.
- **Cost:** ``call_cost``, in credits. **Analysis:** the platform analysis's summary
  and classification.
- **Recording:** ``recording_url`` (mixed; it expires about a day later).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..versions import RINGG_NORMALIZER_VERSION
from .base import (
    ProviderAdapter,
    call_window,
    event_name,
    iso_unix_nano,
    items,
    mapping,
    present,
    provider_id,
    transcript_line,
)
from .evidence import (
    AGENT_ID,
    AGENT_NAME,
    ANALYSIS,
    ANALYSIS_CLASSIFICATION,
    ANALYSIS_SUMMARY,
    CALL,
    CALL_ENDED_REASON,
    CALL_STATUS,
    CALL_TYPE,
    COST,
    COST_AMOUNT,
    COST_SOURCE,
    COST_UNIT,
    LATENCY,
    LATENCY_COMPONENT,
    LATENCY_VALUES_MS,
    Evidence,
    EvidenceSpan,
    RecordingSource,
    bounded_text,
    content_type_for,
    https_url,
    number,
)
from .signatures import HeaderSecretVerifier

ADAPTER_VERSION = RINGG_NORMALIZER_VERSION
ALL_PROCESSING_COMPLETED = "all_processing_completed"
_SPEAKERS = (("bot", "agent"), ("user", "user"))
_FAILED_STATUSES = frozenset({"failed", "error"})
_LATENCIES = (
    ("overall_latency_seconds", "overall"),
    ("first_utterance_seconds", "first_utterance"),
)


class RinggConnectorAdapter(ProviderAdapter):
    provider = "ringg"
    translator_version = ADAPTER_VERSION
    consumed_events = frozenset({ALL_PROCESSING_COMPLETED})

    def verifier(self, headers, secrets, *, now):
        del now
        return HeaderSecretVerifier(headers, secrets, header=b"x-webhook-secret")

    def event_type(self, payload: Mapping[str, Any]) -> str:
        return event_name(payload.get("event_type"))

    def delivery_key(self, event_type: str, provider_call_id: str) -> str:
        # Ringg's documented deduplication key, in the order 0.3.x keyed receipts by.
        return f"{provider_call_id}:{event_type}"

    def translate(self, event_type: str, payload: Mapping[str, Any]) -> Evidence:
        del event_type
        call_id = provider_id(payload.get("call_id"))
        called_on = iso_unix_nano(payload.get("called_on"))
        duration = number(payload.get("call_duration")) or 0.0
        start, end = call_window(
            called_on, None if called_on is None else called_on + int(duration * 1_000_000_000)
        )
        status = bounded_text(payload.get("status"))
        spans = [
            EvidenceSpan(
                CALL,
                0,
                start,
                end,
                present(
                    {
                        AGENT_ID: bounded_text(payload.get("agent_id")),
                        AGENT_NAME: bounded_text(payload.get("agent_name")),
                        CALL_TYPE: bounded_text(payload.get("call_type")),
                        CALL_STATUS: status,
                        CALL_ENDED_REASON: bounded_text(payload.get("sub_status")),
                    }
                ),
                error=status in _FAILED_STATUSES,
            ),
            *_lines(payload.get("transcript"), start),
        ]
        for position, (field, component) in enumerate(_LATENCIES):
            seconds = number(payload.get(field))
            if seconds is not None:
                spans.append(
                    EvidenceSpan(
                        LATENCY,
                        position,
                        start,
                        end,
                        {LATENCY_COMPONENT: component, LATENCY_VALUES_MS: (seconds * 1_000,)},
                    )
                )
        cost = number(payload.get("call_cost"))
        if cost is not None:
            spans.append(
                EvidenceSpan(
                    COST,
                    0,
                    start,
                    end,
                    {COST_AMOUNT: cost, COST_UNIT: "credit", COST_SOURCE: "call_cost"},
                )
            )
        analysis = mapping(payload.get("platform_analysis"))
        analysed = present(
            {
                ANALYSIS_SUMMARY: bounded_text(analysis.get("summary"), maximum=64 * 1024),
                ANALYSIS_CLASSIFICATION: bounded_text(analysis.get("classification"), maximum=128),
            }
        )
        if analysed:
            spans.append(EvidenceSpan(ANALYSIS, 0, start, end, analysed))
        url = https_url(payload.get("recording_url"))
        recordings = (
            ()
            if url is None
            else (
                RecordingSource(
                    track="mixed",
                    speaker="mixed",
                    content_type=content_type_for(url, "audio/mpeg"),
                    started_at_unix_nano=start,
                    url=url,
                ),
            )
        )
        return Evidence(self.provider, call_id, tuple(spans), recordings)


def _lines(transcript: object, call_start: int) -> list[EvidenceSpan]:
    lines = []
    for ordinal, raw in enumerate(items(transcript)):
        turn = mapping(raw)
        for field, speaker in _SPEAKERS:
            if field in turn:
                line = transcript_line(
                    ordinal,
                    speaker=speaker,
                    text=turn[field],
                    start=call_start,
                    end=call_start,
                    timing="order_only",
                )
                if line is not None:
                    lines.append(line)
                break
    return lines
