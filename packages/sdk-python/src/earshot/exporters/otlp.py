"""Deterministic projection of an ``IncidentBundle`` into an OTLP/JSON document.

This is the "export everywhere" seam: an incident is projected into the universal
OpenTelemetry trace shape (``{"resourceSpans": [...]}``) so a user can keep their
existing observability backend and still receive earshot's richer voice artifact.

The projection is **identity-preserving** and **evidence-faithful**:

* Every :class:`~earshot.contract.Operation` becomes one OTLP span whose
  ``traceId``/``spanId``/``parentSpanId`` are the exact hex identifiers the bundle
  carried (OTLP/JSON encodes span/trace ids as case-insensitive hex, so the values
  are emitted verbatim). Wall-clock ``startTimeUnixNano``/``endTimeUnixNano`` come
  only from the recorded :class:`~earshot.contract.TimePoint`\\ s -- a point
  operation with no ``ended_at`` gets no end, and no duration is ever invented.
* Every :class:`~earshot.contract.Event` becomes a span event on its owning
  operation's span, or a zero-duration span when it has no owning operation.
* Every :class:`~earshot.contract.QualitySample` measurement rides as attributes on
  a session-level span event, carrying name/value/unit plus its measurement basis
  and evidence confidence.

OTLP cannot natively hold earshot's clock domains/relations, coverage availability,
the privacy omission ledger, or per-record evidence provenance. Those facts are
projected into ``earshot.*`` attributes so that "what was *not* observed" survives,
and any place the projection is forced to drop or approximate a fact is marked with
``earshot.projection.lossy=true`` and an ``earshot.projection.note`` rather than
silently discarded. The projection never fabricates a value or a duration the bundle
did not contain.

The output is deterministic: list ordering is fully specified (spans by start time
then span id, scopes and attributes by key) so projecting the same bundle twice
yields byte-identical JSON under ``sort_keys``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contract import (
    Coverage,
    Event,
    Evidence,
    IncidentBundle,
    Operation,
    TimePoint,
)

# OTLP SpanKind enum values (opentelemetry-proto). Voice-pipeline operations are
# internal work; we do not invent client/server/producer roles we cannot observe.
_SPAN_KIND_INTERNAL = 1

# OTLP StatusCode enum values.
_STATUS_UNSET = 0
_STATUS_OK = 1
_STATUS_ERROR = 2

# Operation status labels that project to an OTLP error status. The raw status is
# always preserved verbatim in ``earshot.operation.status`` regardless of mapping.
_ERROR_STATUSES = frozenset(
    {
        "error",
        "failed",
        "failure",
        "timeout",
        "timed_out",
        "deadline_exceeded",
        "cancelled",
        "canceled",
        "aborted",
        "unavailable",
        "rejected",
    }
)
_OK_STATUSES = frozenset({"ok", "success", "succeeded", "completed", "complete"})

_PROJECTION_NOTE = (
    "OTLP cannot natively represent earshot clock domains/relations, coverage "
    "availability, the privacy omission ledger, or per-record evidence provenance; "
    "those facts are projected into earshot.* attributes and may be lossy."
)


# --------------------------------------------------------------------------- #
# AnyValue / attribute encoding (OTLP/JSON ProtoJSON shapes)
# --------------------------------------------------------------------------- #
def _any_value(value: Any) -> dict[str, Any]:
    """Encode a JSON scalar/collection as an OTLP ``AnyValue``.

    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass, and
    int64 is serialized as a decimal string per ProtoJSON.
    """

    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    if isinstance(value, Mapping):
        return {
            "kvlistValue": {
                "values": [
                    {"key": key, "value": _any_value(value[key])}
                    for key in sorted(value)
                    if value[key] is not None
                ]
            }
        }
    # Unrepresentable scalar (e.g. None handled by callers): fall back to a string.
    return {"stringValue": str(value)}


def _attributes(mapping: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project a flat attribute mapping into a key-sorted OTLP KeyValue list."""

    return [
        {"key": key, "value": _any_value(mapping[key])}
        for key in sorted(mapping)
        if mapping[key] is not None
    ]


