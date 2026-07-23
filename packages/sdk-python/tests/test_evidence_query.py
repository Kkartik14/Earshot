from __future__ import annotations

import json
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.codec import decode_incident_json
from earshot.contract import (
    Coverage,
    Event,
    Evidence,
    Operation,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from earshot.query import (
    BOUNDARY_DIAGNOSIS_CODES,
    LATENCY_METRICS,
    EvidenceQuery,
    compare_incidents,
    detect_contradictions,
)
from incident_factory import evidence, make_valid_bundle, point

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
FAULTS = ROOT / "fixtures" / "faults"


def _fault(identifier: str):
    return decode_incident_json((FAULTS / f"{identifier}.incident.json").read_bytes())


def _clean_bundle():
    return decode_incident_json((ROOT / "fixtures" / "valid" / "minimal.json").read_bytes())


# --------------------------------------------------------------------------- #
# known_about_turn                                                            #
# --------------------------------------------------------------------------- #


def test_known_about_turn_returns_metrics_and_diagnoses() -> None:
    query = EvidenceQuery(_fault("render_delay"))
    knowledge = query.known_about_turn("turn-1")

    assert knowledge.found is True
    assert [metric["metric"] for metric in knowledge.metrics] == list(LATENCY_METRICS)

    render_metric = next(
        metric
        for metric in knowledge.metrics
        if metric["metric"] == "render_start_response_latency"
    )
    assert render_metric["availability"] == "available"
    assert render_metric["value"] > 0
    assert render_metric["unit"] == "ms"
    assert render_metric["evidence_ids"]

    codes = {diagnosis["code"] for diagnosis in knowledge.diagnoses}
    assert "render.delayed" in codes
    for diagnosis in knowledge.diagnoses:
        assert diagnosis["evidence_ids"]
        assert "turn-1" in diagnosis["turn_ids"]


def test_known_about_turn_reports_unknown_turn() -> None:
    query = EvidenceQuery(_fault("render_delay"))
    knowledge = query.known_about_turn("turn-does-not-exist")

    assert knowledge.found is False
    assert knowledge.metrics == ()
    assert knowledge.diagnoses == ()


# --------------------------------------------------------------------------- #
# first_abnormal_boundary                                                     #
# --------------------------------------------------------------------------- #


def _with_device_permission_denied(bundle, *, monotonic_nano: int):
    """Add a later, same-domain device fault to a transport-fault fixture."""

    device_event = Event(
        event_id="event-device-permission-denied",
        session_id="fixture-session",
        event_name="earshot.device.permission_denied",
        time=TimePoint(
            clock_domain_id="fault-fixture-clock",
            monotonic_time_nano=str(monotonic_nano),
            source_time_unix_nano=str(1_800_000_000_000_000_000 + monotonic_nano),
        ),
        turn_id="turn-1",
        evidence=Evidence(
            source="browser",
            observer="test_harness",
            method="deterministic_fault_injection",
            method_version="1",
            confidence="measured",
            availability="available",
        ),
    )
    profile = bundle.profile.model_copy(update={"events": (*bundle.profile.events, device_event)})
    return bundle.model_copy(update={"profile": profile})


def test_first_abnormal_boundary_picks_earliest_for_multi_fault() -> None:
    multi = _with_device_permission_denied(
        _fault("websocket_reconnect"), monotonic_nano=2_500_000_000
    )
    query = EvidenceQuery(multi)

    # Both boundaries are present; the earliest by canonical coordinate wins.
    codes = {diagnosis["code"] for diagnosis in query.summary().diagnoses}
    assert {"transport.reconnect", "device.unavailable"} <= codes

    boundary = query.first_abnormal_boundary()
    assert boundary.found is True
    assert boundary.code == "transport.reconnect"
    assert boundary.boundary == "transport"
    # The reconnect signal begins before the (later) device fault at 2.5s.
    assert boundary.coordinate is not None
    assert int(boundary.coordinate["at_nano"]) < 2_500_000_000
    assert all(code in BOUNDARY_DIAGNOSIS_CODES for code in {boundary.code})


def test_first_abnormal_boundary_none_for_clean_incident() -> None:
    boundary = EvidenceQuery(_clean_bundle()).first_abnormal_boundary()
    assert boundary.found is False
    assert boundary.reason == "no_boundary_diagnosis"


def test_first_abnormal_boundary_unknown_across_incomparable_clocks() -> None:
    # A transport fault on the fixture clock and a device fault on a second,
    # uncalibrated clock cannot be ordered against each other: say unknown.
    bundle = _fault("websocket_reconnect")
    clock = bundle.profile.clock_domains[0]
    other_clock = clock.model_copy(update={"clock_domain_id": "second-clock"})
    device_event = Event(
        event_id="event-device-permission-denied",
        session_id="fixture-session",
        event_name="earshot.device.permission_denied",
        time=TimePoint(
            clock_domain_id="second-clock",
            monotonic_time_nano="500000000",
            source_time_unix_nano="1800000000500000000",
        ),
        turn_id="turn-1",
        evidence=Evidence(
            source="browser",
            observer="test_harness",
            method="deterministic_fault_injection",
            method_version="1",
            confidence="measured",
            availability="available",
        ),
    )
    profile = bundle.profile.model_copy(
        update={
            "clock_domains": (*bundle.profile.clock_domains, other_clock),
            "events": (*bundle.profile.events, device_event),
        }
    )
    query = EvidenceQuery(bundle.model_copy(update={"profile": profile}))

    boundary = query.first_abnormal_boundary()
    assert boundary.found is False
    assert boundary.reason == "boundaries_span_incomparable_clocks"


# --------------------------------------------------------------------------- #
# not_observed / recomputable                                                 #
# --------------------------------------------------------------------------- #


def test_not_observed_unifies_coverage_gaps() -> None:
    not_observed = EvidenceQuery(_fault("device_unavailable")).not_observed()
    signals = {gap["signal"] for gap in not_observed.coverage_gaps}
    assert signals == {"device.microphone", "capture", "client.render"}
    assert all(gap["availability"] != "available" for gap in not_observed.coverage_gaps)


def test_recomputable_resolves_diagnosis_and_metric_references() -> None:
    query = EvidenceQuery(_fault("render_delay"))
    diagnosis_id = query.summary().diagnoses[0]["diagnosis_id"]

    diagnosis_answer = query.recomputable(diagnosis_id)
    assert diagnosis_answer.found is True
    assert diagnosis_answer.kind == "diagnosis"
    assert diagnosis_answer.recomputable is True
    assert diagnosis_answer.missing_evidence_ids == ()

    metric_answer = query.recomputable("turn-1/render_start_response_latency")
    assert metric_answer.found is True
    assert metric_answer.kind == "metric"
    assert metric_answer.recomputable is True

    unknown = query.recomputable("not-a-real-reference")
    assert unknown.found is False
    assert unknown.recomputable is False


# --------------------------------------------------------------------------- #
# detect_contradictions                                                       #
# --------------------------------------------------------------------------- #


def test_detect_contradictions_finds_duplicate_and_out_of_order() -> None:
    contradictions = detect_contradictions(_fault("websocket_reconnect"))
    by_kind = {contradiction.kind: contradiction for contradiction in contradictions}

    assert "duplicate_delivery" in by_kind
    assert "out_of_order_delivery" in by_kind
    assert by_kind["duplicate_delivery"].evidence_ids == ("event-message-duplicate",)
    assert by_kind["out_of_order_delivery"].evidence_ids == ("event-message-out-of-order",)
    assert by_kind["duplicate_delivery"].boundary == "transport"


def test_detect_contradictions_finds_render_claim_conflict() -> None:
    bundle = make_valid_bundle()
    coverage = (
        Coverage(
            signal="client.render",
            availability="not_observed",
            reason="app_backgrounded",
        ),
    )
    conflicted = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"coverage": coverage})}
    )

    contradictions = detect_contradictions(conflicted)
    render = [c for c in contradictions if c.kind == "render_claim_conflict"]
    assert len(render) == 1
    assert render[0].turn_id == "turn-1"
    # The conflict cites the render evidence that says render *did* happen.
    assert set(render[0].evidence_ids) == {"op-render", "evt-render"}


