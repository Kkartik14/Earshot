"""Deterministic, evidence-linked query surface over an incident's graph.

This module turns an already-governed incident (its evidence graph plus the
derived, replaceable ``DerivedAnalysis`` projection) into a *machine-queryable*
lens. Every answer is metadata-only, JSON-serializable, and cites the evidence
ids it was derived from, so a coding agent or incident responder can ask a
structured question and get a structured (not prose) answer.

Three surfaces live here:

* ``EvidenceQuery`` -- structured questions about a single incident
  (``known_about_turn``, ``first_abnormal_boundary``, ``not_observed``,
  ``recomputable``, ``summary``).
* ``detect_contradictions`` -- evidence-linked contradictions in one incident.
* ``compare_incidents`` -- a structured diff of an incident against a known-good
  session (added/removed diagnoses, per-turn metric deltas, coverage-gap and
  contradiction changes).

Discipline: deterministic and source-order-invariant, evidence-faithful, and
honest about ignorance. No value, ordering, or delta is ever fabricated; when
evidence is insufficient or two clocks are incomparable the answer says so
rather than inventing a coordinate. Time math is delegated to the analyzer's
``comparable_delta`` so cross-clock arithmetic stays governed by declared
calibrations. There are no network calls and no source payloads are surfaced.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .analysis import (
    _ClockAligner,
    _comparable_coordinate,
    analyze_incident,
    comparable_delta,
)
from .codec import analysis_input_sha256
from .contract import (
    DerivedAnalysis,
    Diagnosis,
    IncidentBundle,
    QualitySample,
    TimePoint,
)


def _coordinate_sort_key(point: TimePoint) -> tuple[int, str, str, int]:
    """A sortable key over the analyzer's canonical coordinate.

    Orderable points sort by ``(0, domain, basis, value)``; a point with no
    comparable coordinate sorts last as ``(1, "", "", 0)`` so an incomparable
    boundary never masquerades as the earliest one.
    """

    coordinate = _comparable_coordinate(point)
    if coordinate is None:
        return (1, "", "", 0)
    domain, basis, value = coordinate
    return (0, domain, basis, value)


# The turn-relative latency metrics an agent can compare, in a stable order.
LATENCY_METRICS: tuple[str, ...] = (
    "first_token_latency",
    "generated_response_latency",
    "sent_response_latency",
    "received_response_latency",
    "render_start_response_latency",
    "response_latency",
)

# Codes emitted by the analyzer's boundary-attribution engine. Each names the
# boundary it blames; ``operation.failed`` is a raw fact (an operation status),
# not a boundary hypothesis, so it is deliberately excluded here.
BOUNDARY_DIAGNOSIS_CODES: frozenset[str] = frozenset(
    {
        "network.degraded",
        "transport.reconnect",
        "tool.retry",
        "device.unavailable",
        "audio.stale_playback",
        "render.delayed",
        "interruption.false",
        "stage.slow",
        "endpointing.slow",
    }
)

# Maps a diagnosis code to the pipeline boundary it attributes fault to. Codes
# absent from the table fall back to their leading dotted segment.
_BOUNDARY_BY_CODE: dict[str, str] = {
    "network.degraded": "transport",
    "transport.reconnect": "transport",
    "tool.retry": "tool",
    "device.unavailable": "capture",
    "audio.stale_playback": "render",
    "render.delayed": "render",
    "interruption.false": "interruption",
    "stage.slow": "stage",
    "endpointing.slow": "turn_detection",
    "operation.failed": "operation",
}

_RENDER_COVERAGE_SIGNALS = frozenset({"render", "client.render"})
_RENDER_STARTED_EVENT = "earshot.audio.render.started"
_DUPLICATE_EVENT = "earshot.transport.message.duplicate"
_OUT_OF_ORDER_EVENT = "earshot.transport.message.out_of_order"
_UNCERTAINTY_SUFFIX = ".uncertainty"


def boundary_for_code(code: str) -> str:
    """Return the pipeline boundary a diagnosis code attributes fault to."""

    return _BOUNDARY_BY_CODE.get(code, code.split(".", 1)[0])


def _metric_dict(metric: object, name: str) -> dict[str, object]:
    """Project one ``AnalysisMetric`` as a JSON-ready, name-tagged answer."""

    payload = metric.model_dump(mode="json", exclude_none=True)  # type: ignore[attr-defined]
    return {"metric": name, **payload}


# --------------------------------------------------------------------------- #
# Structured result shapes                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TurnKnowledge:
    """Everything the evidence graph knows about one turn."""

    turn_id: str
    found: bool
    metrics: tuple[dict[str, object], ...] = ()
    provider_measurements: tuple[dict[str, object], ...] = ()
    diagnoses: tuple[dict[str, object], ...] = ()
    interruption_chain: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "found": self.found,
            "metrics": list(self.metrics),
            "provider_measurements": list(self.provider_measurements),
            "diagnoses": list(self.diagnoses),
            "interruption_chain": self.interruption_chain,
        }


@dataclass(frozen=True)
class FirstBoundary:
    """The earliest boundary diagnosis, or an honest 'unknown'."""

    found: bool
    reason: str | None = None
    diagnosis_id: str | None = None
    code: str | None = None
    boundary: str | None = None
    turn_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    coordinate: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        if not self.found:
            return {"found": False, "reason": self.reason}
        return {
            "found": True,
            "diagnosis_id": self.diagnosis_id,
            "code": self.code,
            "boundary": self.boundary,
            "turn_ids": list(self.turn_ids),
            "evidence_ids": list(self.evidence_ids),
            "coordinate": self.coordinate,
        }


@dataclass(frozen=True)
class NotObserved:
    """Every coverage gap, limitation, and omission, unified."""

    coverage_gaps: tuple[dict[str, object], ...] = ()
    limitations: tuple[dict[str, object], ...] = ()
    omissions: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "coverage_gaps": list(self.coverage_gaps),
            "limitations": list(self.limitations),
            "omissions": list(self.omissions),
        }


@dataclass(frozen=True)
class Recomputable:
    """Whether a conclusion can still be recomputed from retained evidence."""

    reference: str
    found: bool
    kind: str
    recomputable: bool
    evidence_ids: tuple[str, ...] = ()
    missing_evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "found": self.found,
            "kind": self.kind,
            "recomputable": self.recomputable,
            "evidence_ids": list(self.evidence_ids),
            "missing_evidence_ids": list(self.missing_evidence_ids),
        }


@dataclass(frozen=True)
class SummaryDigest:
    """A compact, agent-facing digest of a whole incident."""

    session_id: str | None
    counts: dict[str, int]
    diagnoses: tuple[dict[str, object], ...]
    first_abnormal_boundary: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "counts": self.counts,
            "diagnoses": list(self.diagnoses),
            "first_abnormal_boundary": self.first_abnormal_boundary,
        }


@dataclass(frozen=True)
class Contradiction:
    """One evidence-linked contradiction found in an incident graph.

    ``subject`` is a per-kind semantic discriminator (a turn, a quantity, or an
    operation) that stays stable across incidents, so ``compare_incidents`` can
    tell a genuinely new contradiction from one both sessions share. It never
    contains source payload.
    """

    kind: str
    summary: str
    evidence_ids: tuple[str, ...]
    boundary: str | None = None
    turn_id: str | None = None
    subject: str | None = None

    def signature(self) -> tuple[str, str | None, str | None]:
        return (self.kind, self.boundary, self.subject)

    def _sort_key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.kind, self.subject or "", self.turn_id or "", self.evidence_ids)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
        }
        if self.boundary is not None:
            payload["boundary"] = self.boundary
        if self.turn_id is not None:
            payload["turn_id"] = self.turn_id
        if self.subject is not None:
            payload["subject"] = self.subject
        return payload


@dataclass(frozen=True)
class IncidentComparison:
    """A structured diff of an incident against a known-good session."""

    diagnoses_added: tuple[dict[str, object], ...] = ()
    diagnoses_removed: tuple[dict[str, object], ...] = ()
    turn_metric_deltas: tuple[dict[str, object], ...] = ()
    turn_metric_availability_changes: tuple[dict[str, object], ...] = ()
    unmatched_turns: dict[str, list[str]] = field(default_factory=dict)
    coverage_gaps_new: tuple[dict[str, object], ...] = ()
    coverage_gaps_removed: tuple[dict[str, object], ...] = ()
    contradictions_new: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "diagnoses_added": list(self.diagnoses_added),
            "diagnoses_removed": list(self.diagnoses_removed),
            "turn_metric_deltas": list(self.turn_metric_deltas),
            "turn_metric_availability_changes": list(self.turn_metric_availability_changes),
            "unmatched_turns": self.unmatched_turns,
            "coverage_gaps_new": list(self.coverage_gaps_new),
            "coverage_gaps_removed": list(self.coverage_gaps_removed),
            "contradictions_new": list(self.contradictions_new),
        }


# --------------------------------------------------------------------------- #
# Shared, deterministic evidence indexing                                      #
# --------------------------------------------------------------------------- #


def _derive_analysis(bundle: IncidentBundle) -> DerivedAnalysis:
    """Compute the deterministic analysis bound to this exact evidence digest."""

    return analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano="0",
    )


def _operation_and_event_turns(
    analysis: DerivedAnalysis,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read authoritative operation/event -> turn ownership from the analysis."""

    operation_turns: dict[str, str] = {}
    event_turns: dict[str, str] = {}
    for turn in analysis.projections.turns:
        for operation_id in turn.operation_ids:
            operation_turns[operation_id] = turn.turn_id
        for event_id in turn.event_ids:
            event_turns[event_id] = turn.turn_id
    return operation_turns, event_turns


