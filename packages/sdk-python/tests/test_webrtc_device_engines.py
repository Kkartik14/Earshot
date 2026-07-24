"""Gate: the server-side WebRTC and device/audio-graph engines turn raw browser
telemetry into governed facts that the existing T2 boundary rules diagnose.

The pure ``analyze_*`` functions are exercised directly (delta correctness,
missing-member discipline, counter-reset handling, determinism), and the recorder
seam is exercised end to end: raw ``getStats`` snapshots + device events ->
recorded incident -> ``analyze_incident`` -> ``network.degraded`` /
``transport.reconnect`` / ``device.unavailable`` / ``audio.stale_playback``.
"""

from __future__ import annotations

import pytest

import earshot
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.engines.device import DeviceFacts, analyze_audio_graph, apply_audio_graph
from earshot.engines.webrtc import WebRtcFacts, analyze_webrtc_stats, apply_webrtc_stats
from earshot.validation import validate_derived_analysis, validate_incident

pytestmark = pytest.mark.unit

START = 1_800_000_000_000_000_000


# -- builders ------------------------------------------------------------------


def _inbound(
    *,
    received: int | None = None,
    lost: int | None = None,
    jitter: float | None = None,
    buffer_delay: float | None = None,
    emitted: int | None = None,
    concealed: int | None = None,
    total: int | None = None,
) -> dict:
    stat: dict = {"type": "inbound-rtp", "kind": "audio"}
    if received is not None:
        stat["packetsReceived"] = received
    if lost is not None:
        stat["packetsLost"] = lost
    if jitter is not None:
        stat["jitter"] = jitter
    if buffer_delay is not None:
        stat["jitterBufferDelay"] = buffer_delay
    if emitted is not None:
        stat["jitterBufferEmittedCount"] = emitted
    if concealed is not None:
        stat["concealedSamples"] = concealed
    if total is not None:
        stat["totalSamplesReceived"] = total
    return stat


def _transport(ice: str, pair_id: str = "CP1") -> dict:
    return {"type": "transport", "iceState": ice, "selectedCandidatePairId": pair_id}


def _route(pair_id: str, network: str) -> dict:
    return {
        pair_id: {
            "type": "candidate-pair",
            "nominated": True,
            "state": "succeeded",
            "localCandidateId": f"L{pair_id}",
        },
        f"L{pair_id}": {"type": "local-candidate", "networkType": network},
    }


def _snap(timestamp_ms: float, stats: dict) -> dict:
    return {"timestamp_ms": timestamp_ms, "stats": stats}


def _record(*appliers) -> earshot.IncidentBundle:
    session = earshot.pipeline(session_id="engine-test", started_at_unix_nano=START)
    with session.turn() as turn:
        for applier in appliers:
            applier(turn)
    return session.close()


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000009000000000",
    )


def _codes(analysis) -> set[str]:
    return {diagnosis.code for diagnosis in analysis.diagnoses}


def _named(facts: WebRtcFacts | DeviceFacts, name: str) -> list:
    return [m for m in facts.measurements if m.name == name]


# -- WebRTC: delta correctness + network.degraded ------------------------------


def test_rising_loss_jitter_rtt_emit_correct_deltas() -> None:
    snapshots = [
        _snap(
            1000,
            {
                "IT": _inbound(received=1000, lost=5, jitter=0.010),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.080},
            },
        ),
        _snap(
            2000,
            {
                "IT": _inbound(received=1100, lost=55, jitter=0.050),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.220},
            },
        ),
    ]
    facts = analyze_webrtc_stats(snapshots)

    # Loss over the interval: dLost=50, dReceived=100 -> 50 / (100 + 50).
    [loss] = _named(facts, "packet_loss_ratio")
    assert loss.value == pytest.approx(50 / 150)
    assert loss.unit == "1"
    assert loss.at_ms == 1000.0
    # Jitter and RTT are instantaneous per-snapshot readings, seconds -> ms.
    jitters = sorted(m.value for m in _named(facts, "jitter"))
    assert jitters == pytest.approx([10.0, 50.0])
    assert {m.unit for m in _named(facts, "jitter")} == {"ms"}
    rtts = sorted(m.value for m in _named(facts, "round_trip_time"))
    assert rtts == pytest.approx([80.0, 220.0])


