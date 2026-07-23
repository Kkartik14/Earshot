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

    def add_record_ownership(record: Operation | Event) -> None:
        nonlocal unresolved_stream
        if record.stream_id is not None:
            direction = stream_directions.get(record.stream_id)
            if direction is None:
                unresolved_stream = True
            else:
                directions.add(direction)
        if record.participant_id is not None and participant_directions is not None:
            direction = participant_directions.get(record.participant_id)
            if direction is not None:
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

    if unresolved_stream:
        return False
    if directions:
        return directions == {expected_direction}
    return not require_explicit


@dataclass(frozen=True)
class Delta:
    availability: str
    nanoseconds: int | None
    basis: str
    confidence: str
    limitation: str | None = None

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


def comparable_delta(start: TimePoint, end: TimePoint) -> Delta:
    """Subtract only evidence sharing an explicit clock domain.

    Wall timestamps from different processes can look ordered while being skewed.
    A declared clock mapping belongs in a future alignment layer; until then, an
    exact cross-domain latency is unavailable rather than clamped to zero.
    """

    if not start.clock_domain_id or start.clock_domain_id != end.clock_domain_id:
        return Delta(
            "unavailable",
            None,
            "clock_domain",
            "unavailable",
            "cross_clock_domain",
        )

    if start.monotonic_time_nano is not None and end.monotonic_time_nano is not None:
        value = int(end.monotonic_time_nano) - int(start.monotonic_time_nano)
        basis = "monotonic"
    elif start.source_time_unix_nano is not None and end.source_time_unix_nano is not None:
        value = int(end.source_time_unix_nano) - int(start.source_time_unix_nano)
        basis = "source_wall"
    else:
        return Delta(
            "unavailable",
            None,
            "clock_domain",
            "unavailable",
            "timestamp_representation_unavailable",
        )

    if value < 0:
        return Delta(
            "inconsistent",
            None,
            basis,
            "unavailable",
            "same_domain_time_reversed",
        )

    uncertainty = int(start.uncertainty_nano or "0") + int(end.uncertainty_nano or "0")
    confidence = "estimated" if uncertainty else "measured"
    return Delta("available", value, basis, confidence)


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
        if operation.ended_at is not None:
            end_delta = comparable_delta(point, operation.ended_at)
            if end_delta.availability == "inconsistent":
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


def _latency_metric(anchor: Event | None, target: Event | None, basis: str) -> dict[str, object]:
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
    output = comparable_delta(anchor.time, target.time).as_dict()
    confidence_candidates = [str(output["confidence"])]
    for boundary in (anchor, target):
        if boundary.attributes.get("earshot.analysis.synthetic_projection"):
            confidence_candidates.append("estimated")
        if boundary.evidence is not None:
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


def _interval_nanos(operation: Operation) -> int | None:
    if operation.ended_at is None:
        return None
    delta = comparable_delta(operation.started_at, operation.ended_at)
    return delta.nanoseconds if delta.availability == "available" else None


def _tool_metrics(operations: Sequence[Operation]) -> dict[str, object]:
    tools = [item for item in operations if item.operation_name == "tool"]
    durations = [(item, _interval_nanos(item)) for item in tools]
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
    if operation.ended_at is not None:
        end_delta = comparable_delta(point, operation.ended_at)
        if end_delta.availability == "inconsistent":
            return current
    confidence = "estimated"
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


def _turn_projection(
    turn_id: str,
    operations: Sequence[Operation],
    events: Sequence[Event],
    quality_samples: Sequence[QualitySample] = (),
    stream_directions: Mapping[str, str] | None = None,
    participant_directions: Mapping[str, str] | None = None,
    operations_by_id: Mapping[str, Operation] | None = None,
    operations_by_otel: Mapping[tuple[str, str], Operation] | None = None,
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
        _latency_metric(anchor, first_token, "first_token"),
        provider_measurements,
        ("earshot.llm.ttft", "livekit.llm_node_ttft", "pipecat.llm.ttfb"),
        target=first_token,
        target_is_provider_projection=first_token_is_provider_projection,
        operations=operations,
        attribute_names=("lk.response.ttft", "metrics.ttfb"),
    )
    generated_response_latency = _provider_stage_latency_fallback(
        _latency_metric(anchor, generated, "generated"),
        provider_measurements,
        ("earshot.tts.ttfb", "livekit.tts_node_ttfb", "pipecat.tts.ttfb"),
        target=generated,
        target_is_provider_projection=generated_is_provider_projection,
        operations=operations,
        attribute_names=("lk.response.ttfb", "metrics.ttfb"),
    )
    response_latency = _latency_metric(anchor, response_target, response_basis)
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
            "sent_response_latency": _latency_metric(anchor, sent, "sent"),
            "received_response_latency": _latency_metric(anchor, received, "received"),
            "render_start_response_latency": _latency_metric(anchor, rendered, "render"),
            "response_latency": response_latency,
            "tools": _tool_metrics(operations),
            "provider_measurements": provider_measurements,
        },
        "interruptions": [
            {"event_name": event.event_name, "evidence_ids": [event.event_id]}
            for event in interruption_events
        ],
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


def analyze_incident(
    bundle: IncidentBundle,
    *,
    input_sha256: str,
    generated_at_unix_nano: int | str,
) -> DerivedAnalysis:
    """Return a stable projection for an exact immutable input digest."""

    profile = bundle.profile
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
        )
        for turn_id in ordered_turn_ids
    ]

    diagnoses: list[Diagnosis] = []
    for operation in sorted(profile.operations, key=lambda item: item.operation_id):
        if operation.status in {"error", "timeout", "failed"}:
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