def _sample_turn(sample: QualitySample, operation_turns: Mapping[str, str]) -> str | None:
    """Resolve a quality sample's turn exactly as the analyzer buckets it."""

    turn_value = sample.attributes.get("earshot.turn.id")
    if turn_value is None:
        operation_value = sample.attributes.get("earshot.operation.id")
        if isinstance(operation_value, str):
            turn_value = operation_turns.get(operation_value)
    if isinstance(turn_value, (str, int)) and not isinstance(turn_value, bool):
        return str(turn_value)
    return None


class _EvidenceIndex:
    """A deterministic index binding evidence ids to records and their turns."""

    def __init__(self, bundle: IncidentBundle, analysis: DerivedAnalysis) -> None:
        profile = bundle.profile
        self.operations = {item.operation_id: item for item in profile.operations}
        self.events = {item.event_id: item for item in profile.events}
        self.samples = {item.sample_id: item for item in profile.quality_samples}
        self.media_ids = {item.media_id for item in profile.media_refs}
        self.operation_turns, self.event_turns = _operation_and_event_turns(analysis)
        self.sample_turns = {
            sample_id: _sample_turn(sample, self.operation_turns)
            for sample_id, sample in self.samples.items()
        }
        self.all_ids = (
            set(self.operations) | set(self.events) | set(self.samples) | set(self.media_ids)
        )

    def evidence_turns(self, evidence_ids: Iterable[str]) -> set[str | None]:
        turns: set[str | None] = set()
        for evidence_id in evidence_ids:
            if evidence_id in self.operation_turns:
                turns.add(self.operation_turns[evidence_id])
            elif evidence_id in self.event_turns:
                turns.add(self.event_turns[evidence_id])
            elif evidence_id in self.sample_turns:
                turns.add(self.sample_turns[evidence_id])
        return turns

    def diagnosis_turn_ids(self, diagnosis: Diagnosis) -> tuple[str, ...]:
        turns = self.evidence_turns(diagnosis.evidence_refs)
        return tuple(sorted(turn for turn in turns if turn is not None))

    def anchor_point(self, evidence_id: str) -> TimePoint | None:
        operation = self.operations.get(evidence_id)
        if operation is not None:
            return operation.started_at
        event = self.events.get(evidence_id)
        if event is not None:
            return event.time
        sample = self.samples.get(evidence_id)
        if sample is not None:
            return sample.sample_window.start
        return None


