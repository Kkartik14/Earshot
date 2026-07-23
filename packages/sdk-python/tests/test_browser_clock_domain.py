"""F2: the browser->server timing path must not invent clock comparability.

Browser-derived facts are recorded in a declared BROWSER clock domain at their
raw browser timestamps -- never rebased onto the server clock. Two engine batches
with different real origins therefore land at DISTINCT coordinates (not falsely
simultaneous), and cross-clock latency between a browser event and a server event
stays *unavailable* until a real ``ClockRelation`` calibration is supplied, at
which point it becomes honestly *estimated*.
"""

from __future__ import annotations

import pytest

import earshot
from earshot.analysis import _ClockAligner, comparable_delta
from earshot.contract import ClockRelation
from earshot.engines import BrowserClockDomain
from earshot.engines.device import apply_audio_graph
from earshot.engines.webrtc import apply_webrtc_stats
from earshot.validation import validate_incident

pytestmark = pytest.mark.unit

START = 1_800_000_000_000_000_000
# The browser wall clock reads this many ns ahead of the server wall clock.
SKEW = 3000


def _events(bundle) -> dict:
    return {event.event_name: event for event in bundle.profile.events}


def _domains(bundle) -> dict:
    return {domain.clock_domain_id: domain for domain in bundle.profile.clock_domains}


# -- distinct origins, not falsely simultaneous --------------------------------


def test_two_browser_batches_with_distinct_origins_are_not_falsely_simultaneous() -> None:
    clock = BrowserClockDomain(clock_domain_id="clk_session")
    # WebRTC batch observed at browser-time 5000ms; device batch 15s later.
    webrtc = [
        {"timestamp_ms": 5000, "stats": {"T": {"type": "transport", "iceState": "disconnected"}}},
        {"timestamp_ms": 5500, "stats": {"T": {"type": "transport", "iceState": "connected"}}},
    ]
    devices = [{"type": "permission", "timestamp_ms": 20000, "state": "denied"}]

    session = earshot.pipeline(session_id="f2-distinct", started_at_unix_nano=START)
    with session.turn() as turn:
        apply_webrtc_stats(turn, webrtc, clock_domain=clock)
        apply_audio_graph(turn, devices, clock_domain=clock)
    bundle = session.close()

    events = _events(bundle)
    recon = events["earshot.transport.reconnecting"]
    denied = events["earshot.device.permission_denied"]

    # Both are in the BROWSER domain, NOT the server clock.
    server_domain = bundle.profile.session.started_at.clock_domain_id
    assert recon.time.clock_domain_id == "clk_session"
    assert denied.time.clock_domain_id == "clk_session"
    assert recon.time.clock_domain_id != server_domain

    # They land at DISTINCT monotonic coordinates -- their real origins survive.
    assert recon.time.monotonic_time_nano != denied.time.monotonic_time_nano
    assert int(recon.time.monotonic_time_nano) == 5_000_000_000
    assert int(denied.time.monotonic_time_nano) == 20_000_000_000
    # The server clock domain is not calibrated to the browser: no relation invented.
    assert bundle.profile.clock_relations == ()
    assert validate_incident(bundle).ok


def test_periodic_drains_do_not_restart_the_browser_timeline() -> None:
    # The browser's monotonic clock is continuous across drains, so a later batch
    # keeps a strictly-later coordinate rather than resetting to zero.
    clock = BrowserClockDomain(clock_domain_id="clk_session")
    session = earshot.pipeline(session_id="f2-drain", started_at_unix_nano=START)
    with session.turn("turn-a") as turn:
        apply_audio_graph(turn, [{"type": "underrun", "timestamp_ms": 1000}], clock_domain=clock)
    with session.turn("turn-b") as turn:
        apply_audio_graph(turn, [{"type": "underrun", "timestamp_ms": 90000}], clock_domain=clock)
    bundle = session.close()

    stale = sorted(
        (e for e in bundle.profile.events if e.event_name == "earshot.audio.render.stale"),
        key=lambda e: int(e.time.monotonic_time_nano),
    )
    assert [int(e.time.monotonic_time_nano) for e in stale] == [1_000_000_000, 90_000_000_000]


# -- browser facts land in a browser clock domain with uncertainty -------------


