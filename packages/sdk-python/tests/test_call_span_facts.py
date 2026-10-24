"""Usage and the per-framework anchors: what a call's stored spans say for cost and timing.

The usage→meter table (``call_span_facts.USAGE_SPANS`` and ``usage_items``) and the
turn anchors (``turn_anchors``) are pinned here on both real captures: Pipecat 1.5
(``fixtures/captured/pipecat-otlp-1.5``) and LiveKit Agents 1.8.8
(``fixtures/captured/livekit-otlp-1.8``), as the receiver stores them.
"""

from __future__ import annotations

import math

import pytest

from captured_livekit_otlp import livekit_manifest, livekit_spans
from captured_otlp import captured_requests
from earshot.call_capture import (
    apply_capture_preset,
    resource_payload,
    span_payload,
    spans_from_request,
    stored_span,
)
from earshot.call_span_facts import UsageItem, provider_name, turn_anchors, usage_items

pytestmark = pytest.mark.unit


def _stored(spans, preset: str = "full"):
    kept = [apply_capture_preset(preset, span) for span in spans]
    return [stored_span(span_payload(span.span), resource_payload(span)) for span in kept]


def _pipecat(preset: str = "full"):
    return _stored([s for r in captured_requests() for s in spans_from_request(r)], preset)


def _livekit(preset: str = "full"):
    return _stored(livekit_spans(), preset)


def _quantities(items, operation: str, meter: str) -> list:
    return [item.quantity for item in items if (item.operation, item.meter) == (operation, meter)]


def _nano(seconds: float) -> int:
    return round(seconds * 1_000_000_000)


# --- usage -> meter -------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_pipecat_usage_is_one_item_per_meter_of_every_billed_span(preset) -> None:
    items = usage_items(_pipecat(preset))

    # Five llm spans; the two cancelled ones reported nothing (not_reported, never 0).
    assert _quantities(items, "llm", "input_token") == [119, None, 161, None, 215]
    assert _quantities(items, "llm", "output_token") == [101, None, 116, None, 102]
    assert _quantities(items, "llm", "cached_input_token") == []
    assert _quantities(items, "tts", "character") == [59, 42, 17]
    # STT seconds are on no span: one not_reported item per stt span, for an estimate.
    assert _quantities(items, "stt", "audio_second") == [None] * 5
    assert {(item.operation, item.provider, item.model) for item in items} == {
        ("llm", "groq", "openai/gpt-oss-20b"),
        ("tts", "cartesia", "sonic-3"),
        ("stt", "deepgram", "nova-3-general"),
    }
    assert {item.framework for item in items} == {"pipecat"}


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_livekit_usage_counts_every_llm_request_including_discarded_generations(preset) -> None:
    items = usage_items(_livekit(preset))

    # Five llm_request spans for three turns: two preemptive generations were discarded,
    # but billed. Output tokens include the reasoning tokens (never added twice).
    assert _quantities(items, "llm", "input_token") == [119, 153, 162, 205, 212]
    assert _quantities(items, "llm", "output_token") == [38, 38, 39, 38, 32]
    # Cached tokens of 0 are LiveKit's "unknown": no cached item, nothing subtracted.
    assert _quantities(items, "llm", "cached_input_token") == []
    # TTS usage lives in lk.tts_metrics; its 0 input/output tokens are unknown, not 0.
    assert _quantities(items, "tts", "character") == [84, 91, 59]
    assert _quantities(items, "tts", "input_token") == []
    assert _quantities(items, "stt", "audio_second") == [None] * 3
    assert {(item.operation, item.provider, item.model) for item in items} == {
        ("llm", "groq", "openai/gpt-oss-20b"),
        ("tts", "cartesia", "sonic-3"),
        ("stt", "deepgram", "nova-3"),
    }
    # Only the allowlisted spans bill: never llm_node, agent_turn, tts_node or *_run.
    spans = {span.span.span_id.hex(): span.span.name for span in _livekit()}
    assert {spans[item.span_id] for item in items} == {"llm_request", "tts_request", "user_turn"}


def _span_with(name: str, scope: str, **attributes):
    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    from earshot.call_capture import OtlpSpan
    from otlp_support import string_attribute

    span = Span(trace_id=b"\x01" * 16, span_id=b"\x02" * 8, name=name, start_time_unix_nano=1)
    for key, value in attributes.items():
        key = key.replace("__", ".")
        if isinstance(value, str):
            span.attributes.append(string_attribute(key, value))
        else:
            item = span.attributes.add(key=key)
            item.value.int_value = value
    return OtlpSpan(resource=Resource(), scope=InstrumentationScope(name=scope), span=span)


