"""What a call's stored spans say about its cost and its timing, per framework (pure).

The call projection reads two facts from the spans a revision was built from, which a
bundle does not carry in this form: the usage each request billed, and each turn's
anchors on the span clock. Both are read by attribute and span name, never by
framework version, from spans as the receiver stores them (``call_capture``); every
attribute used is metadata, so both work under the ``metadata_only`` preset.

**Usage → meter** (``usage_items``). Only the spans in ``USAGE_SPANS`` bill:

| Framework | Span | Operation | Meters |
|---|---|---|---|
| Pipecat | ``llm`` | llm | ``input_token``, ``cached_input_token``, ``output_token`` |
| Pipecat | ``tts`` | tts | ``character`` |
| Pipecat | ``stt`` | stt | ``audio_second`` (never reported) |
| LiveKit | ``llm_request`` | llm | as Pipecat ``llm`` |
| LiveKit | ``tts_request`` | tts | ``character``; ``input_token``/``output_token`` if reported |
| LiveKit | ``user_turn`` | stt | ``audio_second`` (never reported) |

LiveKit's ``llm_node``, ``agent_turn``, ``tts_node``, ``*_run`` and
``llm_fallback_adapter`` spans repeat a request's usage and never bill. Every
``llm_request`` bills, a discarded preemptive generation included: it was billed
though never spoken. The values, the first source present winning:

- input tokens: ``gen_ai.usage.input_tokens``, else ``lk.llm_metrics`` ``prompt_tokens``;
- cached input tokens: ``gen_ai.usage.cache_read.input_tokens``, else
  ``gen_ai.usage.input_cached_tokens`` (LiveKit 1.8 writes both spellings), else
  ``lk.llm_metrics`` ``prompt_cached_tokens``;
- output tokens: ``gen_ai.usage.output_tokens``, else ``lk.llm_metrics``
  ``completion_tokens``. They include reasoning tokens, which are never added
  (``gen_ai.usage.reasoning*`` is ignored);
- characters: ``metrics.character_count`` (Pipecat), else ``lk.tts_metrics``
  ``characters_count``; TTS tokens: ``lk.tts_metrics`` ``input_tokens``/``output_tokens``.

**Subtraction.** Input tokens include cached ones (OpenTelemetry GenAI), so
``input_token`` = input - cached and ``cached_input_token`` = cached. Cached tokens
above the input leave ``input_token`` unknown. Cache writes
(``gen_ai.usage.cache_write.input_tokens``) have no price unit and stay inside
``input_token``.

**Zero is unknown.** LiveKit writes 0 for a count it did not get, so a count of 0 (or
absent, or not a non-negative integer) is never a quantity: a request's
``input_token``, ``output_token``, ``character`` and ``audio_second`` items are always
emitted, with ``quantity`` ``None`` (not reported) when unknown, and the other meters
only when reported. STT seconds are on no span (Pipecat 1.5, LiveKit 1.6 to 1.8): each
STT span gives one ``audio_second`` item, not reported, naming the provider and model
whose audio a projection estimates once per call from the recording or call duration.

**Provider and model.** ``gen_ai.provider.name``, else the metric JSON's
``metadata.model_provider``, normalized by ``provider_name`` (LiveKit's host-and-name
normalizer, ported from livekit-agents 1.8 ``telemetry/trace_types.py``, Apache-2.0:
``api.groq.com``, ``Groq`` and ``groq`` are all ``groq``); a provider outside the
GenAI registry is lower-cased. The model is ``gen_ai.request.model``, else
``gen_ai.response.model``, else the metric JSON's ``metadata.model_name``.

**Turn anchors** (``turn_anchors``), on the span clock, one per framework turn:

- **Pipecat** (a ``turn`` span): user on = each child ``stt`` span's start; agent on =
  the first child ``tts`` span's start plus its ``metrics.ttfb``; user off = agent on
  minus ``turn.user_bot_latency_seconds``, the turn's reported latency.
- **LiveKit** (an ``agent_turn`` span): agent on = its first ``agent_speaking``
  child's start; user off = the end of the last ``user_speaking`` span ending by then
  (by the ``agent_turn``'s end when the agent played nothing), and user on = the
  starts of that ``user_speaking`` span's ``user_turn``'s speaking spans up to it; the
  reported latency is ``lk.e2e_latency``, which on the real 1.8 capture equals agent
  on minus user off to the millisecond. The ``user_turn`` end is the commit point, not
  an audio anchor, and an ``agent_turn`` starts at its first (possibly preemptive)
  generation, so its transcript line (whose span ID is ``turn_span_id``) is placed
  at its agent on.

A missing anchor is ``None`` (an empty tuple for user on), never 0.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .call_capture import LIVEKIT_SCOPE, NOT_GIVEN, OtlpSpan, any_value

FRAMEWORK_PIPECAT = "pipecat"
FRAMEWORK_LIVEKIT = "livekit"
_PIPECAT_SCOPES = ("pipecat", "pipecat.turn")

# Meters, as the price table's units name them.
INPUT_TOKEN = "input_token"
CACHED_INPUT_TOKEN = "cached_input_token"
OUTPUT_TOKEN = "output_token"
CHARACTER = "character"
AUDIO_SECOND = "audio_second"

# The spans that bill, by (instrumentation scope, span name): (framework, operation).
USAGE_SPANS: dict[tuple[str, str], tuple[str, str]] = {
    **{(scope, "llm"): (FRAMEWORK_PIPECAT, "llm") for scope in _PIPECAT_SCOPES},
    **{(scope, "tts"): (FRAMEWORK_PIPECAT, "tts") for scope in _PIPECAT_SCOPES},
    **{(scope, "stt"): (FRAMEWORK_PIPECAT, "stt") for scope in _PIPECAT_SCOPES},
    (LIVEKIT_SCOPE, "llm_request"): (FRAMEWORK_LIVEKIT, "llm"),
    (LIVEKIT_SCOPE, "tts_request"): (FRAMEWORK_LIVEKIT, "tts"),
    (LIVEKIT_SCOPE, "user_turn"): (FRAMEWORK_LIVEKIT, "stt"),
}
# The JSON attribute each operation's LiveKit metrics travel in.
_METRICS_ATTRIBUTE = {"llm": "lk.llm_metrics", "tts": "lk.tts_metrics"}
# Sources in order: a span attribute, or a field of the operation's metric JSON.
_INPUT_TOKENS = ("gen_ai.usage.input_tokens", ("prompt_tokens",))
_CACHED_TOKENS = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.input_cached_tokens",
    ("prompt_cached_tokens",),
)
_OUTPUT_TOKENS = ("gen_ai.usage.output_tokens", ("completion_tokens",))
_CHARACTERS = ("metrics.character_count", ("characters_count",))
_TTS_INPUT_TOKENS = (("input_tokens",),)
_TTS_OUTPUT_TOKENS = (("output_tokens",),)
_PROVIDER = ("gen_ai.provider.name", ("metadata", "model_provider"))
_MODEL = ("gen_ai.request.model", "gen_ai.response.model", ("metadata", "model_name"))
# The span attributes usage is read from (the metric JSON attribute is added per span).
_USAGE_KEYS = frozenset(
    source
    for sources in (_INPUT_TOKENS, _CACHED_TOKENS, _OUTPUT_TOKENS, _CHARACTERS, _PROVIDER, _MODEL)
    for source in sources
    if isinstance(source, str)
)

_PIPECAT_ANCHOR_KEYS = ("metrics.ttfb", "turn.user_bot_latency_seconds")
_LIVEKIT_ANCHOR_KEYS = ("lk.e2e_latency",)


@dataclass(frozen=True, slots=True)
class UsageItem:
    """What one billed request reported for one meter (``quantity`` ``None``: not reported)."""

    framework: str
    operation: str  # "llm", "tts" or "stt"
    provider: str | None
    model: str | None
    meter: str
    quantity: int | None
    trace_id: str
    span_id: str


@dataclass(frozen=True, slots=True)
class TurnAnchors:
    """One framework turn's anchors on the span clock (Unix nanoseconds)."""

    framework: str
    trace_id: str
    # The Pipecat ``turn`` span, or the LiveKit ``agent_turn`` span (whose agent
    # transcript line has this span ID).
    turn_span_id: str
    turn_start_unix_nano: int
    user_on_unix_nano: tuple[int, ...]
    user_off_unix_nano: int | None
    agent_on_unix_nano: int | None
    # The framework's own latency for the turn.
    reported_latency_ms: float | None


