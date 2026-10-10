"""Gate: :class:`~earshot.engines.webrtc.WebRtcCarry` makes the stateful WebRTC
delta engine *resumable* without changing what a single-shot call derives.

A browser call is captured as many separately-drained batches. The engine is a
delta over consecutive snapshot pairs plus two small state machines (reconnect,
route), so analysing each batch from a clean slate silently drops the interval
that spans the boundary and forgets an outstanding ``disconnected`` -- losing
loss/jitter/concealment/playout evidence at every drain and never reporting a
reconnect whose down/up snapshots land in different batches.

The load-bearing property is **fold-equivalence**: folding one snapshot series
through the carry in N pieces yields facts byte-identical to analysing the whole
series in one call. This module proves it for a fixed series that contains a
reconnect, a route change, a counter reset and stale-render events, by splitting
at *every* index (2-way) and by treating every snapshot as its own piece
(N-way); and, via Hypothesis, over randomly generated monotonic series. It also
pins the two bugs the carry fixes: the boundary delta interval is recovered, and
a boundary-spanning reconnect is observed (a carry-less fold loses both).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from earshot.engines.webrtc import WebRtcCarry, WebRtcFacts, analyze_webrtc_stats

pytestmark = pytest.mark.unit


def _inbound(
    *,
    received: float,
    lost: float,
    jitter: float,
    buffer_delay: float,
    emitted: float,
    concealed: float,
    total: float,
    processing: float,
) -> dict[str, Any]:
    return {
        "type": "inbound-rtp",
        "kind": "audio",
        "packetsReceived": received,
        "packetsLost": lost,
        "jitter": jitter,
        "jitterBufferDelay": buffer_delay,
        "jitterBufferEmittedCount": emitted,
        "concealedSamples": concealed,
        "totalSamplesReceived": total,
        "totalProcessingDelay": processing,
    }


def _playout(
    *, delay: float, samples: float, synthesized: float, total_dur: float
) -> dict[str, Any]:
    return {
        "type": "media-playout",
        "kind": "audio",
        "totalPlayoutDelay": delay,
        "totalSamplesCount": samples,
        "synthesizedSamplesDuration": synthesized,
        "totalSamplesDuration": total_dur,
    }


def _route(pair_id: str, network: str) -> dict[str, dict[str, Any]]:
    return {
        pair_id: {"type": "candidate-pair", "nominated": True, "localCandidateId": f"L{pair_id}"},
        f"L{pair_id}": {"type": "local-candidate", "networkType": network},
    }


def _snap(
    timestamp_ms: float,
    *,
    ice: str,
    pair: str,
    network: str,
    inbound: dict[str, Any],
    playout: dict[str, Any],
    rtt: float,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "T": {"type": "transport", "iceState": ice, "selectedCandidatePairId": pair},
        "IT": inbound,
        "RI": {"type": "remote-inbound-rtp", "roundTripTime": rtt},
        "PO": playout,
        **_route(pair, network),
    }
    return {"timestamp_ms": timestamp_ms, "stats": stats}


# A single continuous call, time-ordered, exercising every stateful path:
#   s0..s1  connected, counters climb  -> loss/jitter-buffer/concealment/playout
#   s2      DISCONNECTED               -> reconnecting event; synth samples appear
#   s3      CONNECTED again            -> reconnect closes (spans a boundary)
#   s3->s4  every counter falls        -> counter-reset coverage (a stream reset)
#   s5      new candidate pair/network -> route change; more synth samples
_SERIES: list[dict[str, Any]] = [
    _snap(
        1000,
        ice="connected",
        pair="CP1",
        network="wifi",
        rtt=0.080,
        inbound=_inbound(
            received=1000,
            lost=5,
            jitter=0.010,
            buffer_delay=0.0,
            emitted=0,
            concealed=10,
            total=48000,
            processing=0.0,
        ),
        playout=_playout(delay=0.0, samples=0, synthesized=0.0, total_dur=0.0),
    ),
    _snap(
        2000,
        ice="connected",
        pair="CP1",
        network="wifi",
        rtt=0.090,
        inbound=_inbound(
            received=2000,
            lost=15,
            jitter=0.020,
            buffer_delay=2.0,
            emitted=100,
            concealed=30,
            total=96000,
            processing=1.0,
        ),
        playout=_playout(delay=1.0, samples=48000, synthesized=0.0, total_dur=1.0),
    ),
    _snap(
        3000,
        ice="disconnected",
        pair="CP1",
        network="wifi",
        rtt=0.120,
        inbound=_inbound(
            received=3000,
            lost=40,
            jitter=0.030,
            buffer_delay=7.0,
            emitted=200,
            concealed=80,
            total=144000,
            processing=2.5,
        ),
        playout=_playout(delay=3.0, samples=96000, synthesized=0.05, total_dur=2.0),
    ),
    _snap(
        4000,
        ice="connected",
        pair="CP1",
        network="wifi",
        rtt=0.085,
        inbound=_inbound(
            received=4000,
            lost=50,
            jitter=0.015,
            buffer_delay=16.0,
            emitted=300,
            concealed=100,
            total=192000,
            processing=3.5,
        ),
        playout=_playout(delay=6.0, samples=144000, synthesized=0.05, total_dur=3.0),
    ),
    _snap(
        5000,
        ice="connected",
        pair="CP1",
        network="wifi",
        rtt=0.088,
        inbound=_inbound(
            received=20,
            lost=0,
            jitter=0.010,
            buffer_delay=0.0,
            emitted=0,
            concealed=0,
            total=1000,
            processing=0.0,
        ),
        playout=_playout(delay=0.0, samples=0, synthesized=0.0, total_dur=0.0),
    ),
    _snap(
        6000,
        ice="connected",
        pair="CP2",
        network="cellular",
        rtt=0.092,
        inbound=_inbound(
            received=1000,
            lost=8,
            jitter=0.012,
            buffer_delay=3.0,
            emitted=120,
            concealed=20,
            total=49000,
            processing=1.2,
        ),
        playout=_playout(delay=1.5, samples=48000, synthesized=0.03, total_dur=1.0),
    ),
]


def _fold(pieces: Sequence[Sequence[dict[str, Any]]]) -> WebRtcFacts:
    """Analyse ``pieces`` in order, threading the carry, and union the facts.

    Measurements and events are concatenated in emission order; coverage is a
    de-duplicating ledger per call, so its union is a set (the same signal noted
    in two segments collapses exactly as one segment would collapse two
    intervals). The three summary booleans OR across segments.
    """

    carry: WebRtcCarry | None = None
    measurements: list = []
    events: list = []
    coverage: list = []
    growth = reconnected = route_changed = False
    for piece in pieces:
        facts = analyze_webrtc_stats(piece, carry=carry)
        carry = facts.carry
        measurements.extend(facts.measurements)
        events.extend(facts.events)
        coverage.extend(facts.coverage)
        growth = growth or facts.jitter_buffer_growth
        reconnected = reconnected or facts.reconnected
        route_changed = route_changed or facts.route_changed
    return WebRtcFacts(
        measurements=tuple(measurements),
        events=tuple(events),
        coverage=tuple(coverage),
        jitter_buffer_growth=growth,
        reconnected=reconnected,
        route_changed=route_changed,
    )


def _assert_equivalent(folded: WebRtcFacts, single: WebRtcFacts) -> None:
    assert folded.measurements == single.measurements
    assert folded.events == single.events
    # Coverage is order-free and de-duplicated per signal.
    assert set(folded.coverage) == set(single.coverage)
    assert folded.jitter_buffer_growth == single.jitter_buffer_growth
    assert folded.reconnected == single.reconnected
    assert folded.route_changed == single.route_changed


def test_the_fixture_series_exercises_every_stateful_path() -> None:
    """Guard: the fold assertions below are meaningful, not vacuously satisfied."""

    single = analyze_webrtc_stats(_SERIES)
    names = {m.name for m in single.measurements}
    assert {
        "packet_loss_ratio",
        "jitter",
        "round_trip_time",
        "jitter_buffer_delay",
        "concealment_ratio",
        "processing_delay",
        "playout_delay",
        "synthesized_samples_ratio",
    } <= names
    assert single.reconnected is True
    assert single.route_changed is True
    assert single.jitter_buffer_growth is True
    assert [e.name for e in single.events] == [
        "earshot.transport.reconnecting",
        "earshot.audio.render.stale",
        "earshot.transport.route_changed",
        "earshot.audio.render.stale",
    ]
    # The stream reset is recorded as coverage on every governed counter family.
    assert {c.reason for c in single.coverage} == {"counter_reset"}
    assert {c.signal for c in single.coverage} == {
        "webrtc.packet_loss",
        "webrtc.jitter_buffer",
        "webrtc.concealment",
        "webrtc.processing_delay",
        "webrtc.playout",
    }


def test_folding_through_the_carry_at_every_split_index_matches_single_shot() -> None:
    single = analyze_webrtc_stats(_SERIES)
    for split in range(len(_SERIES) + 1):
        folded = _fold([_SERIES[:split], _SERIES[split:]])
        _assert_equivalent(folded, single)


def test_folding_one_snapshot_per_piece_matches_single_shot() -> None:
    single = analyze_webrtc_stats(_SERIES)
    folded = _fold([[snapshot] for snapshot in _SERIES])
    _assert_equivalent(folded, single)


def test_folding_at_two_split_points_matches_single_shot() -> None:
    single = analyze_webrtc_stats(_SERIES)
    for first in range(len(_SERIES) + 1):
        for second in range(first, len(_SERIES) + 1):
            folded = _fold([_SERIES[:first], _SERIES[first:second], _SERIES[second:]])
            _assert_equivalent(folded, single)


def test_the_boundary_delta_interval_is_recovered_not_dropped() -> None:
    """The interval spanning a drain boundary is differenced exactly once.

    Splitting between s1 and s2 puts the s1->s2 loss/jitter-buffer/concealment
    deltas on the boundary. With the carry they are emitted in the second segment
    at s2's coordinate; a carry-less second segment (fresh state) drops them.
    """

    single = analyze_webrtc_stats(_SERIES)
    boundary = [m for m in single.measurements if m.at_ms == 2000.0]
    assert boundary  # s1->s2 deltas and s2 instants live at at_ms == 2000

    first = analyze_webrtc_stats(_SERIES[:2])
    resumed = analyze_webrtc_stats(_SERIES[2:], carry=first.carry)
    fresh = analyze_webrtc_stats(_SERIES[2:])  # no carry: the boundary is lost

    resumed_boundary = [m for m in resumed.measurements if m.at_ms == 2000.0]
    assert resumed_boundary == boundary
    # The carry-less second segment restarts its timeline (origin at s2) and never
    # differences the s1->s2 interval, so it cannot reproduce those facts.
    assert [m for m in fresh.measurements if m.at_ms == 2000.0] != boundary


def test_a_reconnect_spanning_a_boundary_is_observed_only_with_the_carry() -> None:
    """s2 is ``disconnected`` and s3 is ``connected``; splitting between them
    puts the down and the recovery in different segments."""

    single = analyze_webrtc_stats(_SERIES)
    assert single.reconnected is True

    first = analyze_webrtc_stats(_SERIES[:3])  # ends on the disconnected snapshot
    assert first.reconnected is False  # recovery not yet seen
    resumed = analyze_webrtc_stats(_SERIES[3:], carry=first.carry)
    assert resumed.reconnected is True  # the carry remembers the outstanding down
    # The reconnecting event still fires exactly once (in the first segment), at
    # the same coordinate the single-shot run reports it.
    assert [e.name for e in first.events].count("earshot.transport.reconnecting") == 1
    assert [e.name for e in resumed.events].count("earshot.transport.reconnecting") == 0

    fresh = analyze_webrtc_stats(_SERIES[3:])  # no carry: the down is forgotten
    assert fresh.reconnected is False


def test_single_shot_with_no_carry_is_unchanged() -> None:
    assert analyze_webrtc_stats([]) == WebRtcFacts((), (), ())
    assert analyze_webrtc_stats([]).carry is None
    # Threading an empty segment is a genuine no-op: the carry passes straight
    # through untouched.
    seeded = analyze_webrtc_stats(_SERIES[:2])
    passthrough = analyze_webrtc_stats([], carry=seeded.carry)
    assert passthrough.measurements == ()
    assert passthrough.carry is seeded.carry


@st.composite
def _monotonic_series(draw: st.DrawFn) -> list[dict[str, Any]]:
    """A random, time-ordered getStats series with resets and reconnects.

    Timestamps are non-decreasing so the first snapshot carries the origin (the
    real-world drain case); counters usually climb but occasionally reset, and the
    ICE state walks connected/disconnected so reconnects arise at random offsets.
    """

    count = draw(st.integers(min_value=1, max_value=8))
    ts = draw(st.integers(min_value=0, max_value=1000))
    received = draw(st.integers(min_value=0, max_value=5000))
    lost = draw(st.integers(min_value=0, max_value=500))
    emitted = draw(st.integers(min_value=0, max_value=5000))
    buffer_delay = draw(st.floats(min_value=0.0, max_value=5.0))
    concealed = draw(st.integers(min_value=0, max_value=5000))
    total = draw(st.integers(min_value=0, max_value=200000))
    processing = draw(st.floats(min_value=0.0, max_value=5.0))
    synthesized = draw(st.floats(min_value=0.0, max_value=5.0))
    total_dur = draw(st.floats(min_value=0.0, max_value=50.0))
    delay = draw(st.floats(min_value=0.0, max_value=5.0))
    samples = draw(st.integers(min_value=0, max_value=200000))
    series: list[dict[str, Any]] = []
    for _ in range(count):
        ts += draw(st.integers(min_value=0, max_value=1000))
        if draw(st.booleans()):  # occasional stream reset
            received = draw(st.integers(min_value=0, max_value=100))
            lost = emitted = concealed = total = samples = 0
            buffer_delay = processing = synthesized = total_dur = delay = 0.0
        else:
            received += draw(st.integers(min_value=1, max_value=2000))
            lost += draw(st.integers(min_value=0, max_value=200))
            emitted += draw(st.integers(min_value=1, max_value=2000))
            buffer_delay += draw(st.floats(min_value=0.0, max_value=5.0))
            concealed += draw(st.integers(min_value=0, max_value=2000))
            total += draw(st.integers(min_value=1, max_value=96000))
            processing += draw(st.floats(min_value=0.0, max_value=5.0))
            synthesized += draw(st.floats(min_value=0.0, max_value=2.0))
            total_dur += draw(st.floats(min_value=0.1, max_value=10.0))
            delay += draw(st.floats(min_value=0.0, max_value=5.0))
            samples += draw(st.integers(min_value=1, max_value=48000))
        ice = draw(st.sampled_from(["connected", "disconnected", "connected", "completed"]))
        pair = draw(st.sampled_from(["CP1", "CP2"]))
        network = draw(st.sampled_from(["wifi", "cellular"]))
        series.append(
            _snap(
                float(ts),
                ice=ice,
                pair=pair,
                network=network,
                rtt=draw(st.floats(0.0, 1.0)),
                inbound=_inbound(
                    received=received,
                    lost=lost,
                    jitter=draw(st.floats(0.0, 0.1)),
                    buffer_delay=buffer_delay,
                    emitted=emitted,
                    concealed=concealed,
                    total=total,
                    processing=processing,
                ),
                playout=_playout(
                    delay=delay, samples=samples, synthesized=synthesized, total_dur=total_dur
                ),
            )
        )
    return series


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_fold_equivalence_holds_for_random_monotonic_series(data: st.DataObject) -> None:
    series = data.draw(_monotonic_series())
    split = data.draw(st.integers(min_value=0, max_value=len(series)))
    single = analyze_webrtc_stats(series)
    folded = _fold([series[:split], series[split:]])
    _assert_equivalent(folded, single)
