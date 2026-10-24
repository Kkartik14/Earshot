"""Projections: the read-model rows derived from one canonical incident.

An incident's projections are its operation, causal-link and event rows (the
graph read model), its turn-fact rows (the fleet read model, from the
deterministic analyzer) and three ``incidents`` columns: the retention deadline
and the two local export flags. Every one of them is a function of the canonical
bundle and its project alone.

``compute_projection_rows`` is pure: it reads no clock, database, randomness or
environment, so it runs before the store takes its write lock. Only
``write_projection_rows`` touches the database, inside the caller's transaction,
which keeps an incident and its projections atomic.

``INCIDENT_PROJECTION_VERSION`` (``versions.py``) is stored per incident. A row
whose version is missing or older is stale; the server re-projects stale rows in
the background, in bounded batches, after it starts serving.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .versions import INCIDENT_PROJECTION_VERSION, TURN_FACT_PROJECTION_VERSION

if TYPE_CHECKING:
    from .contract import IncidentBundle
    from .storage_database import DatabaseConnection


@dataclass(frozen=True, slots=True)
class ProjectionRows:
    """Every projected row of one incident, ready to write."""

    bundle_id: str
    expires_at_unix_nano: str | None
    export_allowed_local_api: int
    export_allowed_local_cli: int
    operations: tuple[tuple[object, ...], ...]
    causal_links: tuple[tuple[object, ...], ...]
    events: tuple[tuple[object, ...], ...]
    turn_metrics: tuple[tuple[object, ...], ...]


def effective_expiry(bundle: IncidentBundle) -> str | None:
    """Return the earliest retention deadline governing retained evidence.

    A class-level TTL is relative to the immutable bundle creation timestamp. An
    immutable bundle cannot selectively delete one class in place, so its most
    restrictive captured-class deadline governs the complete artifact.
    """

    created = int(bundle.profile.manifest.created_at_unix_nano)
    deadlines: list[int] = []
    for policy in bundle.profile.privacy.capture_classes:
        if not policy.captured or policy.retention is None:
            continue
        retention = policy.retention
        if retention.expires_at_unix_nano is not None:
            deadlines.append(int(retention.expires_at_unix_nano))
        if retention.ttl_nano is not None:
            deadlines.append(created + int(retention.ttl_nano))
    return str(min(deadlines)) if deadlines else None


def export_allowed(bundle: IncidentBundle, destination: str) -> bool:
    for policy in bundle.profile.privacy.capture_classes:
        if not policy.captured or policy.export is None:
            continue
        export = policy.export
        if not export.allowed:
            return False
        if export.destinations and destination not in export.destinations:
            return False
    return True


def compute_projection_rows(
    bundle: IncidentBundle,
    project_id: str,
    *,
    canonical_payload: bytes | None = None,
) -> ProjectionRows:
    """Derive every projected row of ``bundle`` in ``project_id``; no I/O.

    ``canonical_payload`` is the bundle's canonical encoding when the caller
    holds it (ingest and re-projection both do); the analyzer's input digest is
    taken from those bytes instead of encoding the bundle again.
    """

    operations, causal_links, events = _graph_rows(bundle)
    return ProjectionRows(
        bundle_id=bundle.profile.manifest.bundle_id,
        expires_at_unix_nano=effective_expiry(bundle),
        export_allowed_local_api=int(export_allowed(bundle, "local_api")),
        export_allowed_local_cli=int(export_allowed(bundle, "local_cli")),
        operations=operations,
        causal_links=causal_links,
        events=events,
        turn_metrics=_turn_metric_rows(bundle, project_id, canonical_payload),
    )


def write_projection_rows(
    connection: DatabaseConnection,
    projections: Sequence[ProjectionRows],
    *,
    replace: bool = True,
) -> None:
    """Write the projections of ``projections`` inside the caller's write transaction.

    With ``replace`` the incidents' existing projected rows are deleted and their
    projected ``incidents`` columns (and ``projection_version``) are updated. An
    ingest of a new incident passes ``replace=False``: it has no projected rows
    yet, and its ``INSERT`` already carries the columns. Every table is written
    with a few set-based statements for the whole batch, so the write lock is
    held for a handful of round trips rather than several per incident.
    """

    if not projections:
        return
    if replace:
        bundle_ids = [projection.bundle_id for projection in projections]
        for offset in range(0, len(bundle_ids), _BUNDLE_IDS_PER_STATEMENT):
            chunk = bundle_ids[offset : offset + _BUNDLE_IDS_PER_STATEMENT]
            placeholders = ", ".join("?" for _ in chunk)
            for table in ("events", "causal_links", "operations", "turn_metrics"):
                connection.execute(
                    f"DELETE FROM {table} WHERE bundle_id IN ({placeholders})", chunk
                )
    connection.executemany(
        _INSERT_OPERATION, [row for projection in projections for row in projection.operations]
    )
    connection.executemany(
        _INSERT_CAUSAL_LINK,
        [row for projection in projections for row in projection.causal_links],
    )
    connection.executemany(
        _INSERT_EVENT, [row for projection in projections for row in projection.events]
    )
    connection.executemany(
        _INSERT_TURN_METRIC,
        [row for projection in projections for row in projection.turn_metrics],
    )
    if replace:
        connection.executemany(
            """
            UPDATE incidents
            SET expires_at_unix_nano = ?,
                export_allowed_local_api = ?,
                export_allowed_local_cli = ?,
                projection_version = ?
            WHERE bundle_id = ?
            """,
            [
                (
                    projection.expires_at_unix_nano,
                    projection.export_allowed_local_api,
                    projection.export_allowed_local_cli,
                    INCIDENT_PROJECTION_VERSION,
                    projection.bundle_id,
                )
                for projection in projections
            ],
        )


def _evidence_columns(evidence: Any | None) -> tuple[str | None, str | None, str | None]:
    if evidence is None:
        return None, None, None
    return evidence.source, evidence.confidence, evidence.availability


def _graph_rows(
    bundle: IncidentBundle,
) -> tuple[
    tuple[tuple[object, ...], ...],
    tuple[tuple[object, ...], ...],
    tuple[tuple[object, ...], ...],
]:
    bundle_id = bundle.profile.manifest.bundle_id
    operation_rows: list[tuple[object, ...]] = []
    link_rows: list[tuple[object, ...]] = []
    for operation in bundle.profile.operations:
        evidence_source, evidence_confidence, evidence_availability = _evidence_columns(
            operation.evidence
        )
        ended = operation.ended_at
        operation_rows.append(
            (
                bundle_id,
                operation.operation_id,
                operation.operation_name,
                operation.status,
                operation.participant_id,
                operation.stream_id,
                operation.turn_id,
                operation.trace_id,
                operation.span_id,
                operation.parent_span_id,
                operation.parent_scope,
                operation.started_at.source_time_unix_nano,
                operation.started_at.observed_time_unix_nano,
                operation.started_at.monotonic_time_nano,
                operation.started_at.clock_domain_id,
                ended.source_time_unix_nano if ended else None,
                ended.observed_time_unix_nano if ended else None,
                ended.monotonic_time_nano if ended else None,
                ended.clock_domain_id if ended else None,
                evidence_source,
                evidence_confidence,
                evidence_availability,
                operation.capture_class,
            )
        )
        link_rows.extend(
            (
                bundle_id,
                operation.operation_id,
                link_index,
                link.relationship,
                link.target_scope,
                link.target_operation_id,
                link.trace_id,
                link.span_id,
            )
            for link_index, link in enumerate(operation.links)
        )

    event_rows: list[tuple[object, ...]] = []
    for event in bundle.profile.events:
        evidence_source, evidence_confidence, evidence_availability = _evidence_columns(
            event.evidence
        )
        event_rows.append(
            (
                bundle_id,
                event.event_id,
                event.event_name,
                event.operation_id,
                event.participant_id,
                event.stream_id,
                event.turn_id,
                event.trace_id,
                event.span_id,
                event.time.source_time_unix_nano,
                event.time.observed_time_unix_nano,
                event.time.monotonic_time_nano,
                event.time.clock_domain_id,
                evidence_source,
                evidence_confidence,
                evidence_availability,
                event.capture_class,
            )
        )
    return tuple(operation_rows), tuple(link_rows), tuple(event_rows)


def _turn_metric_rows(
    bundle: IncidentBundle,
    project_id: str,
    canonical_payload: bytes | None,
) -> tuple[tuple[object, ...], ...]:
    """The fleet read model: one row per analyzed turn."""

    from .analysis import _ClockAligner, analyze_incident, comparable_delta
    from .codec import analysis_input_sha256
    from .storage import StorageError

    bundle_id = bundle.profile.manifest.bundle_id
    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle, canonical_payload=canonical_payload),
        generated_at_unix_nano="0",
    )
    aligner = _ClockAligner(bundle.profile.clock_relations)
    operation_by_id = {operation.operation_id: operation for operation in bundle.profile.operations}
    event_by_id = {event.event_id: event for event in bundle.profile.events}
    framework = (
        bundle.profile.manifest.adapters[0].framework if bundle.profile.manifest.adapters else None
    )
    coverage_by_signal = {item.signal: item for item in bundle.profile.coverage}

    def metric_values(metric: Any) -> tuple[object, ...]:
        value_ms: float | None = None
        if metric.availability == "available":
            if metric.unit == "ns":
                value_ms = float(metric.value) / 1_000_000
            elif metric.unit == "ms":
                value_ms = float(metric.value)
            elif metric.unit == "s":
                value_ms = float(metric.value) * 1_000
            else:
                raise StorageError("analysis metric uses an unsupported latency unit")
        return (
            value_ms,
            metric.availability,
            metric.basis,
            metric.confidence,
            metric.limitation,
        )

    def provider_metric_values(
        metric: Any,
        *,
        basis: str,
        limitation: str,
    ) -> tuple[object, ...]:
        value, availability, _, confidence, existing_limitation = metric_values(metric)
        return (
            value,
            availability,
            basis,
            confidence,
            existing_limitation or limitation,
        )

    def absent_metric(
        signal: str,
        *,
        basis: str,
        limitation: str,
    ) -> tuple[object, ...]:
        coverage = coverage_by_signal.get(signal)
        availability = (
            coverage.availability
            if coverage is not None and coverage.availability != "available"
            else "not_observed"
        )
        reason = coverage.reason if coverage is not None else None
        return (
            None,
            availability,
            basis,
            "unavailable",
            reason or limitation,
        )

    def delta_metric(
        start: Any,
        end: Any,
        *,
        basis: str,
        evidence: tuple[Any, ...],
        limitation: str | None = None,
    ) -> tuple[object, ...]:
        delta = comparable_delta(start, end, aligner)
        if delta.availability != "available" or delta.nanoseconds is None:
            return (
                None,
                delta.availability,
                basis,
                "unavailable",
                delta.limitation or limitation,
            )
        confidence = delta.confidence
        evidence_confidences = {item.confidence for item in evidence if item is not None}
        if "inferred" in evidence_confidences:
            confidence = "inferred"
        elif "estimated" in evidence_confidences:
            confidence = "estimated"
        return (
            delta.nanoseconds / 1_000_000,
            "available",
            basis,
            confidence,
            limitation,
        )

    def event_delta_metric(
        events: list[Any],
        start_name: str,
        end_name: str,
        *,
        basis: str,
    ) -> tuple[object, ...] | None:
        starts = [item for item in events if item.event_name == start_name]
        ends = [item for item in events if item.event_name == end_name]
        if not starts or not ends:
            return None
        if len(starts) != 1 or len(ends) != 1:
            return (
                None,
                "unavailable",
                basis,
                "unavailable",
                "ambiguous_event_boundaries",
            )
        start = starts[0]
        end = ends[0]
        return delta_metric(
            start.time,
            end.time,
            basis=basis,
            evidence=(start.evidence, end.evidence),
        )

    rows: list[tuple[object, ...]] = []
    for turn_index, turn in enumerate(analysis.projections.turns):
        operations = [
            operation_by_id[operation_id]
            for operation_id in turn.operation_ids
            if operation_id in operation_by_id
        ]
        events = [event_by_id[event_id] for event_id in turn.event_ids if event_id in event_by_id]
        wall_times = [
            value
            for value in (
                *(operation.started_at.source_time_unix_nano for operation in operations),
                *(event.time.source_time_unix_nano for event in events),
            )
            if value is not None
        ]
        started_at = min(wall_times, key=int) if wall_times else None
        # Use the response-generating LLM or unified agent to avoid provider mislabeling.
        dimension_operations = [
            operation for operation in operations if operation.operation_name == "llm"
        ]
        if not dimension_operations:
            dimension_operations = [
                operation
                for operation in operations
                if operation.operation_name in {"agent", "agent_response"}
            ]
        dimension_pairs: set[tuple[str | None, str | None]] = set()
        for operation in dimension_operations:
            provider_value = operation.attributes.get(
                "gen_ai.provider.name",
                operation.resource.get("gen_ai.provider.name"),
            )
            model_value = operation.attributes.get(
                "gen_ai.request.model",
                operation.resource.get("gen_ai.request.model"),
            )
            provider_value = provider_value if isinstance(provider_value, str) else None
            model_value = model_value if isinstance(model_value, str) else None
            if provider_value is not None or model_value is not None:
                dimension_pairs.add((provider_value, model_value))
        if len(dimension_pairs) == 1:
            provider, model = next(iter(dimension_pairs))
        else:
            provider = None
            model = None
        language_values = {
            value
            for operation in operations
            if operation.operation_name == "stt"
            for value in (
                operation.attributes.get(
                    "earshot.language.code",
                    operation.resource.get("earshot.language.code"),
                ),
            )
            if isinstance(value, str)
        }
        language = next(iter(language_values)) if len(language_values) == 1 else None
        status = (
            "failed"
            if any(operation.status in {"error", "failed", "timeout"} for operation in operations)
            else "completed"
        )
        metrics = turn.metrics
        provider_measurements = json.dumps(
            {
                name: metric.model_dump(mode="json", exclude_none=True)
                for name, metric in sorted(metrics.provider_measurements.items())
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        stt_finalization = event_delta_metric(
            events,
            "earshot.speech.ended",
            "earshot.transcript.final",
            basis="speech_end_to_transcript_final",
        )
        if stt_finalization is None:
            provider_stt = metrics.provider_measurements.get("earshot.stt.finalization_latency")
            provider_basis = "provider_finalization_latency"
            provider_limitation = "final_transcript_boundary_not_observed"
            if provider_stt is None:
                provider_stt = metrics.provider_measurements.get("lk.eou.transcription_delay")
                provider_basis = "provider_transcription_delay"
                provider_limitation = "provider_defined_transcription_boundary"
            stt_finalization = (
                provider_metric_values(
                    provider_stt,
                    basis=provider_basis,
                    limitation=provider_limitation,
                )
                if provider_stt is not None and provider_stt.availability == "available"
                else absent_metric(
                    "stt.final",
                    basis="speech_end_to_transcript_final",
                    limitation="final_transcript_boundary_not_observed",
                )
            )

        eou = event_delta_metric(
            events,
            "earshot.speech.ended",
            "earshot.turn.committed",
            basis="speech_end_to_turn_commit",
        )
        if eou is None:
            provider_eou = metrics.provider_measurements.get("lk.eou.endpointing_delay")
            eou = (
                provider_metric_values(
                    provider_eou,
                    basis="provider_endpointing_delay",
                    limitation="provider_defined_endpointing_boundary",
                )
                if provider_eou is not None and provider_eou.availability == "available"
                else absent_metric(
                    "turn.end",
                    basis="speech_end_to_turn_commit",
                    limitation="end_of_turn_boundary_not_observed",
                )
            )

        lifecycle_operations = [
            operation
            for operation in operations
            if operation.operation_name == "framework_operation"
            and operation.attributes.get("earshot.framework.operation.name") == "turn"
        ]
        if len(lifecycle_operations) == 1 and lifecycle_operations[0].ended_at is not None:
            lifecycle = lifecycle_operations[0]
            turn_duration = delta_metric(
                lifecycle.started_at,
                lifecycle.ended_at,
                basis="native_turn_lifecycle",
                evidence=(lifecycle.evidence,),
            )
        else:
            turn_duration = absent_metric(
                "turn.lifecycle",
                basis="native_turn_lifecycle",
                limitation=(
                    "multiple_turn_lifecycle_intervals"
                    if len(lifecycle_operations) > 1
                    else "turn_lifecycle_interval_not_observed"
                ),
            )

        interruption_events = [
            event for event in events if event.event_name.startswith("earshot.interruption.")
        ]
        accepted_interruptions = [
            event
            for event in interruption_events
            if event.event_name == "earshot.interruption.accepted"
        ]
        ignored_interruptions = [
            event
            for event in interruption_events
            if event.event_name == "earshot.interruption.ignored"
        ]
        terminal_interruptions = accepted_interruptions + ignored_interruptions
        if terminal_interruptions:
            interruption_evidence = {
                event.evidence.confidence
                for event in terminal_interruptions
                if event.evidence is not None
            }
            interruption_confidence = (
                "inferred"
                if "inferred" in interruption_evidence
                else "estimated"
                if "estimated" in interruption_evidence
                else "measured"
            )
            interruption = (
                len(accepted_interruptions),
                "available",
                "accepted_interruption_events",
                interruption_confidence,
                None,
            )
        elif interruption_events:
            interruption = (
                None,
                "unavailable",
                "accepted_interruption_events",
                "unavailable",
                "interruption_outcome_not_observed",
            )
        else:
            interruption_coverage = coverage_by_signal.get(
                "interruption.per_turn"
            ) or coverage_by_signal.get("interruption")
            unassigned_interruption = any(
                event.event_name.startswith("earshot.interruption.") and event.turn_id is None
                for event in bundle.profile.events
            )
            if (
                interruption_coverage is not None
                and interruption_coverage.availability == "available"
            ):
                interruption = (
                    0,
                    "available",
                    "accepted_interruption_events",
                    "measured",
                    None,
                )
            elif interruption_coverage is not None:
                interruption = (
                    None,
                    interruption_coverage.availability,
                    "accepted_interruption_events",
                    "unavailable",
                    interruption_coverage.reason or "interruption_signal_unavailable",
                )
            elif unassigned_interruption:
                interruption = (
                    None,
                    "unavailable",
                    "accepted_interruption_events",
                    "unavailable",
                    "turn_correlation_not_observed",
                )
            else:
                interruption = (
                    None,
                    "not_observed",
                    "accepted_interruption_events",
                    "unavailable",
                    "interruption_signal_not_observed",
                )
        rows.append(
            (
                project_id,
                bundle_id,
                bundle.profile.session.session_id,
                turn.turn_id,
                turn_index,
                started_at,
                framework,
                provider,
                model,
                language,
                status,
                *stt_finalization,
                *eou,
                *metric_values(metrics.first_token_latency),
                *metric_values(metrics.generated_response_latency),
                *metric_values(metrics.sent_response_latency),
                *metric_values(metrics.received_response_latency),
                *metric_values(metrics.render_start_response_latency),
                *metric_values(metrics.response_latency),
                *turn_duration,
                metrics.tools.operation_count,
                metrics.tools.total_work_ms,
                *interruption,
                provider_measurements,
                TURN_FACT_PROJECTION_VERSION,
                bundle.profile.manifest.schema_version,
            )
        )
    return tuple(rows)


# Stays well under SQLite's bind-variable limit.
_BUNDLE_IDS_PER_STATEMENT = 500

_INSERT_OPERATION = """
    INSERT INTO operations (
        bundle_id, operation_id, operation_name, status,
        participant_id, stream_id, turn_id, trace_id, span_id,
        parent_span_id, parent_scope,
        started_source_time_unix_nano, started_observed_time_unix_nano,
        started_monotonic_time_nano, started_clock_domain_id,
        ended_source_time_unix_nano, ended_observed_time_unix_nano,
        ended_monotonic_time_nano, ended_clock_domain_id,
        evidence_source, evidence_confidence, evidence_availability,
        capture_class
    ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
    )
