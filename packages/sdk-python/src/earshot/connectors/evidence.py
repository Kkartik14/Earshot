"""A provider delivery's evidence: the provider's facts as spans of the call they describe.

A translator turns one authenticated payload into an ``Evidence``: the provider's
call ID, the facts as ``EvidenceSpan``\\ s in the ``earshot.connector.*`` vocabulary
(registered in ``semconv/earshot.yaml``), the recordings the provider holds, and
whether the delivery ends the call. ``otlp_spans`` makes the spans the call store
records, exactly as it records a framework's OTLP spans, so a connector call gets
revisions, merging, purge and presets the way any call does.

**Identity.** One trace per session key (``H(provider, key)``); a span's ID is
``H(kind, ordinal)``, never the event that carried it. Two deliveries of one call
(Retell ``call_ended`` and ``call_analyzed``) therefore write the same span for the
same fact, and the store keeps it once. A fact only one event carries has a kind of
its own (``ANALYSIS``, ``COST``, ``TURN``, ``LATENCY``), so it is new evidence, never
a conflict. The scope carries no version and the resource only the provider: the
translator's version is on a span of its own (``TRANSLATOR``, keyed by version), so
an upgrade changes no shared span.

**The digest** (``Evidence.digest``) is the sha256 of the canonical facts and the
recording locators (provider, call ID, track): never the raw body, a URL, the
session key or the translator version. A retry whose payload differs only in
re-signed URLs is the same delivery.

**Privacy.** No URL is ever an attribute: recording URLs travel in
``RecordingSource`` to the fetch job only. Free text goes under keys the capture
classifier reads as content (``…transcript…`` is transcript, ``…message…`` model
payload), so ``metadata_only`` stores none of it; the raw call ID is only the session
key, which ``metadata_only`` never stores either.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from earshot.call_capture import OtlpSpan

AttributeValue = str | bool | int | float | tuple[float, ...] | tuple[int, ...]

# --- Span kinds (span names) ------------------------------------------------------------

# The call: its window, status and end. Every other span is its child.
CALL = "earshot.connector.call"
# One transcript line, in transcript order (ordinal = line index).
TRANSCRIPT_LINE = "earshot.connector.transcript_line"
# One turn's provider-reported latency (ordinal = the provider's turn index).
TURN = "earshot.connector.turn"
# A provider latency series not tied to turns (ordinal = the component's position).
LATENCY = "earshot.connector.latency"
# The provider's analysis of the call (summary, success, sentiment).
ANALYSIS = "earshot.connector.analysis"
# What the provider reports the call cost.
COST = "earshot.connector.cost"
# The translator that wrote a delivery's spans (keyed by its version).
TRANSLATOR = "earshot.connector.translator"
SPAN_KINDS = (CALL, TRANSCRIPT_LINE, TURN, LATENCY, ANALYSIS, COST, TRANSLATOR)

# --- Attributes ---------------------------------------------------------------------------

# On the resource: which provider the evidence came from.
PROVIDER = "earshot.connector.provider"
# Call.
CALL_TYPE = "earshot.connector.call.type"
CALL_STATUS = "earshot.connector.call.status"
CALL_ENDED_REASON = "earshot.connector.call.ended_reason"
# Whether the provider exposed per-turn latency for this call (``available`` or
# ``not_exposed``): a call without it still has its transcript and audio.
COVERAGE_TURN_LATENCY = "earshot.connector.coverage.turn_latency"
# Transcript lines. Under keys the classifier reads as transcript content.
TRANSCRIPT_SPEAKER = "earshot.connector.transcript.speaker"
TRANSCRIPT_TEXT = "earshot.connector.transcript.text"
# ``measured`` (start and end), ``start_only`` (the span's end is its start, not a
# measurement) or ``order_only`` (neither time is a measurement; order by index).
TRANSCRIPT_TIMING = "earshot.connector.transcript.timing"
TRANSCRIPT_INDEX = "earshot.connector.transcript.index"
TRANSCRIPT_INTERRUPTED = "earshot.connector.transcript.interrupted"
# Latency, in milliseconds as the provider reported it. ``turn_ms`` is the whole
# turn (the provider's own reported latency); the others are stage splits.
TURN_INDEX = "earshot.connector.turn.index"
LATENCY_TURN_MS = "earshot.connector.latency.turn_ms"
LATENCY_LLM_MS = "earshot.connector.latency.llm_ms"
LATENCY_TTS_MS = "earshot.connector.latency.tts_ms"
LATENCY_STT_MS = "earshot.connector.latency.stt_ms"
LATENCY_ENDPOINTING_MS = "earshot.connector.latency.endpointing_ms"
LATENCY_COMPONENT = "earshot.connector.latency.component"
LATENCY_VALUES_MS = "earshot.connector.latency.values_ms"
# Analysis. The summary is model output (a ``…message…`` key: content).
ANALYSIS_SUMMARY = "earshot.connector.analysis.summary_message"
ANALYSIS_SUCCESSFUL = "earshot.connector.analysis.successful"
ANALYSIS_SENTIMENT = "earshot.connector.analysis.sentiment"
ANALYSIS_IN_VOICEMAIL = "earshot.connector.analysis.in_voicemail"
ANALYSIS_CLASSIFICATION = "earshot.connector.analysis.classification"
# Cost: the amount in ``unit`` (``usd``, ``usd_cent`` or ``credit``), read from
# ``source`` (the payload field). Never added to Earshot's own estimate.
COST_AMOUNT = "earshot.connector.cost.amount"
COST_UNIT = "earshot.connector.cost.unit"
COST_SOURCE = "earshot.connector.cost.source"
TRANSLATOR_VERSION = "earshot.connector.translator.version"

# OpenTelemetry GenAI keys reused as they are defined there.
AGENT_ID = "gen_ai.agent.id"
AGENT_NAME = "gen_ai.agent.name"

TIMINGS = ("measured", "start_only", "order_only")
SPEAKERS = ("user", "agent")
COST_UNITS = ("usd", "usd_cent", "credit")
# A recording's speakers, as the recordings API names them.
RECORDING_SPEAKERS = ("mixed", "user", "agent")

_TRACE_NAMESPACE = b"earshot.connector.trace.v1\x00"
_SPAN_NAMESPACE = b"earshot.connector.span.v1\x00"
_DIGEST_VERSION = 1
_SPAN_KIND_INTERNAL = 1
_STATUS_ERROR = 2


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """One provider fact: a span of ``kind``, unique by ``(kind, ordinal)`` in its call."""

    kind: str
    ordinal: int | str
    start_unix_nano: int
    end_unix_nano: int
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)
    error: bool = False

    def __post_init__(self) -> None:
        if self.kind not in SPAN_KINDS:
            raise ValueError(f"unknown connector span kind {self.kind!r}")
        if not 0 <= self.start_unix_nano <= self.end_unix_nano <= (1 << 63) - 1:
            raise ValueError("a connector span needs 0 <= start <= end < 2^63")


@dataclass(frozen=True, slots=True)
class RecordingSource:
    """A recording the provider holds, and where the fetch job may get it.

    ``url`` is a payload URL (a bearer capability: it never reaches a span, a log or
    an API response); ``provider_api`` says the provider's own API serves it by call
    ID, which a connector with an API key uses instead. The recording's manifest
    (``speaker``, ``content_type``, ``started_at_unix_nano``) is a pure function of
    the payload, so a retry declares the same recording.
    """

    track: str
    speaker: str
    content_type: str
    started_at_unix_nano: int
    url: str | None = field(default=None, repr=False)
    url_expires_unix_nano: int | None = None
    provider_api: bool = False

    def __post_init__(self) -> None:
        if self.speaker not in RECORDING_SPEAKERS:
            raise ValueError(f"unknown recording speaker {self.speaker!r}")
        if not 1 <= self.started_at_unix_nano <= (1 << 63) - 1:
            raise ValueError("a recording starts at a Unix nanosecond time in [1, 2^63 - 1]")


@dataclass(frozen=True, slots=True)
class Evidence:
    """What one delivery says about one provider call."""

    provider: str
    provider_call_id: str
    spans: tuple[EvidenceSpan, ...]
    recording_sources: tuple[RecordingSource, ...] = ()
    # Whether this delivery ends the call (its call span is an end marker).
    terminal: bool = True

    def digest(self) -> str:
        """sha256 of the canonical facts and recording locators (hex)."""

        canonical = {
            "v": _DIGEST_VERSION,
            "provider": self.provider,
            "call": self.provider_call_id,
            "terminal": self.terminal,
            "spans": sorted(
                [
                    span.kind,
                    str(span.ordinal),
                    span.start_unix_nano,
                    span.end_unix_nano,
                    span.error,
                    sorted(
                        [key, _canonical_value(value)] for key, value in span.attributes.items()
                    ),
                ]
                for span in self.spans
            ),
            "recordings": sorted(
                [self.provider, self.provider_call_id, source.track]
                for source in self.recording_sources
            ),
        }
        encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_value(value: AttributeValue) -> object:
    return list(value) if isinstance(value, tuple) else value


def scope_name(provider: str) -> str:
    """The instrumentation scope of a provider's evidence spans."""

    return f"earshot.connector.{provider}"