@pytest.mark.integration
def test_degradation_incident_produces_network_degraded_citing_the_sample() -> None:
    snapshots = [
        _snap(
            1000,
            {
                "IT": _inbound(received=1000, lost=5, jitter=0.010),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.080},
            },
        ),
        _snap(
            2000,
            {
                "IT": _inbound(received=1100, lost=200, jitter=0.050),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.220},
            },
        ),
    ]
    bundle = _record(lambda turn: apply_webrtc_stats(turn, snapshots))
    assert validate_incident(bundle).ok

    analysis = _analyze(bundle)
    degraded = [d for d in analysis.diagnoses if d.code == "network.degraded"]
    assert degraded, _codes(analysis)
    # Every network.degraded diagnosis cites a real recorded quality sample id.
    sample_ids = {sample.sample_id for sample in bundle.profile.quality_samples}
    assert all(set(d.evidence_refs) <= sample_ids for d in degraded)
    assert validate_derived_analysis(bundle, analysis).ok


def test_jitter_buffer_growth_is_detected() -> None:
    # Per-interval average buffer delay rises 20ms -> 50ms -> 90ms.
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, lost=0, buffer_delay=0.0, emitted=0)}),
        _snap(1000, {"IT": _inbound(received=2000, lost=0, buffer_delay=2.0, emitted=100)}),
        _snap(2000, {"IT": _inbound(received=3000, lost=0, buffer_delay=7.0, emitted=200)}),
        _snap(3000, {"IT": _inbound(received=4000, lost=0, buffer_delay=16.0, emitted=300)}),
    ]
    facts = analyze_webrtc_stats(snapshots)

    assert facts.jitter_buffer_growth is True
    values = [round(m.value, 3) for m in _named(facts, "jitter_buffer_delay")]
    assert values == [20.0, 50.0, 90.0]


def test_steady_jitter_buffer_does_not_grow() -> None:
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, buffer_delay=0.0, emitted=0)}),
        _snap(1000, {"IT": _inbound(received=2000, buffer_delay=2.0, emitted=100)}),
        _snap(2000, {"IT": _inbound(received=3000, buffer_delay=4.0, emitted=200)}),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.jitter_buffer_growth is False


@pytest.mark.integration
def test_high_jitter_buffer_delay_alone_is_not_network_degraded() -> None:
    # A 90ms de-jitter buffer with healthy inter-arrival jitter and no loss must
    # not be misread as excess jitter: it says nothing about the network SLO.
    snapshots = [
        _snap(
            0, {"IT": _inbound(received=1000, lost=0, jitter=0.005, buffer_delay=0.0, emitted=0)}
        ),
        _snap(
            1000,
            {"IT": _inbound(received=2000, lost=0, jitter=0.005, buffer_delay=9.0, emitted=100)},
        ),
    ]
    bundle = _record(lambda turn: apply_webrtc_stats(turn, snapshots))
    analysis = _analyze(bundle)
    assert "network.degraded" not in _codes(analysis)


# -- WebRTC: reconnect + route change ------------------------------------------


@pytest.mark.integration
def test_ice_disconnect_then_reconnect_fires_transport_reconnect() -> None:
    snapshots = [
        _snap(0, {"T": _transport("connected")}),
        _snap(500, {"T": _transport("disconnected")}),
        _snap(1200, {"T": _transport("connected")}),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.reconnected is True
    assert [e.name for e in facts.events] == ["earshot.transport.reconnecting"]

    bundle = _record(lambda turn: apply_webrtc_stats(turn, snapshots))
    analysis = _analyze(bundle)
    reconnect = [d for d in analysis.diagnoses if d.code == "transport.reconnect"]
    assert reconnect
    assert reconnect[0].confidence == "measured"
    assert validate_derived_analysis(bundle, analysis).ok


def test_candidate_pair_change_emits_a_route_change_event() -> None:
    snapshots = [
        _snap(
            0,
            {"T": {"type": "transport", "selectedCandidatePairId": "CP1"}, **_route("CP1", "wifi")},
        ),
        _snap(
            1000,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP2"},
                **_route("CP2", "cellular"),
            },
        ),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.route_changed is True
    assert [e.name for e in facts.events] == ["earshot.transport.route_changed"]


def test_transport_selected_pair_change_emits_route_change() -> None:
    # No pair marks itself selected/nominated; the transport names the active
    # pair. A change to that named pair is a real route change.
    snapshots = [
        _snap(
            0,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP1"},
                "CP1": {"type": "candidate-pair", "localCandidateId": "LCP1"},
                "CP2": {"type": "candidate-pair", "localCandidateId": "LCP2"},
                "LCP1": {"type": "local-candidate", "networkType": "wifi"},
                "LCP2": {"type": "local-candidate", "networkType": "cellular"},
            },
        ),
        _snap(
            1000,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP2"},
                "CP1": {"type": "candidate-pair", "localCandidateId": "LCP1"},
                "CP2": {"type": "candidate-pair", "localCandidateId": "LCP2"},
                "LCP1": {"type": "local-candidate", "networkType": "wifi"},
                "LCP2": {"type": "local-candidate", "networkType": "cellular"},
            },
        ),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.route_changed is True
    assert [e.name for e in facts.events] == ["earshot.transport.route_changed"]