def test_cached_input_is_subtracted_from_input_and_billed_as_its_own_meter() -> None:
    span = _span_with(
        "llm_request",
        "livekit-agents",
        gen_ai__provider__name="openai",
        gen_ai__request__model="gpt-4o",
        gen_ai__usage__input_tokens=1000,
        gen_ai__usage__input_cached_tokens=400,
        gen_ai__usage__output_tokens=50,
    )

    assert [(item.meter, item.quantity) for item in usage_items([span])] == [
        ("input_token", 600),
        ("cached_input_token", 400),
        ("output_token", 50),
    ]


def test_cache_read_spellings_and_metric_json_fall_back_in_order() -> None:
    span = _span_with(
        "llm",
        "pipecat",
        gen_ai__usage__input_tokens=500,
        gen_ai__usage__cache_read__input_tokens=100,
        gen_ai__usage__input_cached_tokens=999,  # a second spelling never wins over the first
    )
    from_metrics = _span_with(
        "llm_request",
        "livekit-agents",
        lk__llm_metrics='{"prompt_tokens": 70, "completion_tokens": 9, "prompt_cached_tokens": 20,'
        ' "metadata": {"model_name": "llama-3.1-8b-instant", "model_provider": "api.groq.com"}}',
    )

    assert [(item.meter, item.quantity) for item in usage_items([span])] == [
        ("input_token", 400),
        ("cached_input_token", 100),
        ("output_token", None),
    ]
    assert [
        (item.meter, item.quantity, item.provider, item.model)
        for item in usage_items([from_metrics])
    ] == [
        ("input_token", 50, "groq", "llama-3.1-8b-instant"),
        ("cached_input_token", 20, "groq", "llama-3.1-8b-instant"),
        ("output_token", 9, "groq", "llama-3.1-8b-instant"),
    ]


def test_cached_tokens_above_the_input_leave_the_uncached_input_unknown() -> None:
    span = _span_with(
        "llm",
        "pipecat",
        gen_ai__usage__input_tokens=10,
        gen_ai__usage__cache_read__input_tokens=40,
        gen_ai__usage__output_tokens=0,
    )

    assert [(item.meter, item.quantity) for item in usage_items([span])] == [
        ("input_token", None),
        ("cached_input_token", 40),
        ("output_token", None),  # 0 is unknown
    ]


def test_usage_items_are_in_span_order_whatever_the_arrival_order() -> None:
    spans = _livekit()

    assert usage_items(reversed(spans)) == usage_items(spans)
    assert all(isinstance(item, UsageItem) for item in usage_items(spans))


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("groq", "groq"),
        ("Groq", "groq"),
        ("api.groq.com", "groq"),
        ("api.openai.com", "openai"),
        ("my-resource.openai.azure.com", "azure.ai.openai"),
        ("bedrock-runtime.us-east-1.amazonaws.com", "aws.bedrock"),
        ("AWS Bedrock", "aws.bedrock"),
        ("MistralAI", "mistral_ai"),
        ("Google", "gcp.gen_ai"),
        ("Deepgram", "deepgram"),
        ("Cartesia", "cartesia"),
        ("  ", None),
        (None, None),
    ],
)
def test_providers_normalize_to_the_genai_registry_spelling_or_lower_case(raw, normalized) -> None:
    assert provider_name(raw) == normalized


# --- anchors --------------------------------------------------------------------------------


