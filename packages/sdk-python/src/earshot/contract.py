"""Pydantic models for the experimental Earshot v1alpha1 incident contract.

The normalized profile is deliberately independent of any one runtime or
transport.  Vocabulary supplied by frameworks and providers is represented by
open strings; only structural primitives such as identifiers and nanosecond
values are constrained here.  Cross-record invariants live in ``validation`` so
they can return stable, language-independent issue codes.
"""

from __future__ import annotations

import math
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


def _int64_signed(value: str) -> str:
    v = int(value)
    if v > (1 << 63) - 1 or v < -(1 << 63):
        raise ValueError("signed nanosecond value exceeds int64")
    return value


SignedDecimalNano = Annotated[
    str,
    StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)$", max_length=21),
    AfterValidator(_int64_signed),
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
NonNegativeFiniteFloat = Annotated[
    StrictFloat,
    Field(ge=0, allow_inf_nan=False),
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


class RecoveryRecord(ContractModel):
    """How this artifact was reconstructed, and what that costs its completeness.

    A recovered incident has to be structurally unable to pass as a cleanly
    closed one, so the declaration is a typed manifest member rather than an
    attribute. Attribute bags are a weak channel: their keys need a privacy
    allowlist, and validation cannot cross-check an open bag against
    ``finality``, ``completeness``, ``session.status``, and coverage — which is
    exactly what makes the declaration enforceable here.

    There is deliberately no "recovered at" timestamp. Two recoveries of the
    same journal must produce the same bytes under the same ``bundle_id``, or
    content-addressed ingest would reject the second as a conflict. When
    recovery ran is an operational fact for the CLI and the diagnostic channel,
    not evidence.
    """

    method: SemanticCode
    reason: SemanticCode
    close_observed: StrictBool
    journal_id: OpaqueId
    last_sequence: StrictInt = Field(ge=0)
    # The last coordinate the journal durably observed. This is *not* the end of
    # the session: the session may have run on for a long time after it.
    last_observation: TimePoint | None = None
    torn_tail_bytes: StrictInt = Field(default=0, ge=0)
    discarded_records: StrictInt = Field(default=0, ge=0)
    journal_complete: StrictBool = True
    recoverer: Producer
    attributes: dict[str, Any] = Field(default_factory=dict)


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
    recovery: RecoveryRecord | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


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


class ClockRelation(ContractModel):
    """A declared calibration mapping between two clock domains.

    ``offset_nano`` converts a ``from``-domain wall timestamp into the ``to``
    domain: ``to_wall = from_wall + offset_nano`` (plus optional drift). ``drift_ppm``
    is an optional linear parts-per-million rate anchored at ``reference_unix_nano``,
    so the total correction at wall time ``t`` is
    ``offset_nano + drift_ppm * (t - reference_unix_nano) / 1e6`` nanoseconds.
    ``uncertainty_nano`` is the calibration's own error bound and is propagated into
    any cross-domain latency derived through this relation. ``valid_from_unix_nano``
    and ``valid_to_unix_nano`` bound the wall-time window (in the ``from`` domain)
    where the calibration is trustworthy; timestamps outside it are not aligned.
    """

    relation_id: OpaqueId
    from_clock_domain_id: OpaqueId
    to_clock_domain_id: OpaqueId
    offset_nano: SignedDecimalNano
    drift_ppm: StrictFloat | None = None
    uncertainty_nano: DecimalNano | None = None
    method: SemanticCode
    reference_unix_nano: DecimalNano | None = None
    valid_from_unix_nano: DecimalNano | None = None
    valid_to_unix_nano: DecimalNano | None = None
    evidence: Evidence | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keeps_calibration_coherent(self) -> ClockRelation:
        if self.from_clock_domain_id == self.to_clock_domain_id:
            raise ValueError("a clock relation must map between two different domains")
        if (
            self.valid_from_unix_nano is not None
            and self.valid_to_unix_nano is not None
            and int(self.valid_to_unix_nano) < int(self.valid_from_unix_nano)
        ):
            raise ValueError("clock relation validity window ends before it begins")
        if self.drift_ppm is not None and not math.isfinite(self.drift_ppm):
            # A NaN/inf drift rate has no affine meaning and would poison every
            # cross-clock alignment it touches; a rate must be a finite number.
            raise ValueError("clock relation drift_ppm must be finite")
        if (
            self.drift_ppm is not None
            and self.drift_ppm != 0.0
            and self.reference_unix_nano is None
        ):
            # Drift is a rate about an anchor instant. Without a reference the
            # correction ``drift_ppm * (t - reference)`` is undefined, so a
            # non-zero drift requires the reference it is measured from.
            raise ValueError("clock relation drift_ppm requires reference_unix_nano")
        return self


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
    """A reference to media somebody else holds — never the media itself.

    Earshot stores custody, not content: who holds the bytes, what they are,
    which window of the session they cover, under what consent and retention,
    and which clock domain their own timeline runs on. It never ingests,
    fetches, caches, or proxies them.

    ``integrity`` is the honesty discriminator that makes that distinction
    legible instead of overloading a null:

    ``content_digest``
        Somebody measured these bytes and declared a ``sha256`` and
        ``size_bytes`` for them. The digest is a *declaration carried by the
        artifact*, not an earshot verification — earshot still never read the
        bytes — but it is a checkable commitment a holder can be held to.
    ``opaque_handle``
        Nobody measured the bytes on this path, so the reference carries no
        digest and no size and names the ``custodian`` who does hold them.
        ``byte_range`` is meaningless here: you cannot range into bytes whose
        length was never observed.

    Making ``sha256``/``size_bytes`` optional is what the real custody case
    requires. The alternative — keeping them required and letting a producer
    fill them with something it did not compute — is exactly the dishonesty
    this contract exists to prevent. The coherence rule is enforced by
    :func:`media_custody_incoherence` at every boundary, not by convention.
    """

    media_id: OpaqueId
    session_id: OpaqueId
    stream_id: OpaqueId
    media_kind: NonEmptyStr
    content_type: NonEmptyStr
    integrity: Literal["content_digest", "opaque_handle"] = "content_digest"
    sha256: Sha256 | None = None
    size_bytes: StrictInt | None = Field(default=None, ge=0)
    # Where the bytes actually live. Required for an opaque handle: a reference
    # earshot cannot attest to is worthless unless it names who can.
    custodian: SemanticCode | None = None
    # The media file's own timeline, as an ordinary clock domain. Aligning it to
    # the session reuses ``ClockRelation`` rather than inventing a second,
    # parallel synchronization model with its own uncertainty semantics.
    clock_domain_id: OpaqueId | None = None
    consent: ConsentRecord | None = None
    retention: RetentionPolicy | None = None
    time_range: TimeRange | None = None
    byte_range: ByteRange | None = None
    locator: MediaLocator | None = None
    capture_class: NormalizedCaptureClassName = "audio"
    attributes: dict[str, Any] = Field(default_factory=dict)


def media_custody_incoherence(media: MediaRef) -> str | None:
    """Return why this custody claim contradicts itself, or ``None``.

    One implementation of the rule, used by the recorder (which refuses the
    record at admission) and by ``validation`` (which refuses the artifact at
    every boundary with a stable code). Two copies of an honesty rule are two
    chances for them to disagree, and a disagreement here would let an
    unverifiable reference pass as a verified one.
    """

    if media.integrity == "content_digest":
        if media.sha256 is None or media.size_bytes is None:
            return "a content_digest media reference must carry sha256 and size_bytes"
        return None
    if media.sha256 is not None or media.size_bytes is not None:
        return "an opaque_handle media reference cannot assert a digest or a size"
    if media.custodian is None:
        return "an opaque_handle media reference must name the custodian holding the bytes"
    if media.byte_range is not None:
        return "an opaque_handle media reference cannot range into unmeasured bytes"
    return None


def media_declares_custody_extensions(media: MediaRef) -> bool:
    """Report whether this reference uses a member the 0.1.0 contract lacked.

    A 0.1.0 ``MediaRef`` could only be a digest-and-size reference. An artifact
    claiming 0.1.0 while using an opaque handle, a custodian, a media clock
    domain, consent, or retention is asserting a contract it cannot express —
    the same failure ``manifest.recovery`` has at 0.1.0.
    """

    return (
        media.integrity != "content_digest"
        or media.sha256 is None
        or media.size_bytes is None
        or media.custodian is not None
        or media.clock_domain_id is not None
        or media.consent is not None
        or media.retention is not None
    )


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
    timed_operation_count: StrictInt = Field(default=0, ge=0)
    untimed_operation_count: StrictInt = Field(default=0, ge=0)
    total_work_ms: NonNegativeFiniteFloat
    total_work_completeness: Literal["complete", "partial", "unavailable"] = "complete"
    limitation: SemanticCode | None = None
    elapsed_ms_by_clock_domain: dict[
        OpaqueId,
        dict[Literal["monotonic", "source_wall"], NonNegativeFiniteFloat],
    ] = Field(default_factory=dict)
    evidence_ids: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def binds_counts_to_tool_evidence(self) -> ToolAnalysis:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("tool evidence IDs must be unique")
        if self.operation_count != len(self.evidence_ids):
            raise ValueError("tool operation_count must equal cited operation evidence")
        duration_count = self.timed_operation_count + self.untimed_operation_count
        if duration_count not in {0, self.operation_count}:
            raise ValueError("tool duration counts must cover every cited operation")
        if self.operation_count == 0 and (
            self.total_work_ms != 0
            or self.elapsed_ms_by_clock_domain
            or self.timed_operation_count != 0
            or self.untimed_operation_count != 0
        ):
            raise ValueError("empty tool analysis cannot assert elapsed work")
        if any(
            not elapsed_by_basis for elapsed_by_basis in self.elapsed_ms_by_clock_domain.values()
        ):
            raise ValueError("tool elapsed-time basis maps cannot be empty")
        if self.total_work_completeness == "complete":
            if self.untimed_operation_count or self.limitation is not None:
                raise ValueError("complete tool work cannot carry missing intervals")
        elif self.total_work_completeness == "partial":
            if (
                self.timed_operation_count == 0
                or self.untimed_operation_count == 0
                or self.limitation is None
            ):
                raise ValueError("partial tool work requires known and unknown intervals")
        elif (
            self.timed_operation_count != 0
            or self.untimed_operation_count == 0
            or self.limitation is None
        ):
            raise ValueError("unavailable tool work requires only unknown intervals")
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


class InterruptionStage(AnalysisContractModel):
    """One canonical stage of a barge-in teardown, observed or not.

    An observed stage cites a real event/operation/sample and carries the exact
    coordinate that evidence recorded (never a synthesized timestamp). A stage the
    artifact does not contain is reported as ``observed=False`` with a
    ``coverage_reason`` and no coordinate, so absence is coverage, not fabrication.
    ``outcome`` carries the disposition of the ``tool_outcome`` stage (the tool's
    ok/error/timeout/cancelled status) and stays ``None`` for every other stage.
    """

    stage: SemanticCode
    observed: StrictBool
    at_nano: DecimalNano | None = None
    clock_domain_id: OpaqueId | None = None
    time_basis: SemanticCode | None = None
    evidence_id: OpaqueId | None = None
    coverage_reason: SemanticCode | None = None
    outcome: SemanticCode | None = None

    @model_validator(mode="after")
    def keeps_observation_coherent(self) -> InterruptionStage:
        if self.observed:
            if self.evidence_id is None:
                raise ValueError("an observed interruption stage must cite evidence")
            if self.coverage_reason is not None:
                raise ValueError("an observed interruption stage cannot carry a coverage reason")
        else:
            if any(
                value is not None
                for value in (
                    self.at_nano,
                    self.clock_domain_id,
                    self.time_basis,
                    self.evidence_id,
                    self.outcome,
                )
            ):
                raise ValueError(
                    "an unobserved interruption stage cannot assert a coordinate, "
                    "evidence, or outcome"
                )
            if self.coverage_reason is None:
                raise ValueError("an unobserved interruption stage requires a coverage reason")
        return self


class InterruptionChainProjection(AnalysisContractModel):
    """The ordered causal chain a single turn's interruption produced.

    Every stage in the canonical vocabulary is present exactly once, marked
    observed or not. ``effectiveness`` is the barge-in latency from the observed
    overlap to the observed render stop, computed only when both endpoints are
    comparable (same clock, or a declared calibration aligns them); otherwise it
    honestly asserts no value.
    """

    turn_id: OpaqueId
    classification: Literal["accepted", "ignored", "false", "unknown"]
    stages: tuple[InterruptionStage, ...] = Field(min_length=1)
    effectiveness: AnalysisMetric


class TurnProjection(AnalysisContractModel):
    turn_id: OpaqueId
    operation_ids: tuple[OpaqueId, ...] = ()
    event_ids: tuple[OpaqueId, ...] = ()
    metrics: TurnMetrics
    interruptions: tuple[InterruptionProjection, ...] = ()
    interruption_chains: tuple[InterruptionChainProjection, ...] = ()


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
    clock_relations: tuple[ClockRelation, ...] = ()
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