def _rtt_sample(sample_id: str, observer: str, value: float, uncertainty: float):
    return QualitySample(
        sample_id=sample_id,
        session_id="session-1",
        quality_kind="transport.quality",
        sample_window=TimeRange(start=point(1), end=point(2)),
        measurements=(
            QualityMeasurement(name="earshot.metric.round_trip_time", value=value, unit="ms"),
            QualityMeasurement(
                name="earshot.metric.round_trip_time.uncertainty",
                value=uncertainty,
                unit="ms",
            ),
        ),
        evidence=Evidence(
            source="webrtc_stats",
            observer=observer,
            method="getStats",
            confidence="measured",
            availability="available",
        ),
        participant_id="participant-user",
        stream_id="stream-input",
        attributes={"earshot.turn.id": "turn-1"},
    )


def _bundle_with_samples(samples):
    bundle = make_valid_bundle()
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"quality_samples": samples})}
    )


def test_detect_contradictions_provider_client_disagreement_beyond_uncertainty() -> None:
    # 100ms vs 180ms with +/-5ms each: 80 > 10, so the observers truly disagree.
    bundle = _bundle_with_samples(
        (
            _rtt_sample("q-server", "server", 100.0, 5.0),
            _rtt_sample("q-client", "browser", 180.0, 5.0),
        )
    )
    contradictions = [
        c for c in detect_contradictions(bundle) if c.kind == "provider_client_disagreement"
    ]
    assert len(contradictions) == 1
    assert contradictions[0].evidence_ids == ("q-client", "q-server")
    assert contradictions[0].turn_id == "turn-1"


