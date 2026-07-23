from __future__ import annotations

from pathlib import Path

import earshot
from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.codec import analysis_input_sha256, decode_incident_json
from earshot.contract import ErrorRecord, QualityMeasurement, QualitySample, TimeRange
from earshot.explanation import ExplainedDiagnosis, ExplainedError, explain_incident
from earshot.validation import validate_explanation
from incident_factory import LLM_SPAN_ID, ROOT_SPAN_ID, TRACE_ID, point
from test_contract_validation import replace_profile

ROOT = Path(__file__).resolve().parents[3]


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=1,
    )


def _fault(name: str):
    path = ROOT / "fixtures" / "faults" / f"{name}.incident.json"
    return decode_incident_json(path.read_bytes())


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


def test_explanation_preserves_event_operation_and_otel_identity(valid_bundle) -> None:
    events = tuple(
        event.model_copy(update={"trace_id": TRACE_ID, "span_id": LLM_SPAN_ID})
        if event.event_id == "evt-token"
        else event
        for event in valid_bundle.profile.events
    )
    bundle = replace_profile(valid_bundle, events=events)

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    token = next(event for event in turn.events if event.event_id == "evt-token")
    assert token.operation_id == "op-llm"
    assert token.trace_id == TRACE_ID
    assert token.span_id == LLM_SPAN_ID


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


def test_explanation_surfaces_failed_operation_diagnosis_with_evidence() -> None:
    bundle = _fault("tool_timeout_retry")

    explanation = explain_incident(bundle, _analyze(bundle))

    [diagnosis] = explanation.diagnoses
    assert diagnosis.code == "operation.failed"
    assert diagnosis.evidence_ids == ("op-tool-attempt-1",)
    assert diagnosis.confidence == "measured"


def test_explanation_surfaces_causal_links_with_resolvable_targets() -> None:
    bundle = _fault("tool_timeout_retry")

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    by_id = {operation.operation_id: operation for operation in turn.operations}
    retry = by_id["op-tool-attempt-2"]
    consume = by_id["op-downstream-agent"]

    [retry_link] = retry.links
    assert retry_link.relationship == "retries"
    assert retry_link.target_operation_id == "op-tool-attempt-1"
    assert retry_link.target_scope == "internal"
    assert retry_link.target_operation_id in by_id

    [consume_link] = consume.links
    assert consume_link.relationship == "consumes"
    assert consume_link.target_operation_id == "op-tool-attempt-2"
    assert consume_link.target_operation_id in by_id


def test_explanation_surfaces_trace_span_parent_identity() -> None:
    bundle = _fault("tool_timeout_retry")

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    attempt = next(item for item in turn.operations if item.operation_id == "op-tool-attempt-1")
    assert attempt.trace_id == "a" * 32
    assert attempt.span_id == "1" * 16
    assert attempt.parent_scope == "external"


def test_explanation_carries_parent_span_identity(valid_bundle) -> None:
    explanation = explain_incident(valid_bundle, _analyze(valid_bundle))

    [turn] = explanation.turns
    llm = next(item for item in turn.operations if item.operation_id == "op-llm")
    assert llm.parent_span_id == ROOT_SPAN_ID
    assert llm.parent_scope == "internal"


def test_explanation_retains_non_prefixed_owned_measurement() -> None:
    session = earshot.pipeline(
        session_id="non-prefixed-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-np") as turn:
        turn.llm("openai", ttft_ms=250)
    bundle = session.close()
    llm_operation = next(
        operation for operation in bundle.profile.operations if operation.operation_name == "llm"
    )
    provider_sample = QualitySample(
        sample_id="quality-livekit",
        session_id=bundle.profile.session.session_id,
        quality_kind="provider.metric",
        sample_window=TimeRange(
            start=llm_operation.started_at,
            end=llm_operation.ended_at or llm_operation.started_at,
        ),
        measurements=(
            QualityMeasurement(
                name="livekit.llm_node_ttft",
                value=180.0,
                unit="ms",
                aggregation="instant",
            ),
        ),
        attributes={
            "earshot.turn.id": "turn-np",
            "earshot.operation.id": llm_operation.operation_id,
        },
    )
    bundle = replace_profile(
        bundle,
        quality_samples=(*bundle.profile.quality_samples, provider_sample),
    )

    explanation = explain_incident(bundle, _analyze(bundle))

    [turn] = explanation.turns
    llm = next(
        operation
        for operation in turn.operations
        if operation.operation_id == llm_operation.operation_id
    )
    retained = {measurement.name: measurement for measurement in llm.measurements}
    assert "livekit.llm_node_ttft" in retained
    assert retained["livekit.llm_node_ttft"].value == 180.0
    assert retained["livekit.llm_node_ttft"].unit == "ms"
    assert retained["livekit.llm_node_ttft"].confidence == "unavailable"


