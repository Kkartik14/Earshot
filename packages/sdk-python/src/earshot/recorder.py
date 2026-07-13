"""Framework-neutral, fail-open incident recorder.

Adapters feed already-observed framework facts into this recorder. It does not
replace a tracer provider or manufacture duplicate spans for telemetry that already
exists. Manual operations are provided as an escape hatch for raw pipelines.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import math
import secrets
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeVar, overload

from pydantic import BaseModel

from .clock import Clock, SystemClock
from .codec import MAX_PROFILE_DEPTH
from .contract import (
    Adapter,
    AudioStream,
    BundleManifest,
    CaptureClassPolicy,
    CausalLink,
    ClockDomain,
    ConsentRecord,
    Coverage,
    ErrorRecord,
    Event,
    Evidence,
    ExportPolicy,
    IncidentBundle,
    IncidentProfile,
    MediaRef,
    Operation,
    Participant,
    PrivacyManifest,
    Producer,
    QualitySample,
    RawOtlpChunk,
    RedactionRecord,
    RetentionPolicy,
    Session,
    TimePoint,
)
from .contract import (
    Omission as ContractOmission,
)
from .exporter import BoundedAsyncExporter, ExportItem
from .privacy import (
    CaptureClass,
    CaptureGovernance,
    CapturePolicy,
    Omission,
    classify_attribute,
    is_canonical_otel_schema_url,
    is_safe_metadata_key,
    normalize_event_name,
    normalize_operation_name,
    sanitize_attributes,
    sanitize_error_label,
    sanitize_measurement_label,
    sanitize_measurement_unit,
    sanitize_provenance_label,
    sanitize_schema_url,
    sanitize_semantic_label,
    sanitize_source_label,
    sanitize_version_label,
    snapshot_portable_value,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True)
class RecorderConfig:
    producer_name: str = "earshot"
    producer_version: str = "0.1.0"
    clock_domain_id: str | None = None
    capture_policy: CapturePolicy = field(default_factory=CapturePolicy.metadata_only)
    adapters: tuple[Adapter, ...] = ()


class IncidentRecorder:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        bundle_id: str | None = None,
        config: RecorderConfig | None = None,
        clock: Clock | None = None,
        exporter: BoundedAsyncExporter | None = None,
    ) -> None:
        source_config = config or RecorderConfig()
        source_policy = source_config.capture_policy
        policy_snapshot = CapturePolicy(
            enabled=frozenset(source_policy.enabled),
            policy_id=source_policy.policy_id,
            policy_version=source_policy.policy_version,
            governance=MappingProxyType(copy.deepcopy(dict(source_policy.governance))),
        )
        self.config = RecorderConfig(
            producer_name=source_config.producer_name,
            producer_version=source_config.producer_version,
            clock_domain_id=source_config.clock_domain_id,
            capture_policy=policy_snapshot,
            adapters=source_config.adapters,
        )
        self._validate_capture_policy_config()
        self.clock = clock or SystemClock()
        self.session_id = session_id or f"session-{uuid.uuid4().hex}"
        self.bundle_id = bundle_id or f"bundle-{uuid.uuid4().hex}"
        self.clock_domain_id = self.config.clock_domain_id or f"process-{uuid.uuid4().hex}"
        self.exporter = exporter
        self._started_wall = self.clock.unix_nano()
        self._started_mono = self.clock.monotonic_nano()
        self._participants: list[Participant] = []
        self._streams: list[AudioStream] = []
        self._coverage: list[Coverage] = []
        self._operations: list[Operation] = []
        self._events: list[Event] = []
        self._quality_samples: list[QualitySample] = []
        self._media_refs: list[MediaRef] = []
        self._adapters: list[Adapter] = []
        self._raw_otlp_chunks: list[RawOtlpChunk] = []
        self._omissions: list[Omission] = []
        self._retained_classes: set[CaptureClass] = {CaptureClass.METADATA}
        initial_adapter_extensions: list[bool] = []
        for configured_adapter in self.config.adapters:
            adapter, has_extensions = self._prepare_model(configured_adapter)
            self._assert_profile_record_depth(adapter, root_depth=4)
            self._adapters.append(adapter)
            initial_adapter_extensions.append(has_extensions)
        if any(initial_adapter_extensions):
            self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
        self.config = RecorderConfig(
            producer_name=self.config.producer_name,
            producer_version=self.config.producer_version,
            clock_domain_id=self.config.clock_domain_id,
            capture_policy=self.config.capture_policy,
            adapters=tuple(self._adapters),
        )
        self._status = "running"
        self._closed = False
        self._bundle: IncidentBundle | None = None
        self.last_export_error: Exception | None = None
        self.export_accepted: bool | None = None
        self._lock = threading.RLock()

    def _time(self) -> TimePoint:
        return TimePoint(
            source_time_unix_nano=str(self.clock.unix_nano()),
            monotonic_time_nano=str(self.clock.monotonic_nano() - self._started_mono),
            clock_domain_id=self.clock_domain_id,
        )

    def add_participant(
        self,
        participant_id: str,
        *,
        role: str,
        endpoint_kind: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        capture_class: str | None = None,
    ) -> Participant:
        safe, omitted = sanitize_attributes(attributes or {}, self.config.capture_policy)
        inferred_class = self._capture_class_for(safe)
        record_class = self._resolve_record_class(capture_class, inferred_class)
        participant = Participant(
            participant_id=participant_id,
            session_id=self.session_id,
            role=sanitize_semantic_label(role) or "unknown",
            endpoint_kind=sanitize_semantic_label(endpoint_kind),
            capture_class=record_class,
            attributes=safe,
        )
        self._assert_profile_record_depth(participant, root_depth=3)
        with self._lock:
            self._require_open()
            for existing in self._participants:
                if existing.participant_id != participant.participant_id:
                    continue
                if existing != participant:
                    raise ValueError("conflicting duplicate participant identity")
                return existing.model_copy(deep=True)
            self._participants.append(participant.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._track_retained_classes(safe)
            self._retained_classes.add(CaptureClass(record_class))
        return participant

    def add_stream(
        self,
        stream_id: str,
        *,
        participant_id: str,
        direction: str,
        transport_ref: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        capture_class: str | None = None,
    ) -> AudioStream:
        safe, omitted = sanitize_attributes(attributes or {}, self.config.capture_policy)
        inferred_class = self._capture_class_for(safe)
        record_class = self._resolve_record_class(capture_class, inferred_class)
        stream = AudioStream(
            stream_id=stream_id,
            session_id=self.session_id,
            participant_id=participant_id,
            direction=sanitize_semantic_label(direction) or "unknown",
            transport_ref=transport_ref,
            capture_class=record_class,
            attributes=safe,
        )
        self._assert_profile_record_depth(stream, root_depth=3)
        with self._lock:
            self._require_open()
            for existing in self._streams:
                if existing.stream_id != stream.stream_id:
                    continue
                if existing != stream:
                    raise ValueError("conflicting duplicate stream identity")
                return existing.model_copy(deep=True)
            self._streams.append(stream.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._track_retained_classes(safe)
            self._retained_classes.add(CaptureClass(record_class))
        return stream

    def record_coverage(self, signal: str, availability: str, reason: str | None = None) -> None:
        safe_signal = sanitize_semantic_label(signal) or "unknown"
        safe_availability = sanitize_semantic_label(availability) or "unavailable"
        safe_reason = sanitize_semantic_label(reason)
        with self._lock:
            self._require_open()
            for index, existing in enumerate(self._coverage):
                if existing.signal != safe_signal:
                    continue
                if existing.availability == safe_availability and existing.reason == safe_reason:
                    return
                if safe_availability == "available" and existing.availability != "available":
                    self._coverage[index] = Coverage(
                        signal=safe_signal,
                        availability=safe_availability,
                        reason=safe_reason,
                    )
                return
            self._coverage.append(
                Coverage(
                    signal=safe_signal,
                    availability=safe_availability,
                    reason=safe_reason,
                )
            )

    def register_adapter(self, adapter: Adapter) -> None:
        adapter, has_model_extensions = self._prepare_model(adapter)
        self._assert_profile_record_depth(adapter, root_depth=4)
        with self._lock:
            self._require_open()
            if adapter not in self._adapters:
                self._adapters.append(adapter)
            if has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)

    def add_raw_otlp_chunk(
        self,
        *,
        chunk_id: str,
        signal: str,
        payload: bytes,
        content_type: str = "application/x-protobuf",
        compression: str = "identity",
    ) -> bool:
        """Retain exact filtered OTLP only when raw OTLP capture is enabled."""

        with self._lock:
            self._require_open()
            if not self.config.capture_policy.allows(CaptureClass.RAW_OTLP):
                self._omissions.append(
                    Omission(
                        field_key_sha256=hashlib.sha256(f"otlp:{signal}".encode()).hexdigest(),
                        capture_class=CaptureClass.RAW_OTLP,
                    )
                )
                return False
            self._raw_otlp_chunks.append(
                RawOtlpChunk(
                    chunk_id=chunk_id,
                    signal=signal,
                    content_type=content_type,
                    compression=compression,
                    payload=payload,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
            self._retained_classes.add(CaptureClass.RAW_OTLP)
        return True

    def record_operation(
        self,
        *,
        operation_id: str,
        operation_name: str,
        status: str,
        started_at: TimePoint,
        ended_at: TimePoint | None = None,
        participant_id: str | None = None,
        stream_id: str | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        parent_scope: str = "unknown",
        links: tuple[CausalLink, ...] = (),
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        instrumentation_scope_name: str | None = None,
        instrumentation_scope_version: str | None = None,
        instrumentation_scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
        evidence: Evidence | None = None,
        attributes: Mapping[str, Any] | None = None,
        error: ErrorRecord | None = None,
        capture_class: str | None = None,
    ) -> Operation:
        started_at, started_extensions = self._prepare_model(started_at)
        ended_at, ended_extensions = self._prepare_model(ended_at)
        evidence, evidence_extensions = self._prepare_model(evidence)
        error, error_extensions = self._prepare_model(error)
        prepared_links: list[CausalLink] = []
        link_extensions = False
        for link in links:
            prepared_link, has_extensions = self._prepare_model(link)
            prepared_links.append(prepared_link)
            link_extensions = link_extensions or has_extensions
        links = tuple(prepared_links)
        has_model_extensions = any(
            (
                started_extensions,
                ended_extensions,
                evidence_extensions,
                link_extensions,
                error_extensions,
            )
        )
        operation_name, source_name_digest = normalize_operation_name(operation_name)
        source_attributes = dict(attributes or {})
        if source_name_digest is not None:
            source_attributes["earshot.source.name_sha256"] = source_name_digest
        safe_status = sanitize_semantic_label(status) or "unknown"
        if safe_status.startswith("sha256:"):
            source_attributes["earshot.source.status_sha256"] = safe_status.removeprefix("sha256:")
            safe_status = "unknown"
        safe_scope_name = sanitize_semantic_label(instrumentation_scope_name)
        safe_scope_version = sanitize_version_label(instrumentation_scope_version)
        safe_schema_url, schema_url_digest = sanitize_schema_url(
            schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if schema_url_digest is not None:
            source_attributes["earshot.source.schema_url_sha256"] = schema_url_digest
        safe_resource_schema_url, resource_schema_url_digest = sanitize_schema_url(
            resource_schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if resource_schema_url_digest is not None:
            source_attributes["earshot.source.resource_schema_url_sha256"] = (
                resource_schema_url_digest
            )
        safe_parent_scope = (
            parent_scope if parent_scope in {"internal", "external", "unknown"} else "unknown"
        )
        safe, omitted = sanitize_attributes(source_attributes, self.config.capture_policy)
        safe_resource, resource_omitted = sanitize_attributes(
            resource or {}, self.config.capture_policy
        )
        safe_scope_attributes, scope_omitted = sanitize_attributes(
            instrumentation_scope_attributes or {},
            self.config.capture_policy,
        )
        safe_evidence, evidence_omitted = self._sanitize_evidence(evidence)
        safe_links, link_omitted = self._sanitize_links(links)
        safe_error, error_omitted = self._sanitize_error(error)
        governed_attributes: list[Mapping[str, Any]] = [
            safe,
            safe_resource,
            safe_scope_attributes,
        ]
        if safe_evidence is not None:
            governed_attributes.append(safe_evidence.attributes)
        governed_attributes.extend(link.attributes for link in safe_links)
        inferred_class = self._capture_class_for_many(governed_attributes)
        record_class = self._resolve_record_class(capture_class, inferred_class)
        operation = Operation(
            operation_id=operation_id,
            session_id=self.session_id,
            operation_name=operation_name,
            status=safe_status,
            started_at=started_at,
            ended_at=ended_at,
            participant_id=participant_id,
            stream_id=stream_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            parent_scope=safe_parent_scope,
            links=safe_links,
            resource=safe_resource,
            resource_schema_url=safe_resource_schema_url,
            instrumentation_scope_name=safe_scope_name,
            instrumentation_scope_version=safe_scope_version,
            instrumentation_scope_attributes=safe_scope_attributes,
            schema_url=safe_schema_url,
            evidence=safe_evidence,
            capture_class=record_class,
            attributes=safe,
            error=safe_error,
        )
        self._assert_profile_record_depth(operation, root_depth=3)
        with self._lock:
            self._require_open()
            self._operations.append(operation.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._omissions.extend(resource_omitted)
            self._omissions.extend(scope_omitted)
            self._omissions.extend(evidence_omitted)
            self._omissions.extend(link_omitted)
            self._omissions.extend(error_omitted)
            self._track_retained_classes(safe)
            self._track_retained_classes(safe_resource)
            self._track_retained_classes(safe_scope_attributes)
            if safe_evidence is not None:
                self._track_retained_classes(safe_evidence.attributes)
            for link in safe_links:
                self._track_retained_classes(link.attributes)
            self._retained_classes.add(CaptureClass(record_class))
            if safe_schema_url is not None and not is_canonical_otel_schema_url(safe_schema_url):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if safe_resource_schema_url is not None and not is_canonical_otel_schema_url(
                safe_resource_schema_url
            ):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if safe_error is not None:
                self._retained_classes.add(CaptureClass(safe_error.capture_class))
                self._track_retained_classes(safe_error.attributes)
            if has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
        return operation

    @contextlib.contextmanager
    def operation(
        self,
        operation_name: str,
        *,
        operation_id: str | None = None,
        participant_id: str | None = None,
        stream_id: str | None = None,
        turn_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        capture_class: str | None = None,
    ) -> Iterator[dict[str, str]]:
        """Manual instrumentation for raw pipelines; exceptions are re-raised."""

        identity = operation_id or f"operation-{uuid.uuid4().hex}"
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        started_at = self._time()
        status = "ok"
        error: ErrorRecord | None = None
        application_error = False
        try:
            yield {"operation_id": identity, "trace_id": trace_id, "span_id": span_id}
        except BaseException as caught:
            application_error = True
            status = "error"
            # Metadata-only records the exception type, not its possibly sensitive message.
            error = ErrorRecord(
                code=type(caught).__name__,
                category="application",
                message=None,
                capture_class="metadata",
            )
            raise
        finally:
            try:
                self.record_operation(
                    operation_id=identity,
                    operation_name=operation_name,
                    status=status,
                    started_at=started_at,
                    ended_at=self._time(),
                    participant_id=participant_id,
                    stream_id=stream_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    attributes=attributes,
                    error=error,
                    capture_class=capture_class,
                )
            except Exception as recording_error:
                self.last_export_error = recording_error
                if not application_error:
                    raise

    def record_event(
        self,
        event_name: str,
        *,
        event_id: str | None = None,
        time: TimePoint | None = None,
        operation_id: str | None = None,
        participant_id: str | None = None,
        stream_id: str | None = None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        instrumentation_scope_name: str | None = None,
        instrumentation_scope_version: str | None = None,
        instrumentation_scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
        evidence: Evidence | None = None,
        attributes: Mapping[str, Any] | None = None,
        capture_class: str | None = None,
    ) -> Event:
        event_time = time or self._time()
        event_time, time_extensions = self._prepare_model(event_time)
        evidence, evidence_extensions = self._prepare_model(evidence)
        has_model_extensions = time_extensions or evidence_extensions
        event_name, source_name_digest = normalize_event_name(event_name)
        source_attributes = dict(attributes or {})
        if source_name_digest is not None:
            source_attributes["earshot.source.name_sha256"] = source_name_digest
        safe_scope_name = sanitize_semantic_label(instrumentation_scope_name)
        safe_scope_version = sanitize_version_label(instrumentation_scope_version)
        safe_schema_url, schema_url_digest = sanitize_schema_url(
            schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if schema_url_digest is not None:
            source_attributes["earshot.source.schema_url_sha256"] = schema_url_digest
        safe_resource_schema_url, resource_schema_url_digest = sanitize_schema_url(
            resource_schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if resource_schema_url_digest is not None:
            source_attributes["earshot.source.resource_schema_url_sha256"] = (
                resource_schema_url_digest
            )
        safe, omitted = sanitize_attributes(source_attributes, self.config.capture_policy)
        safe_resource, resource_omitted = sanitize_attributes(
            resource or {}, self.config.capture_policy
        )
        safe_scope_attributes, scope_omitted = sanitize_attributes(
            instrumentation_scope_attributes or {},
            self.config.capture_policy,
        )
        safe_evidence, evidence_omitted = self._sanitize_evidence(evidence)
        governed_attributes: list[Mapping[str, Any]] = [
            safe,
            safe_resource,
            safe_scope_attributes,
        ]
        if safe_evidence is not None:
            governed_attributes.append(safe_evidence.attributes)
        inferred_class = self._capture_class_for_many(governed_attributes)
        record_class = self._resolve_record_class(capture_class, inferred_class)
        event = Event(
            event_id=event_id or f"event-{uuid.uuid4().hex}",
            session_id=self.session_id,
            event_name=event_name,
            time=event_time,
            operation_id=operation_id,
            participant_id=participant_id,
            stream_id=stream_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=span_id,
            resource=safe_resource,
            resource_schema_url=safe_resource_schema_url,
            instrumentation_scope_name=safe_scope_name,
            instrumentation_scope_version=safe_scope_version,
            instrumentation_scope_attributes=safe_scope_attributes,
            schema_url=safe_schema_url,
            evidence=safe_evidence,
            capture_class=record_class,
            attributes=safe,
        )
        self._assert_profile_record_depth(event, root_depth=3)
        with self._lock:
            self._require_open()
            self._events.append(event.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._omissions.extend(resource_omitted)
            self._omissions.extend(scope_omitted)
            self._omissions.extend(evidence_omitted)
            self._track_retained_classes(safe)
            self._track_retained_classes(safe_resource)
            self._track_retained_classes(safe_scope_attributes)
            if safe_evidence is not None:
                self._track_retained_classes(safe_evidence.attributes)
            self._retained_classes.add(CaptureClass(record_class))
            if safe_schema_url is not None and not is_canonical_otel_schema_url(safe_schema_url):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if safe_resource_schema_url is not None and not is_canonical_otel_schema_url(
                safe_resource_schema_url
            ):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
        return event

    def record_quality_sample(
        self,
        sample: QualitySample,
        *,
        capture_class: str | None = None,
    ) -> QualitySample:
        """Retain a provider/transport sample after recursively filtering it."""

        sample, has_model_extensions = self._prepare_model(sample)
        if sample.session_id != self.session_id:
            raise ValueError("quality sample belongs to a different session")
        source_attributes = dict(sample.attributes)
        safe_schema_url, schema_url_digest = sanitize_schema_url(
            sample.schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if schema_url_digest is not None:
            source_attributes["earshot.source.schema_url_sha256"] = schema_url_digest
        safe_resource_schema_url, resource_schema_url_digest = sanitize_schema_url(
            sample.resource_schema_url,
            allow_extension=self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD),
        )
        if resource_schema_url_digest is not None:
            source_attributes["earshot.source.resource_schema_url_sha256"] = (
                resource_schema_url_digest
            )
        safe, omitted = sanitize_attributes(source_attributes, self.config.capture_policy)
        safe_resource, resource_omitted = sanitize_attributes(
            sample.resource,
            self.config.capture_policy,
        )
        safe_scope_attributes, scope_omitted = sanitize_attributes(
            sample.instrumentation_scope_attributes,
            self.config.capture_policy,
        )
        safe_evidence, evidence_omitted = self._sanitize_evidence(sample.evidence)
        safe_measurements = []
        measurement_omitted: list[Omission] = []
        governed: list[Mapping[str, Any]] = [safe, safe_resource, safe_scope_attributes]
        if safe_evidence is not None:
            governed.append(safe_evidence.attributes)
        for measurement in sample.measurements:
            measurement_attributes, measurement_drops = sanitize_attributes(
                measurement.attributes,
                self.config.capture_policy,
            )
            safe_measurements.append(
                measurement.model_copy(
                    update={
                        "name": sanitize_measurement_label(measurement.name),
                        "unit": sanitize_measurement_unit(measurement.unit),
                        "attributes": measurement_attributes,
                    }
                )
            )
            measurement_omitted.extend(measurement_drops)
            governed.append(measurement_attributes)
        inferred = self._capture_class_for_many(governed)
        requested = capture_class
        if requested is None and sample.capture_class != CaptureClass.METADATA.value:
            requested = sample.capture_class
        record_class = self._resolve_record_class(requested, inferred)
        sanitized = sample.model_copy(
            update={
                "quality_kind": sanitize_semantic_label(sample.quality_kind),
                "attributes": safe,
                "resource": safe_resource,
                "resource_schema_url": safe_resource_schema_url,
                "evidence": safe_evidence,
                "instrumentation_scope_name": sanitize_semantic_label(
                    sample.instrumentation_scope_name
                ),
                "instrumentation_scope_version": sanitize_version_label(
                    sample.instrumentation_scope_version
                ),
                "instrumentation_scope_attributes": safe_scope_attributes,
                "schema_url": safe_schema_url,
                "measurements": tuple(safe_measurements),
                "capture_class": record_class,
            }
        )
        sanitized, _ = self._prepare_model(sanitized)
        self._assert_profile_record_depth(sanitized, root_depth=3)
        with self._lock:
            self._require_open()
            self._quality_samples.append(sanitized.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._omissions.extend(resource_omitted)
            self._omissions.extend(scope_omitted)
            self._omissions.extend(evidence_omitted)
            self._omissions.extend(measurement_omitted)
            for attributes in governed:
                self._track_retained_classes(attributes)
            self._retained_classes.add(CaptureClass(record_class))
            if safe_schema_url is not None and not is_canonical_otel_schema_url(safe_schema_url):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if safe_resource_schema_url is not None and not is_canonical_otel_schema_url(
                safe_resource_schema_url
            ):
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            if has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
        return sanitized

    def add_media_ref(self, media: MediaRef) -> bool:
        """Attach governed media metadata; media bytes are never embedded."""

        from .privacy import locator_has_credentials

        media, has_model_extensions = self._prepare_model(media)
        if media.session_id != self.session_id:
            raise ValueError("media reference belongs to a different session")
        if media.capture_class != CaptureClass.AUDIO.value:
            raise ValueError("media references require the audio capture class")
        if not self.config.capture_policy.allows(CaptureClass.AUDIO):
            with self._lock:
                self._require_open()
                self._omissions.append(
                    Omission(
                        field_key_sha256=hashlib.sha256(b"media_ref").hexdigest(),
                        capture_class=CaptureClass.AUDIO,
                    )
                )
            return False
        safe, omitted = sanitize_attributes(media.attributes, self.config.capture_policy)
        locator = media.locator
        if locator is not None and locator_has_credentials(locator.uri):
            omitted.append(
                Omission(
                    field_key_sha256=hashlib.sha256(b"media.locator.uri").hexdigest(),
                    capture_class=CaptureClass.AUDIO,
                    reason="credential_bearing_locator",
                )
            )
            locator = None
        sanitized = media.model_copy(update={"attributes": safe, "locator": locator})
        sanitized, _ = self._prepare_model(sanitized)
        self._assert_profile_record_depth(sanitized, root_depth=3)
        with self._lock:
            self._require_open()
            self._media_refs.append(sanitized.model_copy(deep=True))
            self._omissions.extend(omitted)
            self._retained_classes.add(CaptureClass.AUDIO)
            self._track_retained_classes(safe)
            if has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
        return True

    def close(self, status: str = "completed") -> IncidentBundle:
        with self._lock:
            if self._bundle is not None:
                return self._bundle.model_copy(deep=True)
            safe_status = sanitize_semantic_label(status) or "unknown"
            status_attributes: dict[str, str] = {}
            if safe_status.startswith("sha256:"):
                status_attributes["earshot.source.status_sha256"] = safe_status.removeprefix(
                    "sha256:"
                )
                safe_status = "unknown"
            ended = self._time()
            privacy_omissions = tuple(
                ContractOmission(
                    omission_id=f"omission-{index}",
                    capture_class=item.capture_class.value,
                    reason=item.reason,
                    count=1,
                    attributes={"field_key_sha256": item.field_key_sha256},
                )
                for index, item in enumerate(self._omissions)
            )
            capture_classes = tuple(
                CaptureClassPolicy(
                    capture_class=capture_class.value,
                    decision=(
                        "allow" if self.config.capture_policy.allows(capture_class) else "deny"
                    ),
                    captured=capture_class in self._retained_classes,
                    consent=(
                        ConsentRecord(
                            status=governance.consent.status,
                            legal_basis=governance.consent.legal_basis,
                            recorded_at_unix_nano=governance.consent.recorded_at_unix_nano,
                            authority=governance.consent.authority,
                        )
                        if governance is not None and governance.consent is not None
                        else None
                    ),
                    redaction=(
                        RedactionRecord(
                            policy_id=governance.redaction.policy_id,
                            policy_version=governance.redaction.policy_version,
                            status=governance.redaction.status,
                            findings_count=governance.redaction.findings_count,
                            redacted_count=governance.redaction.redacted_count,
                            executed_at_unix_nano=governance.redaction.executed_at_unix_nano,
                        )
                        if governance is not None and governance.redaction is not None
                        else None
                    ),
                    retention=(
                        RetentionPolicy(
                            expires_at_unix_nano=governance.retention.expires_at_unix_nano,
                            ttl_nano=governance.retention.ttl_nano,
                            policy_id=governance.retention.policy_id,
                        )
                        if governance is not None and governance.retention is not None
                        else None
                    ),
                    export=(
                        ExportPolicy(
                            allowed=governance.export.allowed,
                            destinations=governance.export.destinations,
                            policy_id=governance.export.policy_id,
                        )
                        if governance is not None and governance.export is not None
                        else None
                    ),
                )
                for capture_class in CaptureClass
                for governance in (self.config.capture_policy.governance.get(capture_class),)
            )
            profile = IncidentProfile(
                manifest=BundleManifest(
                    bundle_id=self.bundle_id,
                    session_id=self.session_id,
                    created_at_unix_nano=str(self._started_wall),
                    producer=Producer(
                        name=self.config.producer_name,
                        version=self.config.producer_version,
                        sdk_version=self.config.producer_version,
                    ),
                    adapters=tuple(self._adapters),
                ),
                session=Session(
                    session_id=self.session_id,
                    status=safe_status,
                    started_at=TimePoint(
                        source_time_unix_nano=str(self._started_wall),
                        monotonic_time_nano="0",
                        clock_domain_id=self.clock_domain_id,
                    ),
                    ended_at=ended,
                    attributes=status_attributes,
                ),
                privacy=PrivacyManifest(
                    policy_id=self.config.capture_policy.policy_id,
                    policy_version=self.config.capture_policy.policy_version,
                    capture_classes=capture_classes,
                    omissions=privacy_omissions,
                ),
                participants=tuple(self._participants),
                audio_streams=tuple(self._streams),
                clock_domains=(
                    ClockDomain(
                        clock_domain_id=self.clock_domain_id,
                        kind="process_monotonic",
                        observer="earshot.sdk",
                        monotonic_origin_nano=str(self._started_mono),
                        wall_origin_unix_nano=str(self._started_wall),
                        uncertainty_nano="0",
                        synchronization_method="same_process_sample",
                    ),
                ),
                coverage=tuple(self._coverage),
                operations=tuple(self._operations),
                events=tuple(self._events),
                quality_samples=tuple(self._quality_samples),
                media_refs=tuple(self._media_refs),
            )
            bundle = IncidentBundle(
                profile=profile,
                raw_otlp_chunks=tuple(self._raw_otlp_chunks),
            )
            from .validation import assert_valid_incident  # imported lazily

            assert_valid_incident(bundle)
            self._status = safe_status
            self._closed = True
            # Keep an internal immutable-by-ownership snapshot. Contract models are
            # frozen, but nested dict/list extras are mutable Python containers.
            self._bundle = bundle.model_copy(deep=True)
            export_bundle = self._bundle

        if self.exporter is not None:
            try:
                from .codec import encode_incident_protobuf  # imported lazily
                from .privacy import assert_export_allowed

                assert_export_allowed(export_bundle, "sdk_http")
                self.export_accepted = self.exporter.submit(
                    ExportItem(
                        bundle_id=self.bundle_id,
                        payload=encode_incident_protobuf(export_bundle),
                    )
                )
            except Exception as error:
                from .privacy import ExportPolicyError

                if isinstance(error, ExportPolicyError):
                    self.export_accepted = False
                self.last_export_error = error
        return export_bundle.model_copy(deep=True)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("incident recorder is closed")

    def _validate_capture_policy_config(self) -> None:
        """Fail before recording when governance cannot form contract records."""

        try:
            for governance in self.config.capture_policy.governance.values():
                if not isinstance(governance, CaptureGovernance):
                    raise TypeError("governance values must be CaptureGovernance")
                if governance.consent is not None:
                    ConsentRecord(
                        status=governance.consent.status,
                        legal_basis=governance.consent.legal_basis,
                        recorded_at_unix_nano=governance.consent.recorded_at_unix_nano,
                        authority=governance.consent.authority,
                    )
                if governance.redaction is not None:
                    RedactionRecord(
                        policy_id=governance.redaction.policy_id,
                        policy_version=governance.redaction.policy_version,
                        status=governance.redaction.status,
                        findings_count=governance.redaction.findings_count,
                        redacted_count=governance.redaction.redacted_count,
                        executed_at_unix_nano=governance.redaction.executed_at_unix_nano,
                    )
                if governance.retention is not None:
                    RetentionPolicy(
                        expires_at_unix_nano=governance.retention.expires_at_unix_nano,
                        ttl_nano=governance.retention.ttl_nano,
                        policy_id=governance.retention.policy_id,
                    )
                if governance.export is not None:
                    ExportPolicy(
                        allowed=governance.export.allowed,
                        destinations=governance.export.destinations,
                        policy_id=governance.export.policy_id,
                    )
            PrivacyManifest(
                policy_id=self.config.capture_policy.policy_id,
                policy_version=self.config.capture_policy.policy_version,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("capture governance configuration is invalid") from error

    @overload
    def _prepare_model(self, value: _ModelT) -> tuple[_ModelT, bool]: ...

    @overload
    def _prepare_model(self, value: None) -> tuple[None, bool]: ...

    def _prepare_model(self, value: BaseModel | None) -> tuple[BaseModel | None, bool]:
        """Revalidate and detach a caller-supplied contract model."""

        if value is None:
            return None, False
        # Preflight the source to stop cycles/depth before Pydantic serialization.
        self._authorize_model_extensions(value)
        try:
            dumped = value.model_dump(mode="python", round_trip=True, warnings=False)
            dumped = snapshot_portable_value(dumped)
            validated = type(value).model_validate(dumped)
            snapshot = validated.model_copy(deep=True)
            # Authorize the exact detached object that will be committed. Custom
            # Mapping implementations may expose a different view across reads.
            has_extensions = self._authorize_model_extensions(snapshot)
            return snapshot, has_extensions
        except Exception as error:
            raise ValueError("contract model input is structurally invalid") from error

    @staticmethod
    def _assert_profile_record_depth(model: BaseModel, *, root_depth: int) -> None:
        """Apply the codec's profile-depth definition before recorder mutation."""

        try:
            value = model.model_dump(mode="python", exclude_none=True, warnings=False)
        except Exception as error:
            raise ValueError("contract record is not portable") from error
        stack: list[tuple[Any, int]] = [(value, root_depth)]
        while stack:
            current, depth = stack.pop()
            if depth > MAX_PROFILE_DEPTH:
                raise ValueError("contract record exceeds maximum profile nesting depth")
            if isinstance(current, Mapping):
                stack.extend((child, depth + 1) for child in current.values())
            elif isinstance(current, (list, tuple)):
                stack.extend((child, depth + 1) for child in current)

    def _authorize_model_extensions(self, value: Any) -> bool:
        """Reject model extras before a record mutates recorder state.

        Attribute maps have their own capture-class filtering. Pydantic model extras
        are a separate forward-compatibility channel and always require the explicit
        extension-payload grant.
        """

        found_extension = False
        active_containers: set[int] = set()

        def validate_extension_payload(candidate: Any, depth: int = 0) -> None:
            if depth > 64:
                raise ValueError("model extensions contain an unsafe key or value")
            if candidate is None:
                # Null extras are ambiguous across forward-compatible decoders. The
                # contract requires producers to omit an unsupported field instead.
                raise ValueError("model extensions contain an unsafe key or value")
            if isinstance(candidate, bool):
                return
            if isinstance(candidate, int):
                if abs(candidate) > 9_007_199_254_740_991:
                    raise ValueError("model extensions contain an unsafe key or value")
                return
            if isinstance(candidate, float):
                if not math.isfinite(candidate):
                    raise ValueError("model extensions contain an unsafe key or value")
                return
            if isinstance(candidate, str):
                try:
                    candidate.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ValueError("model extensions contain an unsafe key or value") from error
                return
            if isinstance(candidate, BaseModel):
                # Arbitrary model instances are not JSON values. Producers must
                # explicitly author their portable extension representation.
                raise ValueError("model extensions contain an unsafe key or value")
            if isinstance(candidate, Mapping):
                identity = id(candidate)
                if identity in active_containers:
                    raise ValueError("model extensions contain an unsafe key or value")
                active_containers.add(identity)
                try:
                    for key, child in candidate.items():
                        if not isinstance(key, str):
                            raise ValueError("model extensions contain an unsafe key or value")
                        if classify_attribute(key) is not CaptureClass.METADATA:
                            raise ValueError(
                                "sensitive model extensions must use governed attributes"
                            )
                        kept, omitted = sanitize_attributes(
                            {key: child}, self.config.capture_policy
                        )
                        if omitted or key not in kept:
                            raise ValueError("model extensions contain an unsafe key or value")
                        validate_extension_payload(child, depth + 1)
                finally:
                    active_containers.remove(identity)
                return
            if isinstance(candidate, (list, tuple)):
                identity = id(candidate)
                if identity in active_containers:
                    raise ValueError("model extensions contain an unsafe key or value")
                active_containers.add(identity)
                try:
                    for child in candidate:
                        validate_extension_payload(child, depth + 1)
                finally:
                    active_containers.remove(identity)
                return
            raise ValueError("model extensions contain an unsafe key or value")

        def inspect(candidate: Any, depth: int = 0) -> None:
            nonlocal found_extension

            if depth > MAX_PROFILE_DEPTH:
                raise ValueError("contract model input is structurally invalid")
            if isinstance(candidate, BaseModel):
                identity = id(candidate)
                if identity in active_containers:
                    raise ValueError("model extensions contain an unsafe key or value")
                active_containers.add(identity)
                try:
                    extras = candidate.model_extra or {}
                    if extras:
                        if not self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD):
                            raise ValueError("model extensions require extension_payload capture")
                        validate_extension_payload(extras)
                        found_extension = True
                    for field_name in type(candidate).model_fields:
                        if field_name != "payload":
                            inspect(getattr(candidate, field_name), depth + 1)
                finally:
                    active_containers.remove(identity)
                return
            if isinstance(candidate, Mapping):
                identity = id(candidate)
                if identity in active_containers:
                    raise ValueError("model extensions contain an unsafe key or value")
                active_containers.add(identity)
                try:
                    for child in candidate.values():
                        inspect(child, depth + 1)
                finally:
                    active_containers.remove(identity)
                return
            if isinstance(candidate, (list, tuple)):
                identity = id(candidate)
                if identity in active_containers:
                    raise ValueError("model extensions contain an unsafe key or value")
                active_containers.add(identity)
                try:
                    for child in candidate:
                        inspect(child, depth + 1)
                finally:
                    active_containers.remove(identity)

        inspect(value)
        return found_extension

    @staticmethod
    def _capture_class_for(attributes: Mapping[str, Any]) -> str:
        sensitive = {
            classify_attribute(key)
            for key in attributes
            if classify_attribute(key) is not CaptureClass.METADATA
        }
        if len(sensitive) > 1:
            raise ValueError("one record cannot mix multiple sensitive capture classes")
        return next(iter(sensitive)).value if sensitive else CaptureClass.METADATA.value

    @classmethod
    def _capture_class_for_many(cls, values: list[Mapping[str, Any]]) -> str:
        classes = {cls._capture_class_for(attributes) for attributes in values if attributes}
        classes.discard(CaptureClass.METADATA.value)
        if len(classes) > 1:
            raise ValueError("one record cannot mix multiple sensitive capture classes")
        return next(iter(classes)) if classes else CaptureClass.METADATA.value

    def _resolve_record_class(self, requested: str | None, inferred: str) -> str:
        if requested is None:
            selected = inferred
        else:
            try:
                requested_class = CaptureClass(requested)
            except ValueError as error:
                raise ValueError("unsupported SDK capture class") from error
            if inferred != CaptureClass.METADATA.value and requested != inferred:
                raise ValueError("record capture class does not match retained payload")
            selected = requested_class.value
        capture_class = CaptureClass(selected)
        if capture_class is CaptureClass.RAW_OTLP:
            raise ValueError("raw_otlp applies only to opaque OTLP chunks")
        if not self.config.capture_policy.allows(capture_class):
            raise ValueError("record capture class is disabled by policy")
        return capture_class.value

    def _sanitize_evidence(
        self,
        evidence: Evidence | None,
    ) -> tuple[Evidence | None, list[Omission]]:
        if evidence is None:
            return None, []
        safe, omitted = sanitize_attributes(
            evidence.attributes,
            self.config.capture_policy,
        )
        return (
            evidence.model_copy(
                update={
                    "source": sanitize_provenance_label(evidence.source),
                    "observer": sanitize_provenance_label(evidence.observer),
                    "method": sanitize_provenance_label(evidence.method),
                    "confidence": sanitize_provenance_label(evidence.confidence),
                    "availability": sanitize_provenance_label(evidence.availability),
                    "method_version": sanitize_version_label(evidence.method_version),
                    "source_field": sanitize_source_label(evidence.source_field),
                    "attributes": safe,
                }
            ),
            omitted,
        )

    def _sanitize_links(
        self,
        links: tuple[CausalLink, ...],
    ) -> tuple[tuple[CausalLink, ...], list[Omission]]:
        sanitized: list[CausalLink] = []
        omissions: list[Omission] = []
        for link in links:
            source_attributes = dict(link.attributes)
            for duplicate_key in ("earshot.link.type", "earshot.link.target_scope"):
                if duplicate_key in source_attributes:
                    source_attributes.pop(duplicate_key)
                    omissions.append(
                        Omission(
                            field_key_sha256=hashlib.sha256(duplicate_key.encode()).hexdigest(),
                            capture_class=CaptureClass.METADATA,
                            reason="typed_field_normalized",
                        )
                    )
            safe, omitted = sanitize_attributes(
                source_attributes,
                self.config.capture_policy,
            )
            relationship = sanitize_semantic_label(link.relationship) or "related"
            target_scope = (
                link.target_scope
                if link.target_scope in {"internal", "external", "unknown"}
                else "unknown"
            )
            sanitized.append(
                link.model_copy(
                    update={
                        "relationship": relationship,
                        "target_scope": target_scope,
                        "attributes": safe,
                    }
                )
            )
            omissions.extend(omitted)
        return tuple(sanitized), omissions

    def _sanitize_error(
        self,
        error: ErrorRecord | None,
    ) -> tuple[ErrorRecord | None, list[Omission]]:
        if error is None:
            return None, []
        safe, omissions = sanitize_attributes(
            error.attributes,
            self.config.capture_policy,
        )
        message = error.message
        if message is not None and not self.config.capture_policy.allows(
            CaptureClass.DIAGNOSTIC_PAYLOAD
        ):
            omissions.append(
                Omission(
                    field_key_sha256=hashlib.sha256(b"error.message").hexdigest(),
                    capture_class=CaptureClass.DIAGNOSTIC_PAYLOAD,
                )
            )
            message = None
        inferred = self._capture_class_for(safe)
        if message is not None:
            if inferred not in {
                CaptureClass.METADATA.value,
                CaptureClass.DIAGNOSTIC_PAYLOAD.value,
            }:
                raise ValueError("one error cannot mix multiple sensitive capture classes")
            inferred = CaptureClass.DIAGNOSTIC_PAYLOAD.value
        capture_class = self._resolve_record_class(None, inferred)
        return (
            error.model_copy(
                update={
                    "code": sanitize_error_label(error.code),
                    "category": sanitize_semantic_label(error.category),
                    "message": message,
                    "capture_class": capture_class,
                    "attributes": safe,
                }
            ),
            omissions,
        )

    def _track_retained_classes(self, attributes: Mapping[str, Any]) -> None:
        for key in attributes:
            self._retained_classes.add(classify_attribute(key))
            if (
                self.config.capture_policy.allows(CaptureClass.EXTENSION_PAYLOAD)
                and classify_attribute(key) is CaptureClass.METADATA
                and not is_safe_metadata_key(key)
            ):
                # The extension grant is what authorized this unknown key.
                # Mark it retained so the manifest is internally consistent.
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)

    def __enter__(self) -> IncidentRecorder:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        try:
            self.close("failed" if exc_type is not None else "completed")
        except Exception as error:
            # Instrumentation must never mask or replace application behavior.
            self.last_export_error = error