"""
_INSERT_CAUSAL_LINK = """
    INSERT INTO causal_links (
        bundle_id, source_operation_id, link_index, relationship,
        target_scope, target_operation_id, trace_id, span_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_EVENT = """
    INSERT INTO events (
        bundle_id, event_id, event_name, operation_id,
        participant_id, stream_id, turn_id, trace_id, span_id,
        source_time_unix_nano, observed_time_unix_nano,
        monotonic_time_nano, clock_domain_id,
        evidence_source, evidence_confidence, evidence_availability,
        capture_class
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_TURN_METRIC_COLUMNS = (
    "project_id",
    "bundle_id",
    "session_id",
    "turn_id",
    "turn_index",
    "started_at_unix_nano",
    "framework",
    "provider",
    "model",
    "language",
    "status",
    "stt_finalization_ms",
    "stt_finalization_availability",
    "stt_finalization_basis",
    "stt_finalization_confidence",
    "stt_finalization_limitation",
    "eou_ms",
    "eou_availability",
    "eou_basis",
    "eou_confidence",
    "eou_limitation",
    "first_token_ms",
    "first_token_availability",
    "first_token_basis",
    "first_token_confidence",
    "first_token_limitation",
    "generated_response_ms",
    "generated_response_availability",
    "generated_response_basis",
    "generated_response_confidence",
    "generated_response_limitation",
    "sent_response_ms",
    "sent_response_availability",
    "sent_response_basis",
    "sent_response_confidence",
    "sent_response_limitation",
    "received_response_ms",
    "received_response_availability",
    "received_response_basis",
    "received_response_confidence",
    "received_response_limitation",
    "render_start_response_ms",
    "render_start_response_availability",
    "render_start_response_basis",
    "render_start_response_confidence",
    "render_start_response_limitation",
    "response_ms",
    "response_availability",
    "response_basis",
    "response_confidence",
    "response_limitation",
    "turn_duration_ms",
    "turn_duration_availability",
    "turn_duration_basis",
    "turn_duration_confidence",
    "turn_duration_limitation",
    "tool_operation_count",
    "tool_total_work_ms",
    "interruption_count",
    "interruption_availability",
    "interruption_basis",
    "interruption_confidence",
    "interruption_limitation",
    "provider_measurements_json",
    "projection_version",
    "contract_version",
)

_INSERT_TURN_METRIC = (
    f"INSERT INTO turn_metrics ({', '.join(_TURN_METRIC_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _TURN_METRIC_COLUMNS)})"
)