def test_explanation_keeps_turn_only_measurement_off_unrelated_operations() -> None:
    session = earshot.pipeline(
        session_id="turn-only-measurement-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-only") as turn:
        turn.stt("deepgram", ttfb_ms=90)
        turn.llm("openai", ttft_ms=180)
        turn.tts("cartesia", ttfb_ms=70)
    bundle = session.close()
    turn_only_sample = QualitySample(
        sample_id="quality-turn-only",
        session_id=bundle.profile.session.session_id,
        quality_kind="provider.metric",
        sample_window=TimeRange(
            start=bundle.profile.operations[0].started_at,
            end=bundle.profile.operations[-1].ended_at
            or bundle.profile.operations[-1].started_at,
        ),
        measurements=(
            QualityMeasurement(
                name="provider.turn_latency",
                value=340.0,
                unit="ms",
                aggregation="instant",
            ),
        ),
        attributes={"earshot.turn.id": "turn-only"},
    )
    bundle = replace_profile(
        bundle,
        quality_samples=(*bundle.profile.quality_samples, turn_only_sample),
    )

    explanation = explain_incident(bundle, _analyze(bundle))

    [explained_turn] = explanation.turns
    assert explained_turn.metrics.provider_measurements[
        "provider.turn_latency"
    ].evidence_ids == ("quality-turn-only",)
    assert all(
        "provider.turn_latency" not in {item.name for item in operation.measurements}
        for operation in explained_turn.operations
    )


def test_explanation_surfaces_unassigned_measurements_without_turns() -> None:
    bundle = _fault("webrtc_degradation")

    explanation = explain_incident(bundle, _analyze(bundle))

    assert explanation.turns == ()
    unassigned = {item.name: item for item in explanation.unassigned_measurements}
    assert set(unassigned) == {"jitter", "round_trip_time", "packet_loss_ratio"}
    assert unassigned["jitter"].unit == "ms"
    assert unassigned["round_trip_time"].unit == "ms"
    assert unassigned["packet_loss_ratio"].unit == "1"
    assert all(item.evidence_ids == ("quality-webrtc",) for item in unassigned.values())


def test_explanation_projects_error_record_without_message(valid_bundle) -> None:
    target = next(
        operation
        for operation in valid_bundle.profile.operations
        if operation.operation_id == "op-llm"
    )
    errored = target.model_copy(
        update={
            "status": "error",
            "error": ErrorRecord(
                code="provider.timeout",
                category="timeout",
                message="raw operator message that must never surface",
            ),
        }
    )
    operations = tuple(
        errored if operation.operation_id == "op-llm" else operation
        for operation in valid_bundle.profile.operations
    )
    bundle = replace_profile(valid_bundle, operations=operations)

    explanation = explain_incident(bundle, _analyze(bundle))

    llm = next(
        operation
        for turn in explanation.turns
        for operation in turn.operations
        if operation.operation_id == "op-llm"
    )
    assert llm.error is not None
    assert llm.error.code == "provider.timeout"
    assert llm.error.category == "timeout"
    assert llm.error.capture_class == "diagnostic_payload"
    assert llm.error.message is None
    dumped = explanation.model_dump(mode="json", exclude_none=True)
    serialized = next(
        operation
        for turn in dumped["turns"]
        for operation in turn["operations"]
        if operation["operation_id"] == "op-llm"
    )
    assert "message" not in serialized["error"]


def test_explanation_is_deterministic() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)

    first = explain_incident(bundle, analysis)
    second = explain_incident(bundle, analysis)

    assert first.model_dump_json() == second.model_dump_json()


def test_validate_explanation_accepts_faithful_projection() -> None:
    for name in ("tool_timeout_retry", "webrtc_degradation"):
        bundle = _fault(name)
        analysis = _analyze(bundle)
        explanation = explain_incident(bundle, analysis)
        report = validate_explanation(bundle, analysis, explanation)
        assert report.ok, [issue.code for issue in report.errors]