def _diagnosis_dict(
    diagnosis: Diagnosis,
    index: _EvidenceIndex,
) -> dict[str, object]:
    return {
        "diagnosis_id": diagnosis.diagnosis_id,
        "code": diagnosis.code,
        "boundary": boundary_for_code(diagnosis.code),
        "turn_ids": list(index.diagnosis_turn_ids(diagnosis)),
        "summary": diagnosis.summary,
        "confidence": diagnosis.confidence,
        "evidence_ids": list(diagnosis.evidence_refs),
        "limitations": list(diagnosis.limitations),
    }


# --------------------------------------------------------------------------- #
# EvidenceQuery                                                                #
# --------------------------------------------------------------------------- #


class EvidenceQuery:
    """A deterministic, structured question surface over one incident.

    Construct from a decoded ``IncidentBundle``; the bound ``DerivedAnalysis`` is
    computed internally (deterministically) unless one is supplied. Every method
    returns a JSON-serializable structured result whose asserted facts cite the
    evidence ids they came from.
    """

    def __init__(
        self,
        bundle: IncidentBundle,
        analysis: DerivedAnalysis | None = None,
    ) -> None:
        self.bundle = bundle
        self.analysis = analysis if analysis is not None else _derive_analysis(bundle)
        self._index = _EvidenceIndex(bundle, self.analysis)
        self._turns_by_id = {turn.turn_id: turn for turn in self.analysis.projections.turns}

    # -- known_about_turn --------------------------------------------------- #

    def known_about_turn(self, turn_id: str) -> TurnKnowledge:
        """Return all latency metrics, diagnoses, and the interruption chain."""

        turn = self._turns_by_id.get(turn_id)
        if turn is None:
            return TurnKnowledge(turn_id=turn_id, found=False)

        metrics = tuple(_metric_dict(getattr(turn.metrics, name), name) for name in LATENCY_METRICS)
        provider = tuple(
            _metric_dict(metric, name)
            for name, metric in sorted(turn.metrics.provider_measurements.items())
        )
        diagnoses = tuple(
            _diagnosis_dict(diagnosis, self._index)
            for diagnosis in self.analysis.diagnoses
            if turn_id in self._index.diagnosis_turn_ids(diagnosis)
        )
        chain = (
            turn.interruption_chain.model_dump(mode="json", exclude_none=True)
            if turn.interruption_chain is not None
            else None
        )
        return TurnKnowledge(
            turn_id=turn_id,
            found=True,
            metrics=metrics,
            provider_measurements=provider,
            diagnoses=diagnoses,
            interruption_chain=chain,
        )

    # -- first_abnormal_boundary -------------------------------------------- #

    def _boundary_coordinate(self, diagnosis: Diagnosis) -> tuple[int, str, str, int] | None:
        """The earliest canonical coordinate among a diagnosis's cited evidence."""

        keys = [
            _coordinate_sort_key(point)
            for evidence_id in diagnosis.evidence_refs
            if (point := self._index.anchor_point(evidence_id)) is not None
        ]
        return min(keys) if keys else None

    def first_abnormal_boundary(self) -> FirstBoundary:
        """Return the earliest boundary diagnosis, ordered by its coordinate.

        Ordering happens only inside one comparable coordinate group (same clock
        domain and representation). If the boundary diagnoses span incomparable
        clocks -- or a coordinate is unavailable so an earliest cannot be proven
        -- the answer is an honest 'unknown' rather than an invented order.
        """

        boundary = [
            diagnosis
            for diagnosis in self.analysis.diagnoses
            if diagnosis.code in BOUNDARY_DIAGNOSIS_CODES
        ]
        if not boundary:
            return FirstBoundary(found=False, reason="no_boundary_diagnosis")

        keyed = [(self._boundary_coordinate(diagnosis), diagnosis) for diagnosis in boundary]

        if len(boundary) > 1:
            if any(key is None or key[0] != 0 for key, _ in keyed):
                return FirstBoundary(found=False, reason="boundary_coordinate_incomparable")
            groups = {(key[1], key[2]) for key, _ in keyed}  # type: ignore[index]
            if len(groups) != 1:
                return FirstBoundary(found=False, reason="boundaries_span_incomparable_clocks")

        _, diagnosis = min(
            keyed,
            key=lambda item: (
                item[0] if item[0] is not None else (2, "", 9, 0),
                item[1].code,
                item[1].diagnosis_id,
            ),
        )
        return FirstBoundary(
            found=True,
            diagnosis_id=diagnosis.diagnosis_id,
            code=diagnosis.code,
            boundary=boundary_for_code(diagnosis.code),
            turn_ids=self._index.diagnosis_turn_ids(diagnosis),
            evidence_ids=tuple(diagnosis.evidence_refs),
            coordinate=self._coordinate_dict(diagnosis),
        )

    def _coordinate_dict(self, diagnosis: Diagnosis) -> dict[str, object] | None:
        best: tuple[tuple[int, str, str, int], TimePoint] | None = None
        for evidence_id in diagnosis.evidence_refs:
            point = self._index.anchor_point(evidence_id)
            if point is None:
                continue
            candidate = _coordinate_sort_key(point)
            if best is None or candidate < best[0]:
                best = (candidate, point)
        if best is None or best[0][0] != 0:
            return None
        point = best[1]
        if point.monotonic_time_nano is not None:
            basis, at_nano = "monotonic", point.monotonic_time_nano
        elif point.source_time_unix_nano is not None:
            basis, at_nano = "source_wall", point.source_time_unix_nano
        else:
            basis, at_nano = "observed_wall", point.observed_time_unix_nano
        return {
            "clock_domain_id": point.clock_domain_id,
            "time_basis": basis,
            "at_nano": at_nano,
        }

    # -- not_observed ------------------------------------------------------- #

    def not_observed(self) -> NotObserved:
        """Unify coverage gaps, analysis/turn limitations, and omissions."""

        profile = self.bundle.profile
        coverage_gaps = tuple(
            sorted(
                (
                    {
                        "signal": coverage.signal,
                        "availability": coverage.availability,
                        "reason": coverage.reason,
                    }
                    for coverage in profile.coverage
                    if coverage.availability.lower() != "available"
                ),
                key=lambda item: (item["signal"], item["availability"] or ""),
            )
        )

        limitations: list[dict[str, object]] = [
            {"scope": "analysis", "limitation": limitation}
            for limitation in self.analysis.projections.limitations
        ]
        for turn in self.analysis.projections.turns:
            for name in LATENCY_METRICS:
                metric = getattr(turn.metrics, name)
                if metric.availability == "available" or metric.limitation is None:
                    continue
                limitations.append(
                    {
                        "scope": "turn",
                        "turn_id": turn.turn_id,
                        "metric": name,
                        "availability": metric.availability,
                        "limitation": metric.limitation,
                        "evidence_ids": list(metric.evidence_ids),
                    }
                )
        limitations.sort(
            key=lambda item: (
                item["scope"],
                str(item.get("turn_id") or ""),
                str(item.get("metric") or ""),
                str(item["limitation"]),
            )
        )

        omissions = tuple(
            sorted(
                (
                    {
                        "omission_id": omission.omission_id,
                        "capture_class": omission.capture_class,
                        "reason": omission.reason,
                        "count": omission.count,
                        "source_refs": list(omission.source_refs),
                    }
                    for omission in profile.privacy.omissions
                ),
                key=lambda item: str(item["omission_id"]),
            )
        )
        return NotObserved(
            coverage_gaps=coverage_gaps,
            limitations=tuple(limitations),
            omissions=omissions,
        )

    # -- recomputable ------------------------------------------------------- #

    def recomputable(self, reference: str) -> Recomputable:
        """Whether ``reference`` (a diagnosis id or metric) still resolves.

        ``reference`` is matched first as a diagnosis id, then as a metric named
        ``"<turn_id>/<metric>"`` or a bare metric name searched across turns. The
        conclusion is recomputable only when every evidence id it cited is still
        present in the retained bundle.
        """

        for diagnosis in self.analysis.diagnoses:
            if diagnosis.diagnosis_id == reference:
                return self._recomputable_from(reference, "diagnosis", diagnosis.evidence_refs)

        turn_id, _, metric_name = reference.partition("/")
        if metric_name and metric_name in LATENCY_METRICS:
            turn = self._turns_by_id.get(turn_id)
            if turn is not None:
                metric = getattr(turn.metrics, metric_name)
                return self._recomputable_from(reference, "metric", metric.evidence_ids)
        elif reference in LATENCY_METRICS:
            collected: list[str] = []
            for turn in self.analysis.projections.turns:
                collected.extend(getattr(turn.metrics, reference).evidence_ids)
            if collected:
                return self._recomputable_from(reference, "metric", tuple(collected))

        return Recomputable(
            reference=reference,
            found=False,
            kind="unknown",
            recomputable=False,
        )

    def _recomputable_from(
        self,
        reference: str,
        kind: str,
        evidence_ids: Sequence[str],
    ) -> Recomputable:
        cited = tuple(dict.fromkeys(evidence_ids))
        missing = tuple(
            evidence_id for evidence_id in cited if evidence_id not in self._index.all_ids
        )
        return Recomputable(
            reference=reference,
            found=True,
            kind=kind,
            recomputable=bool(cited) and not missing,
            evidence_ids=cited,
            missing_evidence_ids=missing,
        )

    # -- contradictions ----------------------------------------------------- #

    def contradictions(self) -> list[Contradiction]:
        """Detect evidence-linked contradictions in this incident."""

        return detect_contradictions(self.bundle, self.analysis)

    # -- summary ------------------------------------------------------------ #

    def summary(self) -> SummaryDigest:
        """Return a compact, agent-facing digest of the whole incident."""

        profile = self.bundle.profile
        diagnoses = tuple(
            _diagnosis_dict(diagnosis, self._index)
            for diagnosis in sorted(self.analysis.diagnoses, key=lambda item: item.diagnosis_id)
        )
        boundary_count = sum(
            1 for diagnosis in self.analysis.diagnoses if diagnosis.code in BOUNDARY_DIAGNOSIS_CODES
        )
        coverage_gap_count = sum(
            1 for coverage in profile.coverage if coverage.availability.lower() != "available"
        )
        counts = {
            "turn_count": len(self.analysis.projections.turns),
            "operation_count": len(profile.operations),
            "event_count": len(profile.events),
            "quality_sample_count": len(profile.quality_samples),
            "failed_operation_count": sum(
                item.status in {"error", "timeout", "failed"} for item in profile.operations
            ),
            "diagnosis_count": len(self.analysis.diagnoses),
            "boundary_diagnosis_count": boundary_count,
            "coverage_gap_count": coverage_gap_count,
            "contradiction_count": len(self.contradictions()),
        }
        return SummaryDigest(
            session_id=self.analysis.projections.session_id,
            counts=counts,
            diagnoses=diagnoses,
            first_abnormal_boundary=self.first_abnormal_boundary().as_dict(),
        )

    def compare_to(self, known_good: IncidentBundle) -> IncidentComparison:
        """Diff this incident against a known-good session."""

        return compare_incidents(self.bundle, known_good, incident_analysis=self.analysis)


