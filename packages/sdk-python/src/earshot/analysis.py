"""Deterministic, evidence-linked analysis over the incident graph.

Analysis is a replaceable projection. It never mutates source facts and it never
subtracts timestamps from unrelated clock domains. Presentation values use
milliseconds; the artifact keeps exact nanoseconds.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from .contract import (
    UINT64_MAX,
    ClockRelation,
    DerivedAnalysis,
    Diagnosis,
    Event,
    IncidentBundle,
    Operation,
    QualitySample,
    TimePoint,
)
from .measurement_semantics import measurement_value_limitation
from .versions import ANALYZER_VERSION

ANALYZER_NAME = "earshot.deterministic"
_IJSON_INTEGER_MAX = 9_007_199_254_740_991
_SUCCESS_BOUNDARY_STATUSES = frozenset({"completed", "ok", "unset"})
_PROVIDER_DURATION_ATTRIBUTE = "earshot.analysis.provider_duration_attribute"
_PARTICIPANT_ROLE_DIRECTION = {
    "agent": "output",
    "assistant": "output",
    "user": "input",
}
_CONFIDENCE_RANK = {
    "measured": 0,
    "estimated": 1,
    "inferred": 2,
    "unavailable": 3,
}
_T = TypeVar("_T")
_Coordinate = tuple[str, str, int]


def _comparable_coordinate(point: TimePoint) -> _Coordinate | None:
    domain = point.clock_domain_id
    if domain is None:
        return None
    if point.monotonic_time_nano is not None:
        return domain, "monotonic", int(point.monotonic_time_nano)
    if point.source_time_unix_nano is not None:
        return domain, "source_wall", int(point.source_time_unix_nano)
    if point.observed_time_unix_nano is not None:
        return domain, "observed_wall", int(point.observed_time_unix_nano)
    return None


def _order_by_comparable_coordinate(
    items: Iterable[_T],
    *,
    coordinate: Callable[[_T], _Coordinate | None],
    identity: Callable[[_T], str],
) -> tuple[_T, ...]:
    """Return a permutation-invariant presentation order.

    Clock-domain and timestamp-basis labels canonically group comparable points;
    only the numeric order inside one group has temporal meaning. Group and identity
    ordering is a deterministic serialization rule, not cross-clock causality.
    """

    def presentation_key(item: _T) -> tuple[int, str, str, int, str]:
        item_coordinate = coordinate(item)
        if item_coordinate is None:
            return 1, "", "", 0, identity(item)
        domain, basis, value = item_coordinate
        return 0, domain, basis, value, identity(item)

    return tuple(sorted(items, key=presentation_key))


def _order_by_comparable_time(
    items: Iterable[_T],
    *,
    point: Callable[[_T], TimePoint],
    identity: Callable[[_T], str],
) -> tuple[_T, ...]:
    return _order_by_comparable_coordinate(
        items,
        coordinate=lambda item: _comparable_coordinate(point(item)),
        identity=identity,
    )


def _matches_stream_direction(
    value: Operation | Event,
    stream_directions: Mapping[str, str],
    expected_direction: str,
    *,
    require_explicit: bool = False,
    participant_directions: Mapping[str, str] | None = None,
    operations_by_id: Mapping[str, Operation] | None = None,
    operations_by_otel: Mapping[tuple[str, str], Operation] | None = None,
) -> bool:
    directions: set[str] = set()
    unresolved_stream = False
    unresolved_participant = False

    def add_record_ownership(record: Operation | Event) -> None:
        nonlocal unresolved_participant, unresolved_stream
        if record.stream_id is not None:
            direction = stream_directions.get(record.stream_id)
            if direction is None:
                unresolved_stream = True
            else:
                directions.add(direction)
        if record.participant_id is not None and participant_directions is not None:
            direction = participant_directions.get(record.participant_id)
            if direction is None:
                unresolved_participant = True
            else:
                directions.add(direction)

    add_record_ownership(value)
    if isinstance(value, Event):
        linked_operation = (
            operations_by_id.get(value.operation_id)
            if operations_by_id is not None and value.operation_id is not None
            else None
        )
        if (
            linked_operation is None
            and operations_by_otel is not None
            and value.trace_id is not None
            and value.span_id is not None
        ):
            linked_operation = operations_by_otel.get((value.trace_id, value.span_id))
        if linked_operation is not None:
            add_record_ownership(linked_operation)

    if unresolved_stream or unresolved_participant:
        return False
    if directions:
        return directions == {expected_direction}
    return not require_explicit


# --- Boundary-attribution SLO recipe -----------------------------------------
# Deterministic thresholds that turn a governed measurement into a boundary
# hypothesis. Every default is a conservative, real-time-voice-oriented value; a
# caller may override any subset through ``SloRecipe`` without touching the rules.
DEFAULT_PACKET_LOSS_RATIO_SLO = 0.05  # fraction 0..1; >5% loss is audibly degraded
DEFAULT_JITTER_MS_SLO = 30.0  # ms of inter-arrival jitter a jitter buffer must absorb
DEFAULT_ROUND_TRIP_TIME_MS_SLO = 150.0  # ms RTT; conversation feels laggy beyond this
DEFAULT_RENDER_START_LATENCY_MS_SLO = 1500.0  # ms turn-commit -> audio actually rendering
DEFAULT_STAGE_LATENCY_MS_SLO = 1500.0  # ms a single stt/llm/tts stage may occupy
DEFAULT_ENDPOINTING_LATENCY_MS_SLO = 1000.0  # ms a turn_detection (EOU) decision may take


@dataclass(frozen=True)
class SloRecipe:
    """Configurable thresholds for the boundary-attribution engine.

    A metric is only diagnosed as an SLO breach when it was actually measured;
    an ``unavailable``/``not_observed`` metric yields no diagnosis (the analyzer
    says *unknown* rather than inventing slowness it did not observe). Latency
    fields are milliseconds; ``packet_loss_ratio`` is a unit-interval fraction.
    """

    packet_loss_ratio: float = DEFAULT_PACKET_LOSS_RATIO_SLO
    jitter_ms: float = DEFAULT_JITTER_MS_SLO
    round_trip_time_ms: float = DEFAULT_ROUND_TRIP_TIME_MS_SLO
    render_start_latency_ms: float = DEFAULT_RENDER_START_LATENCY_MS_SLO
    stt_latency_ms: float = DEFAULT_STAGE_LATENCY_MS_SLO
    llm_latency_ms: float = DEFAULT_STAGE_LATENCY_MS_SLO
    tts_latency_ms: float = DEFAULT_STAGE_LATENCY_MS_SLO
    endpointing_latency_ms: float = DEFAULT_ENDPOINTING_LATENCY_MS_SLO


DEFAULT_SLO_RECIPE = SloRecipe()


@dataclass(frozen=True)
class Delta:
    availability: str
    nanoseconds: int | None
    basis: str
    confidence: str
    limitation: str | None = None
    uncertainty: int | None = None

    def as_dict(self) -> dict[str, object]:
        output: dict[str, object] = {
            "availability": self.availability,
            "basis": self.basis,
            "confidence": self.confidence,
        }
        if self.nanoseconds is not None:
            output["value"] = self.nanoseconds / 1_000_000
            output["unit"] = "ms"
        if self.limitation:
            output["limitation"] = self.limitation
        return output


def _shared_time_deltas(start: TimePoint, end: TimePoint) -> tuple[tuple[str, int], ...]:
    """Return every authored same-domain coordinate delta without choosing a clock."""

    if not start.clock_domain_id or start.clock_domain_id != end.clock_domain_id:
        return ()
    return tuple(
        (basis, int(end_value) - int(start_value))
        for basis, field_name in (
            ("monotonic", "monotonic_time_nano"),
            ("source_wall", "source_time_unix_nano"),
            ("observed_wall", "observed_time_unix_nano"),
        )
        if (start_value := getattr(start, field_name)) is not None
        and (end_value := getattr(end, field_name)) is not None
    )


class _ClockAligner:
    """Convert a wall timestamp between clock domains using declared calibrations.

    Only ``source_time_unix_nano`` is aligned: monotonic values are domain-local
    and are never comparable across domains. Alignment succeeds only inside a
    relation's declared validity window and always carries the calibration's own
    uncertainty forward, so a cross-domain latency stays honestly estimated.
    """

    def __init__(self, relations: Sequence[ClockRelation] = ()) -> None:
        self._by_pair: dict[tuple[str, str], list[ClockRelation]] = defaultdict(list)
        for relation in sorted(relations, key=lambda item: item.relation_id):
            key = (relation.from_clock_domain_id, relation.to_clock_domain_id)
            self._by_pair[key].append(relation)

    def align(self, point: TimePoint, target_domain: str) -> tuple[int, int] | None:
        """Return ``(aligned_unix_nano, added_uncertainty_nano)`` or ``None``.

        A direct ``point.domain -> target`` relation is applied forward; failing
        that, an inverse ``target -> point.domain`` relation is applied in reverse.
        """

        domain = point.clock_domain_id
        if (
            domain is None
            or not target_domain
            or domain == target_domain
            or point.source_time_unix_nano is None
        ):
            return None
        wall = int(point.source_time_unix_nano)
        for relation in self._by_pair.get((domain, target_domain), ()):
            aligned = self._apply(relation, wall, forward=True)
            if aligned is not None:
                return aligned
        for relation in self._by_pair.get((target_domain, domain), ()):
            aligned = self._apply(relation, wall, forward=False)
            if aligned is not None:
                return aligned
        return None

    @staticmethod
    def _apply(relation: ClockRelation, wall: int, *, forward: bool) -> tuple[int, int] | None:
        # The validity window bounds the point's own timestamp; outside it the
        # calibration is not trusted and no alignment is offered.
        if relation.valid_from_unix_nano is not None and wall < int(relation.valid_from_unix_nano):
            return None
        if relation.valid_to_unix_nano is not None and wall > int(relation.valid_to_unix_nano):
            return None
        reference = (
            int(relation.reference_unix_nano) if relation.reference_unix_nano is not None else wall
        )
        correction = int(relation.offset_nano)
        if relation.drift_ppm is not None:
            correction += int(relation.drift_ppm * (wall - reference) / 1e6)
        aligned = wall + correction if forward else wall - correction
        added_uncertainty = int(relation.uncertainty_nano or "0")
        return (aligned, added_uncertainty)


def comparable_delta(
    start: TimePoint,
    end: TimePoint,
    aligner: _ClockAligner | None = None,
) -> Delta:
    """Subtract evidence sharing a clock domain, or a declared calibration across them.

    Within one clock domain an exact difference is taken across every shared basis,
    failing closed if any basis is reversed. Across domains a value is produced only
    when a declared, in-window ``ClockRelation`` aligns the endpoints; the
    calibration's own uncertainty is propagated and the result is at most
    ``estimated``. Absent such a relation the latency stays ``unavailable`` rather
    than clamped to zero.
    """

    if start.clock_domain_id and start.clock_domain_id == end.clock_domain_id:
        shared_deltas = _shared_time_deltas(start, end)
        reversed_basis = next((basis for basis, value in shared_deltas if value < 0), None)
        if reversed_basis is not None:
            return Delta(
                "inconsistent",
                None,
                reversed_basis,
                "unavailable",
                "same_domain_time_reversed",
            )

        deltas_by_basis = dict(shared_deltas)
        if "monotonic" in deltas_by_basis:
            value = deltas_by_basis["monotonic"]
            basis = "monotonic"
        elif "source_wall" in deltas_by_basis:
            value = deltas_by_basis["source_wall"]
            basis = "source_wall"
        else:
            return Delta(
                "unavailable",
                None,
                "clock_domain",
                "unavailable",
                "timestamp_representation_unavailable",
            )

        uncertainty = int(start.uncertainty_nano or "0") + int(end.uncertainty_nano or "0")
        confidence = "estimated" if uncertainty else "measured"
        return Delta("available", value, basis, confidence, uncertainty=uncertainty)

    # Different clock domains: only a declared, in-window calibration can relate
    # wall timestamps. Monotonic values are domain-local and are never aligned.
    if (
        aligner is not None
        and start.clock_domain_id
        and end.clock_domain_id
        and start.source_time_unix_nano is not None
        and end.source_time_unix_nano is not None
    ):
        aligned = aligner.align(end, start.clock_domain_id)
        if aligned is not None:
            aligned_end, added_uncertainty = aligned
            value = aligned_end - int(start.source_time_unix_nano)
            if value < 0:
                return Delta(
                    "inconsistent",
                    None,
                    "cross_clock_calibrated",
                    "unavailable",
                    "calibrated_time_reversed",
                )
            uncertainty = (
                int(start.uncertainty_nano or "0")
                + int(end.uncertainty_nano or "0")
                + added_uncertainty
            )
            # A calibrated cross-clock latency is at most estimated, never measured.
            return Delta(
                "available",
                value,
                "cross_clock_calibrated",
                "estimated",
                uncertainty=uncertainty,
            )

    return Delta(
        "unavailable",
        None,
        "clock_domain",
        "unavailable",
        "cross_clock_domain",
    )


def _earliest_event(events: Iterable[Event], names: set[str]) -> Event | None:
    candidates = [event for event in events if event.event_name in names]
    if not candidates:
        return None
    domains = {event.time.clock_domain_id for event in candidates}
    if len(domains) == 1 and None not in domains:
        if all(event.time.monotonic_time_nano is not None for event in candidates):
            return min(
                candidates,
                key=lambda event: (int(event.time.monotonic_time_nano or "0"), event.event_id),
            )
        if all(event.time.source_time_unix_nano is not None for event in candidates):
            return min(
                candidates,
                key=lambda event: (
                    int(event.time.source_time_unix_nano or "0"),
                    event.event_id,
                ),
            )
    # There is no honest temporal order across incomparable representations.
    return None


def _operation_point_event(
    operation: Operation,
    event_name: str,
    time: TimePoint,
) -> Event:
    """Create a private, evidence-linked event used only by analysis."""

    return Event(
        # The projection must cite real evidence. Reuse the source operation ID
        # rather than manufacturing an event identity absent from the artifact.
        event_id=operation.operation_id,
        session_id=operation.session_id,
        event_name=event_name,
        time=time,
        operation_id=operation.operation_id,
        participant_id=operation.participant_id,
        stream_id=operation.stream_id,
        turn_id=operation.turn_id,
        trace_id=operation.trace_id,
        span_id=operation.span_id,
        evidence=operation.evidence,
        attributes={"earshot.analysis.synthetic_projection": True},
    )


def _operation_start_event(operation: Operation, event_name: str) -> Event:
    return _operation_point_event(operation, event_name, operation.started_at)


def _operation_end_event(operation: Operation, event_name: str) -> Event | None:
    if operation.ended_at is None:
        return None
    return _operation_point_event(operation, event_name, operation.ended_at)


def _shift_time_point(point: TimePoint, seconds: object) -> TimePoint | None:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    try:
        seconds_value = float(seconds)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(seconds_value) or seconds_value < 0:
        return None
    scaled = seconds_value * 1_000_000_000
    if not math.isfinite(scaled) or scaled > UINT64_MAX:
        return None
    delta = round(scaled)
    update: dict[str, str] = {}
    if point.source_time_unix_nano is not None:
        shifted = int(point.source_time_unix_nano) + delta
        if shifted > UINT64_MAX:
            return None
        update["source_time_unix_nano"] = str(shifted)
    if point.monotonic_time_nano is not None:
        shifted = int(point.monotonic_time_nano) + delta
        if shifted > UINT64_MAX:
            return None
        update["monotonic_time_nano"] = str(shifted)
    if not update:
        return None
    return point.model_copy(update=update)


def _point_exceeds_comparable_end(point: TimePoint, end: TimePoint) -> bool:
    """Return true when any shared authored clock basis places ``point`` after ``end``."""

    return any(delta < 0 for _, delta in _shared_time_deltas(point, end))


def _provider_latency_event(
    operation: Operation,
    event_name: str,
    attribute_names: tuple[str, ...],
) -> Event | None:
    """Project a first-output point only from an explicit provider duration."""

    for attribute_name in attribute_names:
        if attribute_name not in operation.attributes:
            continue
        point = _shift_time_point(operation.started_at, operation.attributes[attribute_name])
        if point is None:
            continue
        if operation.ended_at is not None and _point_exceeds_comparable_end(
            point, operation.ended_at
        ):
            continue
        projected = _operation_point_event(operation, event_name, point)
        return projected.model_copy(
            update={
                "attributes": {
                    **projected.attributes,
                    _PROVIDER_DURATION_ATTRIBUTE: attribute_name,
                }
            }
        )
    return None


def _first_provider_latency_event(
    operations: Sequence[Operation],
    operation_names: set[str],
    event_name: str,
    attribute_names: tuple[str, ...],
) -> Event | None:
    candidates = [
        event
        for operation in operations
        if operation.operation_name in operation_names
        if operation.status in _SUCCESS_BOUNDARY_STATUSES
        if (event := _provider_latency_event(operation, event_name, attribute_names)) is not None
    ]
    return _earliest_event(candidates, {event_name})


def _quality_measurements(samples: Sequence[QualitySample]) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[tuple[str, int | float, str, str, str]]] = defaultdict(list)
    invalid: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sample in sorted(samples, key=lambda item: item.sample_id):
        for measurement in sample.measurements:
            limitation = measurement_value_limitation(
                measurement.name,
                measurement.value,
                measurement.unit,
            )
            if limitation is not None:
                invalid[measurement.name].append((sample.sample_id, limitation))
                continue
            if not isinstance(measurement.value, (int, float)) or isinstance(
                measurement.value, bool
            ):
                continue
            value = measurement.value
            if isinstance(value, int):
                if abs(value) > _IJSON_INTEGER_MAX:
                    continue
            elif not math.isfinite(value):
                continue
            if measurement.unit == "s":
                value *= 1_000
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                unit = "ms"
            else:
                unit = measurement.unit
            grouped[measurement.name].append(
                (
                    sample.sample_id,
                    value,
                    unit,
                    measurement.aggregation,
                    (
                        sample.evidence.confidence
                        if sample.evidence is not None
                        and sample.evidence.availability == "available"
                        else "unavailable"
                    ),
                )
            )

    projected: dict[str, dict[str, object]] = {}
    confidence_rank = {
        "measured": 0,
        "estimated": 1,
        "inferred": 2,
        "unavailable": 3,
    }
    for name in sorted(set(grouped) | set(invalid)):
        entries = sorted(
            grouped[name],
            key=lambda entry: (
                entry[0],
                entry[3],
                entry[2],
                type(entry[1]).__name__,
                repr(entry[1]),
                entry[4],
            ),
        )
        rejected = invalid.get(name, [])
        if rejected:
            limitations = sorted({limitation for _, limitation in rejected})
            projected[name] = {
                "availability": "unavailable",
                "basis": "provider_measurement",
                "confidence": "unavailable",
                "limitation": (
                    limitations[0]
                    if len(limitations) == 1
                    else "multiple_semantic_value_violations"
                ),
                "evidence_ids": sorted(
                    {entry[0] for entry in entries} | {sample_id for sample_id, _ in rejected}
                ),
            }
            continue
        aggregations = {entry[3] for entry in entries}
        evidence_ids = sorted({entry[0] for entry in entries})
        if aggregations == {"delta"}:
            units = {entry[2] for entry in entries}
            if len(units) != 1:
                projected[name] = {
                    "availability": "unavailable",
                    "basis": "provider_delta_sum",
                    "confidence": "unavailable",
                    "limitation": "incompatible_delta_units",
                    "evidence_ids": evidence_ids,
                }
                continue
            confidences = {entry[4] for entry in entries}
            confidence = max(
                confidences,
                key=lambda item: (confidence_rank.get(item, len(confidence_rank)), item),
            )
            values = [entry[1] for entry in entries]
            if all(isinstance(value, int) for value in values):
                total: int | float | None = sum(values)
                if abs(total) > _IJSON_INTEGER_MAX:
                    projected[name] = {
                        "availability": "unavailable",
                        "basis": "provider_delta_sum",
                        "confidence": "unavailable",
                        "limitation": "delta_sum_outside_i_json_domain",
                        "evidence_ids": evidence_ids,
                    }
                    continue
            else:
                try:
                    total = math.fsum(values)
                except OverflowError:
                    total = None
            if total is None or not math.isfinite(total):
                projected[name] = {
                    "availability": "unavailable",
                    "basis": "provider_delta_sum",
                    "confidence": "unavailable",
                    "limitation": "delta_sum_not_finite",
                    "evidence_ids": evidence_ids,
                }
                continue
            projected[name] = {
                "availability": "available",
                "basis": "provider_delta_sum",
                "confidence": confidence,
                "value": total,
                "unit": next(iter(units)),
                "evidence_ids": evidence_ids,
            }
            continue
        if len(aggregations) > 1:
            projected[name] = {
                "availability": "unavailable",
                "basis": "provider_measurement",
                "confidence": "unavailable",
                "limitation": "mixed_measurement_aggregation",
                "evidence_ids": evidence_ids,
            }
            continue

        # Instant/cumulative observations are snapshots, not additive windows.
        # Select the first sample deterministically, but refuse to choose between
        # conflicting same-name snapshots authored inside that one evidence record.
        sample_id = entries[0][0]
        selected_entries = [entry for entry in entries if entry[0] == sample_id]
        selected_shapes = {
            (type(entry[1]).__name__, repr(entry[1]), *entry[2:]) for entry in selected_entries
        }
        if len(selected_shapes) > 1:
            projected[name] = {
                "availability": "unavailable",
                "basis": "provider_measurement",
                "confidence": "unavailable",
                "limitation": "ambiguous_measurements_in_sample",
                "evidence_ids": [sample_id],
            }
            continue
        _sample_id, value, unit, _aggregation, confidence = selected_entries[0]
        if isinstance(value, int) and abs(value) > _IJSON_INTEGER_MAX:
            projected[name] = {
                "availability": "unavailable",
                "basis": "provider_measurement",
                "confidence": "unavailable",
                "limitation": "measurement_outside_i_json_domain",
                "evidence_ids": [sample_id],
            }
            continue
        projected[name] = {
            "availability": "available",
            "basis": "provider_direct",
            "confidence": confidence,
            "value": value,
            "unit": unit,
            "evidence_ids": [sample_id],
        }
    return projected


def _first_operation(operations: Sequence[Operation], names: set[str]) -> Operation | None:
    candidates = [
        operation
        for operation in operations
        if operation.operation_name in names and operation.status in _SUCCESS_BOUNDARY_STATUSES
    ]
    if not candidates:
        return None
    domains = {operation.started_at.clock_domain_id for operation in candidates}
    if len(domains) == 1 and None not in domains:
        if all(operation.started_at.monotonic_time_nano is not None for operation in candidates):
            return min(
                candidates,
                key=lambda operation: (
                    int(operation.started_at.monotonic_time_nano or "0"),
                    operation.operation_id,
                ),
            )
        if all(operation.started_at.source_time_unix_nano is not None for operation in candidates):
            return min(
                candidates,
                key=lambda operation: (
                    int(operation.started_at.source_time_unix_nano or "0"),
                    operation.operation_id,
                ),
            )
    return None


def _latency_metric(
    anchor: Event | None,
    target: Event | None,
    basis: str,
    aligner: _ClockAligner | None = None,
) -> dict[str, object]:
    if anchor is None:
        return {
            "availability": "not_observed",
            "basis": basis,
            "confidence": "unavailable",
            "limitation": "turn_anchor_not_observed",
            "evidence_ids": [],
        }
    if target is None:
        return {
            "availability": "not_observed",
            "basis": basis,
            "confidence": "unavailable",
            "limitation": "target_signal_not_observed",
            "evidence_ids": [anchor.event_id],
        }
    output = comparable_delta(anchor.time, target.time, aligner).as_dict()
    confidence_candidates = [str(output["confidence"])]
    for boundary in (anchor, target):
        if boundary.attributes.get("earshot.analysis.synthetic_projection"):
            confidence_candidates.append("estimated")
        if boundary.evidence is None:
            confidence_candidates.append("unavailable")
        else:
            confidence_candidates.append(
                boundary.evidence.confidence
                if boundary.evidence.availability == "available"
                else "unavailable"
            )
    output["confidence"] = max(
        (
            candidate if candidate in _CONFIDENCE_RANK else "unavailable"
            for candidate in confidence_candidates
        ),
        key=lambda candidate: _CONFIDENCE_RANK[candidate],
    )
    output["basis"] = basis
    output["evidence_ids"] = [anchor.event_id, target.event_id]
    return output


def _interval_nanos(operation: Operation, aligner: _ClockAligner | None = None) -> int | None:
    if operation.ended_at is None:
        return None
    delta = comparable_delta(operation.started_at, operation.ended_at, aligner)
    return delta.nanoseconds if delta.availability == "available" else None


def _tool_metrics(
    operations: Sequence[Operation],
    aligner: _ClockAligner | None = None,
) -> dict[str, object]:
    tools = [item for item in operations if item.operation_name == "tool"]
    durations = [(item, _interval_nanos(item, aligner)) for item in tools]
    timed_operation_count = sum(value is not None for _, value in durations)
    untimed_operation_count = len(durations) - timed_operation_count
    total = sum(value for _, value in durations if value is not None)
    if untimed_operation_count == 0:
        total_work_completeness = "complete"
    elif timed_operation_count:
        total_work_completeness = "partial"
    else:
        total_work_completeness = "unavailable"

    # Calculate union wall time only within comparable clock domains. Intervals in
    # different domains remain separate rather than inventing a global critical path.
    by_basis: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for operation, duration in durations:
        if duration is None or operation.ended_at is None:
            continue
        start: str | None
        end: str | None
        basis: str
        if (
            operation.started_at.monotonic_time_nano is not None
            and operation.ended_at.monotonic_time_nano is not None
        ):
            start = operation.started_at.monotonic_time_nano
            end = operation.ended_at.monotonic_time_nano
            basis = "monotonic"
        elif (
            operation.started_at.source_time_unix_nano is not None
            and operation.ended_at.source_time_unix_nano is not None
        ):
            start = operation.started_at.source_time_unix_nano
            end = operation.ended_at.source_time_unix_nano
            basis = "source_wall"
        else:
            continue
        by_basis[(operation.started_at.clock_domain_id, basis)].append((int(start), int(end)))

    elapsed_by_domain: dict[str, dict[str, float]] = {}
    for (domain, basis), intervals in sorted(by_basis.items()):
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        elapsed_by_domain.setdefault(domain, {})[basis] = (
            sum(end - start for start, end in merged) / 1_000_000
        )

    output: dict[str, object] = {
        "operation_count": len(tools),
        "timed_operation_count": timed_operation_count,
        "untimed_operation_count": untimed_operation_count,
        "total_work_ms": total / 1_000_000,
        "total_work_completeness": total_work_completeness,
        "elapsed_ms_by_clock_domain": elapsed_by_domain,
        "evidence_ids": sorted(item.operation_id for item in tools),
    }
    if untimed_operation_count:
        output["limitation"] = "incomplete_tool_intervals"
    return output


def _provider_stage_latency_fallback(
    current: dict[str, object],
    provider_measurements: dict[str, dict[str, object]],
    measurement_names: Sequence[str],
    *,
    target: Event | None,
    target_is_provider_projection: bool,
    operations: Sequence[Operation],
    attribute_names: Sequence[str],
) -> dict[str, object]:
    """Use a measured stage duration when turn-relative timing cannot be derived."""

    if current.get("availability") not in {"not_observed", "inconsistent"}:
        return current
    direct = next(
        (
            provider_measurements[name]
            for name in measurement_names
            if provider_measurements.get(name, {}).get("availability") == "available"
        ),
        None,
    )
    if direct is not None:
        return {
            **direct,
            "basis": "provider_stage_direct",
            "limitation": "stage_local_excludes_turn_scheduling",
        }

    # Native span attributes can be turn-owned even when a separate framework
    # metrics callback has no safe turn identifier. Only reuse a duration that
    # authored this analyzer's synthetic point; never reinterpret an explicit
    # source event that happens to precede the turn anchor.
    if target is None or not target_is_provider_projection:
        return current
    operation = next(
        (item for item in operations if item.operation_id == target.operation_id),
        None,
    )
    if operation is None:
        return current
    attribute_name = target.attributes.get(_PROVIDER_DURATION_ATTRIBUTE)
    if not isinstance(attribute_name, str) or attribute_name not in attribute_names:
        return current
    raw = operation.attributes.get(attribute_name)
    point = _shift_time_point(operation.started_at, raw)
    if point is None or point != target.time:
        return current
    if operation.ended_at is not None and _point_exceeds_comparable_end(point, operation.ended_at):
        return current
    confidence = "unavailable"
    if operation.evidence is not None:
        confidence = (
            operation.evidence.confidence
            if operation.evidence.availability == "available"
            else "unavailable"
        )
    return {
        "availability": "available",
        "basis": "provider_stage_direct",
        "confidence": confidence,
        "value": float(raw) * 1_000,
        "unit": "ms",
        "limitation": "stage_local_excludes_turn_scheduling",
        "evidence_ids": [operation.operation_id],
    }


# --- Interruption causal chain -----------------------------------------------
# The canonical, ordered stages of a barge-in teardown. Each maps to one or more
# open Earshot event names (or, for ``intent``/``resumed``, a provider
# measurement, and for ``tool_outcome``, a tool operation's status). A stage is
# *observed* only when the artifact actually contains its signal; a missing stage
# is reported as coverage with a reason, never as a fabricated coordinate.
_STAGE_OVERLAP = "overlap_observed"
_STAGE_INTENT = "intent"
_STAGE_CLASSIFIED = "classified"
_STAGE_CANCELLATION_REQUESTED = "cancellation_requested"
_STAGE_GENERATION_STOPPED = "generation_stopped"
_STAGE_QUEUED_AUDIO_DISCARDED = "queued_audio_discarded"
_STAGE_TRANSPORT_STOPPED = "transport_stopped"
_STAGE_BUFFERS_PURGED = "buffers_purged"
_STAGE_RENDER_STOPPED = "render_stopped"
_STAGE_RESUMED = "resumed"
_STAGE_TOOL_OUTCOME = "tool_outcome"

_OVERLAP_EVENT_NAMES = {
    "earshot.interruption.detected",
    "earshot.interruption.overlapping_speech",
}
_ACCEPTED_EVENT_NAME = "earshot.interruption.accepted"
_IGNORED_EVENT_NAME = "earshot.interruption.ignored"
_CLASSIFIED_EVENT_NAMES = {_ACCEPTED_EVENT_NAME, _IGNORED_EVENT_NAME}
_MODEL_CANCELLED_EVENT_NAME = "earshot.model.cancelled"
_CANCELLATION_REQUESTED_EVENT_NAMES = {
    _MODEL_CANCELLED_EVENT_NAME,
    "earshot.interruption.cancellation_requested",
}
_GENERATION_STOPPED_EVENT_NAMES = {"earshot.response.cancelled"}
_QUEUED_AUDIO_DISCARDED_EVENT_NAMES = {"earshot.audio.queued.discarded"}
_TRANSPORT_STOPPED_EVENT_NAMES = {
    "earshot.transport.stopped",
    "earshot.audio.send.stopped",
}
_BUFFERS_PURGED_EVENT_NAMES = {"earshot.audio.buffer.purged"}
_RENDER_STOPPED_EVENT_NAMES = {"earshot.audio.render.stopped"}
_RESUMED_EVENT_NAMES = {"earshot.interruption.resumed"}
_INTERRUPTION_PROBABILITY_MEASUREMENT = "earshot.metric.interruption.probability"
_INTERRUPTION_RESUMED_MEASUREMENT = "earshot.metric.interruption.resumed"


def _first_named_event(events: Sequence[Event], names: set[str]) -> Event | None:
    """Return the earliest event whose name is in ``names`` (deterministic).

    Ordering uses the coordinate-group key, which never subtracts across clock
    domains, so the choice is stable and source-order-invariant.
    """

    candidates = [event for event in events if event.event_name in names]
    if not candidates:
        return None
    ordered = _order_by_comparable_time(
        candidates, point=lambda event: event.time, identity=lambda event: event.event_id
    )
    return ordered[0]


def _first_measurement_sample(
    samples: Sequence[QualitySample],
    measurement_name: str,
    *,
    require_true: bool = False,
) -> QualitySample | None:
    for sample in sorted(samples, key=lambda item: item.sample_id):
        for measurement in sample.measurements:
            if measurement.name != measurement_name:
                continue
            if require_true and measurement.value is not True:
                continue
            return sample
    return None


def _stage_coordinate(point: TimePoint) -> tuple[str | None, str | None, str | None]:
    """Return ``(at_nano, clock_domain_id, time_basis)`` copied from real evidence.

    Prefer the monotonic reading, then source wall, then observed wall. The value
    is taken verbatim from the evidence; analysis never synthesizes a timestamp.
    """

    domain = point.clock_domain_id
    if point.monotonic_time_nano is not None:
        return (point.monotonic_time_nano, domain, "monotonic")
    if point.source_time_unix_nano is not None:
        return (point.source_time_unix_nano, domain, "source_wall")
    if point.observed_time_unix_nano is not None:
        return (point.observed_time_unix_nano, domain, "observed_wall")
    return (None, domain, None)


def _observed_stage(
    stage: str,
    point: TimePoint,
    evidence_id: str,
    *,
    outcome: str | None = None,
) -> dict:
    at_nano, clock_domain_id, time_basis = _stage_coordinate(point)
    projected: dict[str, object] = {
        "stage": stage,
        "observed": True,
        "at_nano": at_nano,
        "clock_domain_id": clock_domain_id,
        "time_basis": time_basis,
        "evidence_id": evidence_id,
    }
    if outcome is not None:
        projected["outcome"] = outcome
    return projected


def _unobserved_stage(stage: str, reason: str = "stage_not_observed") -> dict:
    return {"stage": stage, "observed": False, "coverage_reason": reason}


def _event_stage(stage: str, event: Event | None) -> dict:
    if event is None:
        return _unobserved_stage(stage)
    return _observed_stage(stage, event.time, event.event_id)


def _point_at_or_after(candidate: TimePoint, reference: TimePoint) -> bool | None:
    """Return whether ``candidate >= reference`` within one comparable domain.

    ``None`` means the two coordinates are not comparable (different clock domains
    or incompatible representations), so the caller must not exclude on time.
    """

    if candidate.clock_domain_id is None or candidate.clock_domain_id != reference.clock_domain_id:
        return None
    if candidate.monotonic_time_nano is not None and reference.monotonic_time_nano is not None:
        return int(candidate.monotonic_time_nano) >= int(reference.monotonic_time_nano)
    if candidate.source_time_unix_nano is not None and reference.source_time_unix_nano is not None:
        return int(candidate.source_time_unix_nano) >= int(reference.source_time_unix_nano)
    return None


def _tool_outcome_stage(
    operations: Sequence[Operation],
    overlap_event: Event | None,
) -> dict:
    """Attribute the disposition of a tool the interruption reached, if any.

    A tool is eligible when it is still active at or after the overlap: its end
    coordinate is not strictly before the overlap. When the overlap and the tool
    are not comparable, the tool is not excluded on time. Among eligible tools the
    earliest by coordinate is chosen deterministically, and its status is recorded
    as the outcome (ok/error/timeout/cancelled/...).
    """

    tools = list(
        _order_by_comparable_time(
            (operation for operation in operations if operation.operation_name == "tool"),
            point=lambda operation: operation.started_at,
            identity=lambda operation: operation.operation_id,
        )
    )
    if not tools:
        return _unobserved_stage(_STAGE_TOOL_OUTCOME, "no_tool_in_turn")
    if overlap_event is not None:
        eligible = [
            operation
            for operation in tools
            if _point_at_or_after(operation.ended_at or operation.started_at, overlap_event.time)
            is not False
        ]
    else:
        eligible = tools
    if not eligible:
        return _unobserved_stage(_STAGE_TOOL_OUTCOME, "no_tool_after_interruption")
    chosen = eligible[0]
    return _observed_stage(
        _STAGE_TOOL_OUTCOME,
        chosen.started_at,
        chosen.operation_id,
        outcome=chosen.status,
    )


def _interruption_chain(
    turn_id: str,
    operations: Sequence[Operation],
    events: Sequence[Event],
    quality_samples: Sequence[QualitySample],
    aligner: _ClockAligner | None,
) -> dict | None:
    """Build the ordered causal chain a turn's interruption produced, or ``None``.

    A chain exists only for a turn that actually observed an interruption -- an
    overlap detection or a recorded accept/ignore decision. Downstream teardown
    signals alone (a model cancel, a render stop) never conjure one.
    """

    overlap_event = _first_named_event(events, _OVERLAP_EVENT_NAMES)
    classified_event = _first_named_event(events, _CLASSIFIED_EVENT_NAMES)
    if overlap_event is None and classified_event is None:
        return None

    accepted_event = _first_named_event(events, {_ACCEPTED_EVENT_NAME})
    ignored_event = _first_named_event(events, {_IGNORED_EVENT_NAME})
    if accepted_event is not None:
        classification = "accepted"
    elif overlap_event is not None:
        # Detected without an accept is a false interruption (T2-consistent).
        classification = "false"
    elif ignored_event is not None:
        classification = "ignored"
    else:
        classification = "unknown"

    cancellation_event = _first_named_event(events, _CANCELLATION_REQUESTED_EVENT_NAMES)
    generation_event = _first_named_event(events, _GENERATION_STOPPED_EVENT_NAMES)
    if generation_event is None:
        # Ambiguity: with only earshot.model.cancelled present we cannot separate
        # the request to cancel from generation actually stopping, so we read the
        # model-cancel as the effective stop too, citing that same real event. A
        # distinct earshot.response.cancelled, when present, is preferred above.
        generation_event = _first_named_event(events, {_MODEL_CANCELLED_EVENT_NAME})
    queued_event = _first_named_event(events, _QUEUED_AUDIO_DISCARDED_EVENT_NAMES)
    transport_event = _first_named_event(events, _TRANSPORT_STOPPED_EVENT_NAMES)
    purged_event = _first_named_event(events, _BUFFERS_PURGED_EVENT_NAMES)
    render_stopped_event = _first_named_event(events, _RENDER_STOPPED_EVENT_NAMES)
    resumed_event = _first_named_event(events, _RESUMED_EVENT_NAMES)

    intent_sample = _first_measurement_sample(
        quality_samples, _INTERRUPTION_PROBABILITY_MEASUREMENT
    )
    resumed_sample = (
        _first_measurement_sample(
            quality_samples, _INTERRUPTION_RESUMED_MEASUREMENT, require_true=True
        )
        if resumed_event is None
        else None
    )

    stages: list[dict] = [_event_stage(_STAGE_OVERLAP, overlap_event)]
    if intent_sample is not None:
        stages.append(
            _observed_stage(
                _STAGE_INTENT, intent_sample.sample_window.start, intent_sample.sample_id
            )
        )
    else:
        stages.append(_unobserved_stage(_STAGE_INTENT))
    stages.append(_event_stage(_STAGE_CLASSIFIED, classified_event))
    stages.append(_event_stage(_STAGE_CANCELLATION_REQUESTED, cancellation_event))
    stages.append(_event_stage(_STAGE_GENERATION_STOPPED, generation_event))
    stages.append(_event_stage(_STAGE_QUEUED_AUDIO_DISCARDED, queued_event))
    stages.append(_event_stage(_STAGE_TRANSPORT_STOPPED, transport_event))
    stages.append(_event_stage(_STAGE_BUFFERS_PURGED, purged_event))
    stages.append(_event_stage(_STAGE_RENDER_STOPPED, render_stopped_event))
    if resumed_event is not None:
        stages.append(_observed_stage(_STAGE_RESUMED, resumed_event.time, resumed_event.event_id))
    elif resumed_sample is not None:
        stages.append(
            _observed_stage(
                _STAGE_RESUMED, resumed_sample.sample_window.start, resumed_sample.sample_id
            )
        )
    else:
        stages.append(_unobserved_stage(_STAGE_RESUMED))
    stages.append(_tool_outcome_stage(operations, overlap_event))

    # Barge-in effectiveness is the overlap -> render-stop latency, computed only
    # when both endpoints are observed and comparable (same clock, or a declared
    # calibration aligns them); otherwise it honestly asserts no value.
    effectiveness = _latency_metric(
        overlap_event, render_stopped_event, "interruption_barge_in", aligner
    )

    return {
        "turn_id": turn_id,
        "classification": classification,
        "stages": stages,
        "effectiveness": effectiveness,
    }


def _turn_projection(
    turn_id: str,
    operations: Sequence[Operation],
    events: Sequence[Event],
    quality_samples: Sequence[QualitySample] = (),
    stream_directions: Mapping[str, str] | None = None,
    participant_directions: Mapping[str, str] | None = None,
    operations_by_id: Mapping[str, Operation] | None = None,
    operations_by_otel: Mapping[tuple[str, str], Operation] | None = None,
    aligner: _ClockAligner | None = None,
) -> dict:
    directions = stream_directions or {}

    def matches(
        value: Operation | Event,
        expected_direction: str,
        *,
        require_explicit: bool = False,
    ) -> bool:
        return _matches_stream_direction(
            value,
            directions,
            expected_direction,
            require_explicit=require_explicit,
            participant_directions=participant_directions,
            operations_by_id=operations_by_id,
            operations_by_otel=operations_by_otel,
        )

    input_events = tuple(event for event in events if matches(event, "input"))
    output_events = tuple(event for event in events if matches(event, "output"))
    committed = _earliest_event(input_events, {"earshot.turn.committed"})
    speech_ended = _earliest_event(input_events, {"earshot.speech.ended"})
    anchor = committed or speech_ended
    if anchor is None:
        turn_detection = _first_operation(
            tuple(operation for operation in operations if matches(operation, "input")),
            {"turn_detection"},
        )
        if turn_detection is not None:
            anchor = _operation_end_event(turn_detection, "earshot.turn.committed")

    first_token = _earliest_event(output_events, {"earshot.response.first_token"})
    generated = _earliest_event(output_events, {"earshot.response.first_audio_generated"})
    sent = _earliest_event(output_events, {"earshot.audio.first_byte_sent"})
    received = _earliest_event(output_events, {"earshot.audio.first_packet_received"})
    rendered = _earliest_event(output_events, {"earshot.audio.render.started"})

    # Explicit events are highest fidelity. A provider TTFT/TTFB duration can be
    # projected from operation start, but operation start alone is never treated
    # as first output: that would systematically understate latency.
    first_token_is_provider_projection = False
    if first_token is None:
        first_token = _first_provider_latency_event(
            tuple(operation for operation in operations if matches(operation, "output")),
            {"llm"},
            "earshot.response.first_token",
            ("lk.response.ttft", "metrics.ttfb"),
        )
        first_token_is_provider_projection = first_token is not None
    generated_is_provider_projection = False
    if generated is None:
        generated = _first_provider_latency_event(
            tuple(operation for operation in operations if matches(operation, "output")),
            {"tts"},
            "earshot.response.first_audio_generated",
            ("lk.response.ttfb", "metrics.ttfb"),
        )
        generated_is_provider_projection = generated is not None
    if sent is None:
        transport = _first_operation(
            tuple(
                operation
                for operation in operations
                if matches(operation, "output", require_explicit=True)
            ),
            {"transport_send"},
        )
        sent = (
            _operation_start_event(transport, "earshot.audio.first_byte_sent")
            if transport
            else None
        )
    if received is None:
        transport = _first_operation(
            tuple(
                operation
                for operation in operations
                if matches(operation, "output", require_explicit=True)
            ),
            {"transport_receive"},
        )
        received = (
            _operation_start_event(transport, "earshot.audio.first_packet_received")
            if transport
            else None
        )
    if rendered is None:
        render = _first_operation(
            tuple(operation for operation in operations if matches(operation, "output")),
            {"render"},
        )
        rendered = (
            _operation_start_event(render, "earshot.audio.render.started") if render else None
        )

    response_basis = "render"
    response_target = rendered
    if response_target is None:
        response_target = received or sent or generated
        if received:
            response_basis = "receive_estimate"
        elif sent:
            response_basis = "transport_estimate"
        else:
            response_basis = "tts_estimate"

    interruption_events = _order_by_comparable_time(
        (
            event
            for event in events
            if event.event_name
            in {
                "earshot.interruption.detected",
                "earshot.interruption.accepted",
                "earshot.interruption.ignored",
            }
        ),
        point=lambda event: event.time,
        identity=lambda event: event.event_id,
    )

    provider_measurements = _quality_measurements(quality_samples)
    first_token_latency = _provider_stage_latency_fallback(
        _latency_metric(anchor, first_token, "first_token", aligner),
        provider_measurements,
        ("earshot.llm.ttft", "livekit.llm_node_ttft", "pipecat.llm.ttfb"),
        target=first_token,
        target_is_provider_projection=first_token_is_provider_projection,
        operations=operations,
        attribute_names=("lk.response.ttft", "metrics.ttfb"),
    )
    generated_response_latency = _provider_stage_latency_fallback(
        _latency_metric(anchor, generated, "generated", aligner),
        provider_measurements,
        ("earshot.tts.ttfb", "livekit.tts_node_ttfb", "pipecat.tts.ttfb"),
        target=generated,
        target_is_provider_projection=generated_is_provider_projection,
        operations=operations,
        attribute_names=("lk.response.ttfb", "metrics.ttfb"),
    )
    response_latency = _latency_metric(anchor, response_target, response_basis, aligner)
    direct_e2e = provider_measurements.get("earshot.turn.response_latency")
    if direct_e2e is None:
        direct_e2e = provider_measurements.get("livekit.e2e_latency")
    if direct_e2e is None:
        direct_e2e = provider_measurements.get("pipecat.turn.user_bot_latency")
    if (
        direct_e2e is not None
        and direct_e2e.get("availability") == "available"
        and (response_latency["availability"] == "not_observed" or response_basis == "tts_estimate")
    ):
        # A native user-stop -> bot-start measurement is stronger than deriving
        # server output from two separately observed provider points. Delivery
        # and render evidence still outrank it above.
        response_latency = {
            **direct_e2e,
            "limitation": "server_output_excludes_delivery_and_render",
        }

    return {
        "turn_id": turn_id,
        "operation_ids": [
            item.operation_id
            for item in _order_by_comparable_time(
                operations,
                point=lambda operation: operation.started_at,
                identity=lambda operation: operation.operation_id,
            )
        ],
        "event_ids": [
            item.event_id
            for item in _order_by_comparable_time(
                events,
                point=lambda event: event.time,
                identity=lambda event: event.event_id,
            )
        ],
        "metrics": {
            "first_token_latency": first_token_latency,
            "generated_response_latency": generated_response_latency,
            "sent_response_latency": _latency_metric(anchor, sent, "sent", aligner),
            "received_response_latency": _latency_metric(anchor, received, "received", aligner),
            "render_start_response_latency": _latency_metric(anchor, rendered, "render", aligner),
            "response_latency": response_latency,
            "tools": _tool_metrics(operations, aligner),
            "provider_measurements": provider_measurements,
        },
        "interruptions": [
            {"event_name": event.event_name, "evidence_ids": [event.event_id]}
            for event in interruption_events
        ],
        "interruption_chain": _interruption_chain(
            turn_id, operations, events, quality_samples, aligner
        ),
    }


def _operation_turn_ids(operations: Sequence[Operation]) -> dict[str, str | None]:
    """Project turn ownership through the complete OTel parent graph."""

    by_otel = {
        (operation.trace_id, operation.span_id): operation
        for operation in operations
        if operation.trace_id is not None and operation.span_id is not None
    }
    resolved: dict[str, str | None] = {}
    for operation in operations:
        if operation.operation_id in resolved:
            continue
        chain: list[Operation] = []
        seen: set[str] = set()
        current: Operation | None = operation
        turn_id: str | None = None
        while current is not None and current.operation_id not in seen:
            seen.add(current.operation_id)
            chain.append(current)
            if current.operation_id in resolved:
                turn_id = resolved[current.operation_id]
                break
            if current.turn_id is not None:
                turn_id = current.turn_id
                break
            if current.parent_scope == "external":
                break
            if current.trace_id is None or current.parent_span_id is None:
                break
            current = by_otel.get((current.trace_id, current.parent_span_id))
        for member in chain:
            resolved[member.operation_id] = turn_id
    return resolved


# --- Boundary-attribution engine ---------------------------------------------
# Each rule turns governed evidence into an evidence-linked ``Diagnosis`` that
# names the boundary at fault. Rules are deterministic and source-order-invariant
# (inputs are sorted), they cite only real operation/event/sample ids, and they
# emit nothing when the deciding signal is absent or unmeasured. Confidence is
# ``measured`` when the deciding signal is a direct governed fact (a QoS reading,
# a governed event, an operation status, a causal link) and ``inferred`` when the
# analyzer had to derive it (an SLO breach on a computed latency, or an absence).
_DEVICE_EVENT_PREFIX = "earshot.device."
_TRANSPORT_EVENT_PREFIX = "earshot.transport."
_RENDER_EVENT_PREFIX = "earshot.audio.render."
_STALE_EVENT_NAME = "earshot.audio.render.stale"
_INTERRUPTION_DETECTED = "earshot.interruption.detected"
_INTERRUPTION_ACCEPTED = "earshot.interruption.accepted"
_INTERRUPTION_IGNORED = "earshot.interruption.ignored"
_FALSE_INTERRUPTION_SOURCE = "agent_false_interruption"
_FAILED_STATUSES = {"error", "timeout", "failed"}
_STAGE_LATENCY_FIELDS = {
    "stt": "stt_latency_ms",
    "llm": "llm_latency_ms",
    "tts": "tts_latency_ms",
}


def _boundary_diagnosis_id(code: str, key: str) -> str:
    """Derive a bounded, deterministic diagnosis id from its code and evidence."""

    slug = code.replace(".", "_").replace("-", "_")
    return f"{slug}." + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _network_degraded_diagnoses(
    quality_samples: Sequence[QualitySample],
    slo: SloRecipe,
) -> list[Diagnosis]:
    """Attribute packet loss, jitter growth, and RTT to the transport boundary."""

    diagnoses: list[Diagnosis] = []
    for sample in sorted(quality_samples, key=lambda item: item.sample_id):
        # An unavailable QoS sample cannot support a diagnosis: say unknown.
        if sample.evidence is None or sample.evidence.availability.lower() != "available":
            continue
        breaches: set[str] = set()
        for measurement in sample.measurements:
            if (
                measurement_value_limitation(measurement.name, measurement.value, measurement.unit)
                is not None
            ):
                continue
            value = measurement.value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            name = measurement.name.lower()
            in_ms = value * 1_000 if measurement.unit == "s" else value
            is_packet_loss = name == "packet_loss_ratio" or name.endswith(
                (".packet_loss_ratio", "_packet_loss_ratio")
            )
            is_rtt = "round_trip" in name or "roundtrip" in name or name.endswith((".rtt", "_rtt"))
            # Inter-arrival jitter is the transport signal here; a jitter *buffer*
            # delay (the de-jitter buffer's own depth) is a distinct, healthy-by-
            # default quantity that routinely exceeds the inter-arrival SLO, so it
            # must not be read as excess jitter.
            is_jitter = "jitter" in name and "buffer" not in name
            if is_packet_loss and value > slo.packet_loss_ratio:
                breaches.add("packet_loss_ratio_exceeds_slo")
            elif is_jitter and in_ms > slo.jitter_ms:
                breaches.add("jitter_exceeds_slo")
            elif is_rtt and in_ms > slo.round_trip_time_ms:
                breaches.add("round_trip_time_exceeds_slo")
        if breaches:
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=_boundary_diagnosis_id("network.degraded", sample.sample_id),
                    code="network.degraded",
                    summary="network_degraded",
                    confidence="measured",
                    evidence_refs=(sample.sample_id,),
                    limitations=tuple(sorted(breaches)),
                )
            )
    return diagnoses


def _tool_retry_diagnoses(operations: Sequence[Operation]) -> list[Diagnosis]:
    """Attribute a failure-then-retry pattern to the tool boundary."""

    by_id = {operation.operation_id: operation for operation in operations}
    diagnoses: list[Diagnosis] = []
    for operation in sorted(operations, key=lambda item: item.operation_id):
        if operation.operation_name != "tool":
            continue
        for link in operation.links:
            if link.relationship != "retries" or link.target_operation_id is None:
                continue
            target = by_id.get(link.target_operation_id)
            if (
                target is None
                or target.operation_name != "tool"
                or target.status not in _FAILED_STATUSES
            ):
                continue
            evidence_refs = tuple(sorted({target.operation_id, operation.operation_id}))
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=_boundary_diagnosis_id(
                        "tool.retry", f"{target.operation_id}->{operation.operation_id}"
                    ),
                    code="tool.retry",
                    summary="tool_retry",
                    confidence="measured",
                    evidence_refs=evidence_refs,
                )
            )
    return diagnoses


def _event_prefix_diagnoses(
    events: Sequence[Event],
    *,
    code: str,
    summary: str,
    match: object,
) -> list[Diagnosis]:
    """Emit one measured diagnosis citing every event that matches ``match``."""

    matched = sorted(
        (event for event in events if match(event.event_name)),
        key=lambda event: event.event_id,
    )
    if not matched:
        return []
    evidence_refs = tuple(event.event_id for event in matched)
    return [
        Diagnosis(
            diagnosis_id=_boundary_diagnosis_id(code, "|".join(evidence_refs)),
            code=code,
            summary=summary,
            confidence="measured",
            evidence_refs=evidence_refs,
        )
    ]


def _device_unavailable_diagnoses(events: Sequence[Event]) -> list[Diagnosis]:
    """Attribute permission/context loss to the capture boundary."""

    return _event_prefix_diagnoses(
        events,
        code="device.unavailable",
        summary="device_unavailable",
        match=lambda name: name.startswith(_DEVICE_EVENT_PREFIX),
    )


def _transport_reconnect_diagnoses(events: Sequence[Event]) -> list[Diagnosis]:
    """Attribute reconnect/duplicate/out-of-order signals to the transport boundary."""

    if not any(event.event_name == "earshot.transport.reconnecting" for event in events):
        return []
    return _event_prefix_diagnoses(
        events,
        code="transport.reconnect",
        summary="transport_reconnect",
        match=lambda name: name.startswith(_TRANSPORT_EVENT_PREFIX),
    )


def _stale_playback_diagnoses(events: Sequence[Event]) -> list[Diagnosis]:
    """Attribute stale-buffer playback to the decode/render boundary."""

    return _event_prefix_diagnoses(
        events,
        code="audio.stale_playback",
        summary="audio_stale_playback",
        match=lambda name: name == _STALE_EVENT_NAME,
    )


def _interruption_false_diagnoses(events: Sequence[Event]) -> list[Diagnosis]:
    """Attribute a detected-but-never-accepted interruption to the interruption boundary.

    A cleanly handled barge-in (detected *then* accepted) and a well-handled
    native accept (accepted with no detection) both produce nothing.
    """

    buckets: dict[str | None, list[Event]] = defaultdict(list)
    for event in events:
        if event.event_name in {
            _INTERRUPTION_DETECTED,
            _INTERRUPTION_ACCEPTED,
            _INTERRUPTION_IGNORED,
        }:
            buckets[event.turn_id].append(event)
    diagnoses: list[Diagnosis] = []
    for turn_id in sorted(buckets, key=lambda value: (value is None, value or "")):
        bucket = buckets[turn_id]
        names = {event.event_name for event in bucket}
        if _INTERRUPTION_DETECTED not in names or _INTERRUPTION_ACCEPTED in names:
            continue
        cited = sorted(
            (
                event
                for event in bucket
                if event.event_name in {_INTERRUPTION_DETECTED, _INTERRUPTION_IGNORED}
            ),
            key=lambda event: event.event_id,
        )
        evidence_refs = tuple(event.event_id for event in cited)
        explicit_false = _INTERRUPTION_IGNORED in names or any(
            event.evidence is not None and event.evidence.source == _FALSE_INTERRUPTION_SOURCE
            for event in bucket
        )
        diagnoses.append(
            Diagnosis(
                diagnosis_id=_boundary_diagnosis_id("interruption.false", "|".join(evidence_refs)),
                code="interruption.false",
                summary="interruption_false",
                confidence="measured" if explicit_false else "inferred",
                evidence_refs=evidence_refs,
            )
        )
    return diagnoses


def _render_delayed_diagnoses(turns: Sequence[dict], slo: SloRecipe) -> list[Diagnosis]:
    """Attribute an excessive turn-commit -> render latency to the render boundary."""

    diagnoses: list[Diagnosis] = []
    for turn in turns:
        metric = turn["metrics"]["render_start_response_latency"]
        # not_observed / unavailable render latency is unknown, never a fault.
        if metric.get("availability") != "available":
            continue
        value = metric.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if value <= slo.render_start_latency_ms:
            continue
        evidence_refs = tuple(metric.get("evidence_ids", ()))
        if not evidence_refs:
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=_boundary_diagnosis_id("render.delayed", str(turn["turn_id"])),
                code="render.delayed",
                summary="render_delayed",
                confidence="inferred",
                evidence_refs=evidence_refs,
                limitations=("render_start_latency_exceeds_slo",),
            )
        )
    return diagnoses


def _stage_slow_diagnoses(
    operations: Sequence[Operation],
    slo: SloRecipe,
    aligner: _ClockAligner | None,
) -> list[Diagnosis]:
    """Attribute an over-SLO stt/llm/tts duration to that stage boundary."""

    diagnoses: list[Diagnosis] = []
    for operation in sorted(operations, key=lambda item: item.operation_id):
        field = _STAGE_LATENCY_FIELDS.get(operation.operation_name)
        if field is None:
            continue
        nanos = _interval_nanos(operation, aligner)
        if nanos is None:
            continue
        if nanos / 1_000_000 <= getattr(slo, field):
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=_boundary_diagnosis_id("stage.slow", operation.operation_id),
                code="stage.slow",
                summary=f"{operation.operation_name}_stage_slow",
                confidence="inferred",
                evidence_refs=(operation.operation_id,),
                limitations=(f"{operation.operation_name}_latency_exceeds_slo",),
            )
        )
    return diagnoses


def _endpointing_slow_diagnoses(
    operations: Sequence[Operation],
    slo: SloRecipe,
    aligner: _ClockAligner | None,
) -> list[Diagnosis]:
    """Attribute an over-SLO end-of-utterance decision to the turn-detection boundary."""

    diagnoses: list[Diagnosis] = []
    for operation in sorted(operations, key=lambda item: item.operation_id):
        if operation.operation_name != "turn_detection":
            continue
        nanos = _interval_nanos(operation, aligner)
        if nanos is None:
            continue
        if nanos / 1_000_000 <= slo.endpointing_latency_ms:
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=_boundary_diagnosis_id("endpointing.slow", operation.operation_id),
                code="endpointing.slow",
                summary="endpointing_slow",
                confidence="inferred",
                evidence_refs=(operation.operation_id,),
                limitations=("endpointing_latency_exceeds_slo",),
            )
        )
    return diagnoses


def analyze_incident(
    bundle: IncidentBundle,
    *,
    input_sha256: str,
    generated_at_unix_nano: int | str,
    slo: SloRecipe | None = None,
) -> DerivedAnalysis:
    """Return a stable projection for an exact immutable input digest."""

    profile = bundle.profile
    aligner = _ClockAligner(profile.clock_relations)
    turn_operations: dict[str, list[Operation]] = defaultdict(list)
    turn_events: dict[str, list[Event]] = defaultdict(list)
    turn_quality: dict[str, list[QualitySample]] = defaultdict(list)
    unassigned_quality: list[QualitySample] = []
    stream_directions = {stream.stream_id: stream.direction for stream in profile.audio_streams}
    participant_directions = {
        participant.participant_id: direction
        for participant in profile.participants
        if (direction := _PARTICIPANT_ROLE_DIRECTION.get(participant.role)) is not None
    }
    operation_turns = _operation_turn_ids(profile.operations)
    operations_by_id = {operation.operation_id: operation for operation in profile.operations}
    operation_by_otel = {
        (operation.trace_id, operation.span_id): operation
        for operation in profile.operations
        if operation.trace_id is not None and operation.span_id is not None
    }
    for operation in profile.operations:
        turn_id = operation_turns.get(operation.operation_id)
        if turn_id:
            turn_operations[turn_id].append(operation)
    for event in profile.events:
        turn_id = event.turn_id
        if turn_id is None and event.operation_id is not None:
            turn_id = operation_turns.get(event.operation_id)
        if turn_id is None and event.trace_id is not None and event.span_id is not None:
            owner = operation_by_otel.get((event.trace_id, event.span_id))
            if owner is not None:
                turn_id = operation_turns.get(owner.operation_id)
        if turn_id:
            turn_events[turn_id].append(event)

    for sample in profile.quality_samples:
        turn_value = sample.attributes.get("earshot.turn.id")
        if turn_value is None:
            operation_value = sample.attributes.get("earshot.operation.id")
            if isinstance(operation_value, str):
                turn_value = operation_turns.get(operation_value)
        if isinstance(turn_value, (str, int)) and not isinstance(turn_value, bool):
            turn_quality[str(turn_value)].append(sample)
        else:
            unassigned_quality.append(sample)

    turn_ids = tuple(dict.fromkeys((*turn_operations, *turn_events, *turn_quality)))

    def turn_coordinate(turn_id: str) -> tuple[str, str, int] | None:
        coordinates = [
            _comparable_coordinate(operation.started_at) for operation in turn_operations[turn_id]
        ]
        coordinates.extend(_comparable_coordinate(event.time) for event in turn_events[turn_id])
        coordinates.extend(
            _comparable_coordinate(sample.sample_window.start) for sample in turn_quality[turn_id]
        )
        if not coordinates or any(coordinate is None for coordinate in coordinates):
            return None
        comparable = [coordinate for coordinate in coordinates if coordinate is not None]
        groups = {(domain, basis) for domain, basis, _value in comparable}
        if len(groups) != 1:
            return None
        return min(comparable, key=lambda coordinate: coordinate[2])

    ordered_turn_ids = _order_by_comparable_coordinate(
        turn_ids,
        coordinate=turn_coordinate,
        identity=lambda turn_id: turn_id,
    )
    turns = [
        _turn_projection(
            turn_id,
            tuple(turn_operations[turn_id]),
            tuple(turn_events[turn_id]),
            tuple(turn_quality[turn_id]),
            stream_directions,
            participant_directions,
            operations_by_id,
            operation_by_otel,
            aligner,
        )
        for turn_id in ordered_turn_ids
    ]

    recipe = slo if slo is not None else DEFAULT_SLO_RECIPE
    diagnoses: list[Diagnosis] = []
    for operation in sorted(profile.operations, key=lambda item: item.operation_id):
        if operation.status in _FAILED_STATUSES:
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=(
                        "operation_failed."
                        + hashlib.sha256(operation.operation_id.encode()).hexdigest()
                    ),
                    code="operation.failed",
                    summary="operation_failed",
                    confidence="measured",
                    evidence_refs=(operation.operation_id,),
                )
            )

    # Boundary attribution layers evidence-linked hypotheses on top of the raw
    # operation.failed facts: the failure and its retry pattern can co-exist,
    # each citing its own evidence. The combined list is sorted for a stable,
    # source-order-invariant projection.
    diagnoses.extend(_network_degraded_diagnoses(profile.quality_samples, recipe))
    diagnoses.extend(_tool_retry_diagnoses(profile.operations))
    diagnoses.extend(_device_unavailable_diagnoses(profile.events))
    diagnoses.extend(_transport_reconnect_diagnoses(profile.events))
    diagnoses.extend(_stale_playback_diagnoses(profile.events))
    diagnoses.extend(_interruption_false_diagnoses(profile.events))
    diagnoses.extend(_render_delayed_diagnoses(turns, recipe))
    diagnoses.extend(_stage_slow_diagnoses(profile.operations, recipe, aligner))
    diagnoses.extend(_endpointing_slow_diagnoses(profile.operations, recipe, aligner))
    diagnoses.sort(key=lambda item: item.diagnosis_id)

    render_availabilities = {
        entry.availability
        for entry in profile.coverage
        if entry.signal in {"render", "client.render"}
    }
    has_render = any(
        item.operation_name == "render"
        and item.status in _SUCCESS_BOUNDARY_STATUSES
        and _matches_stream_direction(
            item,
            stream_directions,
            "output",
            participant_directions=participant_directions,
            operations_by_id=operations_by_id,
            operations_by_otel=operation_by_otel,
        )
        for item in profile.operations
    ) or any(
        item.event_name == "earshot.audio.render.started"
        and _matches_stream_direction(
            item,
            stream_directions,
            "output",
            participant_directions=participant_directions,
            operations_by_id=operations_by_id,
            operations_by_otel=operation_by_otel,
        )
        for item in profile.events
    )
    limitations: list[str] = []
    if not has_render:
        if not render_availabilities:
            limitation = "render_evidence_not_observed"
        elif len(render_availabilities) == 1:
            limitation = f"render_evidence_{next(iter(render_availabilities))}"
        else:
            limitation = "render_evidence_conflicting_coverage"
        # Missing evidence is a limitation, not a diagnosed failure. Coverage has
        # no fact identity in v1alpha1, so inventing an evidence-free diagnosis would
        # violate the analysis contract.
        limitations.append(limitation)

    projection = {
        "session_id": profile.session.session_id,
        "turns": turns,
        "summary": {
            "turn_count": len(turns),
            "operation_count": len(profile.operations),
            "event_count": len(profile.events),
            "quality_sample_count": len(profile.quality_samples),
            "failed_operation_count": sum(
                item.status in {"error", "timeout", "failed"} for item in profile.operations
            ),
        },
        "limitations": limitations,
        "unassigned_provider_measurements": {
            sample.sample_id: _quality_measurements((sample,))
            for sample in sorted(unassigned_quality, key=lambda item: item.sample_id)
        },
    }

    return DerivedAnalysis(
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        input_sha256=input_sha256,
        generated_at_unix_nano=str(generated_at_unix_nano),
        projections=projection,
        diagnoses=tuple(diagnoses),
    )