def _order(span: OtlpSpan) -> tuple[int, bytes, bytes]:
    return (span.span.start_time_unix_nano, span.span.trace_id, span.span.span_id)


def _values(span: OtlpSpan, keys: Iterable[str]) -> dict[str, Any]:
    """The listed attributes (first occurrence, ``NOT_GIVEN`` absent), converting only
    those: an LLM span also carries the whole conversation."""

    wanted = set(keys)
    found: dict[str, Any] = {}
    for item in span.span.attributes:
        if item.key in wanted and item.key not in found:
            value = any_value(item.value)
            if value is not None and value != NOT_GIVEN:
                found[item.key] = value
    return found


def _metrics(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _first(attributes: Mapping[str, Any], metrics: Mapping[str, Any], sources: tuple) -> Any:
    for source in sources:
        if isinstance(source, str):
            value = attributes.get(source)
        else:
            value: Any = metrics
            for part in source:
                value = value.get(part) if isinstance(value, Mapping) else None
        if value is not None:
            return value
    return None


def _count(attributes: Mapping[str, Any], metrics: Mapping[str, Any], sources: tuple) -> int | None:
    """The first source's positive integer count; 0 and anything else are unknown, and
    an unknown source never hides a later one."""

    for source in sources:
        value = _first(attributes, metrics, (source,))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def usage_items(spans: Iterable[OtlpSpan]) -> list[UsageItem]:
    """Every billed request's usage, by meter, in span start order (``USAGE_SPANS``)."""

    items: list[UsageItem] = []
    for span in sorted(spans, key=_order):
        billed = USAGE_SPANS.get((span.scope.name, span.span.name))
        if billed is None:
            continue
        framework, operation = billed
        metrics_key = _METRICS_ATTRIBUTE.get(operation)
        attributes = _values(span, _USAGE_KEYS | ({metrics_key} if metrics_key else set()))
        metrics = _metrics(attributes.get(metrics_key)) if metrics_key else {}
        provider = _first(attributes, metrics, _PROVIDER)
        model = _first(attributes, metrics, _MODEL)
        request = {
            "framework": framework,
            "operation": operation,
            "provider": provider_name(provider if isinstance(provider, str) else None),
            "model": model if isinstance(model, str) and model.strip() else None,
            "trace_id": span.span.trace_id.hex(),
            "span_id": span.span.span_id.hex(),
        }
        items.extend(
            UsageItem(**request, meter=meter, quantity=quantity)
            for meter, quantity in _meters(operation, attributes, metrics)
        )
    return items


def _meters(
    operation: str, attributes: Mapping[str, Any], metrics: Mapping[str, Any]
) -> list[tuple[str, int | None]]:
    """One request's ``(meter, quantity)`` pairs, by the rules above."""

    if operation == "llm":
        given = _count(attributes, metrics, _INPUT_TOKENS)
        cached = _count(attributes, metrics, _CACHED_TOKENS)
        uncached = given
        if given is not None and cached is not None:
            uncached = given - cached if cached <= given else None
        meters = [(INPUT_TOKEN, uncached)]
        if cached is not None:
            meters.append((CACHED_INPUT_TOKEN, cached))
        return [*meters, (OUTPUT_TOKEN, _count(attributes, metrics, _OUTPUT_TOKENS))]
    if operation == "tts":
        meters = [(CHARACTER, _count(attributes, metrics, _CHARACTERS))]
        for meter, sources in (
            (INPUT_TOKEN, _TTS_INPUT_TOKENS),
            (OUTPUT_TOKEN, _TTS_OUTPUT_TOKENS),
        ):
            quantity = _count(attributes, metrics, sources)
            if quantity is not None:
                meters.append((meter, quantity))
        return meters
    return [(AUDIO_SECOND, None)]


# --- provider names (ported from livekit-agents 1.8 telemetry/trace_types.py, Apache-2.0) --

_PROVIDER_BY_HOST = {
    "api.anthropic.com": "anthropic",
    "api.cohere.ai": "cohere",
    "api.cohere.com": "cohere",
    "api.deepseek.com": "deepseek",
    "api.groq.com": "groq",
    "api.mistral.ai": "mistral_ai",
    "api.moonshot.ai": "moonshot_ai",
    "api.moonshot.cn": "moonshot_ai",
    "api.openai.com": "openai",
    "api.perplexity.ai": "perplexity",
    "api.x.ai": "x_ai",
    "generativelanguage.googleapis.com": "gcp.gemini",
}
_PROVIDER_BY_HOST_SUFFIX = (
    (".openai.azure.com", "azure.ai.openai"),
    (".services.ai.azure.com", "azure.ai.inference"),
    (".aiplatform.googleapis.com", "gcp.vertex_ai"),
)
_PROVIDER_BY_NAME = {
    "amazon": "aws.bedrock",
    "amazonbedrock": "aws.bedrock",
    "anthropic": "anthropic",
    "awsbedrock": "aws.bedrock",
    "azureaiinference": "azure.ai.inference",
    "azureopenai": "azure.ai.openai",
    "bedrock": "aws.bedrock",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "gemini": "gcp.gemini",
    "google": "gcp.gen_ai",
    "googlecloudplatform": "gcp.gen_ai",
    "googlegenai": "gcp.gen_ai",
    "groq": "groq",
    "ibmwatsonxai": "ibm.watsonx.ai",
    "mistral": "mistral_ai",
    "mistralai": "mistral_ai",
    "moonshot": "moonshot_ai",
    "moonshotai": "moonshot_ai",
    "openai": "openai",
    "perplexity": "perplexity",
    "vertexai": "gcp.vertex_ai",
    "vertexaimodelgarden": "gcp.vertex_ai",
    "watsonx": "ibm.watsonx.ai",
    "xai": "x_ai",
}


def provider_name(provider: str | None) -> str | None:
    """A provider's OpenTelemetry GenAI registry spelling, by API host or display name;
    any other provider lower-cased (``None`` for blank)."""

    if not provider or not (value := provider.strip()):
        return None
    host = value.lower()
    if (mapped := _PROVIDER_BY_HOST.get(host)) is not None:
        return mapped
    for suffix, mapped in _PROVIDER_BY_HOST_SUFFIX:
        if host.endswith(suffix):
            return mapped
    if host.startswith("bedrock") and host.endswith(".amazonaws.com"):
        return "aws.bedrock"
    return _PROVIDER_BY_NAME.get("".join(c for c in host if c.isalnum()), host)


# --- turn anchors ---------------------------------------------------------------------------


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _nano(seconds: float) -> int:
    return round(seconds * 1_000_000_000)


def _children(spans: Sequence[OtlpSpan]) -> dict[tuple[bytes, bytes], list[OtlpSpan]]:
    by_parent: dict[tuple[bytes, bytes], list[OtlpSpan]] = {}
    for span in spans:
        if span.span.parent_span_id:
            key = (span.span.trace_id, span.span.parent_span_id)
            by_parent.setdefault(key, []).append(span)
    return by_parent


def _named(spans: Iterable[OtlpSpan], scopes: Iterable[str], name: str) -> list[OtlpSpan]:
    wanted = set(scopes)
    return [span for span in spans if span.span.name == name and span.scope.name in wanted]


def _pipecat_anchors(spans: Sequence[OtlpSpan], children: dict) -> list[TurnAnchors]:
    anchors = []
    for turn in _named(spans, _PIPECAT_SCOPES, "turn"):
        kids = children.get((turn.span.trace_id, turn.span.span_id), [])
        user_on = tuple(
            span.span.start_time_unix_nano for span in _named(kids, _PIPECAT_SCOPES, "stt")
        )
        tts = _named(kids, _PIPECAT_SCOPES, "tts")
        agent_on = None
        if tts:
            ttfb = _seconds(_values(tts[0], _PIPECAT_ANCHOR_KEYS).get("metrics.ttfb"))
            if ttfb is not None:
                agent_on = tts[0].span.start_time_unix_nano + _nano(ttfb)
        latency = _seconds(_values(turn, _PIPECAT_ANCHOR_KEYS).get("turn.user_bot_latency_seconds"))
        anchors.append(
            TurnAnchors(
                framework=FRAMEWORK_PIPECAT,
                trace_id=turn.span.trace_id.hex(),
                turn_span_id=turn.span.span_id.hex(),
                turn_start_unix_nano=turn.span.start_time_unix_nano,
                user_on_unix_nano=user_on,
                user_off_unix_nano=(
                    agent_on - _nano(latency)
                    if agent_on is not None and latency is not None
                    else None
                ),
                agent_on_unix_nano=agent_on,
                reported_latency_ms=None if latency is None else latency * 1000,
            )
        )
    return anchors


def _livekit_anchors(spans: Sequence[OtlpSpan], children: dict) -> list[TurnAnchors]:
    speaking = _named(spans, (LIVEKIT_SCOPE,), "user_speaking")
    anchors = []
    for turn in _named(spans, (LIVEKIT_SCOPE,), "agent_turn"):
        kids = children.get((turn.span.trace_id, turn.span.span_id), [])
        played = _named(kids, (LIVEKIT_SCOPE,), "agent_speaking")
        agent_on = played[0].span.start_time_unix_nano if played else None
        by = turn.span.end_time_unix_nano if agent_on is None else agent_on
        before = [span for span in speaking if span.span.end_time_unix_nano <= by]
        user_on: tuple[int, ...] = ()
        user_off = None
        if before:
            last = max(before, key=lambda span: (span.span.end_time_unix_nano, _order(span)))
            same_turn = sorted(
                (
                    span
                    for span in before
                    if (span.span.trace_id, span.span.parent_span_id)
                    == (last.span.trace_id, last.span.parent_span_id)
                ),
                key=_order,
            )
            user_on = tuple(span.span.start_time_unix_nano for span in same_turn)
            user_off = last.span.end_time_unix_nano
        latency = _seconds(_values(turn, _LIVEKIT_ANCHOR_KEYS).get("lk.e2e_latency"))
        anchors.append(
            TurnAnchors(
                framework=FRAMEWORK_LIVEKIT,
                trace_id=turn.span.trace_id.hex(),
                turn_span_id=turn.span.span_id.hex(),
                turn_start_unix_nano=turn.span.start_time_unix_nano,
                user_on_unix_nano=user_on,
                user_off_unix_nano=user_off,
                agent_on_unix_nano=agent_on,
                reported_latency_ms=None if latency is None else latency * 1000,
            )
        )
    return anchors


def turn_anchors(spans: Iterable[OtlpSpan]) -> list[TurnAnchors]:
    """Each framework turn's anchors, in turn start order (Pipecat ``turn`` and LiveKit
    ``agent_turn`` spans)."""

    ordered = sorted(spans, key=_order)
    children = _children(ordered)
    anchors = _pipecat_anchors(ordered, children) + _livekit_anchors(ordered, children)
    return sorted(
        anchors, key=lambda item: (item.turn_start_unix_nano, item.trace_id, item.turn_span_id)
    )