# --------------------------------------------------------------------------- #
# Contradiction detection                                                      #
# --------------------------------------------------------------------------- #


def _measurements_by_turn(
    bundle: IncidentBundle,
    index: _EvidenceIndex,
) -> dict[str, dict[str, list[tuple[str, str, float, float]]]]:
    """Group each turn's provider/client scalars by measurement name.

    Returns ``turn -> name -> [(sample_id, observer, value_ms, uncertainty_ms)]``.
    Values in seconds are normalized to milliseconds. A same-sample companion
    measurement named ``"<name>.uncertainty"`` (same unit) supplies the scalar's
    uncertainty; absent that, uncertainty is zero. Companion measurements are not
    themselves treated as comparable quantities.
    """

    grouped: dict[str, dict[str, list[tuple[str, str, float, float]]]] = {}
    for sample in sorted(bundle.profile.quality_samples, key=lambda item: item.sample_id):
        turn = index.sample_turns.get(sample.sample_id)
        if turn is None or sample.evidence is None:
            continue
        observer = sample.evidence.observer
        uncertainties: dict[str, tuple[float, str]] = {}
        values: dict[str, tuple[float, str]] = {}
        for measurement in sample.measurements:
            if not isinstance(measurement.value, (int, float)) or isinstance(
                measurement.value, bool
            ):
                continue
            magnitude = float(measurement.value)
            unit = measurement.unit
            if unit == "s":
                magnitude *= 1000.0
                unit = "ms"
            if measurement.name.endswith(_UNCERTAINTY_SUFFIX):
                base = measurement.name[: -len(_UNCERTAINTY_SUFFIX)]
                uncertainties[base] = (abs(magnitude), unit)
            else:
                values[measurement.name] = (magnitude, unit)
        for name, (magnitude, unit) in values.items():
            uncertainty, uncertainty_unit = uncertainties.get(name, (0.0, unit))
            if uncertainty_unit != unit:
                uncertainty = 0.0
            grouped.setdefault(turn, {}).setdefault(name, []).append(
                (sample.sample_id, observer, magnitude, uncertainty)
            )
    return grouped