# --------------------------------------------------------------------------- #
# Deterministic identity synthesis for records that carry no OTel identity
# --------------------------------------------------------------------------- #
def _synth_id(bundle_id: str, kind: str, *parts: str, nbytes: int) -> str:
    """Derive a stable, non-zero hex id from a bundle-scoped key.

    Used only when a record has no native trace/span id; the conversion is always
    marked lossy so the identifier is never mistaken for source-preserved identity.
    """

    digest = hashlib.sha256("\x1f".join((bundle_id, kind, *parts)).encode("utf-8")).hexdigest()
    value = digest[: nbytes * 2]
    if all(character == "0" for character in value):
        value = "0" * (nbytes * 2 - 1) + "1"
    return value


# --------------------------------------------------------------------------- #
# Time projection
# --------------------------------------------------------------------------- #
def _wall_nano(point: TimePoint) -> tuple[str, bool]:
    """Return a wall-clock unix-nano string for ``point`` and whether it is lossy.

    Prefers the source time, then the collector-observed time. Falls back to the
    monotonic value only when no wall time exists at all, flagging the result lossy
    (a monotonic reading is not a wall clock). The exact monotonic/observed/domain
    coordinates are always preserved separately via :func:`_time_attributes`.
    """

    if point.source_time_unix_nano is not None:
        return point.source_time_unix_nano, False
    if point.observed_time_unix_nano is not None:
        return point.observed_time_unix_nano, False
    # The contract guarantees at least one timestamp; only monotonic remains.
    return point.monotonic_time_nano or "0", True


def _time_attributes(point: TimePoint | None) -> dict[str, Any]:
    if point is None:
        return {}
    attributes: dict[str, Any] = {}
    if point.clock_domain_id is not None:
        attributes["earshot.clock.domain.id"] = point.clock_domain_id
    if point.monotonic_time_nano is not None:
        attributes["earshot.time.monotonic_nano"] = point.monotonic_time_nano
    if point.observed_time_unix_nano is not None:
        attributes["earshot.time.observed_unix_nano"] = point.observed_time_unix_nano
    if point.uncertainty_nano is not None:
        attributes["earshot.time.uncertainty_nano"] = point.uncertainty_nano
    return attributes


def _evidence_attributes(evidence: Evidence | None) -> dict[str, Any]:
    if evidence is None:
        return {}
    attributes = {
        "earshot.evidence.source": evidence.source,
        "earshot.evidence.observer": evidence.observer,
        "earshot.evidence.method": evidence.method,
        "earshot.evidence.confidence": evidence.confidence,
        "earshot.evidence.availability": evidence.availability,
    }
    if evidence.method_version is not None:
        attributes["earshot.evidence.method_version"] = evidence.method_version
    if evidence.source_field is not None:
        attributes["earshot.source.field"] = evidence.source_field
    return attributes


def _operation_status(operation: Operation) -> dict[str, Any]:
    lowered = operation.status.lower()
    if operation.error is not None or lowered in _ERROR_STATUSES:
        status: dict[str, Any] = {"code": _STATUS_ERROR}
        # error.code is a governed SemanticCode and the status label is a governed
        # non-empty string; neither carries the potentially sensitive error message.
        message = operation.error.code if operation.error is not None else operation.status
        if message:
            status["message"] = message
        return status
    if lowered in _OK_STATUSES:
        return {"code": _STATUS_OK}
    return {"code": _STATUS_UNSET}


# --------------------------------------------------------------------------- #
# Internal span record: an OTLP span plus its resource/scope provenance
# --------------------------------------------------------------------------- #
@dataclass
class _SpanRecord:
    span: dict[str, Any]
    resource_schema_url: str | None
    scope_name: str | None
    scope_version: str | None
    scope_attributes: dict[str, Any]
    scope_schema_url: str | None
    sort_key: tuple[int, str]

    def scope_group_key(self) -> tuple[str, str, str, str]:
        attribute_signature = "\x1f".join(
            f"{key}={self.scope_attributes[key]!r}" for key in sorted(self.scope_attributes)
        )
        return (
            self.scope_name or "",
            self.scope_version or "",
            attribute_signature,
            self.scope_schema_url or "",
        )