def test_unrelated_candidate_pair_change_is_not_a_route_change() -> None:
    # The active pair (named by the transport) is CP2/cellular in BOTH snapshots.
    # An UNRELATED, non-selected pair CP1 flips its local networkType. The old
    # arbitrary-first-pair fallback would track CP1 and cry route change; the
    # honest resolver tracks only the selected CP2, which never changed.
    snapshots = [
        _snap(
            0,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP2"},
                "CP1": {"type": "candidate-pair", "localCandidateId": "LCP1"},
                "CP2": {"type": "candidate-pair", "localCandidateId": "LCP2"},
                "LCP1": {"type": "local-candidate", "networkType": "wifi"},
                "LCP2": {"type": "local-candidate", "networkType": "cellular"},
            },
        ),
        _snap(
            1000,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP2"},
                "CP1": {"type": "candidate-pair", "localCandidateId": "LCP1"},
                "CP2": {"type": "candidate-pair", "localCandidateId": "LCP2"},
                "LCP1": {"type": "local-candidate", "networkType": "ethernet"},
                "LCP2": {"type": "local-candidate", "networkType": "cellular"},
            },
        ),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.route_changed is False
    assert [e.name for e in facts.events] == []


def test_route_change_alone_is_not_a_reconnect() -> None:
    snapshots = [
        _snap(
            0,
            {"T": {"type": "transport", "selectedCandidatePairId": "CP1"}, **_route("CP1", "wifi")},
        ),
        _snap(
            1000,
            {
                "T": {"type": "transport", "selectedCandidatePairId": "CP2"},
                **_route("CP2", "cellular"),
            },
        ),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert facts.reconnected is False
    bundle = _record(lambda turn: apply_webrtc_stats(turn, snapshots))
    assert "transport.reconnect" not in _codes(_analyze(bundle))


# -- WebRTC: missing-member and counter-reset discipline -----------------------


def test_missing_jitter_member_yields_no_jitter_measurement() -> None:
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, lost=0)}),
        _snap(1000, {"IT": _inbound(received=2000, lost=1)}),
    ]
    facts = analyze_webrtc_stats(snapshots)

    assert _named(facts, "jitter") == []  # never a fabricated 0
    assert _named(facts, "round_trip_time") == []
    # Loss is still derivable and is emitted.
    assert len(_named(facts, "packet_loss_ratio")) == 1


def test_missing_loss_member_yields_no_packet_loss_measurement() -> None:
    # packetsLost absent in the second snapshot -> the ratio is unknown, not 0.
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, lost=0)}),
        _snap(1000, {"IT": {"type": "inbound-rtp", "kind": "audio", "packetsReceived": 2000}}),
    ]
    facts = analyze_webrtc_stats(snapshots)
    assert _named(facts, "packet_loss_ratio") == []


def test_counter_reset_drops_the_interval_with_a_coverage_note() -> None:
    # packetsReceived and packetsLost both fall: a stream reset, not negative loss.
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, lost=50)}),
        _snap(1000, {"IT": _inbound(received=10, lost=0)}),
    ]
    facts = analyze_webrtc_stats(snapshots)

    assert _named(facts, "packet_loss_ratio") == []  # no negative delta reported
    assert all(m.value >= 0 for m in facts.measurements)
    reset_notes = [c for c in facts.coverage if c.reason == "counter_reset"]
    assert reset_notes and reset_notes[0].signal == "webrtc.packet_loss"
    assert reset_notes[0].availability == "not_observed"


def test_concealment_ratio_is_a_bounded_interval_delta() -> None:
    snapshots = [
        _snap(0, {"IT": _inbound(received=1000, concealed=10, total=48000)}),
        _snap(1000, {"IT": _inbound(received=2000, concealed=490, total=96000)}),
    ]
    facts = analyze_webrtc_stats(snapshots)
    [concealment] = _named(facts, "concealment_ratio")
    assert concealment.value == pytest.approx(480 / 48000)
    assert 0.0 <= concealment.value <= 1.0


# -- Device / audio-graph engine ----------------------------------------------