def _same_domain_reversed_contradictions(
    bundle: IncidentBundle,
    aligner: _ClockAligner,
) -> list[Contradiction]:
    contradictions: list[Contradiction] = []
    for operation in bundle.profile.operations:
        if operation.ended_at is None:
            continue
        delta = comparable_delta(operation.started_at, operation.ended_at, aligner)
        if delta.availability == "inconsistent" and delta.limitation == "same_domain_time_reversed":
            contradictions.append(
                Contradiction(
                    kind="same_domain_time_reversed",
                    summary="operation_interval_time_reversed",
                    evidence_ids=(operation.operation_id,),
                    boundary=boundary_for_code(f"{operation.operation_name}."),
                    turn_id=operation.turn_id,
                    subject=operation.operation_id,
                )
            )
    return contradictions


def _delivery_contradictions(
    bundle: IncidentBundle,
    *,
    event_name: str,
    kind: str,
    summary: str,
) -> list[Contradiction]:
    return [
        Contradiction(
            kind=kind,
            summary=summary,
            evidence_ids=(event.event_id,),
            boundary="transport",
            turn_id=event.turn_id,
            subject=event.event_id,
        )
        for event in bundle.profile.events
        if event.event_name == event_name
    ]


def _render_claim_contradictions(
    bundle: IncidentBundle,
    index: _EvidenceIndex,
) -> list[Contradiction]:
    """One source says render happened; coverage says it was not observed."""

    coverage_denied = any(
        coverage.signal in _RENDER_COVERAGE_SIGNALS and coverage.availability.lower() != "available"
        for coverage in bundle.profile.coverage
    )
    if not coverage_denied:
        return []

    render_evidence_by_turn: dict[str | None, set[str]] = {}
    for operation in bundle.profile.operations:
        if operation.operation_name == "render":
            turn = index.operation_turns.get(operation.operation_id, operation.turn_id)
            render_evidence_by_turn.setdefault(turn, set()).add(operation.operation_id)
    for event in bundle.profile.events:
        if event.event_name == _RENDER_STARTED_EVENT:
            turn = event.turn_id or index.event_turns.get(event.event_id)
            render_evidence_by_turn.setdefault(turn, set()).add(event.event_id)

    contradictions: list[Contradiction] = []
    for turn, evidence_ids in render_evidence_by_turn.items():
        contradictions.append(
            Contradiction(
                kind="render_claim_conflict",
                summary="render_observed_while_coverage_not_observed",
                evidence_ids=tuple(sorted(evidence_ids)),
                boundary="render",
                turn_id=turn,
                subject=turn,
            )
        )
    return contradictions


