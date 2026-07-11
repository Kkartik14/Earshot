"""Deterministic, evidence-linked analysis over the incident graph.

Analysis is a replaceable projection. It never mutates source facts and it never
subtracts timestamps from unrelated clock domains. Presentation values use
milliseconds; the artifact keeps exact nanoseconds.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .contract import (
    DerivedAnalysis,
    Diagnosis,
    Event,
    IncidentBundle,
    Operation,
    QualitySample,
    TimePoint,
)

ANALYZER_NAME = "earshot.deterministic"
ANALYZER_VERSION = "1.0.0"


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
    return min(candidates, key=lambda event: event.event_id)


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
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(float(seconds))
        or float(seconds) < 0
    ):
        return None
    delta = round(float(seconds) * 1_000_000_000)
    update: dict[str, str] = {}
    if point.source_time_unix_nano is not None:
        update["source_time_unix_nano"] = str(int(point.source_time_unix_nano) + delta)
    if point.monotonic_time_nano is not None:
        update["monotonic_time_nano"] = str(int(point.monotonic_time_nano) + delta)
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
        if point is not None:
            return _operation_point_event(operation, event_name, point)
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
        if (event := _provider_latency_event(operation, event_name, attribute_names)) is not None
    ]
    return _earliest_event(candidates, {event_name})


def _quality_measurements(samples: Sequence[QualitySample]) -> dict[str, dict[str, object]]:
    projected: dict[str, dict[str, object]] = {}
    for sample in sorted(samples, key=lambda item: item.sample_id):
        for measurement in sample.measurements:
            if measurement.name in projected:
                continue
            if not isinstance(measurement.value, (int, float)) or isinstance(
                measurement.value, bool
            ):
                continue
            value = float(measurement.value)
            if not math.isfinite(value):
                continue
            if measurement.unit == "s":
                value *= 1_000
                unit = "ms"
            else:
                unit = measurement.unit
            projected[measurement.name] = {
                "availability": "available",
                "basis": "provider_direct",
                "confidence": (
                    sample.evidence.confidence if sample.evidence is not None else "unavailable"
                ),
                "value": value,
                "unit": unit,
                "evidence_ids": [sample.sample_id],
            }
    return projected


def _first_operation(operations: Sequence[Operation], names: set[str]) -> Operation | None:
    candidates = [operation for operation in operations if operation.operation_name in names]
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
    return min(candidates, key=lambda operation: operation.operation_id)


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
    if target.attributes.get("earshot.analysis.synthetic_projection") or anchor.attributes.get(
        "earshot.analysis.synthetic_projection"
    ):
        output["confidence"] = "estimated"
    elif target.evidence and target.evidence.confidence in {"estimated", "inferred"}:
        output["confidence"] = target.evidence.confidence
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
    total = sum(value for _, value in durations if value is not None)

    # Calculate union wall time only within comparable clock domains. Intervals in
    # different domains remain separate rather than inventing a global critical path.
    by_basis: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for operation, _duration in durations:
        if operation.ended_at is None or not operation.started_at.clock_domain_id:
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

    elapsed_by_domain: dict[str, float] = {}
    basis_count: dict[str, int] = defaultdict(int)
    for domain, _basis in by_basis:
        basis_count[domain] += 1
    for (domain, basis), intervals in sorted(by_basis.items()):
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        key = domain if basis_count[domain] == 1 else f"{domain}:{basis}"
        elapsed_by_domain[key] = sum(end - start for start, end in merged) / 1_000_000

    return {
        "operation_count": len(tools),
        "total_work_ms": total / 1_000_000,
        "elapsed_ms_by_clock_domain": elapsed_by_domain,
        "evidence_ids": sorted(item.operation_id for item in tools),
    }


def _turn_projection(
    turn_id: str,
    operations: Sequence[Operation],
    events: Sequence[Event],
    quality_samples: Sequence[QualitySample] = (),
) -> dict:
    committed = _earliest_event(events, {"earshot.turn.committed"})
    speech_ended = _earliest_event(events, {"earshot.speech.ended"})
    anchor = committed or speech_ended
    if anchor is None:
        turn_detection = _first_operation(operations, {"turn_detection"})
        if turn_detection is not None:
            anchor = _operation_end_event(turn_detection, "earshot.turn.committed")

    first_token = _earliest_event(events, {"earshot.response.first_token"})
    generated = _earliest_event(events, {"earshot.response.first_audio_generated"})
    sent = _earliest_event(events, {"earshot.audio.first_byte_sent"})
    received = _earliest_event(events, {"earshot.audio.first_packet_received"})
    rendered = _earliest_event(events, {"earshot.audio.render.started"})

    # Explicit events are highest fidelity. A provider TTFT/TTFB duration can be
    # projected from operation start, but operation start alone is never treated
    # as first output: that would systematically understate latency.
    if first_token is None:
        first_token = _first_provider_latency_event(
            operations,
            {"llm"},
            "earshot.response.first_token",
            ("lk.response.ttft", "metrics.ttfb"),
        )
    if generated is None:
        generated = _first_provider_latency_event(
            operations,
            {"tts"},
            "earshot.response.first_audio_generated",
            ("lk.response.ttfb", "metrics.ttfb"),
        )
    if sent is None:
        transport = _first_operation(operations, {"transport_send"})
        sent = (
            _operation_start_event(transport, "earshot.audio.first_byte_sent")
            if transport
            else None
        )
    if received is None:
        transport = _first_operation(operations, {"transport_receive"})
        received = (
            _operation_start_event(transport, "earshot.audio.first_packet_received")
            if transport
            else None
        )
    if rendered is None:
        render = _first_operation(operations, {"render"})
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

    interruption_events = sorted(
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
        key=lambda event: (event.event_name, event.event_id),
    )

    provider_measurements = _quality_measurements(quality_samples)
    first_token_latency = _latency_metric(anchor, first_token, "first_token")
    if first_token_latency["availability"] == "not_observed":
        direct_llm_ttft = provider_measurements.get("livekit.llm_node_ttft")
        if direct_llm_ttft is not None:
            first_token_latency = {
                **direct_llm_ttft,
                "basis": "provider_stage_direct",
                "limitation": "stage_local_excludes_turn_scheduling",
            }
    response_latency = _latency_metric(anchor, response_target, response_basis)
    if response_latency["availability"] == "not_observed":
        direct_e2e = provider_measurements.get("livekit.e2e_latency")
        if direct_e2e is not None:
            response_latency = {
                **direct_e2e,
                "limitation": "server_output_excludes_delivery_and_render",
            }

    return {
        "turn_id": turn_id,
        "operation_ids": sorted(item.operation_id for item in operations),
        "event_ids": sorted(item.event_id for item in events),
        "metrics": {
            "first_token_latency": first_token_latency,
            "generated_response_latency": _latency_metric(anchor, generated, "generated"),
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
    operation_turns = _operation_turn_ids(profile.operations)
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

    turn_ids = sorted(set(turn_operations) | set(turn_events) | set(turn_quality))
    turns = [
        _turn_projection(
            turn_id,
            tuple(turn_operations[turn_id]),
            tuple(turn_events[turn_id]),
            tuple(turn_quality[turn_id]),
        )
        for turn_id in turn_ids
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

    render_coverage = next(
        (entry for entry in profile.coverage if entry.signal in {"render", "client.render"}),
        None,
    )
    has_render = any(item.operation_name == "render" for item in profile.operations) or any(
        item.event_name == "earshot.audio.render.started" for item in profile.events
    )
    limitations: list[str] = []
    if not has_render:
        limitation = (
            f"render_evidence_{render_coverage.availability}"
            if render_coverage
            else "render_evidence_not_observed"
        )
        # Missing evidence is a limitation, not a diagnosed failure. Coverage has
        # no fact identity in v1, so inventing an evidence-free diagnosis would
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