@pytest.mark.integration
def test_permission_denied_fires_device_unavailable_and_marks_microphone() -> None:
    events = [{"type": "permission", "timestamp_ms": 0, "name": "microphone", "state": "denied"}]
    facts = analyze_audio_graph(events)
    assert facts.permission_denied is True
    assert any(
        c.signal == "device.microphone" and c.availability == "not_observed" for c in facts.coverage
    )

    bundle = _record(lambda turn: apply_audio_graph(turn, events))
    analysis = _analyze(bundle)
    device = [d for d in analysis.diagnoses if d.code == "device.unavailable"]
    assert device
    coverage = {c.signal: c.availability for c in bundle.profile.coverage}
    assert coverage.get("device.microphone") == "not_observed"
    assert validate_derived_analysis(bundle, analysis).ok


def test_suspended_context_emits_the_capture_boundary_event() -> None:
    events = [{"type": "audiocontext_state", "timestamp_ms": 0, "state": "suspended"}]
    facts = analyze_audio_graph(events)
    assert facts.context_suspended is True
    assert [e.name for e in facts.events] == ["earshot.device.audio_context_suspended"]


@pytest.mark.integration
def test_sample_rate_mismatch_fires_stale_playback() -> None:
    events = [
        {
            "type": "sample_rate_mismatch",
            "timestamp_ms": 0,
            "configured_hz": 48000,
            "actual_hz": 44100,
        }
    ]
    facts = analyze_audio_graph(events)
    assert facts.stale is True
    assert [e.name for e in facts.events] == ["earshot.audio.render.stale"]

    bundle = _record(lambda turn: apply_audio_graph(turn, events))
    analysis = _analyze(bundle)
    assert "audio.stale_playback" in _codes(analysis)
    assert validate_derived_analysis(bundle, analysis).ok


def test_matching_sample_rate_is_not_stale() -> None:
    events = [
        {
            "type": "sample_rate_mismatch",
            "timestamp_ms": 0,
            "configured_hz": 48000,
            "actual_hz": 48000,
        }
    ]
    assert analyze_audio_graph(events).stale is False


def test_underrun_fires_stale_playback() -> None:
    events = [{"type": "underrun", "timestamp_ms": 0, "frames": 480}]
    facts = analyze_audio_graph(events)
    assert facts.stale is True
    assert [e.name for e in facts.events] == ["earshot.audio.render.stale"]


def test_output_latency_is_estimated_and_base_latency_is_measured() -> None:
    events = [
        {
            "type": "latency",
            "timestamp_ms": 0,
            "base_latency_s": 0.005,
            "output_latency_s": 0.02,
        }
    ]
    facts = analyze_audio_graph(events)

    [base] = [m for m in facts.measurements if m.name == "audio.base_latency"]
    assert base.confidence == "measured"
    assert base.value == pytest.approx(0.005)
    [output] = [m for m in facts.measurements if m.name == "audio.output_latency"]
    # W3C: outputLatency is an estimate; confidence must reflect that.
    assert output.confidence == "estimated"
    assert output.value == pytest.approx(0.02)


def test_devicechange_touching_active_device_is_a_route_change() -> None:
    # A devicechange carrying the tracked device hash (the active input track
    # ended) is real evidence the active route changed.
    events = [{"type": "devicechange", "timestamp_ms": 0, "deviceHash": "dev_1a2b3c4d"}]
    facts = analyze_audio_graph(events)
    assert facts.route_changed is True
    assert [e.name for e in facts.events] == ["earshot.device.route_changed"]


def test_sink_change_is_an_active_output_route_change() -> None:
    events = [{"type": "sink_change", "timestamp_ms": 0, "sinkHash": "sink_9f8e7d6c"}]
    facts = analyze_audio_graph(events)
    assert facts.route_changed is True
    assert [e.name for e in facts.events] == ["earshot.device.route_changed"]


def test_unrelated_devicechange_is_not_a_route_failure() -> None:
    # A bare global devicechange (a USB drive was plugged in) touches no active
    # audio device: it must NOT be a route change / fault, only benign coverage.
    events = [{"type": "devicechange", "timestamp_ms": 0}]
    facts = analyze_audio_graph(events)
    assert facts.route_changed is False
    assert facts.events == ()
    inventory = [c for c in facts.coverage if c.signal == "device.inventory"]
    assert inventory and inventory[0].availability == "available"
    assert inventory[0].reason == "unrelated_device_change"


@pytest.mark.integration
def test_unrelated_devicechange_does_not_fire_device_unavailable() -> None:
    events = [{"type": "devicechange", "timestamp_ms": 0}]
    bundle = _record(lambda turn: apply_audio_graph(turn, events))
    assert validate_incident(bundle).ok
    analysis = _analyze(bundle)
    assert "device.unavailable" not in _codes(analysis)
    assert validate_derived_analysis(bundle, analysis).ok