@dataclass
class _Projection:
    records: list[_SpanRecord] = field(default_factory=list)
    resource_dicts: list[Mapping[str, Any]] = field(default_factory=list)
    resource_schema_urls: list[str] = field(default_factory=list)
    lossy: bool = False
    notes: set[str] = field(default_factory=set)

    def note(self, text: str) -> None:
        self.lossy = True
        self.notes.add(text)


# --------------------------------------------------------------------------- #
# Core builder
# --------------------------------------------------------------------------- #
def _build_document(
    bundle: IncidentBundle,
    *,
    openinference_span_kind: Any | None = None,
) -> dict[str, Any]:
    """Project ``bundle`` into an OTLP/JSON trace document.

    When ``openinference_span_kind`` is supplied it is a callable mapping an
    operation name (or ``None`` for synthetic/point spans) to an OpenInference span
    kind that is attached as ``openinference.span.kind``.
    """

    profile = bundle.profile
    bundle_id = profile.manifest.bundle_id
    session_id = profile.manifest.session_id
    projection = _Projection()

    operations_by_id: dict[str, Operation] = {op.operation_id: op for op in profile.operations}
    events_by_operation: dict[str, list[Event]] = {}
    standalone_events: list[Event] = []
    for event in profile.events:
        owner = event.operation_id
        if owner is not None and owner in operations_by_id:
            events_by_operation.setdefault(owner, []).append(event)
        else:
            standalone_events.append(event)

    # Gather resource provenance from every record so the merged resource reflects
    # the whole incident, including events nested as span events.
    for record_source in (profile.operations, profile.events, profile.quality_samples):
        for item in record_source:
            projection.resource_dicts.append(item.resource)
            if item.resource_schema_url is not None:
                projection.resource_schema_urls.append(item.resource_schema_url)

    for operation in profile.operations:
        projection.records.append(
            _operation_span(
                operation,
                bundle_id=bundle_id,
                session_id=session_id,
                owned_events=events_by_operation.get(operation.operation_id, ()),
                operations_by_id=operations_by_id,
                projection=projection,
                openinference_span_kind=openinference_span_kind,
            )
        )

    for event in standalone_events:
        projection.records.append(
            _standalone_event_span(
                event,
                bundle_id=bundle_id,
                session_id=session_id,
                projection=projection,
                openinference_span_kind=openinference_span_kind,
            )
        )

    if profile.quality_samples:
        projection.records.append(
            _session_span(
                bundle,
                bundle_id=bundle_id,
                session_id=session_id,
                projection=projection,
                openinference_span_kind=openinference_span_kind,
            )
        )

    resource_attributes = _resource_attributes(bundle, projection)
    resource_schema_url = _select_resource_schema_url(projection)

    document: dict[str, Any] = {
        "resourceSpans": [
            _resource_spans_entry(
                resource_attributes=resource_attributes,
                resource_schema_url=resource_schema_url,
                records=projection.records,
            )
        ]
    }
    return document