def test_browser_facts_land_in_browser_domain_with_uncertainty() -> None:
    clock = BrowserClockDomain(
        clock_domain_id="clk_session",
        uncertainty_nano=1_000_000,
        wall_origin_unix_nano=START + SKEW,
    )
    snapshots = [
        {
            "timestamp_ms": 4000,
            "stats": {
                "IT": {
                    "type": "inbound-rtp",
                    "kind": "audio",
                    "packetsReceived": 1000,
                    "packetsLost": 5,
                }
            },
        },
        {
            "timestamp_ms": 5000,
            "stats": {
                "IT": {
                    "type": "inbound-rtp",
                    "kind": "audio",
                    "packetsReceived": 1100,
                    "packetsLost": 55,
                }
            },
        },
    ]
    session = earshot.pipeline(session_id="f2-domain", started_at_unix_nano=START)
    with session.turn() as turn:
        apply_webrtc_stats(turn, snapshots, clock_domain=clock)
    bundle = session.close()

    # The browser clock domain is declared alongside the server one.
    domains = _domains(bundle)
    assert "clk_session" in domains
    browser_domain = domains["clk_session"]
    assert browser_domain.observer == "browser"
    assert int(browser_domain.uncertainty_nano) == 1_000_000

    # The loss sample sits in the browser domain, at its raw browser timestamp,
    # carrying uncertainty -- never rebased to a server coordinate.
    [sample] = [
        s
        for s in bundle.profile.quality_samples
        if any(m.name == "packet_loss_ratio" for m in s.measurements)
    ]
    point = sample.sample_window.start
    assert point.clock_domain_id == "clk_session"
    assert int(point.monotonic_time_nano) == 5_000_000_000  # raw browser ts (2nd snapshot)
    assert int(point.uncertainty_nano) == 1_000_000
    # A wall reading is present ONLY because the browser wall origin was supplied.
    assert int(point.source_time_unix_nano) == START + SKEW + 5_000_000_000
    assert validate_incident(bundle).ok


def test_browser_facts_without_wall_origin_carry_only_monotonic() -> None:
    clock = BrowserClockDomain(clock_domain_id="clk_session")  # no wall origin
    session = earshot.pipeline(session_id="f2-nowall", started_at_unix_nano=START)
    with session.turn() as turn:
        apply_audio_graph(turn, [{"type": "underrun", "timestamp_ms": 700}], clock_domain=clock)
    bundle = session.close()

    stale = _events(bundle)["earshot.audio.render.stale"]
    assert int(stale.time.monotonic_time_nano) == 700_000_000
    assert stale.time.source_time_unix_nano is None  # no wall reading fabricated
    assert validate_incident(bundle).ok


# -- cross-clock latency: unavailable without a relation, estimated with one ----


def _cross_clock_bundle():
    """A turn with a SERVER event (speech.started) and a BROWSER render event."""

    clock = BrowserClockDomain(clock_domain_id="clk_session", wall_origin_unix_nano=START + SKEW)
    session = earshot.pipeline(session_id="f2-cross", started_at_unix_nano=START)
    server_domain = session.clock_domain_id
    with session.turn() as turn:
        turn.vad(speech_start_ms=100)  # a server-clock event at +100ms
        apply_audio_graph(turn, [{"type": "underrun", "timestamp_ms": 500}], clock_domain=clock)
    return session, server_domain


def test_cross_clock_latency_is_unavailable_without_a_relation() -> None:
    session, server_domain = _cross_clock_bundle()
    bundle = session.close()

    events = _events(bundle)
    server_evt = events["earshot.speech.started"]
    browser_evt = events["earshot.audio.render.stale"]
    assert server_evt.time.clock_domain_id == server_domain
    assert browser_evt.time.clock_domain_id == "clk_session"

    # No calibration exists: the analyzer refuses cross-clock comparison.
    delta = comparable_delta(server_evt.time, browser_evt.time)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_domain"
    assert bundle.profile.clock_relations == ()


def test_cross_clock_latency_becomes_estimated_with_a_relation() -> None:
    session, server_domain = _cross_clock_bundle()
    # A declared calibration: browser wall -> server wall removes the skew.
    relation = ClockRelation(
        relation_id="rel-browser-server",
        from_clock_domain_id="clk_session",
        to_clock_domain_id=server_domain,
        offset_nano=str(-SKEW),
        uncertainty_nano="500",
        method="handshake_offset",
    )
    session.register_clock_relation(relation)
    bundle = session.close()

    # The relation is carried on the incident and it validates.
    assert bundle.profile.clock_relations == (relation,)
    assert validate_incident(bundle).ok, validate_incident(bundle)

    events = _events(bundle)
    server_evt = events["earshot.speech.started"]  # server +100ms
    browser_evt = events["earshot.audio.render.stale"]  # browser +500ms

    aligner = _ClockAligner(bundle.profile.clock_relations)
    delta = comparable_delta(server_evt.time, browser_evt.time, aligner)
    assert delta.availability == "available"
    assert delta.basis == "cross_clock_calibrated"
    assert delta.confidence == "estimated"  # a calibrated cross-clock latency is never measured
    assert delta.nanoseconds == 400_000_000  # 500ms - 100ms
    assert delta.uncertainty >= 500  # the relation's own error bound is carried forward
