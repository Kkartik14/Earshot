from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.contract import (
    AnalysisMetric,
    BundleManifest,
    CausalLink,
    ClockDomain,
    DerivedAnalysis,
    Diagnosis,
    Evidence,
    IncidentBundle,
    Operation,
    Producer,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from earshot.validation import assert_valid_incident, validate_derived_analysis, validate_incident
from incident_factory import LLM_SPAN_ID, ROOT_SPAN_ID, TRACE_ID, point

pytestmark = pytest.mark.unit


def replace_profile(bundle: IncidentBundle, **updates: object) -> IncidentBundle:
    return bundle.model_copy(update={"profile": bundle.profile.model_copy(update=updates)})


def issue_codes(bundle: IncidentBundle) -> set[str]:
    return {issue.code for issue in validate_incident(bundle).errors}


def test_factory_is_a_strictly_valid_cross_record_bundle(valid_bundle: IncidentBundle) -> None:
    report = validate_incident(valid_bundle)
    assert report.ok, report


@pytest.mark.parametrize("bundle_id", ["slash/id", "space id", "%2F", "."])
def test_bundle_id_is_one_retrievable_url_path_segment(bundle_id: str) -> None:
    with pytest.raises(ValidationError):
        BundleManifest(
            bundle_id=bundle_id,
            session_id="session",
            created_at_unix_nano="0",
            producer=Producer(name="test", version="1"),
        )


def test_deep_acyclic_graph_does_not_depend_on_python_recursion(valid_bundle) -> None:
    operations = []
    for index in range(1_500):
        parent = f"{index:016x}" if index else None
        operations.append(
            Operation(
                operation_id=f"deep-operation-{index}",
                session_id="session-1",
                operation_name="agent",
                status="ok",
                started_at=point(index),
                ended_at=point(index + 1),
                trace_id=TRACE_ID,
                span_id=f"{index + 1:016x}",
                parent_span_id=parent,
                parent_scope="internal" if parent else "external",
            )
        )
    broken_order_but_valid = replace_profile(
        valid_bundle,
        operations=tuple(reversed(operations)),
        events=(),
    )
    assert validate_incident(broken_order_but_valid).ok


def test_heard_claim_cannot_hide_in_a_forward_compatible_model_extra(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(update={"heard_at": 1_800_000_000_000_000_000})
    broken = valid_bundle.model_copy(update={"profile": profile})
    assert "EARSHOT_UNOBSERVABLE_HEARD_CLAIM" in issue_codes(broken)


def test_heard_claim_cannot_hide_in_the_primary_event_name(valid_bundle) -> None:
    events = list(valid_bundle.profile.events)
    events[0] = events[0].model_copy(update={"event_name": "earshot.audio.heard_at"})
    broken = replace_profile(valid_bundle, events=tuple(events))
    assert "EARSHOT_UNOBSERVABLE_HEARD_CLAIM" in issue_codes(broken)


def test_transport_quality_cannot_claim_pcm_provenance_with_renamed_metrics(
    valid_bundle,
) -> None:
    sample = QualitySample(
        sample_id="qos-smuggle",
        session_id="session-1",
        quality_kind="transport.quality",
        sample_window=TimeRange(start=point(1), end=point(2)),
        measurements=(QualityMeasurement(name="network_drop_0", value=4, unit="count"),),
        evidence=Evidence(
            source="pcm",
            observer="server",
            method="waveform_inference",
            confidence="inferred",
            availability="available",
        ),
    )
    broken = replace_profile(valid_bundle, quality_samples=(sample,))
    assert "EARSHOT_NETWORK_QOS_SOURCE_INVALID" in issue_codes(broken)


@pytest.mark.parametrize("measurement_name", ["packet loss ratio", "roundTripTime"])
def test_network_measurement_aliases_cannot_claim_perceptual_pcm_source(
    valid_bundle,
    measurement_name: str,
) -> None:
    sample = QualitySample(
        sample_id="qos-alias-smuggle",
        session_id="session-1",
        quality_kind="audio_perceptual",
        sample_window=TimeRange(start=point(1), end=point(2)),
        measurements=(QualityMeasurement(name=measurement_name, value=0.1, unit="ratio"),),
        evidence=Evidence(
            source="pcm",
            observer="server",
            method="waveform_inference",
            confidence="inferred",
            availability="available",
        ),
    )
    assert "EARSHOT_NETWORK_QOS_SOURCE_INVALID" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


@pytest.mark.parametrize(
    ("name", "value", "unit"),
    [
        ("earshot.turn.response_latency", -250.0, "ms"),
        ("earshot.duration.audio_seconds", -0.1, "s"),
        ("earshot.metric.inference.count", -1, "count"),
        ("earshot.metric.interruption.probability", 1.01, "1"),
        ("packet_loss_ratio", 1.01, "ratio"),
    ],
)
def test_governed_measurements_enforce_their_numeric_domain(
    valid_bundle: IncidentBundle,
    name: str,
    value: float,
    unit: str,
) -> None:
    sample = QualitySample(
        sample_id="semantically-invalid",
        session_id="session-1",
        quality_kind="provider_metric",
        sample_window=TimeRange(start=point(1), end=point(1)),
        measurements=(QualityMeasurement(name=name, value=value, unit=unit),),
        evidence=Evidence(
            source="provider",
            observer="server",
            method="native_metric",
            confidence="measured",
            availability="available",
        ),
    )

    assert "EARSHOT_MEASUREMENT_VALUE_OUT_OF_RANGE" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_governed_counter_accepts_integral_float_from_normalized_provider(
    valid_bundle: IncidentBundle,
) -> None:
    sample = QualitySample(
        sample_id="normalized-provider-count",
        session_id="session-1",
        quality_kind="provider_metric",
        sample_window=TimeRange(start=point(1), end=point(1)),
        measurements=(QualityMeasurement(name="provider.item_count", value=2.0, unit="count"),),
    )

    assert "EARSHOT_MEASUREMENT_VALUE_OUT_OF_RANGE" not in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


@pytest.mark.parametrize("value", [b"bytes", {1, 2}, object()])
def test_profile_extensions_must_be_strict_json_values(valid_bundle, value: object) -> None:
    profile = valid_bundle.profile.model_copy(update={"future_extension": value})
    broken = valid_bundle.model_copy(update={"profile": profile})
    assert "EARSHOT_NON_JSON_VALUE" in issue_codes(broken)
    assert_valid_incident(valid_bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", "0" * 32),
        ("trace_id", "A" * 32),
        ("trace_id", "1" * 31),
        ("span_id", "0" * 16),
        ("span_id", "a" * 17),
    ],
)
def test_otel_identifiers_are_canonical_nonzero_lowercase_hex(field: str, value: str) -> None:
    values = {
        "operation_id": "bad-id",
        "session_id": "session-1",
        "operation_name": "llm",
        "status": "ok",
        "started_at": point(1),
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
    }
    values[field] = value
    with pytest.raises(ValidationError):
        Operation.model_validate(values)


@pytest.mark.parametrize("value", ["-1", "+1", "01", "1.0", 1, -1])
def test_nanoseconds_are_decimal_strings_without_precision_loss(value: object) -> None:
    with pytest.raises(ValidationError):
        TimePoint(source_time_unix_nano=value)  # type: ignore[arg-type]


def test_uint64_sized_decimal_timestamp_is_retained_as_text() -> None:
    value = "18446744073709551615"
    timestamp = TimePoint(source_time_unix_nano=value)
    assert timestamp.source_time_unix_nano == value
    assert isinstance(timestamp.model_dump(mode="json")["source_time_unix_nano"], str)


def test_nanosecond_value_above_uint64_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TimePoint(source_time_unix_nano=str(2**64))


def test_time_point_requires_a_timestamp() -> None:
    with pytest.raises(ValidationError, match="at least one timestamp"):
        TimePoint()


def test_monotonic_time_requires_an_explicit_clock_domain() -> None:
    with pytest.raises(ValidationError, match="clock_domain_id"):
        TimePoint(monotonic_time_nano="1")


def test_operation_requires_complete_otel_identity() -> None:
    with pytest.raises(ValidationError, match="trace_id and span_id"):
        Operation(
            operation_id="op",
            session_id="session-1",
            operation_name="llm",
            status="ok",
            started_at=point(0),
            trace_id=TRACE_ID,
        )


def test_causal_link_requires_an_identifiable_target() -> None:
    with pytest.raises(ValidationError, match="requires target_operation_id"):
        CausalLink(relationship="retries")


def test_unknown_extension_fields_survive_model_roundtrip(valid_bundle: IncidentBundle) -> None:
    value = valid_bundle.model_dump(mode="python")
    value["profile"]["future_profile_field"] = {"nested": [1, "two"]}
    value["profile"]["operations"][0]["future_operation_field"] = "kept"
    parsed = IncidentBundle.model_validate(value)
    dumped = parsed.model_dump(mode="python")
    assert dumped["profile"]["future_profile_field"] == {"nested": [1, "two"]}
    assert dumped["profile"]["operations"][0]["future_operation_field"] == "kept"


def test_unsupported_schema_version_is_a_semantic_error(valid_bundle: IncidentBundle) -> None:
    manifest = valid_bundle.profile.manifest.model_copy(update={"schema_version": "99.0.0"})
    assert "EARSHOT_SCHEMA_VERSION_UNSUPPORTED" in issue_codes(
        replace_profile(valid_bundle, manifest=manifest)
    )


@pytest.mark.parametrize(
    "collection,id_field",
    [
        ("participants", "participant_id"),
        ("audio_streams", "stream_id"),
        ("clock_domains", "clock_domain_id"),
        ("operations", "operation_id"),
        ("events", "event_id"),
        ("raw_otlp_chunks", "chunk_id"),
    ],
)
def test_each_owned_identifier_namespace_rejects_duplicates(
    valid_bundle: IncidentBundle, collection: str, id_field: str
) -> None:
    del id_field  # Documents which identity this parameterized mutation duplicates.
    if collection == "raw_otlp_chunks":
        broken = valid_bundle.model_copy(
            update={
                "raw_otlp_chunks": (
                    *valid_bundle.raw_otlp_chunks,
                    valid_bundle.raw_otlp_chunks[0],
                )
            }
        )
    else:
        records = getattr(valid_bundle.profile, collection)
        broken = replace_profile(valid_bundle, **{collection: (*records, records[0])})
    assert "EARSHOT_DUPLICATE_ID" in issue_codes(broken)


def test_ids_are_not_ambiguous_across_owned_namespaces(valid_bundle: IncidentBundle) -> None:
    duplicate = valid_bundle.profile.events[0].model_copy(update={"event_id": "op-llm"})
    broken = replace_profile(valid_bundle, events=(duplicate, *valid_bundle.profile.events[1:]))
    assert "EARSHOT_AMBIGUOUS_GLOBAL_ID" in issue_codes(broken)


def test_manifest_session_must_match_profile_session(valid_bundle: IncidentBundle) -> None:
    manifest = valid_bundle.profile.manifest.model_copy(update={"session_id": "other-session"})
    assert "EARSHOT_SESSION_MISMATCH" in issue_codes(
        replace_profile(valid_bundle, manifest=manifest)
    )


@pytest.mark.parametrize("collection", ["participants", "audio_streams", "operations", "events"])
def test_every_session_owned_record_must_match_session(
    valid_bundle: IncidentBundle, collection: str
) -> None:
    records = list(getattr(valid_bundle.profile, collection))
    records[0] = records[0].model_copy(update={"session_id": "cross-session"})
    assert "EARSHOT_SESSION_MISMATCH" in issue_codes(
        replace_profile(valid_bundle, **{collection: tuple(records)})
    )


def test_stream_rejects_dangling_participant(valid_bundle: IncidentBundle) -> None:
    streams = list(valid_bundle.profile.audio_streams)
    streams[0] = streams[0].model_copy(update={"participant_id": "absent"})
    assert "EARSHOT_DANGLING_REF" in issue_codes(
        replace_profile(valid_bundle, audio_streams=tuple(streams))
    )


@pytest.mark.parametrize("field,value", [("participant_id", "absent"), ("stream_id", "absent")])
def test_operation_rejects_dangling_owned_reference(
    valid_bundle: IncidentBundle, field: str, value: str
) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={field: value})
    assert "EARSHOT_DANGLING_REF" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_event_rejects_dangling_operation(valid_bundle: IncidentBundle) -> None:
    events = list(valid_bundle.profile.events)
    events[0] = events[0].model_copy(update={"operation_id": "absent"})
    assert "EARSHOT_DANGLING_REF" in issue_codes(
        replace_profile(valid_bundle, events=tuple(events))
    )