def test_validate_explanation_rejects_changed_operation_status() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [turn] = explanation.turns
    failed = next(
        operation for operation in turn.operations if operation.operation_id == "op-tool-attempt-1"
    )
    changed = failed.model_copy(update={"status": "ok"})
    tampered_turn = turn.model_copy(
        update={
            "operations": tuple(
                changed if operation.operation_id == changed.operation_id else operation
                for operation in turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_changed_operation_error() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [turn] = explanation.turns
    failed = next(
        operation for operation in turn.operations if operation.operation_id == "op-tool-attempt-1"
    )
    changed = failed.model_copy(
        update={
            "error": ExplainedError(
                code="invented.failure",
                category="provider",
                capture_class="diagnostic_payload",
            )
        }
    )
    tampered_turn = turn.model_copy(
        update={
            "operations": tuple(
                changed if operation.operation_id == changed.operation_id else operation
                for operation in turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_changed_causal_link() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [turn] = explanation.turns
    retry = next(
        operation for operation in turn.operations if operation.operation_id == "op-tool-attempt-2"
    )
    [link] = retry.links
    changed = retry.model_copy(
        update={"links": (link.model_copy(update={"relationship": "duplicates"}),)}
    )
    tampered_turn = turn.model_copy(
        update={
            "operations": tuple(
                changed if operation.operation_id == changed.operation_id else operation
                for operation in turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_changed_operation_measurement() -> None:
    session = earshot.pipeline(
        session_id="changed-measurement-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-measurement") as turn:
        turn.llm("openai", ttft_ms=250)
    bundle = session.close()
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [explained_turn] = explanation.turns
    llm = next(
        operation for operation in explained_turn.operations if operation.operation_name == "llm"
    )
    [measurement] = llm.measurements
    changed = llm.model_copy(
        update={"measurements": (measurement.model_copy(update={"value": 1}),)}
    )
    tampered_turn = explained_turn.model_copy(
        update={
            "operations": tuple(
                changed if operation.operation_id == changed.operation_id else operation
                for operation in explained_turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_changed_operation_source_fields() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [turn] = explanation.turns
    source = next(
        operation for operation in turn.operations if operation.operation_id == "op-tool-attempt-2"
    )
    changed = source.model_copy(
        update={
            "operation_name": "invented_stage",
            "start_nano": str(int(source.start_nano) + 1),
            "participant_id": "invented-participant",
            "trace_id": "f" * 32,
            "evidence": None,
        }
    )
    tampered_turn = turn.model_copy(
        update={
            "operations": tuple(
                changed if operation.operation_id == changed.operation_id else operation
                for operation in turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_duplicate_operation() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    [turn] = explanation.turns
    tampered_turn = turn.model_copy(
        update={"operations": (*turn.operations, turn.operations[0])}
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_PLACEMENT_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_operation_moved_between_turns() -> None:
    session = earshot.pipeline(
        session_id="moved-operation-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-one") as turn:
        turn.llm("openai", ttft_ms=120)
    with session.turn(turn_id="turn-two") as turn:
        turn.tts("cartesia", ttfb_ms=80)
    bundle = session.close()
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    first, second = explanation.turns
    [moved] = first.operations
    tampered = explanation.model_copy(
        update={
            "turns": (
                first.model_copy(update={"operations": ()}),
                second.model_copy(update={"operations": (*second.operations, moved)}),
            )
        }
    )

    report = validate_explanation(bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_OPERATION_PLACEMENT_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_changed_event_source_fields(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    explanation = explain_incident(valid_bundle, analysis)
    [turn] = explanation.turns
    source = next(event for event in turn.events if event.event_id == "evt-token")
    changed = source.model_copy(
        update={
            "event_name": "invented.event",
            "at_nano": str(int(source.at_nano) + 1),
            "operation_id": "op-tts",
            "trace_id": "f" * 32,
            "span_id": "f" * 16,
            "evidence": None,
        }
    )
    tampered_turn = turn.model_copy(
        update={
            "events": tuple(
                changed if event.event_id == changed.event_id else event for event in turn.events
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(valid_bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_EVENT_MISMATCH" in {issue.code for issue in report.errors}


def test_validate_explanation_rejects_duplicate_event(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    explanation = explain_incident(valid_bundle, analysis)
    [turn] = explanation.turns
    tampered_turn = turn.model_copy(update={"events": (*turn.events, turn.events[0])})
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(valid_bundle, analysis, tampered)

    assert "EARSHOT_EXPLANATION_EVENT_PLACEMENT_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_validate_explanation_rejects_moved_dropped_or_invented_events(
    valid_bundle,
) -> None:
    operation_template = next(
        operation
        for operation in valid_bundle.profile.operations
        if operation.operation_id == "op-llm"
    )
    second_operation = operation_template.model_copy(
        update={
            "operation_id": "op-turn-two",
            "turn_id": "turn-two",
            "span_id": "a" * 16,
            "parent_span_id": None,
            "parent_scope": "external",
            "started_at": point(2_000_000_000),
            "ended_at": point(2_100_000_000),
        }
    )
    event_template = next(
        event for event in valid_bundle.profile.events if event.event_id == "evt-token"
    )
    second_event = event_template.model_copy(
        update={
            "event_id": "evt-turn-two",
            "turn_id": "turn-two",
            "operation_id": second_operation.operation_id,
            "time": point(2_050_000_000),
            "trace_id": TRACE_ID,
            "span_id": second_operation.span_id,
        }
    )
    bundle = replace_profile(
        valid_bundle,
        operations=(*valid_bundle.profile.operations, second_operation),
        events=(*valid_bundle.profile.events, second_event),
    )
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)
    first, second = explanation.turns
    moved = first.events[0]
    moved_between_turns = explanation.model_copy(
        update={
            "turns": (
                first.model_copy(update={"events": first.events[1:]}),
                second.model_copy(update={"events": (*second.events, moved)}),
            )
        }
    )
    dropped = explanation.model_copy(
        update={"turns": (first.model_copy(update={"events": first.events[1:]}), second)}
    )
    invented_event = moved.model_copy(update={"event_id": "invented-event"})
    invented = explanation.model_copy(
        update={
            "turns": (
                first.model_copy(update={"events": (*first.events, invented_event)}),
                second,
            )
        }
    )

    for tampered in (moved_between_turns, dropped, invented):
        report = validate_explanation(bundle, analysis, tampered)
        assert "EARSHOT_EXPLANATION_EVENT_PLACEMENT_MISMATCH" in {
            issue.code for issue in report.errors
        }


def test_validate_explanation_flags_dangling_evidence() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)

    [turn] = explanation.turns
    tampered_operation = turn.operations[0].model_copy(
        update={"evidence_ids": ("operation-that-does-not-exist",)}
    )
    tampered_turn = turn.model_copy(
        update={"operations": (tampered_operation, *turn.operations[1:])}
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)
    assert not report.ok
    assert "EARSHOT_EXPLANATION_DANGLING_REF" in {issue.code for issue in report.errors}


def test_validate_explanation_flags_invented_diagnosis() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)

    invented = ExplainedDiagnosis(
        diagnosis_id="invented.diagnosis",
        code="operation.failed",
        summary="operation_failed",
        confidence="measured",
        evidence_ids=("op-tool-attempt-1",),
    )
    tampered = explanation.model_copy(update={"diagnoses": (*explanation.diagnoses, invented)})

    report = validate_explanation(bundle, analysis, tampered)
    assert not report.ok
    assert "EARSHOT_EXPLANATION_DIAGNOSIS_MISMATCH" in {issue.code for issue in report.errors}


def test_validate_explanation_flags_dropped_operation() -> None:
    bundle = _fault("tool_timeout_retry")
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)

    [turn] = explanation.turns
    tampered_turn = turn.model_copy(update={"operations": turn.operations[:-1]})
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)
    assert not report.ok
    assert "EARSHOT_EXPLANATION_OPERATION_DROPPED" in {issue.code for issue in report.errors}


def test_validate_explanation_flags_manufactured_interval() -> None:
    session = earshot.pipeline(
        session_id="manufactured-interval-session",
        started_at_unix_nano=1_752_800_000_000_000_000,
    )
    with session.turn(turn_id="turn-point") as turn:
        turn.llm("openai", ttft_ms=125)
    bundle = session.close()
    analysis = _analyze(bundle)
    explanation = explain_incident(bundle, analysis)

    [turn] = explanation.turns
    point_operation = next(item for item in turn.operations if item.operation_name == "llm")
    assert point_operation.shape == "point"
    fabricated = point_operation.model_copy(
        update={
            "shape": "interval",
            "end_nano": str(int(point_operation.start_nano) + 1000),
            "duration_nano": "1000",
        }
    )
    tampered_turn = turn.model_copy(
        update={
            "operations": tuple(
                fabricated if item.operation_id == point_operation.operation_id else item
                for item in turn.operations
            )
        }
    )
    tampered = explanation.model_copy(update={"turns": (tampered_turn,)})

    report = validate_explanation(bundle, analysis, tampered)
    assert not report.ok
    assert "EARSHOT_EXPLANATION_MANUFACTURED_INTERVAL" in {issue.code for issue in report.errors}