def test_detect_contradictions_honors_uncertainty_within_bounds() -> None:
    # 100ms vs 130ms with +/-40ms each: 30 <= 80, so it is within uncertainty.
    bundle = _bundle_with_samples(
        (
            _rtt_sample("q-server", "server", 100.0, 40.0),
            _rtt_sample("q-client", "browser", 130.0, 40.0),
        )
    )
    contradictions = [
        c for c in detect_contradictions(bundle) if c.kind == "provider_client_disagreement"
    ]
    assert contradictions == []


def _scalar_sample(
    sample_id: str,
    observer: str,
    name: str,
    value: float,
    unit: str,
    *,
    uncertainty: float | None = None,
):
    measurements = [QualityMeasurement(name=name, value=value, unit=unit)]
    if uncertainty is not None:
        measurements.append(
            QualityMeasurement(name=f"{name}.uncertainty", value=uncertainty, unit=unit)
        )
    return QualitySample(
        sample_id=sample_id,
        session_id="session-1",
        quality_kind="transport.quality",
        sample_window=TimeRange(start=point(1), end=point(2)),
        measurements=tuple(measurements),
        evidence=Evidence(
            source="webrtc_stats",
            observer=observer,
            method="getStats",
            confidence="measured",
            availability="available",
        ),
        participant_id="participant-user",
        stream_id="stream-input",
        attributes={"earshot.turn.id": "turn-1"},
    )


# F6(c): absent uncertainty is UNKNOWN, never an exact 0 that fakes precision.
def test_missing_uncertainty_is_unknown_not_a_fabricated_zero() -> None:
    # 100ms vs 180ms, but NEITHER sample states an uncertainty. The old code read
    # the missing bound as 0 and reported a disagreement "beyond uncertainty"; an
    # unknown bound cannot prove a disagreement, so nothing is asserted.
    bundle = _bundle_with_samples(
        (
            _scalar_sample("q-server", "server", "earshot.metric.round_trip_time", 100.0, "ms"),
            _scalar_sample("q-client", "browser", "earshot.metric.round_trip_time", 180.0, "ms"),
        )
    )
    contradictions = [
        c for c in detect_contradictions(bundle) if c.kind == "provider_client_disagreement"
    ]
    assert contradictions == []


# F6(d): measurements whose bases differ are incomparable, never compared.
def test_incompatible_measurement_bases_are_not_compared() -> None:
    # Same metric name, two observers, but one is in ms and the other a raw count.
    # Both carry a known uncertainty (so F6(c) does not short-circuit); the old
    # code dropped the unit and compared 100 against 5000 into a fake disagreement.
    bundle = _bundle_with_samples(
        (
            _scalar_sample(
                "q-server", "server", "earshot.metric.round_trip_time", 100.0, "ms", uncertainty=1.0
            ),
            _scalar_sample(
                "q-client",
                "browser",
                "earshot.metric.round_trip_time",
                5000.0,
                "count",
                uncertainty=1.0,
            ),
        )
    )
    contradictions = [
        c for c in detect_contradictions(bundle) if c.kind == "provider_client_disagreement"
    ]
    assert contradictions == []