def test_unknown_clock_domain_is_rejected(valid_bundle: IncidentBundle) -> None:
    events = list(valid_bundle.profile.events)
    events[0] = events[0].model_copy(update={"time": point(1, domain="missing-clock")})
    assert "EARSHOT_UNKNOWN_CLOCK_DOMAIN" in issue_codes(
        replace_profile(valid_bundle, events=tuple(events))
    )


def test_reversed_range_in_same_clock_domain_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={"started_at": point(100), "ended_at": point(99)}
    )
    assert "EARSHOT_TIME_RANGE_REVERSED" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_cross_domain_values_are_not_globally_ordered(valid_bundle: IncidentBundle) -> None:
    browser_clock = ClockDomain(clock_domain_id="browser-clock", kind="browser", observer="browser")
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "started_at": point(100, domain="server-clock"),
            "ended_at": point(1, domain="browser-clock"),
        }
    )
    broken_global_order_but_valid = replace_profile(
        valid_bundle,
        clock_domains=(*valid_bundle.profile.clock_domains, browser_clock),
        operations=tuple(operations),
    )
    assert "EARSHOT_TIME_RANGE_REVERSED" not in issue_codes(broken_global_order_but_valid)


def test_input_array_order_is_not_an_invariant(valid_bundle: IncidentBundle) -> None:
    reversed_bundle = replace_profile(
        valid_bundle,
        operations=tuple(reversed(valid_bundle.profile.operations)),
        events=tuple(reversed(valid_bundle.profile.events)),
    )
    assert validate_incident(reversed_bundle).ok