def _provider_client_contradictions(
    bundle: IncidentBundle,
    index: _EvidenceIndex,
) -> list[Contradiction]:
    """Two observers measured one quantity on one turn beyond their uncertainty."""

    contradictions: list[Contradiction] = []
    grouped = _measurements_by_turn(bundle, index)
    for turn in sorted(grouped):
        for name in sorted(grouped[turn]):
            entries = sorted(grouped[turn][name])
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    sample_a, observer_a, value_a, uncertainty_a = entries[i]
                    sample_b, observer_b, value_b, uncertainty_b = entries[j]
                    if observer_a == observer_b:
                        continue
                    if abs(value_a - value_b) > (uncertainty_a + uncertainty_b):
                        contradictions.append(
                            Contradiction(
                                kind="provider_client_disagreement",
                                summary="observers_disagree_beyond_uncertainty",
                                evidence_ids=tuple(sorted((sample_a, sample_b))),
                                boundary="measurement",
                                turn_id=turn,
                                subject=f"{turn}:{name}",
                            )
                        )
    return contradictions


def detect_contradictions(
    bundle: IncidentBundle,
    analysis: DerivedAnalysis | None = None,
) -> list[Contradiction]:
    """Return every evidence-linked contradiction in an incident graph.

    Detected kinds: ``same_domain_time_reversed`` (an operation interval whose
    same-domain endpoints are reversed, via ``comparable_delta``),
    ``duplicate_delivery`` and ``out_of_order_delivery`` (governed transport
    events), ``render_claim_conflict`` (render evidence exists while coverage
    says render was not observed), and ``provider_client_disagreement`` (two
    observers measure one quantity on one turn beyond their combined
    uncertainty). Deterministic and source-order-invariant; every contradiction
    cites real evidence ids. A graph with no contradiction yields an empty list.
    """

    if analysis is None:
        analysis = _derive_analysis(bundle)
    index = _EvidenceIndex(bundle, analysis)
    aligner = _ClockAligner(bundle.profile.clock_relations)

    contradictions: list[Contradiction] = []
    contradictions.extend(_same_domain_reversed_contradictions(bundle, aligner))
    contradictions.extend(
        _delivery_contradictions(
            bundle,
            event_name=_DUPLICATE_EVENT,
            kind="duplicate_delivery",
            summary="transport_message_duplicate",
        )
    )
    contradictions.extend(
        _delivery_contradictions(
            bundle,
            event_name=_OUT_OF_ORDER_EVENT,
            kind="out_of_order_delivery",
            summary="transport_message_out_of_order",
        )
    )
    contradictions.extend(_render_claim_contradictions(bundle, index))
    contradictions.extend(_provider_client_contradictions(bundle, index))
    contradictions.sort(key=lambda item: item._sort_key())
    return contradictions