# -- Determinism and clean-input discipline ------------------------------------


def test_engines_are_deterministic() -> None:
    snapshots = [
        _snap(
            0,
            {
                "IT": _inbound(received=1000, lost=5, jitter=0.01, buffer_delay=1.0, emitted=100),
                "T": _transport("connected"),
            },
        ),
        _snap(
            1000,
            {
                "IT": _inbound(received=2000, lost=60, jitter=0.05, buffer_delay=4.0, emitted=200),
                "T": _transport("disconnected"),
            },
        ),
        _snap(
            2000,
            {
                "IT": _inbound(received=3000, lost=70, jitter=0.06, buffer_delay=9.0, emitted=300),
                "T": _transport("connected"),
            },
        ),
    ]
    events = [
        {"type": "permission", "timestamp_ms": 0, "state": "denied"},
        {"type": "latency", "timestamp_ms": 10, "base_latency_s": 0.005, "output_latency_s": 0.02},
    ]
    assert analyze_webrtc_stats(snapshots) == analyze_webrtc_stats(snapshots)
    assert analyze_audio_graph(events) == analyze_audio_graph(events)


@pytest.mark.integration
def test_clean_session_produces_no_diagnosis() -> None:
    snapshots = [
        _snap(
            0,
            {
                "IT": _inbound(received=1000, lost=0, jitter=0.005, buffer_delay=1.0, emitted=100),
                "T": _transport("connected"),
                **_route("CP1", "wifi"),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.030},
            },
        ),
        _snap(
            1000,
            {
                "IT": _inbound(received=2000, lost=1, jitter=0.006, buffer_delay=2.0, emitted=200),
                "T": _transport("connected"),
                **_route("CP1", "wifi"),
                "RI": {"type": "remote-inbound-rtp", "roundTripTime": 0.030},
            },
        ),
    ]
    events = [
        {"type": "audiocontext_state", "timestamp_ms": 0, "state": "running"},
        {"type": "latency", "timestamp_ms": 10, "base_latency_s": 0.005, "output_latency_s": 0.02},
    ]
    webrtc = analyze_webrtc_stats(snapshots)
    device = analyze_audio_graph(events)
    assert not any((webrtc.reconnected, webrtc.route_changed, webrtc.jitter_buffer_growth))
    assert not any(
        (device.permission_denied, device.context_suspended, device.route_changed, device.stale)
    )

    bundle = _record(
        lambda turn: apply_webrtc_stats(turn, snapshots),
        lambda turn: apply_audio_graph(turn, events),
    )
    analysis = _analyze(bundle)
    assert analysis.diagnoses == ()
    assert validate_derived_analysis(bundle, analysis).ok


def test_malformed_telemetry_fails_open() -> None:
    # No raise on junk; simply no facts.
    assert analyze_webrtc_stats([]) == WebRtcFacts((), (), ())
    assert analyze_webrtc_stats([{"nope": 1}, "junk", 5]) == WebRtcFacts((), (), ())
    assert analyze_audio_graph([]) == DeviceFacts((), (), ())
    assert analyze_audio_graph([{"type": "unknown", "timestamp_ms": 0}]) == DeviceFacts((), (), ())


# -- Full integration: every boundary from one raw capture ---------------------


@pytest.mark.integration
def test_end_to_end_capture_yields_all_four_boundary_diagnoses() -> None:
    degrade = [
        _snap(
            0, {"IT": _inbound(received=1000, lost=5, jitter=0.010), "T": _transport("connected")}
        ),
        _snap(
            1000,
            {
                "IT": _inbound(received=1100, lost=200, jitter=0.050),
                "T": _transport("disconnected"),
            },
        ),
        _snap(
            2000,
            {"IT": _inbound(received=1300, lost=210, jitter=0.045), "T": _transport("connected")},
        ),
    ]
    devices = [
        {"type": "permission", "timestamp_ms": 0, "state": "denied"},
        {
            "type": "sample_rate_mismatch",
            "timestamp_ms": 100,
            "configured_hz": 48000,
            "actual_hz": 44100,
        },
    ]
    bundle = _record(
        lambda turn: apply_webrtc_stats(turn, degrade),
        lambda turn: apply_audio_graph(turn, devices),
    )
    assert validate_incident(bundle).ok

    analysis = _analyze(bundle)
    assert {
        "network.degraded",
        "transport.reconnect",
        "device.unavailable",
        "audio.stale_playback",
    } <= _codes(analysis)
    assert validate_derived_analysis(bundle, analysis).ok