def test_duplicate_otel_trace_span_identity_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(
        update={"trace_id": TRACE_ID, "span_id": ROOT_SPAN_ID, "parent_span_id": None}
    )
    assert "EARSHOT_DUPLICATE_OTEL_SPAN" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_missing_internal_parent_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(update={"parent_span_id": "f" * 16})
    assert "EARSHOT_INTERNAL_PARENT_MISSING" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_missing_external_parent_is_preserved(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(
        update={"parent_span_id": "f" * 16, "parent_scope": "external"}
    )
    external_parent = replace_profile(valid_bundle, operations=tuple(operations))
    assert "EARSHOT_INTERNAL_PARENT_MISSING" not in issue_codes(external_parent)


def test_dangling_internal_link_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(
        update={
            "links": (
                CausalLink(
                    relationship="retries",
                    target_scope="internal",
                    target_operation_id="missing-op",
                ),
            )
        }
    )
    codes = issue_codes(replace_profile(valid_bundle, operations=tuple(operations)))
    assert {"EARSHOT_DANGLING_REF", "EARSHOT_INTERNAL_LINK_MISSING"} <= codes


def test_external_link_can_target_an_absent_otel_span(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(
        update={
            "links": (
                CausalLink(
                    relationship="produced_by",
                    target_scope="external",
                    trace_id="e" * 32,
                    span_id="e" * 16,
                ),
            )
        }
    )
    assert validate_incident(replace_profile(valid_bundle, operations=tuple(operations))).ok


def test_external_link_cannot_claim_a_bundle_owned_operation_id(
    valid_bundle: IncidentBundle,
) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[1] = operations[1].model_copy(
        update={
            "links": (
                CausalLink(
                    relationship="duplicates",
                    target_scope="external",
                    target_operation_id="op-turn",
                ),
            )
        }
    )
    assert "EARSHOT_EXTERNAL_LINK_OWNS_TARGET" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_parent_cycle_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={"parent_span_id": LLM_SPAN_ID, "parent_scope": "internal"}
    )
    assert "EARSHOT_CAUSAL_CYCLE" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_typed_link_cycle_is_rejected(valid_bundle: IncidentBundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "links": (
                CausalLink(
                    relationship="retries",
                    target_scope="internal",
                    target_operation_id="op-llm",
                ),
            )
        }
    )
    # op-llm already has op-turn as its parent, so this closes the cycle.
    assert "EARSHOT_CAUSAL_CYCLE" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_nonfinite_extension_number_is_rejected(valid_bundle: IncidentBundle) -> None:
    broken = replace_profile(valid_bundle, attributes={"vendor.metric": float("nan")})
    assert "EARSHOT_NONFINITE_NUMBER" in issue_codes(broken)


