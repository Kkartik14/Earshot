from __future__ import annotations

import earshot
from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.explanation import explain_incident
from incident_factory import point
from test_contract_validation import replace_profile


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=1,
    )


def test_explanation_publishes_only_observed_operation_intervals(valid_bundle) -> None:
    explanation = explain_incident(valid_bundle, _analyze(valid_bundle))

    [turn] = explanation.turns
    llm = next(item for item in turn.operations if item.operation_id == "op-llm")
    assert llm.shape == "interval"
    assert llm.start_nano == "1050000000"
    assert llm.end_nano == "1300000000"
    assert llm.duration_nano == "250000000"
    assert llm.time_basis == "monotonic"
    assert llm.clock_domain_id == "server-clock"
    assert llm.evidence_ids == ("op-llm",)


def test_explanation_keeps_pipeline_stage_points_as_points() -> None:
    session = earshot.pipeline(
        session_id="point-session", started_at_unix_nano=1_752_800_000_000_000_000
    )
    with session.turn() as turn:
        turn.llm("openai", ttft_ms=125)
    bundle = session.close()

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    [llm] = [item for item in turn.operations if item.operation_name == "llm"]
    assert llm.shape == "point"
    assert llm.end_nano is None
    assert llm.duration_nano is None
    assert llm.limitation == "end_boundary_not_observed"


def test_explanation_keeps_exact_nanos_and_provenance(valid_bundle) -> None:
    explanation = explain_incident(valid_bundle, _analyze(valid_bundle))

    [turn] = explanation.turns
    rendered = next(item for item in turn.events if item.event_id == "evt-render")
    assert rendered.at_nano == "1720000000"
    assert rendered.clock_domain_id == "server-clock"
    assert rendered.evidence is not None
    assert rendered.evidence.source == "web_audio"
    assert rendered.evidence.confidence == "estimated"
    assert explanation.analyzer_version == ANALYZER_VERSION
    assert explanation.finality == "final"
    assert explanation.completeness == "complete"
    assert explanation.session_status == "completed"
    assert explanation.coverage[0].signal == "client.render"
    assert explanation.coverage[0].availability == "available"


def test_explanation_authors_stage_measurement_associations() -> None:
    session = earshot.pipeline(
        session_id="measurement-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn() as turn:
        turn.llm("openai", ttft_ms=250)
    bundle = session.close()

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    llm = next(item for item in turn.operations if item.operation_name == "llm")
    assert any(item.name == "earshot.llm.ttft" for item in llm.measurements)
    ttft = next(item for item in llm.measurements if item.name == "earshot.llm.ttft")
    assert ttft.unit == "ms"
    assert ttft.value == 250
    assert ttft.evidence_ids


def test_explanation_orders_comparable_stages_by_evidence_time(valid_bundle) -> None:
    template = valid_bundle.profile.operations[0]
    early = template.model_copy(
        update={
            "operation_id": "stage-2",
            "operation_name": "stt",
            "span_id": "6" * 16,
            "started_at": point(1_000_000_000),
            "ended_at": point(1_100_000_000),
        }
    )
    later = template.model_copy(
        update={
            "operation_id": "stage-10",
            "operation_name": "llm",
            "span_id": "7" * 16,
            "started_at": point(2_000_000_000),
            "ended_at": point(2_100_000_000),
        }
    )
    bundle = replace_profile(
        valid_bundle,
        operations=(later, early),
        events=(),
        quality_samples=(),
    )

    analysis = _analyze(bundle)
    [projected_turn] = analysis.projections.turns
    reversed_turn = projected_turn.model_copy(update={"operation_ids": ("stage-10", "stage-2")})
    analysis = analysis.model_copy(
        update={"projections": analysis.projections.model_copy(update={"turns": (reversed_turn,)})}
    )

    explanation = explain_incident(bundle, analysis)

    [turn] = explanation.turns
    assert [operation.operation_id for operation in turn.operations] == ["stage-2", "stage-10"]


def test_explanation_assigns_retry_measurements_to_their_exact_operation() -> None:
    session = earshot.pipeline(
        session_id="retry-measurement-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-retry") as turn:
        turn.stt("deepgram", ttfb_ms=100)
        turn.stt("deepgram", ttfb_ms=900)
    bundle = session.close()

    explanation = explain_incident(bundle, _analyze(bundle))

    [explained_turn] = explanation.turns
    stt_operations = [
        operation for operation in explained_turn.operations if operation.operation_name == "stt"
    ]
    expected = {
        sample.attributes["earshot.operation.id"]: sample.measurements[0].value
        for sample in bundle.profile.quality_samples
        if sample.measurements[0].name == "earshot.stt.ttfb"
    }
    assert {
        operation.operation_id: [measurement.value for measurement in operation.measurements]
        for operation in stt_operations
    } == {operation_id: [value] for operation_id, value in expected.items()}


def test_explanation_omits_ambiguous_turn_stage_measurement_fallback() -> None:
    session = earshot.pipeline(
        session_id="ambiguous-measurement-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-ambiguous") as turn:
        turn.stt("deepgram", ttfb_ms=100)
        turn.stt("deepgram", ttfb_ms=900)
    bundle = session.close()
    samples = tuple(
        sample.model_copy(
            update={
                "attributes": {
                    key: value
                    for key, value in sample.attributes.items()
                    if key != "earshot.operation.id"
                }
            }
        )
        for sample in bundle.profile.quality_samples
    )
    bundle = replace_profile(bundle, quality_samples=samples)

    explanation = explain_incident(bundle, _analyze(bundle))

    [explained_turn] = explanation.turns
    assert all(
        not operation.measurements
        for operation in explained_turn.operations
        if operation.operation_name == "stt"
    )
