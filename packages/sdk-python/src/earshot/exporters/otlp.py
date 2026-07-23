"""Deterministic projection of an ``IncidentBundle`` into an OTLP/JSON document.

This is the "export everywhere" seam: an incident is projected into the universal
OpenTelemetry trace shape (``{"resourceSpans": [...]}``) so a user can keep their
existing observability backend and still receive earshot's richer voice artifact.

The projection is deliberately **lossy but never inventive**. OTLP cannot hold
earshot's clock domains/relations, coverage availability, the privacy omission
ledger, or per-record evidence provenance, so those facts ride in ``earshot.*``
attributes and every place the projection drops or approximates something is marked
with ``earshot.projection.lossy=true`` plus an ``earshot.projection.note`` /
``earshot.projection.details``. What the projection must never do is manufacture
authoritative-looking structure the incident did not contain:

* **Time is only ever a real wall clock.** ``startTimeUnixNano`` /
  ``endTimeUnixNano`` / ``timeUnixNano`` are Unix-epoch fields, so they are filled
  only from a recorded ``source_time_unix_nano`` (preferred) or
  ``observed_time_unix_nano``. A monotonic reading belongs to a different clock
  domain and has no epoch meaning: writing one into these fields would date the span
  to 1970. A record whose only timestamp is monotonic -- an uncalibrated browser or
  device clock -- is *omitted and counted* in
  ``earshot.projection.omitted_record_count``, and an interval end with no wall clock
  is simply left off. ``earshot.projection.time_basis`` names which recorded
  coordinate produced the emitted timestamp, and the monotonic/observed/domain
  coordinates always survive verbatim in the ``earshot.time.*`` attributes.
* **One logical entity becomes exactly one span.** Every
  :class:`~earshot.contract.Operation` becomes one span carrying the exact
  ``traceId``/``spanId``/``parentSpanId`` the bundle recorded. An
  :class:`~earshot.contract.Event` is a point *on* a span, not a span: an event whose
  recorded identity names an emitted operation becomes that span's event, and any
  other event becomes its own uniquely-identified span whose ``parentSpanId`` is the
  span it was recorded on. A span identity is claimed once; a colliding record is
  dropped rather than emitted twice.
* **One incident is one trace.** Records that carry no trace context join the
  incident's single trace: the recorded trace when the bundle has exactly one,
  otherwise a trace id derived deterministically (SHA-256, no clock, no randomness)
  from the incident's own identity. The incident is never scattered across a fresh
  synthetic trace per record.
* **A resource is never reassigned.** Records are grouped by the resource they
  declared and each distinct resource becomes its own ``resourceSpans`` entry, so a
  browser span is never silently filed under the server's ``service.name``. A record
  that declared no resource keeps the producer identity instead of inheriting
  someone else's. Incident-level facts (session/bundle id, coverage, projection
  notes) are repeated on every resource so each entry stands alone.

The output is deterministic: resource groups, scopes, spans, events, and attributes
are all ordered by fully specified keys, so projecting the same bundle twice yields
byte-identical JSON under ``sort_keys``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contract import (
    Coverage,
    Event,
    Evidence,
    IncidentBundle,
    IncidentProfile,
    Operation,
    QualitySample,
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

# Which recorded coordinate produced an emitted Unix-epoch timestamp.
_TIME_BASIS_SOURCE = "source_wall"
_TIME_BASIS_OBSERVED = "observed_wall"

_PROJECTION_NOTE = (
    "OTLP cannot natively represent earshot clock domains/relations, coverage "
    "availability, the privacy omission ledger, or per-record evidence provenance; "
    "those facts are projected into earshot.* attributes and may be lossy."
)
_NO_WALL_CLOCK_NOTE = (
    "omitted a record whose only timestamp is a monotonic reading: an OTLP unix-nano "
    "field must hold a real wall clock, and an uncalibrated clock domain has none"
)
_UNKNOWN_END_NOTE = (
    "an interval end with no wall clock was left off the span rather than backdated "
    "to a monotonic reading"
)
_SYNTHETIC_TRACE_NOTE = (
    "the source did not trace this incident; every span with no recorded trace "
    "context shares one trace id derived deterministically from the incident id"
)
_JOINED_RECORDED_TRACE_NOTE = (
    "a record with no trace context joined the incident's single recorded trace so "
    "that one incident stays one trace"
)
_SYNTHETIC_SPAN_NOTE = (
    "synthesized a span id, derived deterministically from the incident and record "
    "id, for a record the source did not trace"
)
_MULTI_TRACE_NOTE = (
    "the incident carries more than one recorded trace, so records with no trace "
    "context were grouped under one synthetic incident trace rather than attributed "
    "to a recorded one"
)
_EVENT_SPAN_NOTE = (
    "a point event is projected as its own uniquely-identified span; the span it was "
    "recorded on becomes the parent, never the event's own identity"
)
_EVENT_HOSTED_NOTE = (
    "a point event recorded on an operation's span is projected as that span's event "
    "rather than a second span sharing its identity"
)
_DUPLICATE_IDENTITY_NOTE = (
    "dropped a record whose trace/span identity was already emitted: an OTLP span "
    "identity must name exactly one span"
)
_EVENT_RESOURCE_NOTE = (
    "an event declared a resource different from the operation whose span hosts it; "
    "OTLP has no per-event resource, so the operation's resource applies"
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


def _signature(mapping: Mapping[str, Any]) -> str:
    """A stable textual identity for an attribute mapping, used for grouping."""

    return json.dumps(dict(mapping), sort_keys=True, ensure_ascii=False, default=repr)


# --------------------------------------------------------------------------- #
# Deterministic identity synthesis for records that carry no OTel identity
# --------------------------------------------------------------------------- #
def _synth_id(bundle_id: str, kind: str, *parts: str, nbytes: int) -> str:
    """Derive a stable, non-zero hex id from a bundle-scoped key.

    Used only when a record has no native trace/span id. The derivation is a pure
    function of the incident's own identity -- no clock, no randomness -- so the same
    bundle always projects to the same ids, and the conversion is always marked so
    the identifier is never mistaken for source-preserved identity.
    """

    digest = hashlib.sha256("\x1f".join((bundle_id, kind, *parts)).encode("utf-8")).hexdigest()
    value = digest[: nbytes * 2]
    if all(character == "0" for character in value):
        value = "0" * (nbytes * 2 - 1) + "1"
    return value


# --------------------------------------------------------------------------- #
# Time projection
# --------------------------------------------------------------------------- #
def _wall_nano(point: TimePoint) -> tuple[str | None, str | None]:
    """Return ``point``'s Unix-epoch nanoseconds and the basis that supplied them.

    Only a real wall clock may reach an OTLP unix-nano field: the source's own
    timestamp when it exists, otherwise the collector's observed time. A monotonic
    reading is a coordinate in some other clock domain -- for an uncalibrated browser
    or device clock there is no unix time at all -- so it is *not* substituted here;
    ``(None, None)`` means "this endpoint has no wall clock" and callers omit rather
    than fabricate. The monotonic/observed/domain coordinates survive separately via
    :func:`_time_attributes`.
    """

    if point.source_time_unix_nano is not None:
        return point.source_time_unix_nano, _TIME_BASIS_SOURCE
    if point.observed_time_unix_nano is not None:
        return point.observed_time_unix_nano, _TIME_BASIS_OBSERVED
    return None, None


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
    resource: Mapping[str, Any]
    resource_schema_url: str | None
    scope_name: str | None
    scope_version: str | None
    scope_attributes: dict[str, Any]
    scope_schema_url: str | None
    sort_key: tuple[int, str]

    def resource_group_key(self) -> tuple[str, str]:
        """The resource this record *declared*; records never share by accident."""

        return (_signature(self.resource), self.resource_schema_url or "")

    def scope_group_key(self) -> tuple[str, str, str, str]:
        return (
            self.scope_name or "",
            self.scope_version or "",
            _signature(self.scope_attributes),
            self.scope_schema_url or "",
        )


@dataclass(frozen=True)
class _IncidentTrace:
    """The single trace every identity-less span of one incident belongs to.

    One incident is one trace. ``recorded`` marks that the id came from the bundle's
    own single trace context (joined rather than invented); ``ambiguous`` marks that
    the bundle recorded several traces, so no recorded one could be chosen and a
    deterministic incident-scoped id stands in.
    """

    trace_id: str
    recorded: bool
    ambiguous: bool

    def join(self, projection: _Projection) -> str:
        """Return the incident trace id, recording what joining it cost."""

        projection.note(_JOINED_RECORDED_TRACE_NOTE if self.recorded else _SYNTHETIC_TRACE_NOTE)
        if self.ambiguous:
            projection.note(_MULTI_TRACE_NOTE)
        return self.trace_id


@dataclass
class _Projection:
    """Accumulates the spans, the loss ledger, and the claimed span identities."""

    records: list[_SpanRecord] = field(default_factory=list)
    lossy: bool = False
    notes: set[str] = field(default_factory=set)
    omitted_records: int = 0
    claimed_identities: set[tuple[str, str]] = field(default_factory=set)

    def note(self, text: str) -> None:
        self.lossy = True
        self.notes.add(text)

    def omit(self, text: str, count: int = 1) -> None:
        """Record that ``count`` source records could not be projected truthfully."""

        self.omitted_records += count
        self.note(text)

    def claim(self, trace_id: str, span_id: str) -> bool:
        """Reserve a span identity, returning ``False`` if it is already emitted."""

        identity = (trace_id, span_id)
        if identity in self.claimed_identities:
            return False
        self.claimed_identities.add(identity)
        return True


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
    operation_id_by_identity: dict[tuple[str, str], str] = {
        (operation.trace_id, operation.span_id): operation.operation_id
        for operation in profile.operations
        if operation.trace_id is not None and operation.span_id is not None
    }
    incident_trace = _incident_trace(profile, bundle_id, session_id)

    events_by_operation: dict[str, list[Event]] = {}
    standalone_events: list[Event] = []
    for event in profile.events:
        owner = event.operation_id if event.operation_id in operations_by_id else None
        if owner is None and event.trace_id is not None and event.span_id is not None:
            # The event names a span we are already emitting: it is a point *on*
            # that span, so host it there instead of minting a rival span with the
            # same identity.
            owner = operation_id_by_identity.get((event.trace_id, event.span_id))
            if owner is not None:
                projection.note(_EVENT_HOSTED_NOTE)
        if owner is None:
            standalone_events.append(event)
        else:
            events_by_operation.setdefault(owner, []).append(event)

    for operation in profile.operations:
        record = _operation_span(
            operation,
            bundle_id=bundle_id,
            session_id=session_id,
            incident_trace=incident_trace,
            owned_events=tuple(events_by_operation.get(operation.operation_id, ())),
            operations_by_id=operations_by_id,
            projection=projection,
            openinference_span_kind=openinference_span_kind,
        )
        if record is not None:
            projection.records.append(record)

    for event in standalone_events:
        record = _standalone_event_span(
            event,
            bundle_id=bundle_id,
            session_id=session_id,
            incident_trace=incident_trace,
            projection=projection,
            openinference_span_kind=openinference_span_kind,
        )
        if record is not None:
            projection.records.append(record)

    projection.records.extend(
        _session_spans(
            bundle,
            bundle_id=bundle_id,
            session_id=session_id,
            incident_trace=incident_trace,
            projection=projection,
            openinference_span_kind=openinference_span_kind,
        )
    )

    return {"resourceSpans": _resource_spans(bundle, projection)}


def _incident_trace(profile: IncidentProfile, bundle_id: str, session_id: str) -> _IncidentTrace:
    """Choose the one trace this incident's identity-less spans belong to.

    If the bundle already carries exactly one recorded trace context, identity-less
    records *join* it rather than being scattered into rival synthetic traces.
    Otherwise the id is derived deterministically (SHA-256 over the incident's own
    identity -- no clock, no randomness) so the same incident always projects to the
    same trace and no other incident shares it.
    """

    recorded = {
        record.trace_id
        for record in (*profile.operations, *profile.events)
        if record.trace_id is not None
    }
    if len(recorded) == 1:
        return _IncidentTrace(recorded.pop(), recorded=True, ambiguous=False)
    return _IncidentTrace(
        _synth_id(bundle_id, "incident-trace", session_id, nbytes=16),
        recorded=False,
        ambiguous=bool(recorded),
    )


def _operation_span(
    operation: Operation,
    *,
    bundle_id: str,
    session_id: str,
    incident_trace: _IncidentTrace,
    owned_events: Sequence[Event],
    operations_by_id: Mapping[str, Operation],
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> _SpanRecord | None:
    start_wall, start_basis = _wall_nano(operation.started_at)
    if start_wall is None:
        # No wall clock exists for this operation, so no honest startTimeUnixNano
        # does either. Omitting it costs a span; inventing one would date the whole
        # operation to 1970.
        projection.omit(_NO_WALL_CLOCK_NOTE, count=1 + len(owned_events))
        return None

    synthetic_trace = operation.trace_id is None
    synthetic_span = operation.span_id is None
    trace_id = incident_trace.join(projection) if synthetic_trace else operation.trace_id
    if synthetic_span:
        span_id = _synth_id(bundle_id, "operation-span", operation.operation_id, nbytes=8)
        projection.note(_SYNTHETIC_SPAN_NOTE)
    else:
        span_id = operation.span_id
    if not projection.claim(trace_id, span_id):
        projection.omit(_DUPLICATE_IDENTITY_NOTE, count=1 + len(owned_events))
        return None

    attributes: dict[str, Any] = dict(operation.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.operation.name"] = operation.operation_name
    attributes["earshot.operation.id"] = operation.operation_id
    attributes["earshot.operation.status"] = operation.status
    attributes["earshot.privacy.capture_class"] = operation.capture_class
    attributes["earshot.span.parent_scope"] = operation.parent_scope
    attributes["earshot.projection.time_basis"] = start_basis
    if synthetic_trace:
        attributes["earshot.projection.synthetic_trace_id"] = True
    if synthetic_span:
        attributes["earshot.projection.synthetic_span_id"] = True
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
        end_wall, _ = _wall_nano(operation.ended_at)
        if end_wall is None:
            projection.note(_UNKNOWN_END_NOTE)
        else:
            span["endTimeUnixNano"] = end_wall

    span_events = _span_events(
        owned_events, operation=operation, session_id=session_id, projection=projection
    )
    if span_events:
        span["events"] = span_events

    links = _resolve_links(operation, operations_by_id, projection)
    if links:
        span["links"] = links

    return _SpanRecord(
        span=span,
        resource=operation.resource,
        resource_schema_url=operation.resource_schema_url,
        scope_name=operation.instrumentation_scope_name,
        scope_version=operation.instrumentation_scope_version,
        scope_attributes=dict(operation.instrumentation_scope_attributes),
        scope_schema_url=operation.schema_url,
        sort_key=(int(start_wall), span_id),
    )


def _span_events(
    events: Sequence[Event],
    *,
    operation: Operation,
    session_id: str,
    projection: _Projection,
) -> list[dict[str, Any]]:
    """Project an operation's owned events into time-ordered OTLP span events."""

    ordered: list[tuple[tuple[int, str], Event, str, str]] = []
    for event in events:
        wall, basis = _wall_nano(event.time)
        if wall is None:
            projection.omit(_NO_WALL_CLOCK_NOTE)
            continue
        if event.resource and dict(event.resource) != dict(operation.resource):
            projection.note(_EVENT_RESOURCE_NOTE)
        ordered.append(((int(wall), event.event_id), event, wall, basis))
    ordered.sort(key=lambda item: item[0])
    return [
        _span_event(event, session_id=session_id, wall=wall, basis=basis)
        for _, event, wall, basis in ordered
    ]