# F6(e): a derived analysis from another incident must not be trusted.
def test_evidence_query_rejects_stale_analysis_from_another_incident() -> None:
    from earshot.analysis import analyze_incident
    from earshot.codec import analysis_input_sha256

    incident = _fault("render_delay")
    other = _fault("barge_in")
    foreign = analyze_incident(
        other, input_sha256=analysis_input_sha256(other), generated_at_unix_nano="0"
    )
    with pytest.raises(ValueError):
        EvidenceQuery(incident, foreign)
    with pytest.raises(ValueError):
        detect_contradictions(incident, foreign)


def test_evidence_query_accepts_matching_analysis() -> None:
    from earshot.analysis import analyze_incident
    from earshot.codec import analysis_input_sha256

    incident = _fault("render_delay")
    matching = analyze_incident(
        incident, input_sha256=analysis_input_sha256(incident), generated_at_unix_nano="0"
    )
    query = EvidenceQuery(incident, matching)
    assert query.summary().counts["turn_count"] >= 1


def test_detect_contradictions_ignores_same_observer_disagreement() -> None:
    bundle = _bundle_with_samples(
        (
            _rtt_sample("q-a", "server", 100.0, 1.0),
            _rtt_sample("q-b", "server", 180.0, 1.0),
        )
    )
    contradictions = [
        c for c in detect_contradictions(bundle) if c.kind == "provider_client_disagreement"
    ]
    assert contradictions == []


def test_detect_contradictions_finds_same_domain_time_reversed() -> None:
    # Contradiction detection is a read-only lens: it can probe a suspect graph
    # whose operation interval runs backwards within one clock domain.
    reversed_operation = Operation(
        operation_id="op-reversed",
        session_id="session-1",
        operation_name="llm",
        status="ok",
        started_at=point(500_000_000),
        ended_at=point(400_000_000),
        turn_id="turn-1",
        evidence=evidence(source="pipecat", method="native_otel"),
    )
    bundle = make_valid_bundle()
    suspect = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"operations": (*bundle.profile.operations, reversed_operation)}
            )
        }
    )
    contradictions = [
        c for c in detect_contradictions(suspect) if c.kind == "same_domain_time_reversed"
    ]
    assert len(contradictions) == 1
    assert contradictions[0].evidence_ids == ("op-reversed",)


def test_detect_contradictions_empty_on_clean_fixtures() -> None:
    assert detect_contradictions(_clean_bundle()) == []
    assert detect_contradictions(make_valid_bundle()) == []
    assert detect_contradictions(_fault("webrtc_degradation")) == []


# --------------------------------------------------------------------------- #
# compare_incidents                                                           #
# --------------------------------------------------------------------------- #


def _degraded_incident():
    """A known-good session, but with network QoS added and render delayed."""

    known = make_valid_bundle()
    qos = QualitySample(
        sample_id="q-net",
        session_id="session-1",
        quality_kind="transport.quality",
        sample_window=TimeRange(start=point(1), end=point(2)),
        measurements=(
            QualityMeasurement(name="packet_loss_ratio", value=0.2, unit="1"),
            QualityMeasurement(name="jitter", value=50.0, unit="ms"),
            QualityMeasurement(name="round_trip_time", value=200.0, unit="ms"),
        ),
        evidence=Evidence(
            source="webrtc_stats",
            observer="browser",
            method="getStats",
            confidence="measured",
            availability="available",
        ),
        participant_id="participant-user",
        stream_id="stream-input",
        attributes={"earshot.turn.id": "turn-1"},
    )
    operations = tuple(
        operation.model_copy(
            update={"started_at": point(4_000_000_000), "ended_at": point(4_200_000_000)}
        )
        if operation.operation_id == "op-render"
        else operation
        for operation in known.profile.operations
    )
    events = tuple(
        event.model_copy(update={"time": point(4_020_000_000)})
        for event in known.profile.events
        if event.event_id != "evt-token"  # drop first-token -> not_observed
        if event.event_id == "evt-render"
    ) + tuple(
        event for event in known.profile.events if event.event_id not in {"evt-token", "evt-render"}
    )
    profile = known.profile.model_copy(
        update={
            "operations": operations,
            "events": events,
            "quality_samples": (qos,),
        }
    )
    return known.model_copy(update={"profile": profile})