def test_pipecat_anchors_are_stt_starts_and_tts_first_audio_less_the_turn_latency() -> None:
    anchors = turn_anchors(_pipecat())

    expected = [
        # (turn span, stt starts, tts start, tts ttfb, turn.user_bot_latency_seconds)
        ("27195d4734ad5441", [1792372162493948928], 1792372166687571000, 0.12630605697631836,
         1.8003404140472412),
        ("5a226b1a3222d416", [1792372172354711040, 1792372174113479936], 1792372176951151000,
         0.13328886032104492, 1.3520691394805908),
        ("ca9dd4709ed8bb05", [1792372183075557888, 1792372184834028032], 1792372187139897000,
         0.10790205001831055, 1.3341782093048096),
    ]  # fmt: skip
    assert [anchor.turn_span_id for anchor in anchors] == [row[0] for row in expected]
    for anchor, (_, user_on, tts_start, ttfb, latency) in zip(anchors, expected, strict=True):
        agent_on = tts_start + _nano(ttfb)
        assert anchor.framework == "pipecat"
        assert anchor.user_on_unix_nano == tuple(user_on)
        assert anchor.agent_on_unix_nano == agent_on
        assert anchor.user_off_unix_nano == agent_on - _nano(latency)
        assert anchor.reported_latency_ms == pytest.approx(latency * 1000)


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_livekit_anchors_are_the_speaking_spans_and_match_the_reported_latency(preset) -> None:
    started = int(livekit_manifest()["audio"]["started_at_unix_nano"])
    anchors = turn_anchors(_livekit(preset))

    # Seconds from the recording's first sample (the anchor findings of the real capture).
    assert [
        (
            round((anchor.user_on_unix_nano[0] - started) / 1e9, 3),
            round((anchor.user_off_unix_nano - started) / 1e9, 3),
            round((anchor.agent_on_unix_nano - started) / 1e9, 3),
            round(anchor.reported_latency_ms / 1000, 3),
        )
        for anchor in anchors
    ] == [
        (1.339, 4.006, 5.073, 1.067),
        (11.238, 14.726, 15.760, 1.034),
        (21.959, 24.886, 25.933, 1.047),
    ]
    for anchor in anchors:
        assert anchor.framework == "livekit"
        # agent_speaking start is user_speaking end plus lk.e2e_latency, to the ms.
        gap_ms = (anchor.agent_on_unix_nano - anchor.user_off_unix_nano) / 1e6
        assert gap_ms == pytest.approx(anchor.reported_latency_ms, abs=1)


def test_a_livekit_turn_is_its_agent_turn_span_so_its_played_line_joins_its_anchors() -> None:
    from earshot.call_materializer import transcript_lines

    spans = _livekit()
    agent_lines = [line for line in transcript_lines(spans) if line.speaker == "agent"]

    assert [anchor.turn_span_id for anchor in turn_anchors(spans)] == [
        line.span_id for line in agent_lines
    ]


def test_a_turn_without_agent_audio_has_no_agent_anchor_and_no_user_off() -> None:
    spans = [span for span in _livekit() if span.span.name != "agent_speaking"]

    anchors = turn_anchors(spans)

    assert [anchor.agent_on_unix_nano for anchor in anchors] == [None, None, None]
    assert all(anchor.user_off_unix_nano is not None for anchor in anchors)


def _turn_reporting(seconds: float):
    """One Pipecat turn whose latency attribute (and its tts span's time to first
    byte) report ``seconds``, as the receiver stores them."""

    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope, KeyValue
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    from earshot.call_capture import OtlpSpan

    def double(key: str, value: float) -> KeyValue:
        item = KeyValue(key=key)
        item.value.double_value = value
        return item

    trace = b"\x01" * 16
    turn = Span(
        trace_id=trace,
        span_id=b"\x02" * 8,
        name="turn",
        start_time_unix_nano=10**18,
        attributes=[double("turn.user_bot_latency_seconds", seconds)],
    )
    tts = Span(
        trace_id=trace,
        span_id=b"\x03" * 8,
        parent_span_id=b"\x02" * 8,
        name="tts",
        start_time_unix_nano=10**18 + 10**9,
        attributes=[double("metrics.ttfb", seconds)],
    )
    scope = InstrumentationScope(name="pipecat")
    spans = [OtlpSpan(resource=Resource(), scope=scope, span=span) for span in (turn, tts)]
    [anchor] = turn_anchors(spans)
    return anchor


@pytest.mark.parametrize("seconds", [1e300, 9.3e9])
def test_a_reported_time_whose_nanoseconds_overflow_is_unknown(seconds) -> None:
    """A producer-reported duration is converted to nanoseconds for the anchors; one
    that cannot be held as signed 64-bit nanoseconds is unknown, like a negative or
    non-finite one, so it never becomes an anchor or a reported latency."""

    anchor = _turn_reporting(seconds)

    assert (anchor.agent_on_unix_nano, anchor.user_off_unix_nano, anchor.reported_latency_ms) == (
        None,
        None,
        None,
    )


def test_a_reported_time_is_known_exactly_when_its_nanoseconds_fit_signed_64_bits() -> None:
    """The bound is on the converted nanoseconds: the largest duration whose
    nanoseconds are at most 2**63 - 1 is known, the next one up is not."""

    int64_max = 2**63 - 1
    largest = int64_max / 1e9
    while round(largest * 1e9) > int64_max:
        largest = math.nextafter(largest, 0.0)
    smallest_over = math.nextafter(largest, math.inf)
    assert round(largest * 1e9) <= int64_max < round(smallest_over * 1e9)

    assert _turn_reporting(largest).reported_latency_ms == largest * 1000
    assert _turn_reporting(smallest_over).reported_latency_ms is None
