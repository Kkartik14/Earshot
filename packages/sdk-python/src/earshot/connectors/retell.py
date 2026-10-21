"""Retell: ``call_ended`` and ``call_analyzed`` as call evidence.

Verified on documented payloads only (no Retell account): the webhook overview, the
secure-webhook page and the call object of the API reference.

- **Auth:** ``X-Retell-Signature: v=<ms>,d=<hex>``, HMAC-SHA256 of the raw body then
  the timestamp, with the API key; within 300 s.
- **Consumed:** ``call_ended`` and ``call_analyzed``, either of which ends the call.
  ``call_started``, ``transcript_updated`` and the ``transfer_*`` events are ignored.
- **Shared facts** (both events, byte-identical spans): the call (agent, type,
  status, ``disconnection_reason``; ``start_timestamp``..``end_timestamp``, epoch ms),
  its transcript (``transcript_object``; each utterance's first and last word give
  measured times, seconds on the call's own audio) and its recording
  (``recording_url``, mixed).
- **``call_analyzed`` only** (spans of their own, so a later analysis is new
  evidence, never a conflict): ``call_analysis`` (summary, sentiment, success,
  voicemail), ``call_cost.combined_cost`` (cents), and ``latency``: each
  ``e2e.values`` item is a turn's reported latency, and each component's values a
  series (milliseconds).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..versions import RETELL_NORMALIZER_VERSION
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
    ANALYSIS_IN_VOICEMAIL,
    ANALYSIS_SENTIMENT,
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
    LATENCY,
    LATENCY_COMPONENT,
    LATENCY_TURN_MS,
    LATENCY_VALUES_MS,
    TURN,
    TURN_INDEX,
    Evidence,
    EvidenceSpan,
    RecordingSource,
    bounded_text,
    content_type_for,
    https_url,
    number,
    numbers,
)
from .signatures import HmacVerifier, header_values, within_window
from .types import DeliveryTrustError

ADAPTER_VERSION = RETELL_NORMALIZER_VERSION
CALL_ENDED = "call_ended"
CALL_ANALYZED = "call_analyzed"
SIGNATURE_WINDOW_SECONDS = 300
_SIGNATURE = re.compile(r"v=([0-9]{1,16}),d=([0-9a-fA-F]{64})")
_LATENCY_COMPONENTS = (
    "e2e",
    "asr",
    "llm",
    "llm_websocket_network_rtt",
    "tts",
    "knowledge_base",
    "s2s",
)
_SPEAKERS = {"agent": "agent", "user": "user"}
_MAX_LATENCY_VALUES = 100_000


class RetellConnectorAdapter(ProviderAdapter):
    provider = "retell"
    translator_version = ADAPTER_VERSION
    consumed_events = frozenset({CALL_ENDED, CALL_ANALYZED})

    def verifier(self, headers, secrets, *, now):
        values = header_values(headers, b"x-retell-signature")
        if len(values) != 1:
            raise DeliveryTrustError
        try:
            match = _SIGNATURE.fullmatch(values[0].decode("ascii"))
        except UnicodeDecodeError:
            raise DeliveryTrustError from None
        if match is None:
            raise DeliveryTrustError
        timestamp, supplied = match.groups()
        within_window(int(timestamp) / 1_000, now, SIGNATURE_WINDOW_SECONDS)
        return HmacVerifier(secrets, (supplied,), suffix=timestamp.encode("ascii"))

    def event_type(self, payload: Mapping[str, Any]) -> str:
        return event_name(payload.get("event"))

    def translate(self, event_type: str, payload: Mapping[str, Any]) -> Evidence:
        call = required_mapping(payload.get("call"))
        call_id = provider_id(call.get("call_id"))
        start_ms, end_ms = (
            whole_number(call.get("start_timestamp")),
            whole_number(call.get("end_timestamp")),
        )
        start, end = call_window(
            None if start_ms is None else start_ms * 1_000_000,
            None if end_ms is None else end_ms * 1_000_000,
        )
        status = bounded_text(call.get("call_status"))
        reason = bounded_text(call.get("disconnection_reason"))
        spans = [
            EvidenceSpan(
                CALL,
                0,
                start,
                end,
                present(
                    {
                        AGENT_ID: bounded_text(call.get("agent_id")),
                        AGENT_NAME: bounded_text(call.get("agent_name")),
                        CALL_TYPE: bounded_text(call.get("call_type")),
                        CALL_STATUS: status,
                        CALL_ENDED_REASON: reason,
                    }
                ),
                error=status == "error" or (reason is not None and reason.startswith("error")),
            ),
            *_lines(call.get("transcript_object"), start),
        ]
        if event_type == CALL_ANALYZED:
            spans.extend(_analysis(call, start, end))
        url = https_url(call.get("recording_url"))
        recordings = (
            ()
            if url is None
            else (
                RecordingSource(
                    track="mixed",
                    speaker="mixed",
                    content_type=content_type_for(url, "audio/wav"),
                    started_at_unix_nano=start,
                    url=url,
                ),
            )
        )
        return Evidence(
            provider=self.provider,
            provider_call_id=call_id,
            spans=tuple(spans),
            recording_sources=recordings,
        )


def _lines(transcript: object, call_start: int) -> list[EvidenceSpan]:
    lines = []
    for ordinal, raw in enumerate(items(transcript)):
        utterance = mapping(raw)
        speaker = _SPEAKERS.get(utterance.get("role"))  # type: ignore[arg-type]
        if speaker is None:
            continue
        words = [mapping(word) for word in items(utterance.get("words"))]
        first = number(words[0].get("start")) if words else None
        last = number(words[-1].get("end")) if words else None
        if first is not None and last is not None and first <= last:
            timing = "measured"
            start = call_start + int(first * 1_000_000_000)
            end = call_start + int(last * 1_000_000_000)
        else:
            timing, start, end = "order_only", call_start, call_start
        line = transcript_line(
            ordinal,
            speaker=speaker,
            text=utterance.get("content"),
            start=start,
            end=end,
            timing=timing,
        )
        if line is not None:
            lines.append(line)
    return lines


def _analysis(call: Mapping[str, Any], start: int, end: int) -> list[EvidenceSpan]:
    spans = []
    analysis = mapping(call.get("call_analysis"))
    successful = analysis.get("call_successful")
    in_voicemail = analysis.get("in_voicemail")
    analysed = present(
        {
            ANALYSIS_SUMMARY: bounded_text(analysis.get("call_summary"), maximum=64 * 1024),
            ANALYSIS_SENTIMENT: bounded_text(analysis.get("user_sentiment"), maximum=64),
            ANALYSIS_SUCCESSFUL: (
                str(successful).lower() if isinstance(successful, bool) else None
            ),
            ANALYSIS_IN_VOICEMAIL: in_voicemail if isinstance(in_voicemail, bool) else None,
        }
    )
    if analysed:
        spans.append(EvidenceSpan(ANALYSIS, 0, start, end, analysed))
    cost = number(mapping(call.get("call_cost")).get("combined_cost"))
    if cost is not None:
        spans.append(
            EvidenceSpan(
                COST,
                0,
                start,
                end,
                {
                    COST_AMOUNT: cost,
                    COST_UNIT: "usd_cent",
                    COST_SOURCE: "call.call_cost.combined_cost",
                },
            )
        )
    latency = mapping(call.get("latency"))
    for position, component in enumerate(_LATENCY_COMPONENTS):
        values = numbers(
            items(mapping(latency.get(component)).get("values"), maximum=_MAX_LATENCY_VALUES)
        )
        if not values:
            continue
        spans.append(
            EvidenceSpan(
                LATENCY,
                position,
                start,
                end,
                {LATENCY_COMPONENT: component, LATENCY_VALUES_MS: values},
            )
        )
        if component == "e2e":
            spans.extend(
                EvidenceSpan(TURN, index, start, end, {TURN_INDEX: index, LATENCY_TURN_MS: value})
                for index, value in enumerate(values)
            )
    return spans