def test_reversed_wall_timestamps_without_a_declared_domain_are_not_comparable(
    valid_bundle: IncidentBundle,
) -> None:
    operation = valid_bundle.profile.operations[0].model_copy(
        update={
            "started_at": TimePoint(source_time_unix_nano="100"),
            "ended_at": TimePoint(source_time_unix_nano="50"),
        }
    )
    operations = (operation, *valid_bundle.profile.operations[1:])
    assert "EARSHOT_TIME_RANGE_REVERSED" not in issue_codes(
        replace_profile(valid_bundle, operations=operations)
    )


def test_nonavailable_coverage_requires_reason(valid_bundle: IncidentBundle) -> None:
    coverage = valid_bundle.profile.coverage[0].model_copy(
        update={"availability": "not_observed", "reason": None}
    )
    assert "EARSHOT_AVAILABILITY_REASON_REQUIRED" in issue_codes(
        replace_profile(valid_bundle, coverage=(coverage,))
    )


def test_diagnosis_must_cite_existing_evidence(valid_bundle: IncidentBundle) -> None:
    analysis = DerivedAnalysis(
        analyzer_name="test",
        analyzer_version="1",
        input_sha256="a" * 64,
        generated_at_unix_nano="1",
        diagnoses=(
            Diagnosis(
                diagnosis_id="d1",
                code="failure",
                summary="failure",
                confidence="measured",
                evidence_refs=("missing-evidence",),
            ),
        ),
    )
    codes = issue_codes(replace_profile(valid_bundle, analysis=analysis))
    assert "EARSHOT_DANGLING_REF" in codes