# --------------------------------------------------------------------------- #
# Known-good comparison                                                        #
# --------------------------------------------------------------------------- #


def _diagnosis_keys(
    analysis: DerivedAnalysis,
    index: _EvidenceIndex,
) -> dict[tuple[str, str, tuple[str, ...]], dict[str, object]]:
    """Map each diagnosis to a cross-incident identity (code, boundary, turns)."""

    keyed: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}
    for diagnosis in sorted(analysis.diagnoses, key=lambda item: item.diagnosis_id):
        turns = index.diagnosis_turn_ids(diagnosis)
        key = (diagnosis.code, boundary_for_code(diagnosis.code), turns)
        keyed.setdefault(
            key,
            {
                "code": diagnosis.code,
                "boundary": boundary_for_code(diagnosis.code),
                "turn_ids": list(turns),
                "diagnosis_id": diagnosis.diagnosis_id,
                "evidence_ids": list(diagnosis.evidence_refs),
            },
        )
    return keyed


def _coverage_gap_tuples(bundle: IncidentBundle) -> dict[tuple[str, str], dict[str, object]]:
    gaps: dict[tuple[str, str], dict[str, object]] = {}
    for coverage in bundle.profile.coverage:
        if coverage.availability.lower() == "available":
            continue
        gaps[(coverage.signal, coverage.availability)] = {
            "signal": coverage.signal,
            "availability": coverage.availability,
            "reason": coverage.reason,
        }
    return gaps