def test_compare_incidents_reports_new_diagnosis_and_regression() -> None:
    known = make_valid_bundle()
    incident = _degraded_incident()
    comparison = compare_incidents(incident, known)

    added_codes = {entry["code"] for entry in comparison.diagnoses_added}
    assert "network.degraded" in added_codes

    render_delta = next(
        entry
        for entry in comparison.turn_metric_deltas
        if entry["metric"] == "render_start_response_latency"
    )
    assert render_delta["turn_id"] == "turn-1"
    assert render_delta["delta"] > 0  # a real, evidence-backed latency regression
    assert render_delta["unit"] == "ms"

    assert comparison.unmatched_turns == {
        "only_in_incident": [],
        "only_in_known_good": [],
    }


def test_compare_incidents_reports_availability_change_not_fabricated_delta() -> None:
    known = make_valid_bundle()
    incident = _degraded_incident()
    comparison = compare_incidents(incident, known)

    change = next(
        entry
        for entry in comparison.turn_metric_availability_changes
        if entry["metric"] == "first_token_latency"
    )
    assert change["known_good_availability"] == "available"
    assert change["incident_availability"] == "not_observed"

    # A metric whose availability changed must never appear as a numeric delta.
    delta_metrics = {entry["metric"] for entry in comparison.turn_metric_deltas}
    assert "first_token_latency" not in delta_metrics


def test_compare_incidents_reports_unmatched_turns() -> None:
    known = make_valid_bundle()
    incident = make_valid_bundle()
    # Retag the incident's turn so it no longer corresponds to the known-good.
    events = tuple(
        event.model_copy(update={"turn_id": "turn-2"}) for event in incident.profile.events
    )
    operations = tuple(
        operation.model_copy(update={"turn_id": "turn-2"})
        if operation.turn_id == "turn-1"
        else operation
        for operation in incident.profile.operations
    )
    operations = tuple(
        operation.model_copy(
            update={"attributes": {**operation.attributes, "earshot.turn.id": "turn-2"}}
        )
        if operation.attributes.get("earshot.turn.id") == "turn-1"
        else operation
        for operation in operations
    )
    profile = incident.profile.model_copy(update={"events": events, "operations": operations})
    retagged = incident.model_copy(update={"profile": profile})

    comparison = compare_incidents(retagged, known)
    assert comparison.unmatched_turns["only_in_incident"] == ["turn-2"]
    assert comparison.unmatched_turns["only_in_known_good"] == ["turn-1"]


# --------------------------------------------------------------------------- #
# determinism                                                                 #
# --------------------------------------------------------------------------- #


def test_query_surface_is_deterministic() -> None:
    incident = _degraded_incident()
    known = make_valid_bundle()

    first_query = EvidenceQuery(incident)
    second_query = EvidenceQuery(incident)

    def dumps(value) -> str:
        return json.dumps(value, sort_keys=True)

    assert dumps(first_query.summary().as_dict()) == dumps(second_query.summary().as_dict())
    assert dumps([c.as_dict() for c in first_query.contradictions()]) == dumps(
        [c.as_dict() for c in second_query.contradictions()]
    )
    assert dumps(compare_incidents(incident, known).as_dict()) == dumps(
        compare_incidents(incident, known).as_dict()
    )


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_cli_query_contradictions_and_diff(capsys) -> None:
    render_delay = str(FAULTS / "render_delay.incident.json")
    websocket = str(FAULTS / "websocket_reconnect.incident.json")
    webrtc = str(FAULTS / "webrtc_degradation.incident.json")
    minimal = str(ROOT / "fixtures" / "valid" / "minimal.json")

    assert main(["query", render_delay]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["turn_count"] == 1
    assert summary["first_abnormal_boundary"]["code"] == "render.delayed"

    assert main(["query", render_delay, "--turn", "turn-1"]) == 0
    knowledge = json.loads(capsys.readouterr().out)
    assert knowledge["found"] is True
    assert [metric["metric"] for metric in knowledge["metrics"]] == list(LATENCY_METRICS)

    assert main(["contradictions", websocket]) == 0
    contradictions = json.loads(capsys.readouterr().out)["contradictions"]
    kinds = {entry["kind"] for entry in contradictions}
    assert {"duplicate_delivery", "out_of_order_delivery"} <= kinds

    assert main(["diff", webrtc, minimal]) == 0
    comparison = json.loads(capsys.readouterr().out)
    added_codes = {entry["code"] for entry in comparison["diagnoses_added"]}
    assert "network.degraded" in added_codes
