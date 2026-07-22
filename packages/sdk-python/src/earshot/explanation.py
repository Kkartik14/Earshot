"""Backend-authored, evidence-preserving timeline projections for UI clients."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contract import (
    DerivedAnalysis,
    Evidence,
    IncidentBundle,
    QualitySample,
    TimePoint,
    TurnMetrics,
)


class ExplanationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExplainedEvidence(ExplanationModel):
    source: str
    observer: str
    method: str
    confidence: str
    availability: str
    method_version: str | None = None
    source_field: str | None = None


class ExplainedMeasurement(ExplanationModel):
    name: str
    value: bool | int | float
    unit: str
    aggregation: str
    evidence: ExplainedEvidence | None = None
    evidence_ids: tuple[str, ...]


class ExplainedOperation(ExplanationModel):
    operation_id: str
    operation_name: str
    status: str
    shape: Literal["point", "interval"]
    time_basis: Literal["monotonic", "source_wall", "observed_wall"]
    clock_domain_id: str | None = None
    start_nano: str
    end_nano: str | None = None
    duration_nano: str | None = None
    limitation: str | None = None
    participant_id: str | None = None
    stream_id: str | None = None
    provider: str | None = None
    model: str | None = None
    evidence: ExplainedEvidence | None = None
    measurements: tuple[ExplainedMeasurement, ...]
    evidence_ids: tuple[str, ...]


class ExplainedEvent(ExplanationModel):
    event_id: str
    event_name: str
    time_basis: Literal["monotonic", "source_wall", "observed_wall"]
    clock_domain_id: str | None = None
    at_nano: str
    participant_id: str | None = None
    stream_id: str | None = None
    evidence: ExplainedEvidence | None = None
    evidence_ids: tuple[str, ...]


class ExplainedCoverage(ExplanationModel):
    signal: str
    availability: str
    reason: str | None = None
    evidence: ExplainedEvidence | None = None


class ExplainedOmission(ExplanationModel):
    omission_id: str
    capture_class: str
    reason: str
    count: int | None = None
    source_refs: tuple[str, ...]


class ExplainedTurn(ExplanationModel):
    turn_id: str
    operations: tuple[ExplainedOperation, ...]
    events: tuple[ExplainedEvent, ...]
    metrics: TurnMetrics


class IncidentExplanation(ExplanationModel):
    bundle_id: str
    session_id: str
    session_status: str
    finality: str
    completeness: str
    analyzer_name: str
    analyzer_version: str
    input_sha256: str
    turns: tuple[ExplainedTurn, ...]
    coverage: tuple[ExplainedCoverage, ...]
    omissions: tuple[ExplainedOmission, ...]
    limitations: tuple[str, ...]


def _evidence(value: Evidence | None) -> ExplainedEvidence | None:
    if value is None:
        return None
    return ExplainedEvidence(
        source=value.source,
        observer=value.observer,
        method=value.method,
        confidence=value.confidence,
        availability=value.availability,
        method_version=value.method_version,
        source_field=value.source_field,
    )


def _coordinate(
    value: TimePoint,
) -> tuple[Literal["monotonic", "source_wall", "observed_wall"], str | None, str]:
    if value.monotonic_time_nano is not None:
        return "monotonic", value.clock_domain_id, value.monotonic_time_nano
    if value.source_time_unix_nano is not None:
        return "source_wall", value.clock_domain_id, value.source_time_unix_nano
    assert value.observed_time_unix_nano is not None
    return "observed_wall", value.clock_domain_id, value.observed_time_unix_nano


def _sample_belongs_to_operation(
    sample: QualitySample,
    operation,
    *,
    matching_stage_count: int,
) -> bool:
    explicit_owner = sample.attributes.get("earshot.operation.id")
    if explicit_owner is not None:
        return isinstance(explicit_owner, str) and explicit_owner == operation.operation_id
    return (
        matching_stage_count == 1 and sample.attributes.get("earshot.turn.id") == operation.turn_id
    )


def _operation(
    value,
    samples: tuple[QualitySample, ...],
    *,
    matching_stage_count: int,
) -> ExplainedOperation:
    basis, domain, start = _coordinate(value.started_at)
    end: str | None = None
    duration: str | None = None
    limitation = "end_boundary_not_observed"
    if value.ended_at is not None:
        end_basis, end_domain, candidate = _coordinate(value.ended_at)
        if (end_basis, end_domain) != (basis, domain):
            limitation = "end_boundary_not_comparable"
        elif int(candidate) < int(start):
            limitation = "invalid_negative_interval"
        else:
            end = candidate
            duration = str(int(candidate) - int(start))
            limitation = None
    attributes = value.attributes
    provider = attributes.get("gen_ai.provider.name")
    model = attributes.get("gen_ai.request.model")
    measurement_prefix = f"earshot.{value.operation_name}."
    measurements = tuple(
        ExplainedMeasurement(
            name=measurement.name,
            value=measurement.value,
            unit=measurement.unit,
            aggregation=measurement.aggregation,
            evidence=_evidence(sample.evidence),
            evidence_ids=(sample.sample_id,),
        )
        for sample in samples
        if _sample_belongs_to_operation(
            sample,
            value,
            matching_stage_count=matching_stage_count,
        )
        for measurement in sample.measurements
        if measurement.name.startswith(measurement_prefix)
    )
    return ExplainedOperation(
        operation_id=value.operation_id,
        operation_name=value.operation_name,
        status=value.status,
        shape="interval" if end is not None else "point",
        time_basis=basis,
        clock_domain_id=domain,
        start_nano=start,
        end_nano=end,
        duration_nano=duration,
        limitation=limitation,
        participant_id=value.participant_id,
        stream_id=value.stream_id,
        provider=provider if isinstance(provider, str) else None,
        model=model if isinstance(model, str) else None,
        evidence=_evidence(value.evidence),
        measurements=measurements,
        evidence_ids=(value.operation_id,),
    )


def _event(value) -> ExplainedEvent:
    basis, domain, at = _coordinate(value.time)
    return ExplainedEvent(
        event_id=value.event_id,
        event_name=value.event_name,
        time_basis=basis,
        clock_domain_id=domain,
        at_nano=at,
        participant_id=value.participant_id,
        stream_id=value.stream_id,
        evidence=_evidence(value.evidence),
        evidence_ids=(value.event_id,),
    )


def _operation_order(value) -> tuple[int, str, str, int, str]:
    basis, domain, coordinate = _coordinate(value.started_at)
    return (domain is None, domain or "", basis, int(coordinate), value.operation_id)


def _event_order(value) -> tuple[int, str, str, int, str]:
    basis, domain, coordinate = _coordinate(value.time)
    return (domain is None, domain or "", basis, int(coordinate), value.event_id)


def explain_incident(bundle: IncidentBundle, analysis: DerivedAnalysis) -> IncidentExplanation:
    """Project UI-ready facts without inventing intervals or cross-clock ordering."""

    profile = bundle.profile
    operations = {item.operation_id: item for item in profile.operations}
    events = {item.event_id: item for item in profile.events}
    samples = profile.quality_samples
    turns = tuple(
        ExplainedTurn(
            turn_id=turn.turn_id,
            operations=tuple(
                _operation(
                    operation,
                    samples,
                    matching_stage_count=sum(
                        candidate.operation_name == operation.operation_name
                        for identity in turn.operation_ids
                        if (candidate := operations.get(identity)) is not None
                    ),
                )
                for operation in sorted(
                    (
                        operations[identity]
                        for identity in turn.operation_ids
                        if identity in operations
                    ),
                    key=_operation_order,
                )
            ),
            events=tuple(
                _event(event)
                for event in sorted(
                    (events[identity] for identity in turn.event_ids if identity in events),
                    key=_event_order,
                )
            ),
            metrics=turn.metrics,
        )
        for turn in analysis.projections.turns
    )
    return IncidentExplanation(
        bundle_id=profile.manifest.bundle_id,
        session_id=profile.manifest.session_id,
        session_status=profile.session.status,
        finality=profile.manifest.finality,
        completeness=profile.manifest.completeness,
        analyzer_name=analysis.analyzer_name,
        analyzer_version=analysis.analyzer_version,
        input_sha256=analysis.input_sha256,
        turns=turns,
        coverage=tuple(
            ExplainedCoverage(
                signal=item.signal,
                availability=item.availability,
                reason=item.reason,
                evidence=_evidence(item.evidence),
            )
            for item in profile.coverage
        ),
        omissions=tuple(
            ExplainedOmission(
                omission_id=item.omission_id,
                capture_class=item.capture_class,
                reason=item.reason,
                count=item.count,
                source_refs=item.source_refs,
            )
            for item in profile.privacy.omissions
        ),
        limitations=analysis.projections.limitations,
    )
