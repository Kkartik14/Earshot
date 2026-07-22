"""Pydantic models for the experimental Earshot v1alpha1 incident contract.

The normalized profile is deliberately independent of any one runtime or
transport.  Vocabulary supplied by frameworks and providers is represented by
open strings; only structural primitives such as identifiers and nanosecond
values are constrained here.  Cross-record invariants live in ``validation`` so
they can return stable, language-independent issue codes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

from .versions import CONTRACT_VERSION, SEMANTIC_PROFILE_VERSION

SCHEMA_VERSION = CONTRACT_VERSION

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=256)]
BundleId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,255}$"),
]
UINT64_MAX = (1 << 64) - 1


def _uint64_decimal(value: str) -> str:
    if int(value) > UINT64_MAX:
        raise ValueError("nanosecond value exceeds uint64")
    return value


DecimalNano = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=20),
    AfterValidator(_uint64_decimal),
]


def _nonzero_otel_id(value: str) -> str:
    if not any(character != "0" for character in value):
        raise ValueError("OTLP identifiers must not be all zero")
    return value


TraceId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
    AfterValidator(_nonzero_otel_id),
]
SpanId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{16}$"),
    AfterValidator(_nonzero_otel_id),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SemanticCode = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[a-z][a-z0-9_.-]{0,255}|sha256:[0-9a-f]{64})$"),
]
VersionLabel = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:[a-z][a-z0-9_.-]{0,127}|v?[0-9]+(?:\.[0-9]+)*"
            r"(?:[-+][a-z0-9.-]+)?|sha256:[0-9a-f]{64})$"
        )
    ),
]


def _portable_schema_url(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("schema_url is too long")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate an optional numeric port without requiring one.
    except ValueError as error:
        raise ValueError("schema_url is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("schema_url must be a credential-free HTTPS URL")
    return value


SchemaUrl = Annotated[str, StringConstraints(min_length=1), AfterValidator(_portable_schema_url)]
CaptureClassName = Literal[
    "metadata",
    "extension_payload",
    "transcript",
    "audio",
    "tool_payload",
    "model_payload",
    "diagnostic_payload",
    "identity",
    "raw_otlp",
]
NormalizedCaptureClassName = Literal[
    "metadata",
    "extension_payload",
    "transcript",
    "audio",
    "tool_payload",
    "model_payload",
    "diagnostic_payload",
    "identity",
]


class ContractModel(BaseModel):
    """Forward-compatible base model used by every profile record."""

    model_config = ConfigDict(extra="allow", frozen=True, validate_default=True)


class WireContractModel(ContractModel):
    """Closed outer wire records; forward-compatible extras live in the profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AnalysisContractModel(WireContractModel):
    """Closed metadata-only sidecar records.

    Analysis is derived from already-governed evidence and is persisted outside
    the extensible incident profile. Unknown fields must not become a second
    payload channel.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class Producer(ContractModel):
    name: NonEmptyStr
    version: NonEmptyStr
    language: NonEmptyStr = "python"
    sdk_version: NonEmptyStr | None = None


class Adapter(ContractModel):
    name: NonEmptyStr
    version: NonEmptyStr
    framework: NonEmptyStr
    framework_version: NonEmptyStr | None = None


class BundleManifest(ContractModel):
    schema_version: NonEmptyStr = SCHEMA_VERSION
    semantic_profile_version: NonEmptyStr = SEMANTIC_PROFILE_VERSION
    bundle_id: BundleId
    session_id: OpaqueId
    created_at_unix_nano: DecimalNano
    producer: Producer
    adapters: tuple[Adapter, ...] = ()
    finality: NonEmptyStr = "final"
    completeness: NonEmptyStr = "complete"
    attributes: dict[str, Any] = Field(default_factory=dict)


class TimePoint(ContractModel):
    """A timestamp without pretending distributed clocks are globally ordered."""

    source_time_unix_nano: DecimalNano | None = None
    observed_time_unix_nano: DecimalNano | None = None
    monotonic_time_nano: DecimalNano | None = None
    clock_domain_id: OpaqueId | None = None
    uncertainty_nano: DecimalNano | None = None

    @model_validator(mode="after")
    def has_a_time_value(self) -> TimePoint:
        if (
            self.source_time_unix_nano is None
            and self.observed_time_unix_nano is None
            and self.monotonic_time_nano is None
        ):
            raise ValueError("a time point must contain at least one timestamp")
        if self.monotonic_time_nano is not None and self.clock_domain_id is None:
            raise ValueError("monotonic_time_nano requires clock_domain_id")
        return self


class TimeRange(ContractModel):
    start: TimePoint
    end: TimePoint


class Session(ContractModel):
    session_id: OpaqueId
    status: NonEmptyStr
    started_at: TimePoint
    ended_at: TimePoint | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Participant(ContractModel):
    participant_id: OpaqueId
    session_id: OpaqueId
    role: NonEmptyStr
    endpoint_kind: NonEmptyStr | None = None
    pseudonymous_id: NonEmptyStr | None = None
    capture_class: NormalizedCaptureClassName = "metadata"
    attributes: dict[str, Any] = Field(default_factory=dict)


class AudioFormat(ContractModel):
    encoding: NonEmptyStr
    sample_rate_hz: StrictInt = Field(gt=0)
    channels: StrictInt = Field(gt=0)
    clock_rate_hz: StrictInt | None = Field(default=None, gt=0)


class AudioStream(ContractModel):
    stream_id: OpaqueId
    session_id: OpaqueId
    participant_id: OpaqueId
    direction: NonEmptyStr
    media_kind: NonEmptyStr = "audio"
    format: AudioFormat | None = None
    transport_ref: OpaqueId | None = None
    capture_class: NormalizedCaptureClassName = "metadata"
    attributes: dict[str, Any] = Field(default_factory=dict)


class ClockDomain(ContractModel):
    clock_domain_id: OpaqueId
    kind: NonEmptyStr
    observer: NonEmptyStr
    scope: NonEmptyStr | None = None
    monotonic_origin_nano: DecimalNano | None = None
    wall_origin_unix_nano: DecimalNano | None = None
    uncertainty_nano: DecimalNano | None = None
    synchronization_method: NonEmptyStr | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Evidence(ContractModel):
    source: NonEmptyStr
    observer: NonEmptyStr
    method: NonEmptyStr
    confidence: NonEmptyStr
    availability: NonEmptyStr
    method_version: NonEmptyStr | None = None
    source_field: NonEmptyStr | None = None
    sample_window: TimeRange | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Coverage(ContractModel):
    signal: NonEmptyStr
    availability: NonEmptyStr
    reason: NonEmptyStr | None = None
    evidence: Evidence | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class CausalLink(ContractModel):
    relationship: SemanticCode
    target_scope: Literal["internal", "external", "unknown"] = "unknown"
    target_operation_id: OpaqueId | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identifies_a_target(self) -> CausalLink:
        if self.target_operation_id is None and (self.trace_id is None or self.span_id is None):
            raise ValueError("a causal link requires target_operation_id or trace_id and span_id")
        return self


class ErrorRecord(ContractModel):
    code: NonEmptyStr
    category: NonEmptyStr
    message: str | None = None
    capture_class: NormalizedCaptureClassName = "diagnostic_payload"
    attributes: dict[str, Any] = Field(default_factory=dict)


class Operation(ContractModel):
    operation_id: OpaqueId
    session_id: OpaqueId
    operation_name: NonEmptyStr
    status: NonEmptyStr
    started_at: TimePoint
    ended_at: TimePoint | None = None
    participant_id: OpaqueId | None = None
    stream_id: OpaqueId | None = None
    turn_id: OpaqueId | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    parent_span_id: SpanId | None = None
    parent_scope: Literal["internal", "external", "unknown"] = "unknown"
    links: tuple[CausalLink, ...] = ()
    resource: dict[str, Any] = Field(default_factory=dict)
    resource_schema_url: SchemaUrl | None = None
    instrumentation_scope_name: SemanticCode | None = None
    instrumentation_scope_version: VersionLabel | None = None
    instrumentation_scope_attributes: dict[str, Any] = Field(default_factory=dict)
    schema_url: SchemaUrl | None = None
    evidence: Evidence | None = None
    capture_class: NormalizedCaptureClassName = "metadata"
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: ErrorRecord | None = None

    @model_validator(mode="after")
    def keeps_otel_identity_coherent(self) -> Operation:
        if (self.trace_id is None) != (self.span_id is None):
            raise ValueError("trace_id and span_id must be supplied together")
        if self.parent_span_id is not None and self.trace_id is None:
            raise ValueError("parent_span_id requires trace_id")
        return self


class Event(ContractModel):
    event_id: OpaqueId
    session_id: OpaqueId
    event_name: NonEmptyStr
    time: TimePoint
    operation_id: OpaqueId | None = None
    participant_id: OpaqueId | None = None
    stream_id: OpaqueId | None = None
    turn_id: OpaqueId | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    resource: dict[str, Any] = Field(default_factory=dict)
    resource_schema_url: SchemaUrl | None = None
    instrumentation_scope_name: SemanticCode | None = None
    instrumentation_scope_version: VersionLabel | None = None
    instrumentation_scope_attributes: dict[str, Any] = Field(default_factory=dict)
    schema_url: SchemaUrl | None = None
    evidence: Evidence | None = None
    capture_class: NormalizedCaptureClassName = "metadata"
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keeps_otel_identity_coherent(self) -> Event:
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span_id requires trace_id")
        return self


class QualityMeasurement(ContractModel):
    name: NonEmptyStr
    value: StrictBool | StrictInt | StrictFloat
    unit: NonEmptyStr
    aggregation: SemanticCode = "instant"
    raw_counter: StrictInt | StrictFloat | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class QualitySample(ContractModel):
    sample_id: OpaqueId
    session_id: OpaqueId
    quality_kind: NonEmptyStr
    sample_window: TimeRange
    measurements: tuple[QualityMeasurement, ...]
    evidence: Evidence | None = None
    participant_id: OpaqueId | None = None
    stream_id: OpaqueId | None = None
    resource: dict[str, Any] = Field(default_factory=dict)
    resource_schema_url: SchemaUrl | None = None
    instrumentation_scope_name: SemanticCode | None = None
    instrumentation_scope_version: VersionLabel | None = None
    instrumentation_scope_attributes: dict[str, Any] = Field(default_factory=dict)
    schema_url: SchemaUrl | None = None
    capture_class: NormalizedCaptureClassName = "metadata"
    attributes: dict[str, Any] = Field(default_factory=dict)


class ConsentRecord(ContractModel):
    status: NonEmptyStr
    legal_basis: NonEmptyStr | None = None
    recorded_at_unix_nano: DecimalNano | None = None
    authority: NonEmptyStr | None = None


class RedactionRecord(ContractModel):
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    status: NonEmptyStr
    findings_count: StrictInt | None = Field(default=None, ge=0)
    redacted_count: StrictInt | None = Field(default=None, ge=0)
    executed_at_unix_nano: DecimalNano | None = None


class RetentionPolicy(ContractModel):
    expires_at_unix_nano: DecimalNano | None = None
    ttl_nano: DecimalNano | None = None
    policy_id: NonEmptyStr | None = None


class ExportPolicy(ContractModel):
    allowed: StrictBool
    destinations: tuple[str, ...] = ()
    policy_id: NonEmptyStr | None = None


class CaptureClassPolicy(ContractModel):
    capture_class: CaptureClassName
    decision: NonEmptyStr
    captured: StrictBool
    consent: ConsentRecord | None = None
    redaction: RedactionRecord | None = None
    retention: RetentionPolicy | None = None
    export: ExportPolicy | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Omission(ContractModel):
    omission_id: OpaqueId
    capture_class: CaptureClassName
    reason: SemanticCode
    count: StrictInt | None = Field(default=None, ge=0)
    digest: Sha256 | None = None
    source_refs: tuple[OpaqueId, ...] = ()
    attributes: dict[str, Any] = Field(default_factory=dict)


class PrivacyManifest(ContractModel):
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    default_capture_class: NormalizedCaptureClassName = "metadata"
    capture_classes: tuple[CaptureClassPolicy, ...] = Field(
        default_factory=lambda: (
            CaptureClassPolicy(capture_class="metadata", decision="allow", captured=True),
        )
    )
    omissions: tuple[Omission, ...] = ()
    attributes: dict[str, Any] = Field(default_factory=dict)


class ByteRange(ContractModel):
    offset: StrictInt = Field(ge=0)
    length: StrictInt = Field(gt=0)


class MediaLocator(ContractModel):
    uri: NonEmptyStr
    access: NonEmptyStr = "governed"
    expires_at_unix_nano: DecimalNano | None = None


class MediaRef(ContractModel):
    media_id: OpaqueId
    session_id: OpaqueId
    stream_id: OpaqueId
    media_kind: NonEmptyStr
    content_type: NonEmptyStr
    sha256: Sha256
    size_bytes: StrictInt = Field(ge=0)
    time_range: TimeRange | None = None
    byte_range: ByteRange | None = None
    locator: MediaLocator | None = None
    capture_class: NormalizedCaptureClassName = "audio"
    attributes: dict[str, Any] = Field(default_factory=dict)


class Diagnosis(AnalysisContractModel):
    diagnosis_id: SemanticCode
    code: SemanticCode
    summary: SemanticCode
    confidence: SemanticCode
    evidence_refs: tuple[OpaqueId, ...] = Field(min_length=1)
    limitations: tuple[SemanticCode, ...] = ()


class AnalysisMetric(AnalysisContractModel):
    availability: SemanticCode
    basis: SemanticCode
    confidence: SemanticCode
    value: StrictInt | StrictFloat | None = None
    unit: NonEmptyStr | None = None
    limitation: SemanticCode | None = None
    evidence_ids: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def binds_asserted_values_to_evidence(self) -> AnalysisMetric:
        if self.availability == "available":
            if self.value is None or self.unit is None or not self.evidence_ids:
                raise ValueError(
                    "available analysis metrics require value, unit, and source evidence"
                )
        elif self.value is not None or self.unit is not None:
            raise ValueError("non-available analysis metrics cannot assert a value or unit")
        return self


class ToolAnalysis(AnalysisContractModel):
    operation_count: StrictInt = Field(ge=0)
    total_work_ms: StrictFloat = Field(ge=0)
    elapsed_ms_by_clock_domain: dict[str, StrictFloat] = Field(default_factory=dict)
    evidence_ids: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def binds_counts_to_tool_evidence(self) -> ToolAnalysis:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("tool evidence IDs must be unique")
        if self.operation_count != len(self.evidence_ids):
            raise ValueError("tool operation_count must equal cited operation evidence")
        if self.operation_count == 0 and (
            self.total_work_ms != 0 or self.elapsed_ms_by_clock_domain
        ):
            raise ValueError("empty tool analysis cannot assert elapsed work")
        return self


class TurnMetrics(AnalysisContractModel):
    first_token_latency: AnalysisMetric
    generated_response_latency: AnalysisMetric
    sent_response_latency: AnalysisMetric
    received_response_latency: AnalysisMetric
    render_start_response_latency: AnalysisMetric
    response_latency: AnalysisMetric
    tools: ToolAnalysis
    provider_measurements: dict[str, AnalysisMetric] = Field(default_factory=dict)


class InterruptionProjection(AnalysisContractModel):
    event_name: SemanticCode
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=1)


class TurnProjection(AnalysisContractModel):
    turn_id: OpaqueId
    operation_ids: tuple[OpaqueId, ...] = ()
    event_ids: tuple[OpaqueId, ...] = ()
    metrics: TurnMetrics
    interruptions: tuple[InterruptionProjection, ...] = ()


class AnalysisSummary(AnalysisContractModel):
    turn_count: StrictInt = Field(ge=0)
    operation_count: StrictInt = Field(ge=0)
    event_count: StrictInt = Field(ge=0)
    quality_sample_count: StrictInt = Field(ge=0)
    failed_operation_count: StrictInt = Field(ge=0)


class AnalysisProjections(AnalysisContractModel):
    session_id: OpaqueId | None = None
    turns: tuple[TurnProjection, ...] = ()
    summary: AnalysisSummary | None = None
    limitations: tuple[SemanticCode, ...] = ()
    unassigned_provider_measurements: dict[OpaqueId, dict[str, AnalysisMetric]] = Field(
        default_factory=dict
    )

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json", exclude_none=True)[key]


class DerivedAnalysis(AnalysisContractModel):
    analyzer_name: SemanticCode
    analyzer_version: NonEmptyStr
    input_sha256: Sha256
    generated_at_unix_nano: DecimalNano
    projections: AnalysisProjections = Field(default_factory=AnalysisProjections)
    diagnoses: tuple[Diagnosis, ...] = ()
    capture_class: Literal["metadata"] = "metadata"


class IncidentProfile(ContractModel):
    manifest: BundleManifest
    session: Session
    privacy: PrivacyManifest
    participants: tuple[Participant, ...] = ()
    audio_streams: tuple[AudioStream, ...] = ()
    clock_domains: tuple[ClockDomain, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    operations: tuple[Operation, ...] = ()
    events: tuple[Event, ...] = ()
    quality_samples: tuple[QualitySample, ...] = ()
    media_refs: tuple[MediaRef, ...] = ()
    analysis: DerivedAnalysis | None = Field(
        default=None,
        description=(
            "Reserved for a future profile; v1alpha1 validation requires this to be absent."
        ),
        json_schema_extra={"deprecated": True},
    )
    attributes: dict[str, Any] = Field(default_factory=dict)


class RawOtlpChunk(WireContractModel):
    """Exact bytes from one OTLP export request plus its privacy classification."""

    chunk_id: OpaqueId
    signal: NonEmptyStr
    content_type: NonEmptyStr = "application/x-protobuf"
    compression: NonEmptyStr = "identity"
    payload: bytes = Field(repr=False, min_length=1)
    sha256: Sha256 | None = None
    privacy_class: Literal["raw_otlp"] = "raw_otlp"


class IncidentBundle(WireContractModel):
    profile: IncidentProfile
    raw_otlp_chunks: tuple[RawOtlpChunk, ...] = ()


class JsonRawOtlpChunk(WireContractModel):
    """Base64 form used only by the human-readable JSON contract."""

    chunk_id: OpaqueId
    signal: NonEmptyStr
    content_type: NonEmptyStr = "application/x-protobuf"
    compression: NonEmptyStr = "identity"
    payload_base64: str
    sha256: Sha256
    privacy_class: Literal["raw_otlp"] = "raw_otlp"


class IncidentBundleJson(WireContractModel):
    profile: IncidentProfile
    raw_otlp_chunks: tuple[JsonRawOtlpChunk, ...] = ()
