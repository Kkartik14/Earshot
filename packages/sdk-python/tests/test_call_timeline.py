"""The speech timeline's call-level measures from deterministic run boundaries."""

from __future__ import annotations

import pytest

from earshot.call_timeline import (
    Run,
    Timeline,
    Track,
    TurnAnchorsMs,
    audible_gap,
    one_timeline_pairs,
    signals,
)

pytestmark = pytest.mark.unit


def _run(start_ms: int, end_ms: int) -> Run:
    return Run(start_ms, end_ms, start_sigma_ms=10, end_sigma_ms=10)


def _run_with_edge_sigma(
    start_ms: int, end_ms: int, *, start_sigma_ms: int, end_sigma_ms: int
) -> Run:
    return Run(
        start_ms,
        end_ms,
        start_sigma_ms=start_sigma_ms,
        end_sigma_ms=end_sigma_ms,
    )


def _timeline(*, user: tuple[Run, ...], agent: tuple[Run, ...]) -> Timeline:
    return Timeline(
        started_at_unix_nano=0,
        duration_ms=None,
        user_runs=user,
        agent_runs=agent,
        agent_zero_runs=(),
        insert_only=False,
    )


def test_a_user_pause_followed_by_more_user_speech_is_not_agent_dead_air() -> None:
    measured = signals(_timeline(user=(_run(0, 500), _run(3000, 4000)), agent=()))

    assert measured.long_silence_count == 1
    assert measured.dead_air_ms == 0


def test_a_user_pause_followed_by_an_agent_response_is_agent_dead_air() -> None:
    measured = signals(_timeline(user=(_run(0, 500),), agent=(_run(3000, 3500),)))

    assert measured.dead_air_ms == 2500


def test_speech_run_metrics_count_overlaps_barge_ins_and_talk_overs() -> None:
    measured = signals(
        _timeline(user=(_run(500, 2000),), agent=(_run(100, 1200), _run(1300, 1800)))
    )

    assert measured.overlap_ms == 1200
    assert measured.barge_in_count == 1
    assert measured.talk_over_count == 1


def test_overlapping_runs_for_one_speaker_are_measured_as_one_speech_interval() -> None:
    measured = signals(_timeline(user=(_run(0, 1000), _run(500, 1500)), agent=(_run(1000, 1600),)))

    assert measured.talk_time_user_ms == 1500
    assert measured.overlap_ms == 500


def test_a_gap_whose_uncertainty_exceeds_100_ms_is_not_reported_as_measured() -> None:
    timeline = _timeline(
        user=(_run_with_edge_sigma(0, 4000, start_sigma_ms=10, end_sigma_ms=100),),
        agent=(_run_with_edge_sigma(5500, 6000, start_sigma_ms=20, end_sigma_ms=10),),
    )
    anchors = TurnAnchorsMs(
        user_on_unix_nano=(0,),
        user_off_unix_nano=None,
        agent_on_unix_nano=5_500_000_000,
    )

    gap = audible_gap(timeline, anchors)

    assert (gap.status, gap.reason, gap.value_ms, gap.uncertainty_ms) == (
        "not_measurable",
        "boundary_uncertainty_too_high",
        None,
        102,
    )
    assert (gap.user_end_ms, gap.agent_start_ms) == (4000, 5500)


def test_a_gap_at_the_100_ms_uncertainty_limit_remains_measured() -> None:
    timeline = _timeline(
        user=(_run_with_edge_sigma(0, 4000, start_sigma_ms=10, end_sigma_ms=60),),
        agent=(_run_with_edge_sigma(5500, 6000, start_sigma_ms=80, end_sigma_ms=10),),
    )
    anchors = TurnAnchorsMs(
        user_on_unix_nano=(0,),
        user_off_unix_nano=None,
        agent_on_unix_nano=5_500_000_000,
    )

    gap = audible_gap(timeline, anchors)

    assert (gap.status, gap.value_ms, gap.uncertainty_ms) == ("measured", 1500, 100)


def test_a_raw_uncertainty_above_100_ms_is_not_measurable_when_it_rounds_to_100() -> None:
    timeline = _timeline(
        user=(_run_with_edge_sigma(0, 4000, start_sigma_ms=10, end_sigma_ms=100),),
        agent=(_run_with_edge_sigma(5500, 6000, start_sigma_ms=10, end_sigma_ms=10),),
    )
    anchors = TurnAnchorsMs(
        user_on_unix_nano=(0,),
        user_off_unix_nano=None,
        agent_on_unix_nano=5_500_000_000,
    )

    gap = audible_gap(timeline, anchors)

    assert (gap.status, gap.reason, gap.value_ms, gap.uncertainty_ms) == (
        "not_measurable",
        "boundary_uncertainty_too_high",
        None,
        100,
    )


# --- one recorder timeline -----------------------------------------------------------------


def _track(recording_id: str, *speakers: str, start: int = 1, duration: int | None = 1000):
    return Track(recording_id, speakers, start, duration)


@pytest.mark.parametrize(
    ("tracks", "pairs"),
    [
        ([_track("u", "user"), _track("a", "agent", duration=1020)], [("u", "a")]),
        ([_track("u", "user"), _track("a", "agent", duration=1021)], []),
        ([_track("u", "user", duration=None), _track("a", "agent")], []),
        ([_track("u", "user"), _track("a", "agent", start=2)], []),
        # Two tracks of one speaker at one start: neither pairs, from either side.
        ([_track("u1", "user"), _track("u2", "user"), _track("a", "agent")], []),
        ([_track("u", "user"), _track("a1", "agent"), _track("a2", "agent", duration=5)], []),
        # A stereo recording is not a mono track; each start is its own timeline.
        (
            [
                _track("s", "user", "agent"),
                _track("u", "user"),
                _track("a", "agent"),
                _track("u2", "user", start=9),
                _track("a2", "agent", start=9),
            ],
            [("u", "a"), ("u2", "a2")],
        ),
    ],
)
def test_one_timeline_pairs_only_an_unambiguous_mono_pair(tracks, pairs) -> None:
    assert one_timeline_pairs(tracks) == pairs
    assert one_timeline_pairs(reversed(tracks)) == pairs