def test_analysis_projection_references_and_labels_are_governed(valid_bundle) -> None:
    analysis = analyze_incident(
        valid_bundle,
        input_sha256=analysis_input_sha256(valid_bundle),
        generated_at_unix_nano="1",
    )
    malicious = AnalysisMetric(
        availability="available",
        basis="provider_direct",
        confidence="measured",
        value=1,
        unit="SENTINEL-private-transcript",
        evidence_ids=("missing-evidence",),
    )
    projections = analysis.projections.model_copy(
        update={
            "unassigned_provider_measurements": {
                "missing-sample": {"SENTINEL-private-transcript": malicious}
            }
        }
    )
    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"projections": projections}),
    )
    codes = {issue.code for issue in report.errors}
    assert "EARSHOT_DANGLING_REF" in codes
    assert "EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED" in codes


def test_analysis_summary_is_recomputed_from_immutable_evidence(valid_bundle) -> None:
    analysis = analyze_incident(
        valid_bundle,
        input_sha256=analysis_input_sha256(valid_bundle),
        generated_at_unix_nano="1",
    )
    assert analysis.projections.summary is not None
    tampered_summary = analysis.projections.summary.model_copy(
        update={"operation_count": analysis.projections.summary.operation_count + 1}
    )
    projections = analysis.projections.model_copy(update={"summary": tampered_summary})
    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"projections": projections}),
    )
    assert "EARSHOT_ANALYSIS_SUMMARY_MISMATCH" in {issue.code for issue in report.errors}


def test_available_analysis_metric_cannot_drop_its_source_evidence(valid_bundle) -> None:
    analysis = analyze_incident(
        valid_bundle,
        input_sha256=analysis_input_sha256(valid_bundle),
        generated_at_unix_nano="1",
    )
    turn = analysis.projections.turns[0]
    forged_metric = turn.metrics.first_token_latency.model_copy(update={"evidence_ids": ()})
    metrics = turn.metrics.model_copy(update={"first_token_latency": forged_metric})
    forged_turn = turn.model_copy(update={"metrics": metrics})
    projections = analysis.projections.model_copy(update={"turns": (forged_turn,)})
    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"projections": projections}),
    )
    assert "EARSHOT_ANALYSIS_STRUCTURAL_INVALID" in {issue.code for issue in report.errors}


