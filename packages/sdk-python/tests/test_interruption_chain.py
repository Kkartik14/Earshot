"""Barge-in causal-chain projection: an interruption becomes an ordered sequence.

Each turn that observed an interruption produces the canonical, ordered stages
(overlap -> intent -> classified -> cancellation_requested -> generation_stopped
-> queued_audio_discarded -> transport_stopped -> buffers_purged -> render_stopped
-> resumed -> tool_outcome). Every observed stage cites a real event, operation,
or sample and copies its exact coordinate; a missing stage is coverage with a
reason, never a fabricated timestamp. Effectiveness is the overlap -> render-stop
latency, available only when both endpoints are observed and comparable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, decode_incident_json
from earshot.contract import ClockDomain, ClockRelation, TimePoint
from earshot.validation import validate_derived_analysis, validate_incident

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]

WALL_ORIGIN = 1_800_000_000_000_000_000
FAULT_CLOCK = "fault-fixture-clock"
CLIENT_CLOCK = "client-render"
CLIENT_SKEW = 5_000

_CANONICAL_STAGES = (
    "overlap_observed",
    "intent",
    "classified",
    "cancellation_requested",
    "generation_stopped",
    "queued_audio_discarded",
    "transport_stopped",
    "buffers_purged",
    "render_stopped",
    "resumed",
    "tool_outcome",
)


def _fault(name: str):
    path = ROOT / "fixtures" / "faults" / f"{name}.incident.json"
    return decode_incident_json(path.read_bytes())


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )


def _chain(bundle):
    return _analyze(bundle).projections.turns[0].interruption_chain


def _by_stage(chain) -> dict:
    return {stage.stage: stage for stage in chain.stages}


# --- The vocabulary is complete and ordered ----------------------------------


def test_chain_carries_every_canonical_stage_once_in_order() -> None:
    chain = _chain(_fault("full_barge_in_chain"))
    assert tuple(stage.stage for stage in chain.stages) == _CANONICAL_STAGES


# --- full_barge_in_chain: every stage observed, effectiveness available -------


def test_full_chain_observes_every_stage_with_a_measured_effectiveness() -> None:
    bundle = _fault("full_barge_in_chain")
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    assert chain.turn_id == "turn-1"
    assert chain.classification == "accepted"
    stages = _by_stage(chain)
    assert all(stage.observed for stage in chain.stages)
    # Observed stages copy the exact evidence coordinate; none is fabricated.
    for stage in chain.stages:
        assert stage.evidence_id is not None
        assert stage.at_nano is not None
        assert stage.clock_domain_id == FAULT_CLOCK
        assert stage.coverage_reason is None

    assert stages["overlap_observed"].evidence_id == "event-overlap"
    assert stages["render_stopped"].evidence_id == "event-render-stopped"
    assert stages["intent"].evidence_id == "quality-interruption-intent"
    # The tool caught in the barge-in is attributed with its cancelled disposition.
    assert stages["tool_outcome"].evidence_id == "op-tool"
    assert stages["tool_outcome"].outcome == "cancelled"

    effectiveness = chain.effectiveness
    assert effectiveness.availability == "available"
    assert effectiveness.value == 100.0  # 900ms overlap -> 1000ms render-stop
    assert effectiveness.unit == "ms"
    assert effectiveness.confidence == "measured"
    assert effectiveness.evidence_ids == ("event-overlap", "event-render-stopped")
    assert validate_derived_analysis(bundle, analysis).ok


# --- barge_in: partial chain, same-clock effectiveness available --------------


def test_clean_barge_in_partial_chain_has_available_effectiveness() -> None:
    bundle = _fault("barge_in")
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    assert chain.classification == "accepted"
    stages = _by_stage(chain)
    observed = {name for name, stage in stages.items() if stage.observed}
    assert {
        "overlap_observed",
        "classified",
        "cancellation_requested",
        "queued_audio_discarded",
        "render_stopped",
    } <= observed
    # Those signals are simply absent from this fixture: coverage, not fault.
    assert not stages["transport_stopped"].observed
    assert not stages["buffers_purged"].observed
    assert stages["transport_stopped"].coverage_reason == "stage_not_observed"
    # classified cites the accept decision.
    assert stages["classified"].evidence_id == "event-interruption-accepted"

    effectiveness = chain.effectiveness
    assert effectiveness.availability == "available"
    assert effectiveness.value == 100.0
    assert effectiveness.evidence_ids == (
        "event-interruption-detected",
        "event-render-stopped",
    )
    assert validate_derived_analysis(bundle, analysis).ok


def test_barge_in_reads_model_cancel_as_the_effective_stop_when_alone() -> None:
    # With only earshot.model.cancelled present, the same event evidences both the
    # cancellation request and the effective generation stop (documented ambiguity).
    stages = _by_stage(_chain(_fault("barge_in")))
    assert stages["cancellation_requested"].evidence_id == "event-model-cancelled"
    assert stages["generation_stopped"].observed
    assert stages["generation_stopped"].evidence_id == "event-model-cancelled"


# --- false_interruption: classified false, downstream not observed ------------


def test_false_interruption_chain_is_false_and_stops_at_classified() -> None:
    bundle = _fault("false_interruption")
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    assert chain.classification == "false"
    stages = _by_stage(chain)
    assert stages["overlap_observed"].observed
    assert stages["classified"].observed  # the ignore decision
    for downstream in (
        "cancellation_requested",
        "generation_stopped",
        "queued_audio_discarded",
        "transport_stopped",
        "buffers_purged",
        "render_stopped",
    ):
        assert not stages[downstream].observed, downstream

    # No render stop was observed, so the barge-in effectiveness is not computable.
    assert chain.effectiveness.availability == "not_observed"
    assert chain.effectiveness.value is None
    assert chain.effectiveness.limitation == "target_signal_not_observed"
    assert validate_derived_analysis(bundle, analysis).ok


# --- native_s2s_interruption: accepted, minimal chain -------------------------


def test_native_s2s_chain_is_accepted_with_only_the_classify_stage() -> None:
    bundle = _fault("native_s2s_interruption")
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    assert chain.classification == "accepted"
    stages = _by_stage(chain)
    assert stages["classified"].observed
    assert not stages["overlap_observed"].observed
    # Every other stage is coverage; the native accept carries no teardown detail.
    observed = [name for name, stage in stages.items() if stage.observed]
    assert observed == ["classified"]
    # Overlap was never observed, so effectiveness has no anchor.
    assert chain.effectiveness.availability == "not_observed"
    assert chain.effectiveness.limitation == "turn_anchor_not_observed"
    assert validate_derived_analysis(bundle, analysis).ok


# --- A turn without an interruption produces no chain -------------------------


def test_turn_without_interruption_has_no_chain() -> None:
    bundle = _fault("fast_endpointing")
    analysis = _analyze(bundle)
    assert analysis.projections.turns[0].interruption_chain is None
    assert validate_derived_analysis(bundle, analysis).ok


def test_no_interruption_valid_bundle_has_no_chain(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    assert all(turn.interruption_chain is None for turn in analysis.projections.turns)


# --- Cross-clock effectiveness honors calibration -----------------------------


def _cross_clock_bundle(*, relations: tuple[ClockRelation, ...]):
    """Move the render-stop of full_barge_in_chain onto a second clock domain.

    The overlap stays on the fault clock, so the barge-in effectiveness is only
    computable through a declared ``ClockRelation`` between the two domains.
    """

    bundle = _fault("full_barge_in_chain")
    profile = bundle.profile
    client_domain = ClockDomain(
        clock_domain_id=CLIENT_CLOCK,
        kind="wall_clock",
        observer="browser",
        wall_origin_unix_nano=str(WALL_ORIGIN + CLIENT_SKEW),
        uncertainty_nano="0",
    )
    client_render_stop = TimePoint(
        source_time_unix_nano=str(WALL_ORIGIN + CLIENT_SKEW + 1_000_000_000),
        clock_domain_id=CLIENT_CLOCK,
    )
    events = tuple(
        event.model_copy(update={"time": client_render_stop})
        if event.event_id == "event-render-stopped"
        else event
        for event in profile.events
    )
    new_profile = profile.model_copy(
        update={
            "clock_domains": (*profile.clock_domains, client_domain),
            "clock_relations": relations,
            "events": events,
        }
    )
    return bundle.model_copy(update={"profile": new_profile})


def _calibration() -> ClockRelation:
    return ClockRelation(
        relation_id="rel-client-fault",
        from_clock_domain_id=CLIENT_CLOCK,
        to_clock_domain_id=FAULT_CLOCK,
        offset_nano=str(-CLIENT_SKEW),
        uncertainty_nano="500",
        method="handshake_offset",
    )


def test_cross_clock_effectiveness_is_estimated_with_a_calibration() -> None:
    bundle = _cross_clock_bundle(relations=(_calibration(),))
    assert validate_incident(bundle).ok, validate_incident(bundle)
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    # The render stop is still observed; only its coordinate moved domains.
    stages = _by_stage(chain)
    assert stages["render_stopped"].observed
    assert stages["render_stopped"].clock_domain_id == CLIENT_CLOCK

    effectiveness = chain.effectiveness
    assert effectiveness.availability == "available"
    assert effectiveness.confidence == "estimated"
    assert effectiveness.value == pytest.approx(100.0)
    assert effectiveness.unit == "ms"
    assert validate_derived_analysis(bundle, analysis).ok


def test_cross_clock_effectiveness_refuses_without_a_calibration() -> None:
    bundle = _cross_clock_bundle(relations=())
    assert validate_incident(bundle).ok, validate_incident(bundle)
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chain

    assert chain is not None
    # render_stopped is observed, but the two clocks cannot be subtracted.
    assert _by_stage(chain)["render_stopped"].observed
    effectiveness = chain.effectiveness
    assert effectiveness.availability != "available"
    assert effectiveness.value is None
    assert effectiveness.limitation == "cross_clock_domain"
    assert validate_derived_analysis(bundle, analysis).ok


# --- Determinism -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["full_barge_in_chain", "barge_in", "false_interruption", "native_s2s_interruption"],
)
def test_chain_is_deterministic_across_repeated_analysis(name: str) -> None:
    bundle = _fault(name)
    first = _analyze(bundle)
    second = _analyze(bundle)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
