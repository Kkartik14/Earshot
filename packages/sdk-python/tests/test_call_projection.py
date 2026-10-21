"""The call projection on the two real captures (AC3.3-AC3.8, AC4.3; M1, M3, M6, M7, M21,
M22, M28).

The spans are the real Pipecat 1.5 and LiveKit 1.8 OTLP captures as the receiver
stores them; the speech runs are the analysis job's own output
(``earshot.signals.analyze``, the vendored Silero model) on the captures' real
audio; the prices are the built-in defaults. ``goldens.md`` (WS2) pins the runs; the
values below are its per-turn tables, asserted as M28 binds: each gap within ±20 ms
of its golden value and each reported ± within ±5 ms of the golden sigma.
"""

from __future__ import annotations

import functools
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("soundfile")

from captured_livekit_otlp import livekit_manifest, livekit_spans
from captured_otlp import captured_manifest, captured_requests
from earshot.call_capture import spans_from_request
from earshot.call_cost import PriceRow
from earshot.call_projection import (
    CallSource,
    RecordingSource,
    compute_projection,
    reprice,
)
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.signals import analyze
from otlp_support import make_request, make_span, string_attribute

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
LIVEKIT_AUDIO = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"
PRICES = tuple(
    PriceRow(
        id=index,
        project_id=None,
        provider=price.provider,
        model=price.model,
        unit=price.unit,
        price_nano_usd=price.price_nano_usd,
        per_quantity=price.per_quantity,
        effective_from_unix_nano=price.effective_from_unix_nano,
    )
    for index, price in enumerate(BUILTIN_PRICES, start=1)
)


@functools.cache
def _analysis(path: Path, speakers: tuple[str, ...]) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        return analyze(path, speakers=list(speakers), peaks_path=Path(directory) / "p.dat")


def _recording(recording_id: str, speakers: tuple[str, ...], path: Path, **fields):
    mapped = len(speakers) > 1
    return RecordingSource(
        recording_id=recording_id,
        speaker="mixed" if mapped else speakers[0],
        channel_speakers=speakers if mapped else None,
        content_type=fields.pop("content_type", "audio/wav"),
        started_at_unix_nano=fields.pop("started_at_unix_nano"),
        duration_ms=fields.pop("duration_ms"),
        analysis_state=fields.pop("analysis_state", "stored"),
        analysis_error_code=None,
        result=fields.pop("result", None) or _analysis(path, speakers),
        has_peaks=True,
        holds_rendition=fields.pop("holds_rendition", False),
    )


def _pipecat_spans():
    return tuple(span for item in captured_requests() for span in spans_from_request(item))


def _pipecat_recordings(**overrides):
    started = int(captured_manifest()["audio"]["started_at_unix_nano"])
    common = {"started_at_unix_nano": started, "duration_ms": 32_486, **overrides}
    return (
        _recording(
            "conv.user", ("user",), PIPECAT_AUDIO / "user.wav", holds_rendition=True, **common
        ),
        _recording("conv.agent", ("agent",), PIPECAT_AUDIO / "agent.wav", **common),
    )


def _source(spans, recordings, *, ended: bool = True) -> CallSource:
    return CallSource(
        project_id="default",
        call_id="call_fixture",
        revision=1,
        first_evidence_unix_nano=min(span.span.start_time_unix_nano for span in spans),
        ended=ended,
        spans=tuple(spans),
        recordings=tuple(recordings),
    )


def _pipecat():
    return compute_projection(_source(_pipecat_spans(), _pipecat_recordings()), PRICES)


def _livekit():
    audio = livekit_manifest()["audio"]
    recording = _recording(
        "lk.stereo",
        ("user", "agent"),
        LIVEKIT_AUDIO,
        content_type="audio/ogg",
        started_at_unix_nano=int(audio["started_at_unix_nano"]),
        duration_ms=34_004,
    )
    return compute_projection(_source(livekit_spans(), [recording]), PRICES)


def _gaps(projection) -> list[dict]:
    return [turn["audible_gap"] for turn in projection.detail["turns"]]


def _within(actual: int | None, expected: int, tolerance: int) -> bool:
    return actual is not None and abs(actual - expected) <= tolerance


# --- the audible gap at the agent (M1, M3, M6, M28) ------------------------------------------


def test_pipecat_turn_1_is_a_discontinuity_bound_and_turns_2_and_3_are_measured() -> None:
    projection = _pipecat()
    first, second, third = _gaps(projection)

    assert (first["status"], first["reason"], first["value_ms"]) == (
        "not_measurable",
        "recorder_timeline_discontinuity",
        None,
    )
    assert _within(first["bound_ms"], 3080, 20), first
    for gap, value, sigma in ((second, 1510, 75), (third, 1490, 48)):
        assert (gap["status"], gap["reason"], gap["basis"]) == ("measured", None, "anchors_checked")
        assert _within(gap["value_ms"], value, 20), gap
        assert _within(gap["uncertainty_ms"], sigma, 5), gap
    columns = projection.columns
    assert (columns["measurable_turns"], columns["turn_count"]) == (2, 3)
    assert (columns["audible_gap_p50_ms"], columns["audible_gap_max_ms"]) == (1490, 1510)
    assert [turn["reported_latency_ms"] for turn in projection.detail["turns"]] == [
        1800,
        1352,
        1334,
    ]
    assert (columns["reported_latency_p50_ms"], columns["reported_latency_max_ms"]) == (1352, 1800)