def test_analysis_turn_must_exist_in_source_evidence(valid_bundle) -> None:
    analysis = analyze_incident(
        valid_bundle,
        input_sha256=analysis_input_sha256(valid_bundle),
        generated_at_unix_nano="1",
    )
    forged_turn = analysis.projections.turns[0].model_copy(update={"turn_id": "fabricated-turn"})
    projections = analysis.projections.model_copy(update={"turns": (forged_turn,)})
    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"projections": projections}),
    )
    assert "EARSHOT_ANALYSIS_TURN_UNBOUND" in {issue.code for issue in report.errors}


def test_known_failed_operation_diagnosis_cannot_cite_a_success(valid_bundle) -> None:
    analysis = analyze_incident(
        valid_bundle,
        input_sha256=analysis_input_sha256(valid_bundle),
        generated_at_unix_nano="1",
    )
    forged = Diagnosis(
        diagnosis_id="forged_operation_failure",
        code="operation.failed",
        summary="operation_failed",
        confidence="measured",
        evidence_refs=("op-turn",),
    )
    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"diagnoses": (*analysis.diagnoses, forged)}),
    )
    assert "EARSHOT_ANALYSIS_DIAGNOSIS_EVIDENCE_INVALID" in {issue.code for issue in report.errors}


def test_analysis_validator_matches_event_trace_span_turn_inheritance(valid_bundle) -> None:
    event = valid_bundle.profile.events[0].model_copy(
        update={
            "turn_id": None,
            "operation_id": None,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
        }
    )
    bundle = replace_profile(
        valid_bundle,
        events=(event, *valid_bundle.profile.events[1:]),
    )
    assert validate_incident(bundle).ok

    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1",
    )
    projected = next(turn for turn in analysis.projections.turns if turn.turn_id == "turn-1")
    assert event.event_id in projected.event_ids
    assert validate_derived_analysis(bundle, analysis).ok


def test_redundant_event_and_link_identities_must_agree(valid_bundle) -> None:
    event = valid_bundle.profile.events[0].model_copy(
        update={
            "operation_id": "op-turn",
            "trace_id": TRACE_ID,
            "span_id": LLM_SPAN_ID,
        }
    )
    assert "EARSHOT_EVENT_IDENTITY_MISMATCH" in issue_codes(
        replace_profile(
            valid_bundle,
            events=(event, *valid_bundle.profile.events[1:]),
        )
    )

    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "links": (
                CausalLink(
                    relationship="related",
                    target_scope="internal",
                    target_operation_id="op-llm",
                    trace_id=TRACE_ID,
                    span_id=ROOT_SPAN_ID,
                ),
            )
        }
    )
    assert "EARSHOT_LINK_IDENTITY_MISMATCH" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_embedded_analysis_is_rejected_even_when_digest_bound(
    valid_bundle: IncidentBundle,
) -> None:
    digest = analysis_input_sha256(valid_bundle)
    analysis = DerivedAnalysis(
        analyzer_name="test",
        analyzer_version="1",
        input_sha256=digest,
        generated_at_unix_nano="1",
    )
    bound = replace_profile(valid_bundle, analysis=analysis)
    assert "EARSHOT_EMBEDDED_ANALYSIS_UNSUPPORTED" in issue_codes(bound)
    mismatched = replace_profile(
        valid_bundle,
        analysis=analysis.model_copy(update={"input_sha256": "0" * 64}),
    )
    assert "EARSHOT_ANALYSIS_INPUT_MISMATCH" in issue_codes(mismatched)


def test_validation_never_mutates_input(valid_bundle: IncidentBundle) -> None:
    before = deepcopy(valid_bundle.model_dump(mode="python"))
    validate_incident(valid_bundle)
    assert valid_bundle.model_dump(mode="python") == before