def _span_event(event: Event, *, session_id: str, wall: str, basis: str) -> dict[str, Any]:
    attributes: dict[str, Any] = dict(event.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.event.id"] = event.event_id
    attributes["earshot.event.name"] = event.event_name
    attributes["earshot.privacy.capture_class"] = event.capture_class
    attributes["earshot.projection.time_basis"] = basis
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
    incident_trace: _IncidentTrace,
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> _SpanRecord | None:
    wall, basis = _wall_nano(event.time)
    if wall is None:
        projection.omit(_NO_WALL_CLOCK_NOTE)
        return None

    synthetic_trace = event.trace_id is None
    trace_id = incident_trace.join(projection) if synthetic_trace else event.trace_id
    # ``event.span_id`` names the span the event was *recorded on*, not the event
    # itself. Reusing it here would emit a second span with that span's identity, so
    # the event gets its own id and keeps the recorded span as its parent.
    span_id = _synth_id(bundle_id, "event-span", event.event_id, nbytes=8)
    projection.note(_EVENT_SPAN_NOTE)
    if not projection.claim(trace_id, span_id):
        projection.omit(_DUPLICATE_IDENTITY_NOTE)
        return None

    attributes: dict[str, Any] = dict(event.attributes)
    attributes["session.id"] = session_id
    attributes["earshot.event.id"] = event.event_id
    attributes["earshot.event.name"] = event.event_name
    attributes["earshot.privacy.capture_class"] = event.capture_class
    attributes["earshot.projection.time_basis"] = basis
    attributes["earshot.projection.synthetic_span_id"] = True
    if synthetic_trace:
        attributes["earshot.projection.synthetic_trace_id"] = True
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
    if event.span_id is not None:
        span["parentSpanId"] = event.span_id
    return _SpanRecord(
        span=span,
        resource=event.resource,
        resource_schema_url=event.resource_schema_url,
        scope_name=event.instrumentation_scope_name,
        scope_version=event.instrumentation_scope_version,
        scope_attributes=dict(event.instrumentation_scope_attributes),
        scope_schema_url=event.schema_url,
        sort_key=(int(wall), span_id),
    )


def _session_spans(
    bundle: IncidentBundle,
    *,
    bundle_id: str,
    session_id: str,
    incident_trace: _IncidentTrace,
    projection: _Projection,
    openinference_span_kind: Any | None,
) -> list[_SpanRecord]:
    """Synthetic session-level spans that host quality-sample measurements.

    Quality samples reference streams/participants but no owning operation, so they
    have no natural span. The container uses the *real* session start/end interval,
    joins the incident's single trace, and is marked synthetic so it is never
    confused with a source-recorded span. Samples are grouped by the resource they
    declared, so a browser-observed sample is never filed under the server resource.
    """

    samples = bundle.profile.quality_samples
    if not samples:
        return []

    session = bundle.profile.session
    start_wall, start_basis = _wall_nano(session.started_at)
    if start_wall is None:
        projection.omit(_NO_WALL_CLOCK_NOTE, count=len(samples))
        return []
    end_wall: str | None = None
    if session.ended_at is not None:
        end_wall, _ = _wall_nano(session.ended_at)
        if end_wall is None:
            projection.note(_UNKNOWN_END_NOTE)

    # The session container carries no recorded identity of its own, so it belongs to
    # the incident's one trace rather than a trace invented just for it.
    trace_id = incident_trace.join(projection)

    groups: dict[tuple[str, str], list[QualitySample]] = {}
    for sample in samples:
        key = (_signature(sample.resource), sample.resource_schema_url or "")
        groups.setdefault(key, []).append(sample)

    records: list[_SpanRecord] = []
    for (resource_signature, schema_url), group in sorted(groups.items()):
        span_id = _synth_id(
            bundle_id, "session-span", session_id, resource_signature, schema_url, nbytes=8
        )
        if not projection.claim(trace_id, span_id):
            projection.omit(_DUPLICATE_IDENTITY_NOTE, count=len(group))
            continue

        attributes: dict[str, Any] = {
            "session.id": session_id,
            "earshot.session.id": session_id,
            "earshot.bundle.id": bundle_id,
            "earshot.operation.status": session.status,
            "earshot.projection.synthetic": True,
            "earshot.projection.synthetic_span_id": True,
            "earshot.projection.kind": "session",
            "earshot.projection.time_basis": start_basis,
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
        if end_wall is not None:
            span["endTimeUnixNano"] = end_wall

        quality_events = _quality_events(group, session_id=session_id, projection=projection)
        if quality_events:
            span["events"] = quality_events

        records.append(
            _SpanRecord(
                span=span,
                resource=group[0].resource,
                resource_schema_url=group[0].resource_schema_url,
                scope_name=None,
                scope_version=None,
                scope_attributes={},
                scope_schema_url=None,
                sort_key=(int(start_wall), span_id),
            )
        )
    return records


def _quality_events(
    samples: Sequence[QualitySample],
    *,
    session_id: str,
    projection: _Projection,
) -> list[dict[str, Any]]:
    ordered: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for sample in samples:
        wall, basis = _wall_nano(sample.sample_window.end)
        if wall is None:
            projection.omit(_NO_WALL_CLOCK_NOTE, count=len(sample.measurements))
            continue
        for measurement in sample.measurements:
            attributes: dict[str, Any] = {
                "session.id": session_id,
                "earshot.quality.kind": sample.quality_kind,
                "earshot.quality.name": measurement.name,
                "earshot.quality.value": measurement.value,
                "earshot.quality.unit": measurement.unit,
                "earshot.quality.aggregation": measurement.aggregation,
                "earshot.quality.sample_id": sample.sample_id,
                "earshot.projection.time_basis": basis,
            }
            if measurement.raw_counter is not None:
                attributes["earshot.quality.raw_counter"] = measurement.raw_counter
            if sample.participant_id is not None:
                attributes["earshot.participant.id"] = sample.participant_id
            if sample.stream_id is not None:
                attributes["earshot.stream.id"] = sample.stream_id
            if sample.evidence is not None:
                # The measurement basis is the observing source boundary.
                attributes["earshot.metric.basis"] = sample.evidence.source
                attributes.update(_evidence_attributes(sample.evidence))
            attributes.update(_time_attributes(sample.sample_window.end))
            ordered.append(
                (
                    (int(wall), sample.sample_id, measurement.name),
                    {
                        "timeUnixNano": wall,
                        "name": "earshot.quality.sample",
                        "attributes": _attributes(attributes),
                    },
                )
            )
    return [event for _, event in sorted(ordered, key=lambda item: item[0])]


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


# --------------------------------------------------------------------------- #
# Resource projection: one ResourceSpans entry per declared resource
# --------------------------------------------------------------------------- #
def _resource_spans(bundle: IncidentBundle, projection: _Projection) -> list[dict[str, Any]]:
    """Group the projected spans by the resource each record actually declared.

    Distinct resources stay distinct: a span is never reassigned to a service it did
    not claim, and a record that declared no resource falls back to the producer
    identity rather than inheriting another record's.
    """

    default_service = bundle.profile.manifest.producer.name
    incident_attributes = _incident_attributes(bundle, projection)

    groups: dict[tuple[str, str], list[_SpanRecord]] = {}
    representatives: dict[tuple[str, str], _SpanRecord] = {}
    for record in projection.records:
        key = record.resource_group_key()
        groups.setdefault(key, []).append(record)
        representatives.setdefault(key, record)

    if not groups:
        # An incident with no projectable span still carries its coverage and
        # loss ledger; emit the resource so that honesty is not lost with it.
        attributes: dict[str, Any] = {"service.name": default_service}
        attributes.update(incident_attributes)
        return [
            _resource_spans_entry(
                resource_attributes=attributes, resource_schema_url=None, records=[]
            )
        ]

    entries: list[dict[str, Any]] = []
    for key in sorted(groups):
        representative = representatives[key]
        attributes = dict(representative.resource)
        attributes.setdefault("service.name", default_service)
        attributes.update(incident_attributes)
        entries.append(
            _resource_spans_entry(
                resource_attributes=attributes,
                resource_schema_url=representative.resource_schema_url,
                records=groups[key],
            )
        )
    return entries


def _incident_attributes(bundle: IncidentBundle, projection: _Projection) -> dict[str, Any]:
    """Incident-level facts repeated on every resource so each entry stands alone."""

    profile = bundle.profile
    attributes: dict[str, Any] = {
        "earshot.session.id": profile.manifest.session_id,
        "earshot.bundle.id": profile.manifest.bundle_id,
    }

    for coverage in _sorted_coverage(profile.coverage):
        attributes[f"earshot.coverage.{coverage.signal}"] = coverage.availability
        if coverage.reason is not None:
            attributes[f"earshot.coverage.{coverage.signal}.reason"] = coverage.reason

    if profile.privacy.omissions:
        attributes["earshot.privacy.omission_count"] = len(profile.privacy.omissions)
        projection.note("privacy omission ledger is not natively representable in OTLP")

    if profile.clock_domains or profile.clock_relations:
        attributes["earshot.clock.domain.ids"] = [
            domain.clock_domain_id for domain in profile.clock_domains
        ]
        projection.note("clock domain and relation definitions are dropped by OTLP")

    # The projection always drops the higher-level incident structure; say so.
    attributes["earshot.projection.lossy"] = True
    attributes["earshot.projection.note"] = _PROJECTION_NOTE
    if projection.omitted_records:
        attributes["earshot.projection.omitted_record_count"] = projection.omitted_records
    if projection.notes:
        attributes["earshot.projection.details"] = sorted(projection.notes)
    return attributes


def _sorted_coverage(coverage: Iterable[Coverage]) -> list[Coverage]:
    return sorted(coverage, key=lambda item: (item.signal, item.availability))


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
