"""The call projection on the two real captures.

The spans are the real Pipecat 1.5 and LiveKit 1.8 OTLP captures as the receiver
stores them; the speech runs are the analysis job's own output
(``earshot.signals.analyze``, the vendored Silero model) on the captures' real
audio; the prices are the built-in defaults. The golden speech runs are pinned below; the
values are their per-turn tables: each gap within ±20 ms
of its golden value and each reported ± within ±5 ms of the golden sigma.
"""

from __future__ import annotations

import copy
import functools
import multiprocessing
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
from earshot.call_cost import PriceRow, price_usage
from earshot.call_projection import (
    CallSource,
    RecordingSource,
    compute_projection,
    reprice,
)
from earshot.call_span_facts import turn_anchors
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


# What the upload's Ogg Opus header reader gives the LiveKit recording (the last
# granule less the pre-skip, RFC 7845); its decoder yields 34,004 ms.
LIVEKIT_HEADER_DURATION_MS = 34_010


def _livekit():
    audio = livekit_manifest()["audio"]
    recording = _recording(
        "lk.stereo",
        ("user", "agent"),
        LIVEKIT_AUDIO,
        content_type="audio/ogg",
        started_at_unix_nano=int(audio["started_at_unix_nano"]),
        duration_ms=LIVEKIT_HEADER_DURATION_MS,
    )
    return compute_projection(_source(livekit_spans(), [recording]), PRICES)


def _project_cyclic_turn(result_queue, cycle_kind: str) -> None:
    spans = list(_pipecat_spans())
    anchor = turn_anchors(spans)[0]
    root = next(
        span
        for span in spans
        if span.span.trace_id.hex() == anchor.trace_id
        and span.span.span_id.hex() == anchor.turn_span_id
    )
    if cycle_kind == "self":
        root.span.parent_span_id = root.span.span_id
    else:
        child = next(
            span
            for span in spans
            if span.span.trace_id == root.span.trace_id
            and span.span.parent_span_id == root.span.span_id
        )
        root.span.parent_span_id = child.span.span_id
    projection = compute_projection(_source(spans, []), PRICES)
    result_queue.put(
        (
            root.span.span_id.hex(),
            [span["span_id"] for span in projection.detail["turns"][0]["spans"]],
        )
    )


def _gaps(projection) -> list[dict]:
    return [turn["audible_gap"] for turn in projection.detail["turns"]]


def _within(actual: int | None, expected: int, tolerance: int) -> bool:
    return actual is not None and abs(actual - expected) <= tolerance


@pytest.mark.parametrize("cycle_kind", ["self", "two-span"])
def test_a_cyclic_parent_link_does_not_repeat_turn_descendants(cycle_kind: str) -> None:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_project_cyclic_turn, args=(result_queue, cycle_kind))
    process.start()
    try:
        process.join(timeout=10)
        assert not process.is_alive(), "projection did not terminate on a cyclic parent link"
        assert process.exitcode == 0
        root_id, descendants = result_queue.get(timeout=1)
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        result_queue.close()
        result_queue.join_thread()

    assert descendants
    assert root_id not in descendants
    assert len(descendants) == len(set(descendants))


# --- the audible gap at the agent ------------------------------------------------------------


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


def test_a_pipecat_turn_without_a_zero_run_edge_at_its_agent_on_is_not_step_checked() -> None:
    """Without the edge, the agent's audio onset is the nearest run's: comparing it with
    the user-off anchor would compare the heard gap with the reported latency."""

    recordings = []
    for recording in _pipecat_recordings():
        result = copy.deepcopy(recording.result)
        if recording.speaker == "agent":
            for channel in result["channels"]:
                channel["zero_runs"] = []
        recordings.append(replace(recording, result=result))

    gaps = _gaps(compute_projection(_source(_pipecat_spans(), recordings), PRICES))

    assert (gaps[0]["status"], gaps[0]["reason"]) == ("not_measurable", "no_agent_audio")
    for gap, value, sigma in ((gaps[1], 1510, 75), (gaps[2], 1490, 48)):
        assert (gap["status"], gap["reason"], gap["basis"]) == (
            "measured",
            None,
            "continuous_timeline_assumed",
        )
        assert _within(gap["value_ms"], value, 20), gap
        assert _within(gap["uncertainty_ms"], sigma, 5), gap


