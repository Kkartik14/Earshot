"""The provider-free replay e2e: both quick starts, on a real server, with their call views.

``earshot serve`` runs with its default configuration (SQLite and local disk in
its data directory, anonymous on loopback). One real capture is replayed into it
as its example sent it (``capture_replay``): the exporter's requests in arrival
order, as OTLP/HTTP POSTs with the exporter's content type, and its real audio
through the recordings API in the example's order.

- **Pipecat 1.5** (``fixtures/captured/pipecat-otlp-1.5``): two mono WAV tracks,
  created at the call's first audio (so the call is held) and uploaded after the
  spans. The call must pass the same settle checks as the real-provider e2e, and
  its span transcript must be the fixture's caller script and the agent's replies.
- **LiveKit 1.8** (``fixtures/captured/livekit-otlp-1.8``): one stereo Ogg Opus
  recording (channel 0 user, channel 1 agent), created when the session starts and
  uploaded when it ends.

Then the server's own analysis and projection jobs run, and each call view must
pass the done-when checks the real-provider e2e makes (``assert_call_view``) and
hold the fixture's pinned values. Those are the analysis job's own speech runs on
the fixture audio (Silero VAD v6.2.3, refined to 10 ms energy edges): each audible
gap within 20 ms of its pinned value, each reported ± within 5 ms of the pinned
uncertainty, each placed transcript line within 300 ms of a speech run of its
speaker, and the exact cost items.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from call_done_when import (
    Earshot,
    SentCall,
    assert_call_view,
    assert_done_when,
    read_rows,
    wait_until_projected,
    wait_until_settled,
)
from capture_replay import replay_livekit, replay_pipecat
from server_process import ServerProcess

pytestmark = [pytest.mark.e2e, pytest.mark.sqlite_only, pytest.mark.slow]

# Deepgram's transcript of the fixture's caller script, and the agent's replies
# (``llm.output``, exactly as the model wrote them), in the order they were spoken.
TRANSCRIPT = [
    ("user", "Hi. I would like to book a table for two people tomorrow evening."),
    ("agent", "Sure, what time would you like to come in tomorrow evening?"),
    ("user", "Seven thirty works for us."),
    ("user", "Could we sit somewhere quiet near the window?"),
    (
        "agent",
        "Sure, we\u2019ve booked a quiet window seat for you at 7:30. "
        "We look forward to welcoming you tomorrow.",
    ),
    ("user", "Perfect. That is everything I needed."),
    ("user", "Thank you very much. Goodbye."),
    ("agent", "You're welcome! See you tomorrow."),
]

VALUE_TOLERANCE_MS = 20
UNCERTAINTY_TOLERANCE_MS = 5
PLACEMENT_TOLERANCE_MS = 300

# Speech-run starts on the call axis (ms; both fixtures' recordings start at 0).
PIPECAT_RUN_STARTS = {
    "user": (1290, 11770, 13520, 22500, 24280),
    "agent": (6960, 8030, 16750, 20120, 26920, 28130),
}
LIVEKIT_RUN_STARTS = {
    "user": (1310, 11170, 12920, 21900, 23680),
    "agent": (5200, 7850, 16300, 17000, 20510, 26070, 27290, 29250),
}


@contextlib.contextmanager
def _serve(tmp_path: Path) -> Iterator[Earshot]:
    data_dir = tmp_path / "data"
    server = ServerProcess.start(data_dir, tmp_path / "serve.log", entry_point="serve")
    try:
        yield Earshot(
            port=server.port,
            project_id="default",
            catalog=data_dir,
            read_blob=lambda key: (data_dir / "blobs" / key).read_bytes(),
        )
    finally:
        server.stop()


def _within(actual: int | None, expected: int, tolerance: int) -> bool:
    return actual is not None and abs(actual - expected) <= tolerance


def _assert_measured(gap: dict[str, Any], value: int, uncertainty: int, basis: str) -> None:
    assert (gap["status"], gap["reason"], gap["basis"]) == ("measured", None, basis), gap
    assert _within(gap["value_ms"], value, VALUE_TOLERANCE_MS), gap
    assert _within(gap["uncertainty_ms"], uncertainty, UNCERTAINTY_TOLERANCE_MS), gap


def _assert_placed_on_runs(
    transcript: Sequence[dict[str, Any]], runs: dict[str, tuple[int, ...]]
) -> None:
    for line in transcript:
        nearest = min(abs(line["start_ms"] - start) for start in runs[line["speaker"]])
        assert nearest <= PLACEMENT_TOLERANCE_MS, line


def _cost_items(projection: dict[str, Any]) -> list[tuple]:
    return [
        (item["status"], item["operation"], item["provider"], item["unit"], item["quantity"])
        for item in projection["cost_items"]
    ]


def test_the_pipecat_quick_start_replay_gives_a_call_view_with_the_pinned_values(
    tmp_path,
) -> None:
    with _serve(tmp_path) as earshot:
        replayed = replay_pipecat(earshot.send)
        sent = SentCall(
            session_key=replayed.session_key,
            exported_span_count=replayed.exported_span_count,
            started_at_unix_nano=replayed.started_at_unix_nano,
            tracks=dict(replayed.recordings.values()),
        )
        settled = assert_done_when(earshot, sent).detail
        detail = wait_until_projected(earshot, settled["call_id"])

    assert [(line["speaker"], line["text"]) for line in settled["transcript"]] == TRANSCRIPT
    assert settled["transcript_coverage"] == "available"
    assert_call_view(detail, replayed.providers)
    projection = detail["projection"]

    # Turn 1's gap holds a recorder discontinuity: an upper bound, never a value.
    first, second, third = (turn["audible_gap"] for turn in projection["turns"])
    assert (first["status"], first["reason"], first["value_ms"]) == (
        "not_measurable",
        "recorder_timeline_discontinuity",
        None,
    )
    assert _within(first["bound_ms"], 3080, VALUE_TOLERANCE_MS), first
    _assert_measured(second, 1510, 75, "anchors_checked")
    _assert_measured(third, 1490, 48, "anchors_checked")

    # The placed transcript is the span transcript, on the speech runs.
    assert [(line["speaker"], line["text"]) for line in projection["transcript"]] == TRANSCRIPT
    _assert_placed_on_runs(projection["transcript"], PIPECAT_RUN_STARTS)

    # The mono pair plays as one stereo rendition.
    [media] = projection["media"]
    assert (media["source"], media["content_type"]) == ("rendition", "audio/flac")
    assert sorted(media["recording_ids"]) == sorted(replayed.recordings)

    assert _cost_items(projection) == [
        ("priced", "llm", "groq", "input_token", 495),
        ("priced", "llm", "groq", "output_token", 319),
        ("priced", "tts", "cartesia", "character", 118),
        ("estimated", "stt", "deepgram", "audio_second", 32.486),
        ("not_reported", "llm", "groq", None, None),
        ("not_reported", "llm", "groq", None, None),
    ]
    assert projection["cost"]["unknown_items"] == 2


def test_the_livekit_quick_start_replay_gives_a_call_view_with_the_pinned_values(
    tmp_path,
) -> None:
    with _serve(tmp_path) as earshot:
        replayed = replay_livekit(earshot.send)
        call_id = earshot.json("POST", "/v1/calls:resolve", {"session_key": replayed.session_key})[
            "call_id"
        ]
        settled = wait_until_settled(earshot, call_id, recordings=1)
        [(recording_id, (_, uploaded))] = replayed.recordings.items()
        content = earshot.call("GET", f"/v1/recordings/{recording_id}/content")
        [(stored_spans,)] = read_rows(
            earshot.catalog, "SELECT count(*) FROM spans WHERE project_id = 'default'"
        )
        detail = wait_until_projected(earshot, call_id)

    # One closed call, final on the end signal, every span stored, the bytes kept.
    latest = settled["revisions"][-1]
    assert (latest["close_reason"], latest["finality"]) == ("end_signal", "final")
    assert stored_spans == settled["span_count"] == replayed.exported_span_count
    [recording] = settled["recordings"]
    assert (recording["state"], recording["channel_speakers"]) == ("stored", ["user", "agent"])
    assert recording["sha256"] == hashlib.sha256(uploaded).hexdigest()
    assert (content.status, content.media_type, content.body) == (200, "audio/ogg", uploaded)

    assert_call_view(detail, replayed.providers)
    projection = detail["projection"]

    # LiveKit's recorder places frames by wall clock: every turn is measured on it.
    gaps = [turn["audible_gap"] for turn in projection["turns"]]
    assert len(gaps) == 3
    for gap, (value, uncertainty) in zip(gaps, ((1290, 55), (1660, 52), (1250, 45)), strict=True):
        _assert_measured(gap, value, uncertainty, "continuous_timeline_assumed")

    # One agent line per agent turn: the played text, on the agent's speech runs.
    transcript = projection["transcript"]
    assert [line["speaker"] for line in transcript] == ["user", "agent"] * 3
    _assert_placed_on_runs(transcript, LIVEKIT_RUN_STARTS)

    [media] = projection["media"]
    assert (media["source"], media["content_type"], media["recording_ids"]) == (
        "recording",
        "audio/ogg",
        [recording_id],
    )

    # Every LLM request is billed, preemptive ones included; STT is estimated from the
    # recording's duration as its analysis decoded it (its Ogg Opus header says 34.010 s).
    assert recording["duration_ms"] == 34_010
    assert _cost_items(projection) == [
        ("priced", "llm", "groq", "input_token", 119 + 153 + 162 + 205 + 212),
        ("priced", "llm", "groq", "output_token", 38 + 38 + 39 + 38 + 32),
        ("priced", "tts", "cartesia", "character", 84 + 91 + 59),
        ("estimated", "stt", "deepgram", "audio_second", 34.004),
    ]
    assert projection["cost"]["complete"] is True