def test_livekit_turns_are_measured_on_the_continuous_timeline_assumed() -> None:
    gaps = _gaps(_livekit())

    expected = ((1290, 55), (1660, 52), (1250, 45))
    assert len(gaps) == 3
    for gap, (value, sigma) in zip(gaps, expected, strict=True):
        assert (gap["status"], gap["basis"]) == ("measured", "continuous_timeline_assumed")
        assert _within(gap["value_ms"], value, 20), gap
        assert _within(gap["uncertainty_ms"], sigma, 5), gap


# --- transcript placement (AC3.6, M3, M6) ----------------------------------------------------


@pytest.mark.parametrize("project", [_pipecat, _livekit], ids=["pipecat", "livekit"])
def test_every_placed_line_starts_within_300_ms_of_a_speech_run_of_its_speaker(project) -> None:
    projection = project()
    [media] = projection.detail["media"]
    recording_ids = media["recording_ids"]
    runs = {}
    for speaker in ("user", "agent"):
        path = LIVEKIT_AUDIO if len(recording_ids) == 1 else PIPECAT_AUDIO / f"{speaker}.wav"
        speakers = ("user", "agent") if len(recording_ids) == 1 else (speaker,)
        channel = next(c for c in _analysis(path, speakers)["channels"] if c["speaker"] == speaker)
        runs[speaker] = [run["start_ms"] + media["offset_ms"] for run in channel["runs"]]
    transcript = projection.detail["transcript"]

    assert [line["speaker"] for line in transcript].count("agent") == 3
    for line in transcript:
        assert line["timing"] == "measured"
        nearest = min(abs(line["start_ms"] - start) for start in runs[line["speaker"]])
        assert nearest <= 300, line
        assert line["end_ms"] > line["start_ms"], line
    assert [row.text for row in projection.transcript] == [line["text"] for line in transcript]


def test_a_line_of_a_discontinuous_turn_is_alignment_uncertain() -> None:
    transcript = _pipecat().detail["transcript"]

    assert [line["alignment_uncertain"] for line in transcript[:2]] == [True, True]
    assert not any(line["alignment_uncertain"] for line in transcript[2:])


# --- cost (AC4.3, M3, M7) ----------------------------------------------------------------------


def _items(projection) -> list[tuple]:
    return [
        (item["status"], item["operation"], item["provider"], item["unit"], item["quantity"])
        for item in projection.detail["cost_items"]
    ]


def test_pipecat_cost_items_are_exact() -> None:
    projection = _pipecat()

    assert _items(projection) == [
        ("priced", "llm", "groq", "input_token", 495),
        ("priced", "llm", "groq", "output_token", 319),
        ("priced", "tts", "cartesia", "character", 118),
        ("estimated", "stt", "deepgram", "audio_second", 32.486),
        ("not_reported", "llm", "groq", None, None),
        ("not_reported", "llm", "groq", None, None),
    ]
    # 495 x 75 + 319 x 300 + 118 x 38,000 nano-USD; 32.486 s at 7,700,000 per 60 s.
    assert projection.columns["cost_lower_bound_nano_usd"] == 37_125 + 95_700 + 4_484_000
    assert projection.columns["cost_estimated_nano_usd"] == 4_169_037
    assert projection.columns["cost_unknown_items"] == 2


def test_livekit_cost_counts_every_llm_request_and_estimates_stt_from_the_recording() -> None:
    projection = _livekit()

    assert _items(projection) == [
        ("priced", "llm", "groq", "input_token", 119 + 153 + 162 + 205 + 212),
        ("priced", "llm", "groq", "output_token", 38 + 38 + 39 + 38 + 32),
        ("priced", "tts", "cartesia", "character", 84 + 91 + 59),
        ("estimated", "stt", "deepgram", "audio_second", 34.004),
    ]
    assert projection.columns["cost_unknown_items"] == 0


def test_repricing_from_the_stored_usage_equals_a_full_projection_at_the_new_prices() -> None:
    projection = _pipecat()
    cheaper = [
        replace(row, id=row.id + 1000, project_id="default", price_nano_usd=1)
        if row.provider == "groq"
        else row
        for row in PRICES
    ]

    columns, detail = reprice(
        projection.usage,
        projection.detail,
        cheaper,
        project_id="default",
        at_unix_nano=min(span.span.start_time_unix_nano for span in _pipecat_spans()),
    )
    full = compute_projection(_source(_pipecat_spans(), _pipecat_recordings()), cheaper)

    assert detail == full.detail
    assert columns["cost_lower_bound_nano_usd"] == full.columns["cost_lower_bound_nano_usd"]
    assert columns["cost_lower_bound_nano_usd"] == 495 + 319 + 4_484_000


