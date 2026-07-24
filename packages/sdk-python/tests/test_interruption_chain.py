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
from earshot.contract import (
    CausalLink,
    ClockDomain,
    ClockRelation,
    Event,
    Evidence,
    Operation,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from earshot.validation import validate_derived_analysis, validate_incident
from incident_factory import make_valid_bundle, point

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
    return _analyze(bundle).projections.turns[0].interruption_chains[0]


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
    chain = analysis.projections.turns[0].interruption_chains[0]

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


# --- F6(b): a tool is attributed only through an explicit causal link ---------


def _strip_tool_links(bundle):
    profile = bundle.profile
    operations = tuple(
        operation.model_copy(update={"links": ()})
        if operation.operation_id == "op-tool"
        else operation
        for operation in profile.operations
    )
    return bundle.model_copy(
        update={"profile": profile.model_copy(update={"operations": operations})}
    )


def test_causally_linked_tool_is_attributed_as_the_interruption_outcome() -> None:
    # op-tool carries an explicit ``cancelled_by`` edge to the cancelled agent turn.
    stages = _by_stage(_chain(_fault("full_barge_in_chain")))
    assert stages["tool_outcome"].observed
    assert stages["tool_outcome"].evidence_id == "op-tool"
    assert stages["tool_outcome"].outcome == "cancelled"


def test_same_turn_tool_without_causal_link_is_not_attributed() -> None:
    # Remove the causal edge: the tool now merely shares the turn. Co-occurrence is
    # not causality, so it must not be attributed as the interruption's outcome.
    stripped = _strip_tool_links(_fault("full_barge_in_chain"))
    tool_stage = _by_stage(_chain(stripped))["tool_outcome"]
    assert not tool_stage.observed
    assert tool_stage.coverage_reason == "no_causally_linked_tool"
    assert tool_stage.evidence_id is None
    assert tool_stage.outcome is None


# --- F6(a): two episodes in one turn are two chains, never one spliced --------


def _two_episode_bundle():
    def _ev(event_id: str, name: str, nano: int) -> Event:
        return Event(
            event_id=event_id,
            session_id="session-1",
            event_name=name,
            time=point(nano),
            turn_id="turn-1",
            participant_id="participant-user",
            evidence=Evidence(
                source="framework_otel",
                observer="server",
                method="native_span",
                confidence="measured",
                availability="available",
            ),
        )

    events = (
        # Episode 1 has an overlap but never observes a render stop.
        _ev("ep1-overlap", "earshot.interruption.detected", 900_000_000),
        _ev("ep1-accept", "earshot.interruption.accepted", 940_000_000),
        _ev("ep1-cancel", "earshot.model.cancelled", 950_000_000),
        # Episode 2 has its own overlap and its own render stop, 2s later.
        _ev("ep2-overlap", "earshot.interruption.detected", 2_900_000_000),
        _ev("ep2-accept", "earshot.interruption.accepted", 2_940_000_000),
        _ev("ep2-render-stop", "earshot.audio.render.stopped", 3_000_000_000),
    )
    bundle = make_valid_bundle()
    profile = bundle.profile.model_copy(update={"events": events, "operations": ()})
    return bundle.model_copy(update={"profile": profile})


def test_two_episodes_in_one_turn_produce_two_separated_chains() -> None:
    turn = _analyze(_two_episode_bundle()).projections.turns[0]
    chains = turn.interruption_chains
    assert len(chains) == 2

    episode_one = _by_stage(chains[0])
    episode_two = _by_stage(chains[1])

    # Episode 1 keeps its own overlap and, having no render stop of its own, has an
    # unknown effectiveness -- episode 2's render stop is never spliced onto it.
    assert episode_one["overlap_observed"].evidence_id == "ep1-overlap"
    assert not episode_one["render_stopped"].observed
    assert episode_one["render_stopped"].evidence_id is None
    assert chains[0].effectiveness.availability != "available"
    assert chains[0].effectiveness.value is None

    # Episode 2 owns its overlap and its render stop; its effectiveness is its own.
    assert episode_two["overlap_observed"].evidence_id == "ep2-overlap"
    assert episode_two["render_stopped"].evidence_id == "ep2-render-stop"
    assert chains[1].effectiveness.availability == "available"
    assert chains[1].effectiveness.value == 100.0


# --- P1#8(a): operations and samples are scoped to their own episode ----------


def _sample(
    sample_id: str,
    name: str,
    start_nano: int,
    end_nano: int,
    value: float | bool = 0.9,
) -> QualitySample:
    return QualitySample(
        sample_id=sample_id,
        session_id="session-1",
        quality_kind="interruption",
        sample_window=TimeRange(start=point(start_nano), end=point(end_nano)),
        measurements=(QualityMeasurement(name=name, value=value, unit="1"),),
        attributes={"earshot.turn.id": "turn-1"},
        evidence=Evidence(
            source="framework_otel",
            observer="server",
            method="native_span",
            confidence="measured",
            availability="available",
        ),
    )


def _operation(
    operation_id: str,
    operation_name: str,
    start_nano: int,
    end_nano: int,
    *,
    status: str = "ok",
    links: tuple[CausalLink, ...] = (),
) -> Operation:
    return Operation(
        operation_id=operation_id,
        session_id="session-1",
        operation_name=operation_name,
        status=status,
        started_at=point(start_nano),
        ended_at=point(end_nano),
        turn_id="turn-1",
        links=links,
    )


def _episode_scoping_bundle(operations, quality_samples):
    """Two barge-ins in one turn, two seconds apart, plus the given evidence."""

    def _ev(event_id: str, name: str, nano: int) -> Event:
        return Event(
            event_id=event_id,
            session_id="session-1",
            event_name=name,
            time=point(nano),
            turn_id="turn-1",
            participant_id="participant-user",
            evidence=Evidence(
                source="framework_otel",
                observer="server",
                method="native_span",
                confidence="measured",
                availability="available",
            ),
        )

    events = (
        _ev("ep1-overlap", "earshot.interruption.detected", 900_000_000),
        _ev("ep1-accept", "earshot.interruption.accepted", 940_000_000),
        _ev("ep2-overlap", "earshot.interruption.detected", 2_900_000_000),
        _ev("ep2-accept", "earshot.interruption.accepted", 2_940_000_000),
    )
    bundle = make_valid_bundle()
    profile = bundle.profile.model_copy(
        update={
            "events": events,
            "operations": operations,
            "quality_samples": quality_samples,
        }
    )
    return bundle.model_copy(update={"profile": profile})


def test_quality_sample_from_a_later_episode_is_not_read_as_an_earlier_intent() -> None:
    # The sample is measured entirely inside episode two. Before the fix, the
    # per-episode chain drew samples turn-wide, so episode ONE's intent stage cited
    # ep2-intent and carried a coordinate two seconds after its own overlap.
    bundle = _episode_scoping_bundle(
        operations=(),
        quality_samples=(
            _sample(
                "ep2-intent",
                "earshot.metric.interruption.probability",
                2_900_000_000,
                2_910_000_000,
            ),
        ),
    )
    analysis = _analyze(bundle)
    chains = analysis.projections.turns[0].interruption_chains
    assert len(chains) == 2

    episode_one = _by_stage(chains[0])["intent"]
    assert not episode_one.observed
    assert episode_one.evidence_id is None
    assert episode_one.at_nano is None

    episode_two = _by_stage(chains[1])["intent"]
    assert episode_two.observed
    assert episode_two.evidence_id == "ep2-intent"
    assert validate_derived_analysis(bundle, analysis).ok


def test_tool_from_a_later_episode_is_not_attributed_to_an_earlier_one() -> None:
    # op-ep2-tool runs, and is cancelled, entirely inside episode two. Before the
    # fix, operations were drawn turn-wide: the tool's end was after episode ONE's
    # overlap and its causal target shared the turn, so episode one claimed it as
    # its own tool_outcome.
    bundle = _episode_scoping_bundle(
        operations=(
            _operation(
                "op-ep2-agent", "agent_turn", 2_900_000_000, 3_000_000_000, status="cancelled"
            ),
            _operation(
                "op-ep2-tool",
                "tool",
                2_910_000_000,
                2_950_000_000,
                status="cancelled",
                links=(
                    CausalLink(
                        relationship="cancelled_by",
                        target_scope="internal",
                        target_operation_id="op-ep2-agent",
                    ),
                ),
            ),
        ),
        quality_samples=(),
    )
    analysis = _analyze(bundle)
    chains = analysis.projections.turns[0].interruption_chains
    assert len(chains) == 2

    episode_one = _by_stage(chains[0])["tool_outcome"]
    assert not episode_one.observed
    assert episode_one.evidence_id is None
    assert episode_one.outcome is None
    # The turn had a tool; this episode did not. That is a different fact from
    # "no tool ran at all", and the coverage reason says which.
    assert episode_one.coverage_reason == "no_tool_in_episode"

    episode_two = _by_stage(chains[1])["tool_outcome"]
    assert episode_two.observed
    assert episode_two.evidence_id == "op-ep2-tool"
    assert episode_two.outcome == "cancelled"
    assert validate_derived_analysis(bundle, analysis).ok


def test_evidence_straddling_two_episodes_is_a_limitation_not_a_guess() -> None:
    # A tool still running when the second barge-in began, and a sampling window
    # open across both, have no single owner the evidence can name. Neither episode
    # claims them; both say why.
    bundle = _episode_scoping_bundle(
        operations=(
            _operation(
                "op-straddling-tool", "tool", 2_800_000_000, 3_100_000_000, status="cancelled"
            ),
        ),
        quality_samples=(
            _sample(
                "straddling-intent",
                "earshot.metric.interruption.probability",
                2_800_000_000,
                3_100_000_000,
            ),
        ),
    )
    analysis = _analyze(bundle)
    chains = analysis.projections.turns[0].interruption_chains
    assert len(chains) == 2
    for chain in chains:
        stages = _by_stage(chain)
        assert stages["tool_outcome"].coverage_reason == "tool_not_attributable_to_episode"
        assert stages["intent"].coverage_reason == "sample_not_attributable_to_episode"
        assert stages["tool_outcome"].evidence_id is None
        assert stages["intent"].evidence_id is None
    assert validate_derived_analysis(bundle, analysis).ok


def test_single_episode_turn_still_sees_the_whole_turn() -> None:
    # With one interruption there is nothing to mis-attribute a record *to*, so the
    # projection is unchanged: the tool that began before the barge-in and was cut
    # off by it is still attributed.
    stages = _by_stage(_chain(_fault("full_barge_in_chain")))
    assert stages["tool_outcome"].evidence_id == "op-tool"
    assert stages["intent"].evidence_id == "quality-interruption-intent"


# --- barge_in: partial chain, same-clock effectiveness available --------------


def test_clean_barge_in_partial_chain_has_available_effectiveness() -> None:
    bundle = _fault("barge_in")
    analysis = _analyze(bundle)
    chain = analysis.projections.turns[0].interruption_chains[0]

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
    chain = analysis.projections.turns[0].interruption_chains[0]

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
    chain = analysis.projections.turns[0].interruption_chains[0]

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
    assert analysis.projections.turns[0].interruption_chains == ()
    assert validate_derived_analysis(bundle, analysis).ok


def test_no_interruption_valid_bundle_has_no_chain(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    assert all(turn.interruption_chains == () for turn in analysis.projections.turns)


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
    chain = analysis.projections.turns[0].interruption_chains[0]

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
    chain = analysis.projections.turns[0].interruption_chains[0]

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