def test_livekit_turns_are_measured_on_the_continuous_timeline_assumed() -> None:
    gaps = _gaps(_livekit())

    expected = ((1290, 55), (1660, 52), (1250, 45))
    assert len(gaps) == 3
    for gap, (value, sigma) in zip(gaps, expected, strict=True):
        assert (gap["status"], gap["basis"]) == ("measured", "continuous_timeline_assumed")
        assert _within(gap["value_ms"], value, 20), gap
        assert _within(gap["uncertainty_ms"], sigma, 5), gap


def test_two_user_tracks_with_one_agent_track_are_not_one_timeline() -> None:
    user, agent = _pipecat_recordings()
    other_user = replace(user, recording_id="conv.user2")

    projection = compute_projection(_source(_pipecat_spans(), [user, other_user, agent]), PRICES)

    assert {gap["reason"] for gap in _gaps(projection)} == {"not_one_timeline"}
    assert [media["recording_ids"] for media in projection.detail["media"]] == [
        ["conv.agent"],
        ["conv.user"],
        ["conv.user2"],
    ]
    assert projection.detail["signals"]["one_timeline"] is False


# --- transcript placement --------------------------------------------------------------------


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


def test_a_snapped_line_ends_before_the_next_placed_line() -> None:
    from earshot.connectors.base import transcript_line
    from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans

    started = 1_800_000_000_000_000_000
    call_span = EvidenceSpan(CALL, 0, started, started + 100_000_000)
    lines = (
        transcript_line(
            0,
            speaker="user",
            text="first",
            start=started + 10_000_000,
            end=started + 20_000_000,
            timing="measured",
        ),
        transcript_line(
            1,
            speaker="user",
            text="untimed middle",
            start=started,
            end=started,
            timing="order_only",
        ),
        transcript_line(
            2,
            speaker="user",
            text="next",
            start=started + 30_000_000,
            end=started + 45_000_000,
            timing="measured",
        ),
    )
    evidence = Evidence(
        provider="vapi",
        provider_call_id="bounded_transcript_placement",
        spans=(call_span, *(line for line in lines if line is not None)),
    )
    spans = otlp_spans(
        evidence,
        session_key="vapi:bounded_transcript_placement",
        translator_version="test",
    )
    recording = RecordingSource(
        recording_id="recording",
        speaker="mixed",
        channel_speakers=("user", "agent"),
        content_type="audio/wav",
        started_at_unix_nano=started,
        duration_ms=100,
        analysis_state="stored",
        analysis_error_code=None,
        result={
            "source": {"duration_ms": 100},
            "channels": [
                {
                    "speaker": "user",
                    "runs": [
                        {"start_ms": 10, "end_ms": 20, "start_sigma_ms": 5, "end_sigma_ms": 5},
                        {"start_ms": 30, "end_ms": 45, "start_sigma_ms": 5, "end_sigma_ms": 5},
                        {"start_ms": 50, "end_ms": 60, "start_sigma_ms": 5, "end_sigma_ms": 5},
                    ],
                    "zero_runs": [],
                },
                {"speaker": "agent", "runs": [], "zero_runs": []},
            ],
        },
        has_peaks=False,
        holds_rendition=False,
    )

    projection = compute_projection(_source(spans, [recording]), PRICES)

    assert [
        (line["text"], line["timing"], line["start_ms"], line["end_ms"])
        for line in projection.detail["transcript"]
    ] == [
        ("first", "measured", 10, 20),
        ("untimed middle", "order_only", None, None),
        ("next", "measured", 30, 60),
    ]


def test_connector_transcript_order_and_timing_survive_projection() -> None:
    from earshot.call_materializer import stored_transcript_line, transcript_text_json
    from earshot.connectors.base import transcript_line
    from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans

    started = 1_800_000_000_000_000_000
    call_span = EvidenceSpan(CALL, 0, started, started + 2_000_000_000)
    lines = [
        transcript_line(
            index,
            speaker="user" if index % 2 == 0 else "agent",
            text=f"source line {index}",
            start=started,
            end=started,
            timing="order_only",
        )
        for index in range(4)
    ]
    lines.append(
        transcript_line(
            4,
            speaker="user",
            text="start only",
            start=started + 500_000_000,
            end=started + 500_000_000,
            timing="start_only",
        )
    )
    evidence = Evidence(
        provider="vapi",
        provider_call_id="call_with_untimed_lines",
        spans=(call_span, *(line for line in lines if line is not None)),
    )
    spans = otlp_spans(
        evidence,
        session_key="vapi:call_with_untimed_lines",
        translator_version="test",
    )

    projection = compute_projection(_source(spans, []), PRICES)
    detail = projection.detail["transcript"]

    assert [line["text"] for line in detail] == [
        "source line 0",
        "source line 1",
        "source line 2",
        "source line 3",
        "start only",
    ]
    assert [(line["timing"], line["start_ms"], line["end_ms"]) for line in detail] == [
        ("order_only", None, None),
        ("order_only", None, None),
        ("order_only", None, None),
        ("order_only", None, None),
        ("start_only", 500, None),
    ]
    assert [(row.timing, row.start_ms, row.end_ms) for row in projection.transcript] == [
        ("order_only", None, None),
        ("order_only", None, None),
        ("order_only", None, None),
        ("order_only", None, None),
        ("start_only", 500, None),
    ]
    source_span = next(
        span for span in spans if span.span.name == "earshot.connector.transcript_line"
    )
    stored_json = transcript_text_json(source_span)
    assert stored_json is not None
    stored = stored_transcript_line(
        stored_json,
        start_unix_nano=source_span.span.start_time_unix_nano,
        end_unix_nano=source_span.span.end_time_unix_nano,
        trace_id=source_span.span.trace_id,
        span_id=source_span.span.span_id,
    )
    assert (stored.timing, stored.transcript_index) == ("order_only", 0)