def _operation_span(
    operation: Operation,
    *,
    bundle_id: str,
    session_id: str,
    owned_events: Iterable[Event],
    operations_by_id: Mapping[str, Operation],
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> _SpanRecord:
    trace_id = operation.trace_id
    span_id = operation.span_id
    if trace_id is None or span_id is None:
        trace_id = _synth_id(bundle_id, "op-trace", operation.operation_id, nbytes=16)
        span_id = _synth_id(bundle_id, "op-span", operation.operation_id, nbytes=8)
        projection.note("synthesized span identity for an operation with no OTel ids")

    start_wall, start_lossy = _wall_nano(operation.started_at)
    if start_lossy:
        projection.note("operation start projected from a monotonic-only clock")

    attributes: dict[str, Any] = dict(operation.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.operation.name"] = operation.operation_name
    attributes["earshot.operation.id"] = operation.operation_id
    attributes["earshot.operation.status"] = operation.status
    attributes["earshot.privacy.capture_class"] = operation.capture_class
    attributes["earshot.span.parent_scope"] = operation.parent_scope
    if operation.turn_id is not None:
        attributes["earshot.turn.id"] = operation.turn_id
    if operation.participant_id is not None:
        attributes["earshot.participant.id"] = operation.participant_id
    if operation.stream_id is not None:
        attributes["earshot.stream.id"] = operation.stream_id
    attributes.update(_time_attributes(operation.started_at))
    attributes.update(_evidence_attributes(operation.evidence))
    if operation.error is not None:
        attributes["error.type"] = operation.error.category
    if openinference_span_kind is not None:
        attributes["openinference.span.kind"] = openinference_span_kind(operation.operation_name)

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": operation.operation_name,
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": start_wall,
        "attributes": _attributes(attributes),
        "status": _operation_status(operation),
    }
    if operation.parent_span_id is not None:
        span["parentSpanId"] = operation.parent_span_id
    if operation.ended_at is not None:
        end_wall, end_lossy = _wall_nano(operation.ended_at)
        if end_lossy:
            projection.note("operation end projected from a monotonic-only clock")
        span["endTimeUnixNano"] = end_wall

    span_events = [
        _span_event(event, session_id=session_id)
        for event in sorted(owned_events, key=_event_sort_key)
    ]
    if span_events:
        span["events"] = span_events

    links = _resolve_links(operation, operations_by_id, projection)
    if links:
        span["links"] = links

    return _SpanRecord(
        span=span,
        resource_schema_url=operation.resource_schema_url,
        scope_name=operation.instrumentation_scope_name,
        scope_version=operation.instrumentation_scope_version,
        scope_attributes=dict(operation.instrumentation_scope_attributes),
        scope_schema_url=operation.schema_url,
        sort_key=(int(start_wall), span_id),
    )


def _span_event(event: Event, *, session_id: str) -> dict[str, Any]:
    wall, _ = _wall_nano(event.time)
    attributes: dict[str, Any] = dict(event.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.event.id"] = event.event_id
    attributes["earshot.event.name"] = event.event_name
    attributes["earshot.privacy.capture_class"] = event.capture_class
    if event.turn_id is not None:
        attributes["earshot.turn.id"] = event.turn_id
    if event.participant_id is not None:
        attributes["earshot.participant.id"] = event.participant_id
    if event.stream_id is not None:
        attributes["earshot.stream.id"] = event.stream_id
    attributes.update(_time_attributes(event.time))
    attributes.update(_evidence_attributes(event.evidence))
    return {
        "timeUnixNano": wall,
        "name": event.event_name,
        "attributes": _attributes(attributes),
    }


def _standalone_event_span(
    event: Event,
    *,
    bundle_id: str,
    session_id: str,
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> _SpanRecord:
    trace_id = event.trace_id
    span_id = event.span_id
    if trace_id is None:
        trace_id = _synth_id(bundle_id, "event-trace", event.event_id, nbytes=16)
        projection.note("synthesized trace identity for a standalone event with no OTel ids")
    if span_id is None:
        span_id = _synth_id(bundle_id, "event-span", event.event_id, nbytes=8)
        projection.note("synthesized span identity for a standalone event with no OTel ids")

    wall, wall_lossy = _wall_nano(event.time)
    if wall_lossy:
        projection.note("event time projected from a monotonic-only clock")

    attributes: dict[str, Any] = dict(event.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.event.id"] = event.event_id
    attributes["earshot.event.name"] = event.event_name
    attributes["earshot.privacy.capture_class"] = event.capture_class
    if event.turn_id is not None:
        attributes["earshot.turn.id"] = event.turn_id
    if event.participant_id is not None:
        attributes["earshot.participant.id"] = event.participant_id
    if event.stream_id is not None:
        attributes["earshot.stream.id"] = event.stream_id
    attributes.update(_time_attributes(event.time))
    attributes.update(_evidence_attributes(event.evidence))
    if openinference_span_kind is not None:
        attributes["openinference.span.kind"] = openinference_span_kind(None)

    # A point event is instantaneous: a zero-duration span (start == end) records
    # the fact without inventing a duration.
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": event.event_name,
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": wall,
        "endTimeUnixNano": wall,
        "attributes": _attributes(attributes),
    }
    return _SpanRecord(
        span=span,
        resource_schema_url=event.resource_schema_url,
        scope_name=event.instrumentation_scope_name,
        scope_version=event.instrumentation_scope_version,
        scope_attributes=dict(event.instrumentation_scope_attributes),
        scope_schema_url=event.schema_url,
        sort_key=(int(wall), span_id),
    )


def _session_span(
    bundle: IncidentBundle,
    *,
    bundle_id: str,
    session_id: str,
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> _SpanRecord:
    """A synthetic session-level span that hosts quality-sample measurements.

    Quality samples reference streams/participants but no owning operation, so they
    have no natural span. This container uses the *real* session start/end interval
    and is marked synthetic so it is never confused with a source-recorded span.
    """

    session = bundle.profile.session
    trace_id = _synth_id(bundle_id, "session-trace", session_id, nbytes=16)
    span_id = _synth_id(bundle_id, "session-span", session_id, nbytes=8)

    start_wall, start_lossy = _wall_nano(session.started_at)
    if start_lossy:
        projection.note("session start projected from a monotonic-only clock")

    attributes: dict[str, Any] = {
        "session.id": session_id,
        "earshot.session.id": session_id,
        "earshot.bundle.id": bundle_id,
        "earshot.operation.status": session.status,
        "earshot.projection.synthetic": True,
        "earshot.projection.kind": "session",
        "earshot.projection.note": (
            "synthetic session-level span hosting quality-sample measurements"
        ),
    }
    attributes.update(_time_attributes(session.started_at))
    if openinference_span_kind is not None:
        attributes["openinference.span.kind"] = openinference_span_kind(None)

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "earshot.session",
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": start_wall,
        "attributes": _attributes(attributes),
    }
    if session.ended_at is not None:
        end_wall, end_lossy = _wall_nano(session.ended_at)
        if end_lossy:
            projection.note("session end projected from a monotonic-only clock")
        span["endTimeUnixNano"] = end_wall

    quality_events: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for sample in bundle.profile.quality_samples:
        for measurement in sample.measurements:
            wall, _ = _wall_nano(sample.sample_window.end)
            attrs: dict[str, Any] = {
                "session.id": session_id,
                "earshot.quality.kind": sample.quality_kind,
                "earshot.quality.name": measurement.name,
                "earshot.quality.value": measurement.value,
                "earshot.quality.unit": measurement.unit,
                "earshot.quality.aggregation": measurement.aggregation,
                "earshot.quality.sample_id": sample.sample_id,
            }
            if measurement.raw_counter is not None:
                attrs["earshot.quality.raw_counter"] = measurement.raw_counter
            if sample.participant_id is not None:
                attrs["earshot.participant.id"] = sample.participant_id
            if sample.stream_id is not None:
                attrs["earshot.stream.id"] = sample.stream_id
            if sample.evidence is not None:
                # The measurement basis is the observing source boundary.
                attrs["earshot.metric.basis"] = sample.evidence.source
                attrs.update(_evidence_attributes(sample.evidence))
            attrs.update(_time_attributes(sample.sample_window.end))
            quality_events.append(
                (
                    (int(wall), sample.sample_id, measurement.name),
                    {
                        "timeUnixNano": wall,
                        "name": "earshot.quality.sample",
                        "attributes": _attributes(attrs),
                    },
                )
            )
    if quality_events:
        span["events"] = [event for _, event in sorted(quality_events, key=lambda item: item[0])]

    return _SpanRecord(
        span=span,
        resource_schema_url=None,
        scope_name=None,
        scope_version=None,
        scope_attributes={},
        scope_schema_url=None,
        sort_key=(int(start_wall), span_id),
    )


def _resolve_links(
    operation: Operation,
    operations_by_id: Mapping[str, Operation],
    projection: _Projection,
) -> list[dict[str, Any]]:
    links: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
    for link in operation.links:
        trace_id = link.trace_id
        span_id = link.span_id
        if (trace_id is None or span_id is None) and link.target_operation_id is not None:
            target = operations_by_id.get(link.target_operation_id)
            if target is not None and target.trace_id is not None and target.span_id is not None:
                trace_id = target.trace_id
                span_id = target.span_id
        if trace_id is None or span_id is None:
            projection.note(
                f"dropped an unresolved causal link ({link.relationship}) that OTLP "
                "cannot address without a trace/span id"
            )
            continue
        attributes = {
            "earshot.link.type": link.relationship,
            "earshot.link.target_scope": link.target_scope,
        }
        links.append(
            (
                (trace_id, span_id, link.relationship),
                {
                    "traceId": trace_id,
                    "spanId": span_id,
                    "attributes": _attributes(attributes),
                },
            )
        )
    return [entry for _, entry in sorted(links, key=lambda item: item[0])]


def _resource_attributes(bundle: IncidentBundle, projection: _Projection) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for resource in projection.resource_dicts:
        for key in sorted(resource):
            if key in merged and merged[key] != resource[key]:
                conflicts.append(key)
            else:
                merged.setdefault(key, resource[key])
    if conflicts:
        projection.note(
            "merged conflicting per-record resource attributes: "
            + ", ".join(sorted(set(conflicts)))
        )

    profile = bundle.profile
    merged.setdefault("service.name", profile.manifest.producer.name)
    merged["earshot.session.id"] = profile.manifest.session_id
    merged["earshot.bundle.id"] = profile.manifest.bundle_id

    for coverage in _sorted_coverage(profile.coverage):
        merged[f"earshot.coverage.{coverage.signal}"] = coverage.availability
        if coverage.reason is not None:
            merged[f"earshot.coverage.{coverage.signal}.reason"] = coverage.reason

    omissions = profile.privacy.omissions
    if omissions:
        merged["earshot.privacy.omission_count"] = len(omissions)
        projection.note("privacy omission ledger is not natively representable in OTLP")

    if profile.clock_domains or profile.clock_relations:
        merged["earshot.clock.domain.ids"] = [
            domain.clock_domain_id for domain in profile.clock_domains
        ]
        projection.note("clock domain and relation definitions are dropped by OTLP")

    # The projection always drops the higher-level incident structure; say so.
    merged["earshot.projection.lossy"] = True
    merged["earshot.projection.note"] = _PROJECTION_NOTE
    if projection.notes:
        merged["earshot.projection.details"] = sorted(projection.notes)
    return merged


def _sorted_coverage(coverage: Iterable[Coverage]) -> list[Coverage]:
    return sorted(coverage, key=lambda item: (item.signal, item.availability))


def _select_resource_schema_url(projection: _Projection) -> str | None:
    distinct = sorted({url for url in projection.resource_schema_urls if url})
    if not distinct:
        return None
    if len(distinct) > 1:
        projection.note(
            "records declared multiple resource schema urls; kept the first: " + ", ".join(distinct)
        )
    return distinct[0]


def _resource_spans_entry(
    *,
    resource_attributes: dict[str, Any],
    resource_schema_url: str | None,
    records: list[_SpanRecord],
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str], list[_SpanRecord]] = {}
    for record in records:
        groups.setdefault(record.scope_group_key(), []).append(record)

    scope_spans: list[dict[str, Any]] = []
    for _, group in sorted(groups.items(), key=lambda item: item[0]):
        representative = group[0]
        scope: dict[str, Any] = {"name": representative.scope_name or ""}
        if representative.scope_version is not None:
            scope["version"] = representative.scope_version
        if representative.scope_attributes:
            scope["attributes"] = _attributes(representative.scope_attributes)
        entry: dict[str, Any] = {
            "scope": scope,
            "spans": [record.span for record in sorted(group, key=lambda item: item.sort_key)],
        }
        if representative.scope_schema_url is not None:
            entry["schemaUrl"] = representative.scope_schema_url
        scope_spans.append(entry)

    resource_spans: dict[str, Any] = {
        "resource": {"attributes": _attributes(resource_attributes)},
        "scopeSpans": scope_spans,
    }
    if resource_schema_url is not None:
        resource_spans["schemaUrl"] = resource_schema_url
    return resource_spans


def _event_sort_key(event: Event) -> tuple[int, str]:
    wall, _ = _wall_nano(event.time)
    return (int(wall), event.event_id)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def to_otlp(bundle: IncidentBundle) -> dict[str, Any]:
    """Project ``bundle`` into a deterministic OTLP/JSON trace document.

    The returned mapping is ``{"resourceSpans": [...]}`` and is stable: projecting
    the same bundle again yields an equal document (and byte-identical JSON under
    ``sort_keys``).
    """

    return _build_document(bundle)


def span_count(document: Mapping[str, Any]) -> int:
    """Count the spans in an OTLP/JSON document (handy for callers and tests)."""

    total = 0
    for resource_spans in document.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            total += len(scope_spans.get("spans", []))
    return total