def compare_incidents(
    incident: IncidentBundle,
    known_good: IncidentBundle,
    *,
    incident_analysis: DerivedAnalysis | None = None,
    known_good_analysis: DerivedAnalysis | None = None,
) -> IncidentComparison:
    """Diff an incident against a known-good session, structurally and honestly.

    Reports diagnoses added/removed (by code + boundary + turn), per-turn latency
    deltas *only* where both sides are ``available`` and comparable (otherwise an
    availability change is reported, never a fabricated delta), new/removed
    coverage gaps, and contradictions the incident has that the known-good does
    not. Turns are matched by ``turn_id``; unmatched turns are reported
    explicitly. This is "what changed relative to the last known-good release".
    """

    if incident_analysis is None:
        incident_analysis = _derive_analysis(incident)
    if known_good_analysis is None:
        known_good_analysis = _derive_analysis(known_good)
    incident_index = _EvidenceIndex(incident, incident_analysis)
    known_index = _EvidenceIndex(known_good, known_good_analysis)

    # -- diagnoses --------------------------------------------------------- #
    incident_keys = _diagnosis_keys(incident_analysis, incident_index)
    known_keys = _diagnosis_keys(known_good_analysis, known_index)
    diagnoses_added = tuple(
        value for key, value in sorted(incident_keys.items()) if key not in known_keys
    )
    diagnoses_removed = tuple(
        value for key, value in sorted(known_keys.items()) if key not in incident_keys
    )

    # -- per-turn metric deltas / availability changes --------------------- #
    incident_turns = {turn.turn_id: turn for turn in incident_analysis.projections.turns}
    known_turns = {turn.turn_id: turn for turn in known_good_analysis.projections.turns}
    matched = sorted(set(incident_turns) & set(known_turns))

    deltas: list[dict[str, object]] = []
    availability_changes: list[dict[str, object]] = []
    for turn_id in matched:
        incident_metrics = incident_turns[turn_id].metrics
        known_metrics = known_turns[turn_id].metrics
        for name in LATENCY_METRICS:
            incident_metric = getattr(incident_metrics, name)
            known_metric = getattr(known_metrics, name)
            both_available = (
                incident_metric.availability == "available"
                and known_metric.availability == "available"
            )
            if (
                both_available
                and incident_metric.unit == known_metric.unit
                and incident_metric.value is not None
                and known_metric.value is not None
            ):
                deltas.append(
                    {
                        "turn_id": turn_id,
                        "metric": name,
                        "unit": incident_metric.unit,
                        "known_good_value": known_metric.value,
                        "incident_value": incident_metric.value,
                        "delta": incident_metric.value - known_metric.value,
                    }
                )
            elif incident_metric.availability != known_metric.availability or both_available:
                # Availability changed, or both available but units are not
                # comparable: report the change, never a fabricated delta.
                availability_changes.append(
                    {
                        "turn_id": turn_id,
                        "metric": name,
                        "known_good_availability": known_metric.availability,
                        "incident_availability": incident_metric.availability,
                        "comparable": both_available,
                    }
                )

    unmatched_turns = {
        "only_in_incident": sorted(set(incident_turns) - set(known_turns)),
        "only_in_known_good": sorted(set(known_turns) - set(incident_turns)),
    }

    # -- coverage gaps ----------------------------------------------------- #
    incident_gaps = _coverage_gap_tuples(incident)
    known_gaps = _coverage_gap_tuples(known_good)
    coverage_gaps_new = tuple(
        value for key, value in sorted(incident_gaps.items()) if key not in known_gaps
    )
    coverage_gaps_removed = tuple(
        value for key, value in sorted(known_gaps.items()) if key not in incident_gaps
    )

    # -- contradictions ---------------------------------------------------- #
    incident_contradictions = detect_contradictions(incident, incident_analysis)
    known_signatures = {
        contradiction.signature()
        for contradiction in detect_contradictions(known_good, known_good_analysis)
    }
    seen: set[tuple[str, str | None, str | None]] = set()
    contradictions_new: list[dict[str, object]] = []
    for contradiction in incident_contradictions:
        signature = contradiction.signature()
        if signature in known_signatures or signature in seen:
            continue
        seen.add(signature)
        contradictions_new.append(contradiction.as_dict())

    return IncidentComparison(
        diagnoses_added=diagnoses_added,
        diagnoses_removed=diagnoses_removed,
        turn_metric_deltas=tuple(deltas),
        turn_metric_availability_changes=tuple(availability_changes),
        unmatched_turns=unmatched_turns,
        coverage_gaps_new=coverage_gaps_new,
        coverage_gaps_removed=coverage_gaps_removed,
        contradictions_new=tuple(contradictions_new),
    )
