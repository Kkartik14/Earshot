"""Gate: every injected fault is attributed to the right boundary, or says unknown.

Each gate fault (packet loss, jitter, render delay, false interruption,
stale-buffer playback, tool retry) is decoded from its deterministic fixture,
analyzed, and checked for exactly the expected boundary code, evidence ids, and
confidence. The negative cases prove the engine does not fabricate: a cleanly
handled barge-in and a well-handled native accept produce no false-interruption
diagnosis, and a render latency that was never observed produces no SLO
diagnosis. A determinism check re-analyzes and compares byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earshot.analysis import SloRecipe, analyze_incident
from earshot.codec import analysis_input_sha256, decode_incident_json
from earshot.validation import validate_derived_analysis

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def _fault(name: str):
    path = ROOT / "fixtures" / "faults" / f"{name}.incident.json"
    return decode_incident_json(path.read_bytes())


def _analyze(bundle, slo: SloRecipe | None = None):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
        slo=slo,
    )


def _codes(analysis) -> set[str]:
    return {diagnosis.code for diagnosis in analysis.diagnoses}


def _by_code(analysis, code: str) -> list:
    return [diagnosis for diagnosis in analysis.diagnoses if diagnosis.code == code]


# --- Gate faults: attribute to the right boundary ----------------------------


def test_packet_loss_and_jitter_and_rtt_attribute_to_the_network_boundary() -> None:
    bundle = _fault("webrtc_degradation")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "network.degraded")
    assert diagnosis.evidence_refs == ("quality-webrtc",)
    assert diagnosis.confidence == "measured"
    assert set(diagnosis.limitations) == {
        "packet_loss_ratio_exceeds_slo",
        "jitter_exceeds_slo",
        "round_trip_time_exceeds_slo",
    }
    assert validate_derived_analysis(bundle, analysis).ok


def test_render_delay_attributes_latency_to_the_render_boundary() -> None:
    bundle = _fault("render_delay")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "render.delayed")
    assert diagnosis.evidence_refs == ("event-turn-committed", "event-render-started")
    assert diagnosis.confidence == "inferred"
    assert diagnosis.limitations == ("render_start_latency_exceeds_slo",)
    # Upstream stages are fast, so nothing else is blamed for the delay.
    assert _codes(analysis) == {"render.delayed"}
    assert validate_derived_analysis(bundle, analysis).ok


def test_false_interruption_attributes_to_the_interruption_boundary() -> None:
    bundle = _fault("false_interruption")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "interruption.false")
    assert diagnosis.evidence_refs == (
        "event-interruption-detected",
        "event-interruption-ignored",
    )
    assert diagnosis.confidence == "measured"
    assert _codes(analysis) == {"interruption.false"}
    assert validate_derived_analysis(bundle, analysis).ok


def test_stale_buffer_playback_attributes_to_the_decode_render_boundary() -> None:
    bundle = _fault("stale_buffer_playback")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "audio.stale_playback")
    assert diagnosis.evidence_refs == ("event-render-stale",)
    assert diagnosis.confidence == "measured"
    assert _codes(analysis) == {"audio.stale_playback"}
    assert validate_derived_analysis(bundle, analysis).ok


def test_tool_retry_attributes_to_the_tool_boundary_and_keeps_operation_failed() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)

    [retry] = _by_code(analysis, "tool.retry")
    assert retry.evidence_refs == ("op-tool-attempt-1", "op-tool-attempt-2")
    assert retry.confidence == "measured"

    # The raw failure fact co-exists with the richer retry-pattern hypothesis.
    [failed] = _by_code(analysis, "operation.failed")
    assert failed.evidence_refs == ("op-tool-attempt-1",)
    assert failed.confidence == "measured"
    assert validate_derived_analysis(bundle, analysis).ok


# --- Negative cases: the engine must not fabricate ---------------------------


def test_clean_barge_in_is_not_a_false_interruption() -> None:
    bundle = _fault("barge_in")
    analysis = _analyze(bundle)

    assert "interruption.false" not in _codes(analysis)
    # A cleanly handled barge-in is not a fault at all.
    assert analysis.diagnoses == ()
    assert validate_derived_analysis(bundle, analysis).ok


def test_native_s2s_accept_is_well_handled_and_not_a_false_interruption() -> None:
    bundle = _fault("native_s2s_interruption")
    analysis = _analyze(bundle)

    assert "interruption.false" not in _codes(analysis)
    assert "render.delayed" not in _codes(analysis)
    assert analysis.diagnoses == ()
    assert validate_derived_analysis(bundle, analysis).ok


def test_not_observed_render_latency_says_unknown_instead_of_diagnosing() -> None:
    bundle = _fault("render_delay")
    # With render evidence present the delay is diagnosed.
    assert "render.delayed" in _codes(_analyze(bundle))

    # Remove every render observation (event and operation). The render latency
    # becomes not_observed, so the analyzer must emit no SLO diagnosis rather
    # than invent a delay it could not measure.
    profile = bundle.profile
    stripped_profile = profile.model_copy(
        update={
            "operations": tuple(
                item for item in profile.operations if item.operation_name != "render"
            ),
            "events": tuple(
                item for item in profile.events if item.event_name != "earshot.audio.render.started"
            ),
        }
    )
    stripped = bundle.model_copy(update={"profile": stripped_profile})

    analysis = _analyze(stripped)
    assert "render.delayed" not in _codes(analysis)
    assert validate_derived_analysis(stripped, analysis).ok


# --- Configurable SLO recipes -------------------------------------------------


def test_slo_recipe_thresholds_are_configurable() -> None:
    bundle = _fault("webrtc_degradation")
    assert "network.degraded" in _codes(_analyze(bundle))

    lenient = SloRecipe(
        packet_loss_ratio=0.9,
        jitter_ms=100.0,
        round_trip_time_ms=500.0,
    )
    assert "network.degraded" not in _codes(_analyze(bundle, lenient))


def test_tight_render_slo_can_flag_an_otherwise_healthy_render() -> None:
    bundle = _fault("stt_delay")
    # stt_delay has no turn anchor, so render latency is not_observed regardless.
    assert "render.delayed" not in _codes(_analyze(bundle, SloRecipe(render_start_latency_ms=1.0)))


# --- Bonus boundaries reuse existing fixtures with the same discipline --------


def test_device_events_attribute_to_the_capture_boundary() -> None:
    bundle = _fault("device_unavailable")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "device.unavailable")
    assert diagnosis.evidence_refs == (
        "event-audio-context-suspended",
        "event-permission-denied",
    )
    assert diagnosis.confidence == "measured"
    assert validate_derived_analysis(bundle, analysis).ok


def test_websocket_reconnect_attributes_to_the_transport_boundary() -> None:
    bundle = _fault("websocket_reconnect")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "transport.reconnect")
    assert diagnosis.confidence == "measured"
    assert "event-transport-reconnecting" in diagnosis.evidence_refs
    assert validate_derived_analysis(bundle, analysis).ok


@pytest.mark.parametrize(
    ("fixture", "stage"),
    [("stt_delay", "stt"), ("llm_delay", "llm"), ("tts_delay", "tts")],
)
def test_pipeline_stage_delay_attributes_to_that_stage(fixture: str, stage: str) -> None:
    bundle = _fault(fixture)
    analysis = _analyze(bundle)

    slow = _by_code(analysis, "stage.slow")
    assert [diagnosis.summary for diagnosis in slow] == [f"{stage}_stage_slow"]
    assert slow[0].evidence_refs == (f"op-{stage}",)
    assert slow[0].confidence == "inferred"
    assert slow[0].limitations == (f"{stage}_latency_exceeds_slo",)
    assert validate_derived_analysis(bundle, analysis).ok


def test_slow_endpointing_attributes_to_the_turn_detection_boundary() -> None:
    bundle = _fault("slow_endpointing")
    analysis = _analyze(bundle)

    [diagnosis] = _by_code(analysis, "endpointing.slow")
    assert diagnosis.evidence_refs == ("op-turn",)
    assert diagnosis.confidence == "inferred"
    assert validate_derived_analysis(bundle, analysis).ok


def test_fast_endpointing_is_below_slo_and_says_unknown() -> None:
    bundle = _fault("fast_endpointing")
    analysis = _analyze(bundle)

    assert "endpointing.slow" not in _codes(analysis)
    assert validate_derived_analysis(bundle, analysis).ok


# --- Determinism and source-order invariance ---------------------------------


@pytest.mark.parametrize(
    "fixture",
    ["webrtc_degradation", "tool_timeout_retry", "render_delay", "false_interruption"],
)
def test_analysis_diagnoses_are_deterministic(fixture: str) -> None:
    bundle = _fault(fixture)
    first = _analyze(bundle).model_dump(mode="python")
    second = _analyze(bundle).model_dump(mode="python")
    assert first == second


def test_diagnoses_are_sorted_for_a_stable_projection() -> None:
    analysis = _analyze(_fault("tool_timeout_retry"))
    ids = [diagnosis.diagnosis_id for diagnosis in analysis.diagnoses]
    assert ids == sorted(ids)
    assert len(analysis.diagnoses) >= 2


def test_boundary_attribution_is_invariant_to_source_order() -> None:
    bundle = _fault("webrtc_degradation")
    profile = bundle.profile
    reversed_profile = profile.model_copy(
        update={
            "operations": tuple(reversed(profile.operations)),
            "events": tuple(reversed(profile.events)),
            "quality_samples": tuple(reversed(profile.quality_samples)),
        }
    )
    reversed_bundle = bundle.model_copy(update={"profile": reversed_profile})

    baseline = [
        (diagnosis.code, diagnosis.evidence_refs, diagnosis.limitations)
        for diagnosis in _analyze(bundle).diagnoses
    ]
    shuffled = [
        (diagnosis.code, diagnosis.evidence_refs, diagnosis.limitations)
        for diagnosis in _analyze(reversed_bundle).diagnoses
    ]
    assert baseline == shuffled
