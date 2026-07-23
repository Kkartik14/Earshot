from __future__ import annotations

import pytest

from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.contract import (
    Diagnosis,
    Evidence,
    IncidentProfile,
    QualityMeasurement,
    QualitySample,
    TimeRange,
)
from earshot.validation import validate_derived_analysis, validate_incident
from incident_factory import point

pytestmark = pytest.mark.unit


def _replace_profile(bundle, **updates):
    profile = IncidentProfile.model_validate(bundle.profile.model_copy(update=updates))
    return bundle.model_copy(update={"profile": profile})


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="1800000005000000000",
    )


def _replace_first_turn_metrics(analysis, metrics):
    turn = analysis.projections.turns[0].model_copy(update={"metrics": metrics})
    projections = analysis.projections.model_copy(update={"turns": (turn,)})
    return analysis.model_copy(update={"projections": projections})


def _quality_sample(*, owned: bool) -> QualitySample:
    return QualitySample(
        sample_id="quality-provider-value",
        session_id="session-1",
        quality_kind="provider.metric",
        sample_window=TimeRange(start=point(10), end=point(10)),
        measurements=(
            QualityMeasurement(
                name="provider.queue_depth",
                value=3,
                unit="count",
                aggregation="instant",
            ),
        ),
        evidence=Evidence(
            source="provider",
            observer="server",
            method="native_metric",
            confidence="measured",
            availability="available",
        ),
        attributes={"earshot.turn.id": "turn-1"} if owned else {},
    )


@pytest.mark.parametrize(
    "forged_fields",
    (
        {"value": 999.0},
        {"basis": "forged_basis"},
        {"confidence": "estimated"},
        {"limitation": "forged_limitation"},
    ),
    ids=("value", "basis", "confidence", "limitation"),
)
def test_current_builtin_analysis_rejects_forged_latency_with_valid_evidence(
    valid_bundle, forged_fields: dict[str, object]
) -> None:
    analysis = _analyze(valid_bundle)
    metrics = analysis.projections.turns[0].metrics
    forged_latency = metrics.first_token_latency.model_copy(update=forged_fields)
    forged_metrics = metrics.model_copy(update={"first_token_latency": forged_latency})

    report = validate_derived_analysis(
        valid_bundle,
        _replace_first_turn_metrics(analysis, forged_metrics),
    )

    assert "EARSHOT_ANALYSIS_LATENCY_MISMATCH" in {issue.code for issue in report.errors}


def test_current_builtin_analysis_rejects_forged_owned_provider_value(
    valid_bundle,
) -> None:
    bundle = _replace_profile(valid_bundle, quality_samples=(_quality_sample(owned=True),))
    assert validate_incident(bundle).ok
    analysis = _analyze(bundle)
    metrics = analysis.projections.turns[0].metrics
    provider = dict(metrics.provider_measurements)
    provider["provider.queue_depth"] = provider["provider.queue_depth"].model_copy(
        update={"value": 99}
    )
    forged_metrics = metrics.model_copy(update={"provider_measurements": provider})

    report = validate_derived_analysis(
        bundle,
        _replace_first_turn_metrics(analysis, forged_metrics),
    )

    assert "EARSHOT_ANALYSIS_PROVIDER_MEASUREMENT_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_current_builtin_analysis_rejects_forged_unassigned_provider_value(
    valid_bundle,
) -> None:
    sample = _quality_sample(owned=False)
    bundle = _replace_profile(
        valid_bundle,
        operations=(),
        events=(),
        quality_samples=(sample,),
    )
    assert validate_incident(bundle).ok
    analysis = _analyze(bundle)
    unassigned = {
        sample_id: dict(measurements)
        for sample_id, measurements in analysis.projections.unassigned_provider_measurements.items()
    }
    source = unassigned[sample.sample_id]["provider.queue_depth"]
    unassigned[sample.sample_id]["provider.queue_depth"] = source.model_copy(update={"value": 99})
    projections = analysis.projections.model_copy(
        update={"unassigned_provider_measurements": unassigned}
    )
    forged = analysis.model_copy(update={"projections": projections})

    report = validate_derived_analysis(bundle, forged)

    assert "EARSHOT_ANALYSIS_PROVIDER_MEASUREMENT_MISMATCH" in {
        issue.code for issue in report.errors
    }


def test_current_builtin_analysis_rejects_invented_diagnosis(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    forged = Diagnosis(
        diagnosis_id="forged_queue_overload",
        code="queue.overload",
        summary="queue_overload",
        confidence="measured",
        evidence_refs=("op-turn",),
    )

    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"diagnoses": (*analysis.diagnoses, forged)}),
    )

    assert "EARSHOT_ANALYSIS_DIAGNOSIS_MISMATCH" in {issue.code for issue in report.errors}


def test_current_builtin_analysis_rejects_invented_global_limitation(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    projections = analysis.projections.model_copy(
        update={"limitations": (*analysis.projections.limitations, "invented_limitation")}
    )

    report = validate_derived_analysis(
        valid_bundle,
        analysis.model_copy(update={"projections": projections}),
    )

    assert "EARSHOT_ANALYSIS_PROJECTION_MISMATCH" in {issue.code for issue in report.errors}


def test_custom_analyzer_keeps_replaceable_metric_semantics(valid_bundle) -> None:
    analysis = _analyze(valid_bundle)
    metrics = analysis.projections.turns[0].metrics
    custom_latency = metrics.first_token_latency.model_copy(
        update={"value": 999.0, "basis": "custom_model"}
    )
    custom_metrics = metrics.model_copy(update={"first_token_latency": custom_latency})
    custom = _replace_first_turn_metrics(analysis, custom_metrics).model_copy(
        update={"analyzer_name": "custom.analyzer"}
    )

    assert validate_derived_analysis(valid_bundle, custom).ok


def test_historical_builtin_version_is_not_reinterpreted_by_current_code(
    valid_bundle,
) -> None:
    analysis = _analyze(valid_bundle)
    metrics = analysis.projections.turns[0].metrics
    historical_latency = metrics.first_token_latency.model_copy(update={"value": 999.0})
    historical_metrics = metrics.model_copy(update={"first_token_latency": historical_latency})
    historical = _replace_first_turn_metrics(analysis, historical_metrics).model_copy(
        update={"analyzer_version": "0.2.1"}
    )

    assert validate_derived_analysis(valid_bundle, historical).ok
