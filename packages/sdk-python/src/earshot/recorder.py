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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar, overload

from pydantic import BaseModel

from .checkpoint.writer import CheckpointStatus, NullCheckpointWriter, RecordMutation
from .clock import Clock, SystemClock
from .codec import MAX_PROFILE_DEPTH
from .context import _operation_scope, current_context
from .contract import (
    Adapter,
    AudioStream,
    BundleManifest,
    CaptureClassPolicy,
    CausalLink,
    ClockDomain,
    ClockRelation,
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
    RecoveryRecord,
    RedactionRecord,
    RetentionPolicy,
    Session,
    TimePoint,
    media_custody_incoherence,
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
from .versions import PACKAGE_VERSION

if TYPE_CHECKING:
    from .checkpoint.writer import CheckpointWriter

    CheckpointWriterLike = CheckpointWriter | NullCheckpointWriter
else:  # pragma: no cover - the alias exists only for annotations
    CheckpointWriterLike = object

_ModelT = TypeVar("_ModelT", bound=BaseModel)

DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_RAW_OTLP_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_VALUE_BYTES = 64 * 1024
# Defensive bounds on declared clock metadata (one browser domain + a handful of
# calibrations per session is normal; these only guard against pathological input).
_MAX_EXTRA_CLOCK_DOMAINS = 16
_MAX_CLOCK_RELATIONS = 64
_MAX_COUNTER = 9_223_372_036_854_775_807
_RECORD_KINDS = (
    "adapter",
    "coverage",
    "event",
    "media",
    "omission",
    "operation",
    "participant",
    "quality_sample",
    "raw_otlp",
    "stream",
)


def _utf8_size_up_to(value: str, limit: int) -> tuple[int, bool]:
    """Return UTF-8 size without allocating an encoded copy, stopping at ``limit``."""

    total = 0
    for character in value:
        codepoint = ord(character)
        total += (
            1 if codepoint < 0x80 else 2 if codepoint < 0x800 else 3 if codepoint < 0x10000 else 4
        )
        if total > limit:
            return limit + 1, True
    return total, False


def _structural_size_up_to(
    value: Any,
    limit: int,
    *,
    reject_cycles: bool = False,
) -> tuple[int, bool]:
    """Deterministic logical size with bounded traversal and cycle detection.

    Each value/node contributes one byte in addition to scalar content. The estimate
    intentionally models a portable structure rather than CPython heap internals.
    It stops as soon as ``limit`` is exceeded, so hostile strings and collections are
    never copied or walked in full merely to reject them.
    """

    active: set[int] = set()

    def visit(candidate: Any, remaining: int, depth: int) -> tuple[int, bool]:
        if remaining < 1 or depth > MAX_PROFILE_DEPTH:
            return max(0, remaining) + 1, True
        if candidate is None or isinstance(candidate, (bool, int, float)):
            return 1, False
        if isinstance(candidate, str):
            content, exceeded = _utf8_size_up_to(candidate, remaining - 1)
            size = 1 + content
            return (remaining + 1, True) if exceeded or size > remaining else (size, False)
        if isinstance(candidate, (bytes, bytearray, memoryview)):
            size = 1 + len(candidate)
            return (remaining + 1, True) if size > remaining else (size, False)
        if isinstance(candidate, BaseModel):
            candidate = candidate.__dict__
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in active:
                if reject_cycles:
                    raise ValueError("captured value contains an unsafe key or value")
                return remaining + 1, True
            active.add(identity)
            total = 1
            try:
                for key, child in candidate.items():
                    key_size, key_exceeded = visit(key, remaining - total, depth + 1)
                    if key_exceeded:
                        return remaining + 1, True
                    total += key_size
                    child_size, child_exceeded = visit(child, remaining - total, depth + 1)
                    if child_exceeded:
                        return remaining + 1, True
                    total += child_size
            finally:
                active.remove(identity)
            return total, False
        if isinstance(candidate, (list, tuple)):
            identity = id(candidate)
            if identity in active:
                if reject_cycles:
                    raise ValueError("captured value contains an unsafe key or value")
                return remaining + 1, True
            active.add(identity)
            total = 1
            try:
                for child in candidate:
                    child_size, exceeded = visit(child, remaining - total, depth + 1)
                    if exceeded:
                        return remaining + 1, True
                    total += child_size
            finally:
                active.remove(identity)
            return total, False
        # Unknown objects are not copied during capacity preflight. Their existing
        # portability/privacy validation remains authoritative if they are admitted.
        return 1, False

    return visit(value, limit, 0)


@dataclass(frozen=True)
class RecorderConfig:
    producer_name: str = "earshot"
    producer_version: str = PACKAGE_VERSION
    clock_domain_id: str | None = None
    capture_policy: CapturePolicy = field(default_factory=CapturePolicy.metadata_only)
    adapters: tuple[Adapter, ...] = ()
    max_records: int = DEFAULT_MAX_RECORDS
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES
    max_raw_otlp_bytes: int = DEFAULT_MAX_RAW_OTLP_BYTES
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES


@dataclass(frozen=True)
class RecorderStatus:
    state: str
    truncated: bool
    admission_frozen: bool
    captured_records: int
    captured_bytes: int
    raw_otlp_bytes: int
    truncated_records: int
    estimated_omitted_bytes: int
    first_limit_reason: str | None
    omitted_records_by_kind: tuple[tuple[str, int], ...]
    omitted_records_by_capture_class: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RecorderSnapshot:
    """Everything an incident profile is built from, detached from a recorder.

    ``close()`` fills this in from live recorder state; the checkpoint assembler
    fills the same shape in by replaying a journal. One shape and one builder is
    the whole reason a recovered artifact cannot drift from a closed one.
    """

    producer_name: str
    producer_version: str
    bundle_id: str
    session_id: str
    clock_domain_id: str
    started_wall: int
    started_mono: int
    capture_policy: CapturePolicy
    adapters: tuple[Adapter, ...]
    status: str
    status_attributes: dict[str, str]
    ended_at: TimePoint | None
    participants: tuple[Participant, ...] = ()
    audio_streams: tuple[AudioStream, ...] = ()
    extra_clock_domains: tuple[ClockDomain, ...] = ()
    clock_relations: tuple[ClockRelation, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    operations: tuple[Operation, ...] = ()
    events: tuple[Event, ...] = ()
    quality_samples: tuple[QualitySample, ...] = ()
    media_refs: tuple[MediaRef, ...] = ()
    omissions: tuple[Omission, ...] = ()
    retained_classes: frozenset[CaptureClass] = frozenset({CaptureClass.METADATA})
    first_limit_reason: str | None = None
    omitted_records_by_class: tuple[tuple[str, int], ...] = ()
    # Operations whose start was durably observed and whose end never was. Only
    # a recovery can produce these, and only a recovery reports the coverage.
    unfinished_operations: int = 0


# Coverage signals a recovered artifact is authoritative about. They are
# superseded rather than merged so a caller-recorded signal of the same name
# cannot contradict what the journal actually shows.
RECOVERY_COVERAGE_SIGNALS = (
    "recorder.session_close",
    "recorder.checkpoint_journal",
    "recorder.operation_completion",
)


def build_incident_profile(
    state: RecorderSnapshot,
    *,
    recovery: RecoveryRecord | None = None,
) -> IncidentProfile:
    """Project recorder state into an incident profile.

    ``recovery`` is present only when this artifact was reconstructed from a
    checkpoint journal rather than produced by a live ``close()``. When it says
    the close was never observed, the artifact declares itself provisional and
    incomplete here, and validation refuses any bundle whose manifest, session,
    and coverage disagree with that declaration.
    """

    provisional = recovery is not None and not recovery.close_observed
    omissions: list[ContractOmission] = [
        ContractOmission(
            omission_id=f"omission-{index}",
            capture_class=item.capture_class.value,
            reason=item.reason,
            count=1,
            attributes={"field_key_sha256": item.field_key_sha256},
        )
        for index, item in enumerate(state.omissions)
    ]
    for capture_class, count in state.omitted_records_by_class:
        if not count:
            continue
        omissions.append(
            ContractOmission(
                omission_id=f"omission-{len(omissions)}",
                capture_class=capture_class,
                reason="recorder_capture_truncated",
                count=count,
            )
        )
    if recovery is not None and recovery.journal_id is not None:
        # Absence is coverage: evidence lost at the end of a torn journal, or
        # past its cap, is ledgered rather than silently missing. Only a journal
        # replay can suffer these; a journal-less reconstruction never does.
        if recovery.torn_tail_bytes > 0:
            omissions.append(
                ContractOmission(
                    omission_id=f"omission-{len(omissions)}",
                    capture_class=CaptureClass.METADATA.value,
                    reason="checkpoint_torn_tail",
                    count=1,
                )
            )
        if not recovery.journal_complete:
            omissions.append(
                ContractOmission(
                    omission_id=f"omission-{len(omissions)}",
                    capture_class=CaptureClass.METADATA.value,
                    reason="checkpoint_journal_full",
                    count=1,
                )
            )

    capture_classes = tuple(
        CaptureClassPolicy(
            capture_class=capture_class.value,
            decision=("allow" if state.capture_policy.allows(capture_class) else "deny"),
            captured=capture_class in state.retained_classes,
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
        for governance in (state.capture_policy.governance.get(capture_class),)
    )

    # A recovered artifact is authoritative about these signals, so it supersedes
    # anything the recorder was told about them; a clean close leaves them alone.
    reserved_signals = {"recorder.capture"}
    if recovery is not None:
        reserved_signals.update(RECOVERY_COVERAGE_SIGNALS)
    profile_coverage = tuple(item for item in state.coverage if item.signal not in reserved_signals)
    if state.first_limit_reason is not None:
        profile_coverage += (
            Coverage(
                signal="recorder.capture",
                availability="partial",
                reason=state.first_limit_reason,
            ),
        )
    if recovery is not None:
        # Whether the close was observed is universal to every reconstruction, so
        # this signal is always emitted; it is the coverage the validator keys on.
        profile_coverage += (
            Coverage(
                signal="recorder.session_close",
                availability="available" if recovery.close_observed else "unavailable",
                reason=None if recovery.close_observed else recovery.reason,
            ),
        )
        # The journal's own health is only meaningful when a journal existed. A
        # journal-less reconstruction (e.g. a browser capture batch) omits it
        # rather than claiming an "available" journal it never had.
        if recovery.journal_id is not None:
            profile_coverage += (
                Coverage(
                    signal="recorder.checkpoint_journal",
                    availability=(
                        "partial"
                        if recovery.torn_tail_bytes > 0 or not recovery.journal_complete
                        else "available"
                    ),
                    reason=(
                        "torn_tail"
                        if recovery.torn_tail_bytes > 0
                        else "journal_full"
                        if not recovery.journal_complete
                        else None
                    ),
                ),
            )
        if state.unfinished_operations:
            profile_coverage += (
                Coverage(
                    signal="recorder.operation_completion",
                    availability="partial",
                    reason="process_terminated_mid_operation",
                ),
            )

    return IncidentProfile(
        manifest=BundleManifest(
            bundle_id=state.bundle_id,
            session_id=state.session_id,
            created_at_unix_nano=str(state.started_wall),
            producer=Producer(
                name=state.producer_name,
                version=state.producer_version,
                sdk_version=state.producer_version,
            ),
            adapters=state.adapters,
            finality="provisional" if provisional else "final",
            completeness=(
                "complete"
                if not provisional
                and state.status == "completed"
                and state.first_limit_reason is None
                else "incomplete"
            ),
            recovery=recovery,
        ),
        session=Session(
            session_id=state.session_id,
            status=state.status,
            started_at=TimePoint(
                source_time_unix_nano=str(state.started_wall),
                monotonic_time_nano="0",
                clock_domain_id=state.clock_domain_id,
            ),
            ended_at=state.ended_at,
            attributes=state.status_attributes,
        ),
        privacy=PrivacyManifest(
            policy_id=state.capture_policy.policy_id,
            policy_version=state.capture_policy.policy_version,
            capture_classes=capture_classes,
            omissions=tuple(omissions),
        ),
        participants=state.participants,
        audio_streams=state.audio_streams,
        clock_domains=(
            ClockDomain(
                clock_domain_id=state.clock_domain_id,
                kind="process_monotonic",
                observer="earshot.sdk",
                monotonic_origin_nano=str(state.started_mono),
                wall_origin_unix_nano=str(state.started_wall),
                uncertainty_nano="0",
                synchronization_method="same_process_sample",
            ),
            *state.extra_clock_domains,
        ),
        clock_relations=state.clock_relations,
        coverage=profile_coverage,
        operations=state.operations,
        events=state.events,
        quality_samples=state.quality_samples,
        media_refs=state.media_refs,
    )


class IncidentRecorder:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        bundle_id: str | None = None,
        config: RecorderConfig | None = None,
        clock: Clock | None = None,
        exporter: BoundedAsyncExporter | None = None,
        on_close: Callable[[], None] | None = None,
        on_status: Callable[[RecorderStatus], None] | None = None,
        diagnostic: Callable[[Any], None] | None = None,
        checkpoint: CheckpointWriterLike | None = None,
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
            max_records=source_config.max_records,
            max_capture_bytes=source_config.max_capture_bytes,
            max_raw_otlp_bytes=source_config.max_raw_otlp_bytes,
            max_value_bytes=source_config.max_value_bytes,
        )
        self._validate_limit_config()
        self._validate_capture_policy_config()
        self.clock = clock or SystemClock()
        self.session_id = session_id or f"session-{uuid.uuid4().hex}"
        self.bundle_id = bundle_id or f"bundle-{uuid.uuid4().hex}"
        self.clock_domain_id = self.config.clock_domain_id or f"process-{uuid.uuid4().hex}"
        self._manual_trace_id = secrets.token_hex(16)
        self.exporter = exporter
        self._on_close = on_close
        self._on_status = on_status
        self._diagnostic = diagnostic
        # ``NullCheckpointWriter`` keeps every admission path branch-free when
        # checkpointing is not configured, which is the default.
        self._journal = checkpoint if checkpoint is not None else NullCheckpointWriter()
        self._journaled_omissions = 0
        # Metadata is retained by construction, so it is never worth a frame.
        self._journaled_classes: set[CaptureClass] = {CaptureClass.METADATA}
        self._close_notified = False
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
        # Additional clock domains (e.g. a browser's monotonic clock) and any
        # declared calibrations relating them to the server clock. These are
        # small governed metadata; deduped by id and bounded below.
        self._extra_clock_domains: list[ClockDomain] = []
        self._clock_relations: list[ClockRelation] = []
        self._retained_classes: set[CaptureClass] = {CaptureClass.METADATA}
        self._status = "running"
        self._closed = False
        self._bundle: IncidentBundle | None = None
        self._close_error: Exception | None = None
        self.last_export_error: Exception | None = None
        self.export_accepted: bool | None = None
        self._lock = threading.RLock()
        self._captured_records = 0
        self._captured_bytes = 0
        self._raw_otlp_bytes = 0
        self._truncated_records = 0
        self._estimated_omitted_bytes = 0
        self._first_limit_reason: str | None = None
        self._admission_frozen = False
        self._omitted_records_by_kind = dict.fromkeys(_RECORD_KINDS, 0)
        self._omitted_records_by_class = dict.fromkeys(
            (capture_class.value for capture_class in CaptureClass), 0
        )
        self._pending_truncation_diagnostic = False
        self._truncation_diagnostic_emitted = False
        # Open the journal before the first admission so constructor adapters and
        # any limit they trip are journaled like every other fact.
        self._journal.open_journal(
            producer_name=self.config.producer_name,
            producer_version=self.config.producer_version,
            bundle_id=self.bundle_id,
            session_id=self.session_id,
            clock_domain_id=self.clock_domain_id,
            started_wall=self._started_wall,
            started_mono=self._started_mono,
            manual_trace_id=self._manual_trace_id,
            capture_policy=self.config.capture_policy,
            max_records=self.config.max_records,
            max_capture_bytes=self.config.max_capture_bytes,
            max_raw_otlp_bytes=self.config.max_raw_otlp_bytes,
            max_value_bytes=self.config.max_value_bytes,
        )
        initial_adapter_extensions: list[bool] = []
        for configured_adapter in self.config.adapters:
            adapter, has_extensions = self._prepare_model(configured_adapter, kind="adapter")
            self._assert_profile_record_depth(adapter, root_depth=4)
            estimated, _ = _structural_size_up_to(adapter, self.config.max_capture_bytes)
            with self._lock:
                if not self._try_admit_locked("adapter", CaptureClass.METADATA, estimated):
                    break
                self._adapters.append(adapter)
                self._journal_locked("adapter", adapter)
            initial_adapter_extensions.append(has_extensions)
        if any(initial_adapter_extensions):
            with self._lock:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                self._journal_locked("policy")
        self.config = RecorderConfig(
            producer_name=self.config.producer_name,
            producer_version=self.config.producer_version,
            clock_domain_id=self.config.clock_domain_id,
            capture_policy=self.config.capture_policy,
            adapters=tuple(self._adapters),
            max_records=self.config.max_records,
            max_capture_bytes=self.config.max_capture_bytes,
            max_raw_otlp_bytes=self.config.max_raw_otlp_bytes,
            max_value_bytes=self.config.max_value_bytes,
        )

    def _time(self) -> TimePoint:
        return TimePoint(
            source_time_unix_nano=str(self.clock.unix_nano()),
            monotonic_time_nano=str(self.clock.monotonic_nano() - self._started_mono),
            clock_domain_id=self.clock_domain_id,
        )

    def status(self) -> RecorderStatus:
        """Return the authoritative, non-secret in-process capture outcome."""

        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> RecorderStatus:
        return RecorderStatus(
            state="closed" if self._closed else self._status,
            truncated=self._first_limit_reason is not None,
            admission_frozen=self._admission_frozen,
            captured_records=self._captured_records,
            captured_bytes=self._captured_bytes,
            raw_otlp_bytes=self._raw_otlp_bytes,
            truncated_records=self._truncated_records,
            estimated_omitted_bytes=self._estimated_omitted_bytes,
            first_limit_reason=self._first_limit_reason,
            omitted_records_by_kind=tuple(
                (kind, count) for kind, count in self._omitted_records_by_kind.items() if count
            ),
            omitted_records_by_capture_class=tuple(
                (capture_class, count)
                for capture_class, count in self._omitted_records_by_class.items()
                if count
            ),
        )

    def _validate_limit_config(self) -> None:
        for name in (
            "max_records",
            "max_capture_bytes",
            "max_raw_otlp_bytes",
            "max_value_bytes",
        ):
            value = getattr(self.config, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _saturating_add(left: int, right: int) -> int:
        return min(_MAX_COUNTER, left + max(0, right))

    def _note_omission_locked(
        self,
        *,
        reason: str,
        kind: str,
        capture_class: CaptureClass,
        estimated_bytes: int,
        whole_record: bool,
        freeze: bool,
    ) -> None:
        if self._first_limit_reason is None:
            self._first_limit_reason = reason
            self._pending_truncation_diagnostic = True
        if freeze:
            self._admission_frozen = True
        if whole_record:
            self._truncated_records = self._saturating_add(self._truncated_records, 1)
            self._omitted_records_by_kind[kind] = self._saturating_add(
                self._omitted_records_by_kind[kind], 1
            )
        self._omitted_records_by_class[capture_class.value] = self._saturating_add(
            self._omitted_records_by_class[capture_class.value], 1
        )
        self._estimated_omitted_bytes = self._saturating_add(
            self._estimated_omitted_bytes, estimated_bytes
        )
        if self._journal.enabled:
            # Journal the mutation, not the aggregate: replaying these calls
            # re-derives every omission counter without a second implementation.
            self._journal.append_limit(
                reason=reason,
                kind=kind,
                capture_class=capture_class,
                estimated_bytes=estimated_bytes,
                whole_record=whole_record,
                freeze=freeze,
            )

    def _try_admit_locked(
        self,
        kind: str,
        capture_class: CaptureClass,
        estimated_bytes: int,
        *,
        raw_bytes: int = 0,
    ) -> bool:
        if self._admission_frozen:
            self._note_omission_locked(
                reason=self._first_limit_reason or "max_capture_bytes",
                kind=kind,
                capture_class=capture_class,
                estimated_bytes=estimated_bytes,
                whole_record=True,
                freeze=True,
            )
            return False
        if raw_bytes and self._raw_otlp_bytes + raw_bytes > self.config.max_raw_otlp_bytes:
            self._note_omission_locked(
                reason="max_raw_otlp_bytes",
                kind=kind,
                capture_class=capture_class,
                estimated_bytes=raw_bytes,
                whole_record=True,
                freeze=True,
            )
            return False
        if self._captured_records >= self.config.max_records:
            self._note_omission_locked(
                reason="max_records",
                kind=kind,
                capture_class=capture_class,
                estimated_bytes=estimated_bytes,
                whole_record=True,
                freeze=True,
            )
            return False
        if self._captured_bytes + estimated_bytes > self.config.max_capture_bytes:
            self._note_omission_locked(
                reason="max_capture_bytes",
                kind=kind,
                capture_class=capture_class,
                estimated_bytes=estimated_bytes,
                whole_record=True,
                freeze=True,
            )
            return False
        self._captured_records += 1
        self._captured_bytes += estimated_bytes
        self._raw_otlp_bytes += raw_bytes
        return True

    def _emit_pending_truncation_diagnostic(self) -> None:
        with self._lock:
            if not self._pending_truncation_diagnostic or self._truncation_diagnostic_emitted:
                return
            self._pending_truncation_diagnostic = False
            self._truncation_diagnostic_emitted = True
            diagnostic = self._diagnostic
        if diagnostic is None:
            return
        from .exporter import ExportDiagnostic

        with contextlib.suppress(Exception):
            diagnostic(ExportDiagnostic("recorder.capture_truncated", self.bundle_id))

    def _bounded_attributes(
        self,
        kind: str,
        attributes: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Prefix-copy only values whose bounded structural estimate fits."""

        if not attributes:
            return {}
        bounded: dict[str, Any] = {}
        aggregate = 0
        for key, value in attributes.items():
            estimate, exceeded = _structural_size_up_to(
                value,
                self.config.max_value_bytes,
                reject_cycles=True,
            )
            capture_class = (
                classify_attribute(key) if isinstance(key, str) else CaptureClass.METADATA
            )
            if exceeded or aggregate + estimate > self.config.max_value_bytes:
                with self._lock:
                    self._require_open()
                    self._note_omission_locked(
                        reason="max_value_bytes",
                        kind=kind,
                        capture_class=capture_class,
                        estimated_bytes=estimate,
                        whole_record=False,
                        freeze=False,
                    )
                if aggregate + estimate > self.config.max_value_bytes:
                    break
                continue
            bounded[key] = value
            aggregate += estimate
        return bounded

    def _capture_size(
        self,
        record: Any,
        *omission_groups: list[Omission],
    ) -> int:
        """Include retained privacy-ledger cost in the same total byte budget."""

        value = (
            record,
            tuple(omission.as_dict() for group in omission_groups for omission in group),
        )
        estimated, _ = _structural_size_up_to(value, self.config.max_capture_bytes)
        return estimated

    def add_participant(
        self,
        participant_id: str,
        *,
        role: str,
        endpoint_kind: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        capture_class: str | None = None,
    ) -> Participant:
        safe, omitted = sanitize_attributes(
            self._bounded_attributes("participant", attributes), self.config.capture_policy
        )
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
        estimated = self._capture_size(participant, omitted)
        result = participant
        with self._lock:
            self._require_open()
            for existing in self._participants:
                if existing.participant_id != participant.participant_id:
                    continue
                if existing != participant:
                    raise ValueError("conflicting duplicate participant identity")
                result = existing.model_copy(deep=True)
                break
            else:
                admitted = self._try_admit_locked(
                    "participant", CaptureClass(record_class), estimated
                )
                if admitted:
                    self._participants.append(participant.model_copy(deep=True))
                    self._omissions.extend(omitted)
                    self._track_retained_classes(safe)
                    self._retained_classes.add(CaptureClass(record_class))
                    self._journal_locked("participant", participant)
        self._emit_pending_truncation_diagnostic()
        return result

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
        safe, omitted = sanitize_attributes(
            self._bounded_attributes("stream", attributes), self.config.capture_policy
        )
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
        estimated = self._capture_size(stream, omitted)
        result = stream
        with self._lock:
            self._require_open()
            for existing in self._streams:
                if existing.stream_id != stream.stream_id:
                    continue
                if existing != stream:
                    raise ValueError("conflicting duplicate stream identity")
                result = existing.model_copy(deep=True)
                break
            else:
                if self._try_admit_locked("stream", CaptureClass(record_class), estimated):
                    self._streams.append(stream.model_copy(deep=True))
                    self._omissions.extend(omitted)
                    self._track_retained_classes(safe)
                    self._retained_classes.add(CaptureClass(record_class))
                    self._journal_locked("stream", stream)
        self._emit_pending_truncation_diagnostic()
        return result

    def register_clock_domain(self, domain: ClockDomain) -> None:
        """Declare an additional clock domain (e.g. a browser's monotonic clock).

        Idempotent by ``clock_domain_id``: registering the same id twice is a
        no-op, and a re-registration with a conflicting definition is rejected so
        a domain's identity cannot silently change mid-session. The process clock
        domain is implicit and never re-declared here.
        """

        with self._lock:
            self._require_open()
            if domain.clock_domain_id == self.clock_domain_id:
                return
            for existing in self._extra_clock_domains:
                if existing.clock_domain_id == domain.clock_domain_id:
                    if existing != domain:
                        raise ValueError("conflicting duplicate clock domain identity")
                    return
            if len(self._extra_clock_domains) >= _MAX_EXTRA_CLOCK_DOMAINS:
                return  # defensive bound; extra domains beyond the cap are ignored
            self._extra_clock_domains.append(domain.model_copy(deep=True))
            self._journal_locked("clock_domain", domain)

    def register_clock_relation(self, relation: ClockRelation) -> None:
        """Declare a calibration relating two clock domains.

        The relation is what lets the analyzer align cross-clock timestamps into
        an honestly *estimated* latency; without one, cross-clock latency stays
        unavailable. Idempotent by ``relation_id`` with the same conflict guard.
        """

        with self._lock:
            self._require_open()
            for existing in self._clock_relations:
                if existing.relation_id == relation.relation_id:
                    if existing != relation:
                        raise ValueError("conflicting duplicate clock relation identity")
                    return
            if len(self._clock_relations) >= _MAX_CLOCK_RELATIONS:
                return
            self._clock_relations.append(relation.model_copy(deep=True))
            self._journal_locked("clock_relation", relation)

    def record_coverage(self, signal: str, availability: str, reason: str | None = None) -> None:
        safe_signal = sanitize_semantic_label(signal) or "unknown"
        safe_availability = sanitize_semantic_label(availability) or "unavailable"
        safe_reason = sanitize_semantic_label(reason)
        coverage = Coverage(
            signal=safe_signal,
            availability=safe_availability,
            reason=safe_reason,
        )
        estimated, _ = _structural_size_up_to(coverage, self.config.max_capture_bytes)
        handled = False
        with self._lock:
            self._require_open()
            for index, existing in enumerate(self._coverage):
                if existing.signal != safe_signal:
                    continue
                handled = True
                if existing.availability == safe_availability and existing.reason == safe_reason:
                    break
                if safe_availability == "available" and existing.availability != "available":
                    old_size, _ = _structural_size_up_to(existing, self.config.max_capture_bytes)
                    delta = max(0, estimated - old_size)
                    if self._admission_frozen:
                        self._note_omission_locked(
                            reason=self._first_limit_reason or "max_capture_bytes",
                            kind="coverage",
                            capture_class=CaptureClass.METADATA,
                            estimated_bytes=estimated,
                            whole_record=True,
                            freeze=True,
                        )
                    elif self._captured_bytes + delta > self.config.max_capture_bytes:
                        self._note_omission_locked(
                            reason="max_capture_bytes",
                            kind="coverage",
                            capture_class=CaptureClass.METADATA,
                            estimated_bytes=estimated,
                            whole_record=True,
                            freeze=True,
                        )
                    else:
                        self._coverage[index] = coverage
                        self._captured_bytes += delta
                        # Coverage supersedes in place. Journaling the index the
                        # recorder replaced keeps replay from guessing at
                        # last-write-wins.
                        self._journal_locked("coverage", coverage, replaces_index=index)
                break
            if not handled and self._try_admit_locked("coverage", CaptureClass.METADATA, estimated):
                self._coverage.append(coverage)
                self._journal_locked("coverage", coverage)
        self._emit_pending_truncation_diagnostic()

    def record_omission(
        self,
        field_name: str,
        *,
        capture_class: str | CaptureClass,
        reason: str = "adapter_payload_omitted",
    ) -> None:
        """Ledger a discarded source field without retaining its value."""

        if not isinstance(field_name, str) or not field_name:
            raise ValueError("field_name must be a non-empty string")
        try:
            normalized_class = CaptureClass(capture_class)
        except (TypeError, ValueError) as error:
            raise ValueError("capture_class must be a known capture class") from error
        if reason != "adapter_payload_omitted":
            raise ValueError("reason must be the stable adapter omission code")
        omission = Omission(
            field_key_sha256=hashlib.sha256(
                field_name.encode("utf-8", errors="surrogatepass")
            ).hexdigest(),
            capture_class=normalized_class,
            reason=reason,
        )
        with self._lock:
            self._require_open()
            estimated, _ = _structural_size_up_to(omission.as_dict(), self.config.max_capture_bytes)
            if self._try_admit_locked("omission", normalized_class, estimated):
                self._omissions.append(omission)
                self._journal_locked("omission")
        self._emit_pending_truncation_diagnostic()

    def register_adapter(self, adapter: Adapter) -> None:
        adapter, has_model_extensions = self._prepare_model(adapter, kind="adapter")
        self._assert_profile_record_depth(adapter, root_depth=4)
        estimated, _ = _structural_size_up_to(adapter, self.config.max_capture_bytes)
        with self._lock:
            self._require_open()
            admitted = False
            if adapter not in self._adapters and self._try_admit_locked(
                "adapter", CaptureClass.METADATA, estimated
            ):
                self._adapters.append(adapter)
                admitted = True
            if adapter in self._adapters and has_model_extensions:
                self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
            self._journal_locked("adapter" if admitted else "policy", adapter if admitted else None)
        self._emit_pending_truncation_diagnostic()

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

        payload_size = len(payload)
        with self._lock:
            self._require_open()
            if not self.config.capture_policy.allows(CaptureClass.RAW_OTLP):
                omission = Omission(
                    field_key_sha256=hashlib.sha256(f"otlp:{signal}".encode()).hexdigest(),
                    capture_class=CaptureClass.RAW_OTLP,
                )
                estimated, _ = _structural_size_up_to(
                    omission.as_dict(), self.config.max_capture_bytes
                )
                if self._try_admit_locked("omission", CaptureClass.RAW_OTLP, estimated):
                    self._omissions.append(omission)
                    self._journal_locked("omission")
                return False
            # Check the raw-byte cap before hashing or constructing/copying payload data.
            estimated = min(self.config.max_capture_bytes + 1, payload_size + 256)
            if not self._try_admit_locked(
                "raw_otlp",
                CaptureClass.RAW_OTLP,
                estimated,
                raw_bytes=payload_size,
            ):
                admitted = False
            else:
                admitted = True
            if admitted:
                chunk = RawOtlpChunk(
                    chunk_id=chunk_id,
                    signal=signal,
                    content_type=content_type,
                    compression=compression,
                    payload=payload,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
                self._raw_otlp_chunks.append(chunk)
                self._retained_classes.add(CaptureClass.RAW_OTLP)
                # The journal carries only classes the policy already admitted;
                # these bytes were admitted, so a replay that dropped them would
                # not reproduce the artifact this session is going to produce.
                self._journal_locked("raw_otlp", chunk, raw_payload=payload)
        self._emit_pending_truncation_diagnostic()
        return admitted

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
        started_at, started_extensions = self._prepare_model(started_at, kind="operation")
        ended_at, ended_extensions = self._prepare_model(ended_at, kind="operation")
        evidence, evidence_extensions = self._prepare_model(evidence, kind="operation")
        error, error_extensions = self._prepare_model(error, kind="operation")
        prepared_links: list[CausalLink] = []
        link_extensions = False
        if len(links) > self.config.max_records:
            with self._lock:
                self._note_omission_locked(
                    reason="max_value_bytes",
                    kind="operation",
                    capture_class=CaptureClass.METADATA,
                    estimated_bytes=1,
                    whole_record=False,
                    freeze=False,
                )
        for link in links[: self.config.max_records]:
            prepared_link, has_extensions = self._prepare_model(link, kind="operation")
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
        source_attributes = self._bounded_attributes("operation", attributes)
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
            self._bounded_attributes("operation", resource), self.config.capture_policy
        )
        safe_scope_attributes, scope_omitted = sanitize_attributes(
            self._bounded_attributes("operation", instrumentation_scope_attributes),
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
        estimated = self._capture_size(
            operation,
            omitted,
            resource_omitted,
            scope_omitted,
            evidence_omitted,
            link_omitted,
            error_omitted,
        )
        with self._lock:
            self._require_open()
            if self._try_admit_locked("operation", CaptureClass(record_class), estimated):
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
                if safe_schema_url is not None and not is_canonical_otel_schema_url(
                    safe_schema_url
                ):
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
                self._journal_locked("operation", operation)
        self._emit_pending_truncation_diagnostic()
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
        trace_id = self._manual_trace_id
        span_id = secrets.token_hex(8)
        # Adopt an enclosing manual operation as the parent span, but only within
        # this recorder's trace so a nested op from another recorder cannot forge
        # a parent across traces.
        parent = current_context()
        parent_span_id = (
            parent.operation_span_id
            if parent is not None and parent.operation_trace_id == trace_id
            else None
        )
        parent_scope = "internal" if parent_span_id is not None else "unknown"
        started_at = self._time()
        if self._journal.enabled:
            # The completed ``Operation`` is only recorded in the ``finally``
            # block below, so without this frame an operation that hung and took
            # the process down would be invisible to recovery. Only the governed
            # name is journaled; caller attributes have not been through the
            # capture policy at this point and never enter the journal.
            journal_name, journal_name_digest = normalize_operation_name(operation_name)
            self._journal.append_operation_open(
                operation_id=identity,
                operation_name=journal_name,
                operation_name_sha256=journal_name_digest,
                started_at=started_at,
                participant_id=participant_id,
                stream_id=stream_id,
                turn_id=turn_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                parent_scope=parent_scope,
            )
        status = "ok"
        error: ErrorRecord | None = None
        application_error = False
        operation_scope = _operation_scope(identity, span_id=span_id, trace_id=trace_id)
        operation_scope.__enter__()
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
                        parent_span_id=parent_span_id,
                        parent_scope=parent_scope,
                        attributes=attributes,
                        error=error,
                        capture_class=capture_class,
                    )
                except Exception as recording_error:
                    self.last_export_error = recording_error
                    if not application_error:
                        raise
            finally:
                operation_scope.__exit__(None, None, None)

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
        event_time, time_extensions = self._prepare_model(event_time, kind="event")
        evidence, evidence_extensions = self._prepare_model(evidence, kind="event")
        has_model_extensions = time_extensions or evidence_extensions
        event_name, source_name_digest = normalize_event_name(event_name)
        source_attributes = self._bounded_attributes("event", attributes)
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
            self._bounded_attributes("event", resource), self.config.capture_policy
        )
        safe_scope_attributes, scope_omitted = sanitize_attributes(
            self._bounded_attributes("event", instrumentation_scope_attributes),
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
        estimated = self._capture_size(
            event,
            omitted,
            resource_omitted,
            scope_omitted,
            evidence_omitted,
        )
        with self._lock:
            self._require_open()
            if self._try_admit_locked("event", CaptureClass(record_class), estimated):
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
                if safe_schema_url is not None and not is_canonical_otel_schema_url(
                    safe_schema_url
                ):
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                if safe_resource_schema_url is not None and not is_canonical_otel_schema_url(
                    safe_resource_schema_url
                ):
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                if has_model_extensions:
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                self._journal_locked("event", event)
        self._emit_pending_truncation_diagnostic()
        return event

    def record_quality_sample(
        self,
        sample: QualitySample,
        *,
        capture_class: str | None = None,
    ) -> QualitySample:
        """Retain a provider/transport sample after recursively filtering it."""

        # Detach only after a bounded preflight. Attribute maps are shallowly
        # narrowed first, so a single caller-owned payload cannot force a huge copy.
        if len(sample.measurements) > self.config.max_records:
            with self._lock:
                self._note_omission_locked(
                    reason="max_value_bytes",
                    kind="quality_sample",
                    capture_class=CaptureClass.METADATA,
                    estimated_bytes=1,
                    whole_record=False,
                    freeze=False,
                )
        bounded_evidence, _ = self._prepare_model(sample.evidence, kind="quality_sample")
        bounded_measurements = []
        for measurement in sample.measurements[: self.config.max_records]:
            bounded_measurement, _ = self._prepare_model(
                measurement.model_copy(
                    update={
                        "attributes": self._bounded_attributes(
                            "quality_sample", measurement.attributes
                        )
                    }
                ),
                kind="quality_sample",
            )
            bounded_measurements.append(bounded_measurement)
        sample = sample.model_copy(
            update={
                "attributes": self._bounded_attributes("quality_sample", sample.attributes),
                "resource": self._bounded_attributes("quality_sample", sample.resource),
                "instrumentation_scope_attributes": self._bounded_attributes(
                    "quality_sample", sample.instrumentation_scope_attributes
                ),
                "measurements": tuple(bounded_measurements),
                "evidence": bounded_evidence,
            }
        )
        sample, has_model_extensions = self._prepare_model(sample, kind="quality_sample")
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
        sanitized, _ = self._prepare_model(sanitized, kind="quality_sample")
        self._assert_profile_record_depth(sanitized, root_depth=3)
        estimated = self._capture_size(
            sanitized,
            omitted,
            resource_omitted,
            scope_omitted,
            evidence_omitted,
            measurement_omitted,
        )
        with self._lock:
            self._require_open()
            if self._try_admit_locked("quality_sample", CaptureClass(record_class), estimated):
                self._quality_samples.append(sanitized.model_copy(deep=True))
                self._omissions.extend(omitted)
                self._omissions.extend(resource_omitted)
                self._omissions.extend(scope_omitted)
                self._omissions.extend(evidence_omitted)
                self._omissions.extend(measurement_omitted)
                for attributes in governed:
                    self._track_retained_classes(attributes)
                self._retained_classes.add(CaptureClass(record_class))
                if safe_schema_url is not None and not is_canonical_otel_schema_url(
                    safe_schema_url
                ):
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                if safe_resource_schema_url is not None and not is_canonical_otel_schema_url(
                    safe_resource_schema_url
                ):
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                if has_model_extensions:
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                self._journal_locked("quality_sample", sanitized)
        self._emit_pending_truncation_diagnostic()
        return sanitized

    def add_media_ref(self, media: MediaRef) -> bool:
        """Attach custody metadata for media somebody else holds.

        Media bytes are never embedded, fetched, cached, or proxied: the record
        says where the media lives, what it covers, and whether anyone measured
        it. A credential-bearing or non-portable locator is stripped and recorded
        as an omission rather than retained, and an integrity claim the reference
        cannot back is refused outright at admission instead of surviving until
        ``close()``.
        """

        from .privacy import media_locator_safety

        media = media.model_copy(
            update={"attributes": self._bounded_attributes("media", media.attributes)}
        )
        source_size, source_exceeded = _structural_size_up_to(media, self.config.max_value_bytes)
        if source_exceeded:
            with self._lock:
                self._require_open()
                self._note_omission_locked(
                    reason="max_value_bytes",
                    kind="media",
                    capture_class=CaptureClass.AUDIO,
                    estimated_bytes=source_size,
                    whole_record=True,
                    freeze=True,
                )
            self._emit_pending_truncation_diagnostic()
            return False
        media, has_model_extensions = self._prepare_model(media, kind="media")
        if media.session_id != self.session_id:
            raise ValueError("media reference belongs to a different session")
        if media.capture_class != CaptureClass.AUDIO.value:
            raise ValueError("media references require the audio capture class")
        incoherence = media_custody_incoherence(media)
        if incoherence is not None:
            raise ValueError(incoherence)
        if not self.config.capture_policy.allows(CaptureClass.AUDIO):
            with self._lock:
                self._require_open()
                omission = Omission(
                    field_key_sha256=hashlib.sha256(b"media_ref").hexdigest(),
                    capture_class=CaptureClass.AUDIO,
                )
                estimated, _ = _structural_size_up_to(
                    omission.as_dict(), self.config.max_capture_bytes
                )
                if self._try_admit_locked("omission", CaptureClass.AUDIO, estimated):
                    self._omissions.append(omission)
                    self._journal_locked("omission")
            return False
        safe, omitted = sanitize_attributes(media.attributes, self.config.capture_policy)
        locator = media.locator
        locator_safety = media_locator_safety(locator.uri) if locator is not None else "portable"
        if locator is not None and locator_safety != "portable":
            omitted.append(
                Omission(
                    field_key_sha256=hashlib.sha256(b"media.locator.uri").hexdigest(),
                    capture_class=CaptureClass.AUDIO,
                    reason=(
                        "credential_bearing_locator"
                        if locator_safety == "credential"
                        else "invalid_media_locator"
                    ),
                )
            )
            locator = None
        sanitized = media.model_copy(update={"attributes": safe, "locator": locator})
        sanitized, _ = self._prepare_model(sanitized, kind="media")
        self._assert_profile_record_depth(sanitized, root_depth=3)
        estimated = self._capture_size(sanitized, omitted)
        with self._lock:
            self._require_open()
            admitted = self._try_admit_locked("media", CaptureClass.AUDIO, estimated)
            if admitted:
                self._media_refs.append(sanitized.model_copy(deep=True))
                self._omissions.extend(omitted)
                self._retained_classes.add(CaptureClass.AUDIO)
                self._track_retained_classes(safe)
                if has_model_extensions:
                    self._retained_classes.add(CaptureClass.EXTENSION_PAYLOAD)
                self._journal_locked("media", sanitized)
        self._emit_pending_truncation_diagnostic()
        return admitted

    def close(self, status: str = "completed") -> IncidentBundle:
        self._emit_pending_truncation_diagnostic()
        validation_error: Exception | None = None
        terminal_failure = False
        with self._lock:
            if self._close_error is not None:
                raise self._close_error
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
            # The finalize frame is written before validation and before export
            # so a crash between close and delivery still recovers the exact
            # artifact this call is about to produce.
            self._journal.finalize(
                status=safe_status,
                status_attributes=status_attributes,
                ended=ended,
                first_limit_reason=self._first_limit_reason,
                truncated_records=self._truncated_records,
                estimated_omitted_bytes=self._estimated_omitted_bytes,
                omitted_records_by_kind=tuple(self._omitted_records_by_kind.items()),
                omitted_records_by_capture_class=tuple(self._omitted_records_by_class.items()),
                retained_classes=tuple(self._retained_classes),
                record_counts=self._record_counts_locked(),
            )
            profile = build_incident_profile(
                self._snapshot_locked(safe_status, status_attributes, ended)
            )
            bundle = IncidentBundle(
                profile=profile,
                raw_otlp_chunks=tuple(self._raw_otlp_chunks),
            )
            from .validation import assert_valid_incident  # imported lazily

            try:
                assert_valid_incident(bundle)
            except Exception as error:
                self.last_export_error = error
                validation_error = error
                if self._on_close is not None:
                    # SDK-owned recorders cannot remain bound to a released client
                    # route: a repaired retry after reconfiguration could exfiltrate
                    # the old conversation to a new destination. Standalone recorders
                    # retain their historical repair-and-retry behavior.
                    self._status = safe_status
                    self._closed = True
                    self._close_error = error
                    terminal_failure = True
            else:
                self._status = safe_status
                self._closed = True
                # Keep an internal immutable-by-ownership snapshot. Contract models are
                # frozen, but nested dict/list extras are mutable Python containers.
                self._bundle = bundle.model_copy(deep=True)
                export_bundle = self._bundle

        if validation_error is not None:
            # Keep the journal: an artifact that failed validation is exactly the
            # case an operator wants to inspect the raw evidence for.
            self._journal.release(delivered=False)
            if terminal_failure:
                self._notify_close_once()
            raise validation_error

        try:
            if self.exporter is not None:
                from .codec import encode_incident_protobuf  # imported lazily
                from .privacy import assert_export_allowed

                try:
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
        finally:
            # The journal is only discarded once the incident has a successor:
            # a closed bundle the caller now holds, and an exporter that did not
            # refuse it. Anything else keeps the evidence on disk.
            self._journal.release(delivered=self.export_accepted is not False)
            self._notify_close_once()
        return export_bundle.model_copy(deep=True)

    def close_partial(self, recovery: RecoveryRecord, *, status: str) -> IncidentBundle:
        """Close a session whose end the observer never saw, as a provisional incident.

        A partial observation — a browser capture batch flushed while the call is
        still ongoing, for instance — is not a finished call. Its close was never
        observed, so no finalize frame is written (nothing observed a finalize),
        ``session.ended_at`` stays absent (the last snapshot is not the end of the
        call), and the artifact carries ``recovery`` with ``close_observed=False``.
        ``build_incident_profile`` then marks it provisional/incomplete and the
        validator refuses any manifest, session, or coverage that would let it pass
        as a clean, complete close.
        """

        if recovery.close_observed:
            raise ValueError("close_partial requires a recovery whose close was not observed")
        with self._lock:
            if self._close_error is not None:
                raise self._close_error
            if self._bundle is not None:
                return self._bundle.model_copy(deep=True)
            safe_status = sanitize_semantic_label(status) or "unknown"
            if safe_status == "completed":
                raise ValueError("a partial observation cannot present as a completed session")
            # No ``ended_at``: the real end was genuinely not observed. No journal
            # finalize either — a finalize frame means an observed close, and this
            # close was not observed; the journal stays as the partial evidence.
            snapshot = self._snapshot_locked(safe_status, {}, None)
            profile = build_incident_profile(snapshot, recovery=recovery)
            bundle = IncidentBundle(profile=profile, raw_otlp_chunks=tuple(self._raw_otlp_chunks))
            from .validation import assert_valid_incident  # imported lazily

            assert_valid_incident(bundle)
            self._status = safe_status
            self._closed = True
            self._bundle = bundle.model_copy(deep=True)
        return self._bundle.model_copy(deep=True)

    def checkpoint_status(self) -> CheckpointStatus:
        """Report the crash journal's state, including any degradation."""

        return self._journal.status()

    def _record_counts_locked(self) -> dict[str, int]:
        """Per-kind totals the assembler cross-checks its replay against."""

        return {
            "adapters": len(self._adapters),
            "participants": len(self._participants),
            "audio_streams": len(self._streams),
            "clock_domains": len(self._extra_clock_domains),
            "clock_relations": len(self._clock_relations),
            "coverage": len(self._coverage),
            "operations": len(self._operations),
            "events": len(self._events),
            "quality_samples": len(self._quality_samples),
            "media_refs": len(self._media_refs),
            "omissions": len(self._omissions),
            "raw_otlp_chunks": len(self._raw_otlp_chunks),
        }

    def _snapshot_locked(
        self,
        status: str,
        status_attributes: dict[str, str],
        ended_at: TimePoint | None,
    ) -> RecorderSnapshot:
        return RecorderSnapshot(
            producer_name=self.config.producer_name,
            producer_version=self.config.producer_version,
            bundle_id=self.bundle_id,
            session_id=self.session_id,
            clock_domain_id=self.clock_domain_id,
            started_wall=self._started_wall,
            started_mono=self._started_mono,
            capture_policy=self.config.capture_policy,
            adapters=tuple(self._adapters),
            status=status,
            status_attributes=status_attributes,
            ended_at=ended_at,
            participants=tuple(self._participants),
            audio_streams=tuple(self._streams),
            extra_clock_domains=tuple(self._extra_clock_domains),
            clock_relations=tuple(self._clock_relations),
            coverage=tuple(self._coverage),
            operations=tuple(self._operations),
            events=tuple(self._events),
            quality_samples=tuple(self._quality_samples),
            media_refs=tuple(self._media_refs),
            omissions=tuple(self._omissions),
            retained_classes=frozenset(self._retained_classes),
            first_limit_reason=self._first_limit_reason,
            omitted_records_by_class=tuple(self._omitted_records_by_class.items()),
        )

    def _journal_locked(
        self,
        kind: str,
        record: BaseModel | None = None,
        *,
        replaces_index: int | None = None,
        raw_payload: bytes | None = None,
    ) -> None:
        """Append this admitted mutation to the crash journal, under the lock.

        Called at the end of every critical section that changed recorder state,
        so journal order is exactly admission order. The privacy ledger and the
        retained-class set are diffed here rather than at each call site: a new
        admission path then cannot forget to journal what it appended.
        """

        if not self._journal.enabled:
            return
        pending_omissions = tuple(self._omissions[self._journaled_omissions :])
        self._journaled_omissions = len(self._omissions)
        pending_classes = tuple(sorted(self._retained_classes - self._journaled_classes))
        self._journaled_classes.update(pending_classes)
        if record is None and not pending_omissions and not pending_classes:
            return
        self._journal.append_record(
            RecordMutation(
                kind=kind,
                record=record,
                omissions=pending_omissions,
                retained_classes=pending_classes,
                replaces_index=replaces_index,
                raw_payload=raw_payload,
            )
        )

    def _notify_close_once(self) -> None:
        with self._lock:
            if self._close_notified:
                return
            self._close_notified = True
            on_close = self._on_close
            on_status = self._on_status
            recorder_status = self._status_locked()
        if on_close is not None:
            with contextlib.suppress(Exception):
                on_close()
        if on_status is not None:
            with contextlib.suppress(Exception):
                on_status(recorder_status)

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
    def _prepare_model(self, value: _ModelT, *, kind: str) -> tuple[_ModelT, bool]: ...

    @overload
    def _prepare_model(self, value: None, *, kind: str) -> tuple[None, bool]: ...

    def _prepare_model(
        self, value: BaseModel | None, *, kind: str
    ) -> tuple[BaseModel | None, bool]:
        """Revalidate and detach a caller-supplied contract model."""

        if value is None:
            return None, False
        mapping_updates = {
            name: self._bounded_attributes(kind, candidate)
            for name, candidate in value.__dict__.items()
            if isinstance(candidate, Mapping)
        }
        if mapping_updates:
            value = value.model_copy(update=mapping_updates)
        if value.model_extra:
            # Extras are a separate forward-compatibility payload. Bound them before
            # model_dump/deep-copy just like declared attribute maps.
            bounded_extra = self._bounded_attributes(kind, value.model_extra)
            value = value.model_copy()
            object.__setattr__(value, "__pydantic_extra__", bounded_extra)
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