def test_mixed_timing_connector_lines_keep_provider_source_order() -> None:
    from earshot.connectors.base import transcript_line
    from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans

    started = 1_800_000_000_000_000_000
    call_span = EvidenceSpan(CALL, 0, started, started + 4_000_000_000)
    lines = (
        transcript_line(
            0,
            speaker="user",
            text="measured first",
            start=started + 2_000_000_000,
            end=started + 2_500_000_000,
            timing="measured",
        ),
        transcript_line(
            1,
            speaker="agent",
            text="untimed second",
            start=started,
            end=started,
            timing="order_only",
        ),
        transcript_line(
            2,
            speaker="user",
            text="measured third",
            start=started + 3_000_000_000,
            end=started + 3_500_000_000,
            timing="measured",
        ),
    )
    evidence = Evidence(
        provider="vapi",
        provider_call_id="mixed_timing_transcript",
        spans=(call_span, *(line for line in lines if line is not None)),
    )
    spans = otlp_spans(
        evidence,
        session_key="vapi:mixed_timing_transcript",
        translator_version="test",
    )

    projection = compute_projection(_source(spans, []), PRICES)

    assert [line["text"] for line in projection.detail["transcript"]] == [
        "measured first",
        "untimed second",
        "measured third",
    ]


def test_an_unplaced_connector_line_is_not_marked_alignment_uncertain() -> None:
    from earshot.connectors.base import transcript_line
    from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans

    started = int(captured_manifest()["audio"]["started_at_unix_nano"])
    line = transcript_line(
        0,
        speaker="user",
        text="untimed provider line",
        start=started,
        end=started,
        timing="order_only",
    )
    evidence = Evidence(
        provider="vapi",
        provider_call_id="untimed_transcript",
        spans=(
            EvidenceSpan(CALL, 0, started, started + 1_000_000_000),
            *((line,) if line is not None else ()),
        ),
    )
    extra_spans = otlp_spans(
        evidence,
        session_key="vapi:untimed_transcript",
        translator_version="test",
    )

    projection = compute_projection(
        _source((*_pipecat_spans(), *extra_spans), _pipecat_recordings()), PRICES
    )
    transcript_line_detail = next(
        line for line in projection.detail["transcript"] if line["text"] == "untimed provider line"
    )

    assert transcript_line_detail["timing"] == "order_only"
    assert transcript_line_detail["start_ms"] is None
    assert transcript_line_detail["end_ms"] is None
    assert transcript_line_detail["alignment_uncertain"] is False


# --- cost --------------------------------------------------------------------------------------


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

    # The STT estimate is the decoded duration the analysis measured, not the header's.
    assert _items(projection) == [
        ("priced", "llm", "groq", "input_token", 119 + 153 + 162 + 205 + 212),
        ("priced", "llm", "groq", "output_token", 38 + 38 + 39 + 38 + 32),
        ("priced", "tts", "cartesia", "character", 84 + 91 + 59),
        ("estimated", "stt", "deepgram", "audio_second", 34.004),
    ]
    assert projection.columns["cost_unknown_items"] == 0
    [media] = projection.detail["media"]
    assert media["duration_ms"] == 34_004