# --- signals (AC3.5, M22) ------------------------------------------------------------------------


def test_pipecat_signals_follow_the_research_definitions() -> None:
    projection = _pipecat()

    assert projection.detail["signals"] == {
        "one_timeline": True,
        "talk_time_user_ms": 8140,
        "talk_time_agent_ms": 8020,
        "overlap_ms": 0,
        "barge_in_count": 0,
        "talk_over_count": 0,
        "long_silence_count": 1,
        "dead_air_ms": 3080,
    }
    assert projection.columns["analysis_state"] == "stored"


def test_media_is_the_mono_pairs_rendition() -> None:
    [media] = _pipecat().detail["media"]

    assert media == {
        "source": "rendition",
        "recording_ids": ["conv.user", "conv.agent"],
        "content_path": "/v1/recordings/conv.user/rendition",
        "content_type": "audio/flac",
        "peaks_path": "/v1/recordings/conv.user/rendition/peaks",
        "offset_ms": 0,
        "duration_ms": 32_486,
        "channels": [{"index": 0, "speaker": "user"}, {"index": 1, "speaker": "agent"}],
        "analysis_state": "stored",
        "analysis_error_code": None,
    }


# --- not measurable, never fabricated (M8) -----------------------------------------------------


def test_without_recordings_no_turn_is_measured_and_cross_channel_signals_are_null() -> None:
    projection = compute_projection(_source(_pipecat_spans(), []), PRICES)

    assert {(gap["status"], gap["reason"]) for gap in _gaps(projection)} == {
        ("not_measurable", "no_recording")
    }
    assert all(gap["value_ms"] is None and gap["bound_ms"] is None for gap in _gaps(projection))
    assert projection.detail["signals"] == {
        "one_timeline": None,
        **dict.fromkeys(
            (
                "talk_time_user_ms",
                "talk_time_agent_ms",
                "overlap_ms",
                "barge_in_count",
                "talk_over_count",
                "long_silence_count",
                "dead_air_ms",
            )
        ),
    }
    assert projection.columns["audible_gap_p50_ms"] is None
    assert projection.columns["analysis_state"] == "none"
    assert projection.detail["axis_origin"] == "first_evidence"
    # The STT estimate falls back to the call's own duration.
    [stt] = [item for item in projection.detail["cost_items"] if item["operation"] == "stt"]
    assert stt["status"] == "estimated"
    assert stt["quantity"] == projection.columns["duration_ms"] / 1000


def test_a_pending_analysis_is_analysis_not_stored() -> None:
    recordings = _pipecat_recordings(analysis_state="pending", result={})
    projection = compute_projection(_source(_pipecat_spans(), recordings), PRICES)

    assert {gap["reason"] for gap in _gaps(projection)} == {"analysis_not_stored"}
    assert projection.columns["analysis_state"] == "pending"
    assert projection.detail["signals"]["one_timeline"] is None


# --- agent, outcome and tags (AC3.3, M21) ------------------------------------------------------


def _spans(*attribute_sets, resource=("service.name", "unknown_service:python"), status=0):
    request = make_request(
        [
            make_span(1, index, attributes=[string_attribute(key, value) for key, value in pairs])
            for index, pairs in enumerate(attribute_sets, start=1)
        ],
        resource=[string_attribute(*resource)],
    )
    spans = spans_from_request(request)
    if status:
        spans[0].span.status.code = status
    return spans


def test_agent_follows_its_precedence_and_ignores_the_sdk_default_service_name() -> None:
    def agent(*sets, **kwargs):
        return compute_projection(_source(_spans(*sets, **kwargs), []), PRICES).columns["agent"]

    all_keys = (
        [("lk.agent_name", "lk")],
        [("gen_ai.agent.name", "genai")],
        [("earshot.agent.name", "earshot")],
    )
    assert agent(*all_keys) == "earshot"
    assert agent(*all_keys[:2]) == "genai"
    assert agent(all_keys[0]) == "lk"
    assert agent([]) is None
    assert agent([], resource=("service.name", "booking-bot")) == "booking-bot"


def test_outcome_precedence_is_explicit_then_error_then_interrupted_then_completed() -> None:
    def outcome(spans, *, ended=True):
        return compute_projection(_source(spans, [], ended=ended), PRICES).columns["outcome"]

    explicit = _spans([("earshot.call.outcome", "transferred")], status=2)
    assert outcome(explicit, ended=False) == "transferred"
    assert outcome(_spans([], status=2)) == "error"
    assert outcome(_spans([]), ended=False) == "interrupted"
    assert outcome(_spans([])) == "completed"


def test_tags_are_the_union_of_every_spans_tags() -> None:
    spans = _spans([("earshot.call.tags", "vip")], [("earshot.call.tags", "refund")])
    spans[0].span.attributes.add(key="earshot.call.tags").value.array_value.values.add(
        string_value="vip"
    )

    assert compute_projection(_source(spans, []), PRICES).tags == ("refund", "vip")