def trace_id(provider: str, session_key: str) -> bytes:
    """The one trace a provider's evidence for ``session_key`` is recorded under."""

    digest = hashlib.sha256(
        _TRACE_NAMESPACE + provider.encode() + b"\x00" + session_key.encode("utf-8")
    ).digest()
    return digest[:16]


def span_id(kind: str, ordinal: int | str) -> bytes:
    digest = hashlib.sha256(_SPAN_NAMESPACE + f"{kind}\x00{ordinal}".encode()).digest()
    return digest[:8]


def _any_value(value: AttributeValue):
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue

    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, str):
        return AnyValue(string_value=value)
    return AnyValue(array_value=ArrayValue(values=[_any_value(item) for item in value]))


def _attributes(values: Mapping[str, AttributeValue]):
    from opentelemetry.proto.common.v1.common_pb2 import KeyValue

    return [KeyValue(key=key, value=_any_value(values[key])) for key in sorted(values)]


def otlp_spans(evidence: Evidence, *, session_key: str, translator_version: str) -> list[OtlpSpan]:
    """The spans the call store records for ``evidence`` under ``session_key``: the
    evidence's own, each naming the key, plus the translator's span."""

    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

    from earshot.call_capture import EARSHOT_CALL_ENDED, EARSHOT_SESSION_ID, OtlpSpan

    calls = [span for span in evidence.spans if span.kind == CALL]
    if len(calls) != 1:
        raise ValueError("evidence holds exactly one call span")
    [call] = calls
    trace = trace_id(evidence.provider, session_key)
    call_span_id = span_id(CALL, call.ordinal)
    resource = Resource(attributes=_attributes({PROVIDER: evidence.provider}))
    scope = InstrumentationScope(name=scope_name(evidence.provider))
    translator = EvidenceSpan(
        TRANSLATOR,
        translator_version,
        call.start_unix_nano,
        call.end_unix_nano,
        {TRANSLATOR_VERSION: translator_version},
    )
    spans = []
    for item in (*evidence.spans, translator):
        values: dict[str, AttributeValue] = {**item.attributes, EARSHOT_SESSION_ID: session_key}
        if item.kind == CALL and evidence.terminal:
            values[EARSHOT_CALL_ENDED] = True
        message = Span(
            trace_id=trace,
            span_id=span_id(item.kind, item.ordinal),
            parent_span_id=b"" if item.kind == CALL else call_span_id,
            name=item.kind,
            kind=_SPAN_KIND_INTERNAL,
            start_time_unix_nano=item.start_unix_nano,
            end_time_unix_nano=item.end_unix_nano,
            attributes=_attributes(values),
            status=Status(code=_STATUS_ERROR) if item.error else Status(),
        )
        spans.append(OtlpSpan(resource=resource, scope=scope, span=message))
    return spans


def bounded_text(value: object, *, maximum: int = 256) -> str | None:
    """``value`` if it is a non-blank string of at most ``maximum`` characters."""

    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        return None
    return value


def number(value: object) -> float | None:
    """``value`` as a finite, non-negative number (``None`` otherwise; never a bool)."""

    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def numbers(values: Sequence[object]) -> tuple[float, ...] | None:
    """Every item as ``number``; ``None`` when any is not one."""

    converted = tuple(number(value) for value in values)
    return None if any(value is None for value in converted) else converted  # type: ignore[return-value]


_CONTENT_TYPES_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def https_url(value: object) -> str | None:
    """``value`` if it is an absolute https URL of at most 4096 characters."""

    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme == "https" and parts.hostname else None


def content_type_for(url: str | None, default: str) -> str:
    """The audio type a URL's path names by its suffix, else ``default``."""

    from urllib.parse import urlsplit

    if url is None:
        return default
    path = urlsplit(url).path.lower()
    return next(
        (kind for suffix, kind in _CONTENT_TYPES_BY_SUFFIX.items() if path.endswith(suffix)),
        default,
    )