def test_a_cost_total_beyond_the_catalogs_64_bit_range_is_unknown_never_clamped() -> None:
    price = PriceRow(1, None, "groq", "m", "input_token", 1_000_000_000, 1, 0)
    entry = {
        "kind": "reported",
        "operation": "llm",
        "provider": "groq",
        "model": "m",
        "unit": "input_token",
        "quantity_milli": 5_000_000_000_000,
        "span": None,
    }

    cost = price_usage([entry, entry], [price], project_id="default", at_unix_nano=1)

    assert [item["cost_nano_usd"] for item in cost.items] == [5 * 10**18] * 2
    assert (cost.lower_bound_nano_usd, cost.estimated_nano_usd) == (None, None)


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


# --- signals -------------------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("content_type", "channels"),
    [
        ("audio/wav", [{"index": 0, "speaker": "user"}, {"index": 1, "speaker": "agent"}]),
        ("audio/mpeg", [{"index": 0, "speaker": "mixed"}]),
        ("audio/webm", [{"index": 0, "speaker": "mixed"}]),
    ],
)
def test_a_channel_map_is_reported_only_for_a_format_whose_header_the_upload_read(
    content_type: str, channels: list[dict]
) -> None:
    """The upload reads no MP3 or WebM header, so the recording's own speaker stands
    for the whole file there, as in the bundle; the channel map is a claim only for
    WAV, FLAC and Ogg Opus."""

    started = int(captured_manifest()["audio"]["started_at_unix_nano"])
    recording = _recording(
        "conv.stereo",
        ("user", "agent"),
        PIPECAT_AUDIO / "user.wav",
        content_type=content_type,
        started_at_unix_nano=started,
        duration_ms=32_486,
        result={"source": {"duration_ms": 32_486}, "channels": []},
    )

    projection = compute_projection(_source(_pipecat_spans(), [recording]), PRICES)

    [media] = projection.detail["media"]
    assert media["source"] == "recording"
    assert media["channels"] == channels


# --- not measurable, never fabricated ----------------------------------------------------------


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


def _with_turn_latency(seconds: float, *, turn: int) -> list:
    spans = copy.deepcopy(list(_pipecat_spans()))
    target = [span for span in spans if span.span.name == "turn"][turn]
    for item in target.span.attributes:
        if item.key == "turn.user_bot_latency_seconds":
            item.value.double_value = seconds
    return spans


@pytest.mark.parametrize("seconds", [1e20, 1e30, 9.3e9])
def test_a_reported_latency_past_the_catalogs_time_range_is_unknown(seconds: float) -> None:
    """Its milliseconds would not fit as 64-bit nanoseconds: the turn's latency is
    null, the anchor it gives is not used, and the other turns keep theirs."""

    spans = _with_turn_latency(seconds, turn=1)

    projection = compute_projection(_source(spans, _pipecat_recordings()), PRICES)

    assert [turn["reported_latency_ms"] for turn in projection.detail["turns"]] == [
        1800,
        None,
        1334,
    ]
    columns = projection.columns
    assert (columns["reported_latency_p50_ms"], columns["reported_latency_max_ms"]) == (
        1334,
        1800,
    )
    assert _gaps(projection)[1]["basis"] == "continuous_timeline_assumed"
    times = [
        value
        for row in projection.transcript
        for value in (row.start_ms, row.end_ms)
        if value is not None
    ]
    assert all(abs(value) < 2**63 for value in times)


def test_u0000_never_reaches_a_label_a_tag_or_a_transcript_line() -> None:
    """No catalog text column can hold it: a label holding it is absent, and a
    transcript line's text loses it."""

    spans = copy.deepcopy(list(_pipecat_spans()))
    first = spans[0].span
    first.attributes.add(key="earshot.agent.name").value.string_value = "bad\x00agent"
    first.attributes.add(key="earshot.call.outcome").value.string_value = "done\x00"
    tags = first.attributes.add(key="earshot.call.tags").value.array_value
    tags.values.add(string_value="vip")
    tags.values.add(string_value="b\x00d")
    stt = next(span for span in spans if span.span.name == "stt")
    for item in stt.span.attributes:
        if item.key == "transcript":
            spoken = item.value.string_value
            item.value.string_value = spoken[:3] + "\x00" + spoken[3:]

    projection = compute_projection(_source(spans, _pipecat_recordings()), PRICES)
    clean = _pipecat()

    assert (projection.columns["agent"], projection.columns["outcome"]) == (
        clean.columns["agent"],
        clean.columns["outcome"],
    )
    assert projection.tags == ("vip",)
    assert [row.text for row in projection.transcript] == [row.text for row in clean.transcript]


# --- agent, outcome and tags -------------------------------------------------------------------


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
