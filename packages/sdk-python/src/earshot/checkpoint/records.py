"""Closed models for every kind of entry an incident journal can hold.

The journal records *mutations*, never aggregates. A ``limit`` entry replays the
recorder's own omission bookkeeping, so the assembler reconstructs
``first_limit_reason`` and every omission counter by re-running the recorder's
arithmetic instead of duplicating it. The ``finalize`` entry carries the
recorder's authoritative totals only as a cross-check: a mismatch means the
journal and the recorder disagree, which is a bug worth failing loudly for.

Every model is closed (``extra="forbid"``). A journal is a private, single-writer
format read only by code shipped in the same package, so forward-compatible
extras would be an unpoliced channel rather than a feature.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from ..privacy import (
    CaptureClass,
    CaptureGovernance,
    CapturePolicy,
    ConsentConfig,
    ExportConfig,
    RedactionConfig,
    RetentionConfig,
)

JOURNAL_FORMAT_VERSION = 1

ENTRY_OPEN = "open"
ENTRY_RECORD = "record"
ENTRY_LIMIT = "limit"
ENTRY_OPERATION_OPEN = "operation_open"
ENTRY_EXHAUSTED = "exhausted"
ENTRY_FINALIZE = "finalize"

# The journal's own limitation vocabulary, mirrored into the recovered
# artifact's omissions so a reader learns what was lost and why.
REASON_JOURNAL_FULL = "checkpoint_journal_full"
REASON_TORN_TAIL = "checkpoint_torn_tail"
REASON_UNREADABLE_RECORD = "checkpoint_unreadable_record"

DecimalNanoText = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=20)]

# Mutations that carry a contract record, plus the two that mutate only the
# privacy ledger or the retained-class set.
JournalRecordKind = Literal[
    "adapter",
    "participant",
    "stream",
    "coverage",
    "operation",
    "event",
    "quality_sample",
    "media",
    "raw_otlp",
    "clock_domain",
    "clock_relation",
    "omission",
    "policy",
]
_VALUELESS_KINDS = frozenset({"omission", "policy"})


class JournalFormatError(ValueError):
    """Raised when journal bytes are intact but not a journal entry we support."""


def journal_frame_aad(journal_id: str | None, sequence: int) -> bytes:
    """Additional authenticated data binding a frame to its journal and slot."""

    return f"{JOURNAL_FORMAT_VERSION}|{journal_id or 'header'}|{sequence}".encode()


class JournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class JournalOmission(JournalModel):
    """One append to the recorder's privacy ledger, in ledger order."""

    field_key_sha256: str
    capture_class: str
    reason: str


class JournalGovernance(JournalModel):
    """The governance a capture class was configured with, as declared."""

    consent: dict[str, Any] | None = None
    redaction: dict[str, Any] | None = None
    retention: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


class JournalOpen(JournalModel):
    """Everything about the session that is fixed before the first fact."""

    journal_format_version: StrictInt
    journal_id: str
    producer_name: str
    producer_version: str
    bundle_id: str
    session_id: str
    clock_domain_id: str
    started_wall: DecimalNanoText
    started_mono: str
    manual_trace_id: str
    policy_id: str
    policy_version: str
    enabled_classes: tuple[str, ...]
    governance: dict[str, JournalGovernance] = Field(default_factory=dict)
    max_records: StrictInt
    max_capture_bytes: StrictInt
    max_raw_otlp_bytes: StrictInt
    max_value_bytes: StrictInt

    def capture_policy(self) -> CapturePolicy:
        """Rebuild the exact policy the recorder ran under."""

        return CapturePolicy(
            enabled=frozenset(CaptureClass(name) for name in self.enabled_classes),
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            governance={
                CaptureClass(name): _governance_from_journal(value)
                for name, value in self.governance.items()
            },
        )


class JournalRecordEntry(JournalModel):
    """One admitted mutation: a contract record plus the ledger it moved.

    ``omissions`` and ``retained_classes`` are the exact appends the recorder
    made in the same critical section. Journaling them with the record — rather
    than reconstructing them at replay — is what keeps the privacy manifest of a
    recovered artifact identical to the one ``close()`` would have written.
    """

    kind: JournalRecordKind
    value: dict[str, Any] | None = None
    omissions: tuple[JournalOmission, ...] = ()
    retained_classes: tuple[str, ...] = ()
    replaces_index: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def keeps_the_mutation_coherent(self) -> JournalRecordEntry:
        if self.kind in _VALUELESS_KINDS:
            if self.value is not None:
                raise ValueError("ledger-only journal entries cannot carry a record")
        elif self.value is None:
            raise ValueError("a record journal entry requires its contract record")
        if self.replaces_index is not None and self.kind != "coverage":
            raise ValueError("only coverage supersedes an earlier record")
        return self


class JournalLimitEntry(JournalModel):
    """One ``_note_omission_locked`` call, replayed verbatim by the assembler."""

    reason: str
    kind: str
    capture_class: str
    estimated_bytes: StrictInt = Field(ge=0)
    whole_record: StrictBool
    freeze: StrictBool


class JournalOperationOpen(JournalModel):
    """An operation that started; nothing here claims it finished.

    This entry exists because ``IncidentRecorder.operation()`` only records an
    ``Operation`` in its ``finally`` block. Without it, the LLM call that hung
    and took the process down with it is exactly the fact recovery cannot see.
    Only already-governed identity is journaled: the operation name arrives
    normalized, and caller attributes are not journaled at all because they have
    not been through the capture policy yet.
    """

    operation_id: str
    operation_name: str
    operation_name_sha256: str | None = None
    started_at: dict[str, Any]
    participant_id: str | None = None
    stream_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    parent_scope: str = "unknown"


class JournalExhausted(JournalModel):
    """The journal reached a cap and stopped accepting facts, and says so.

    This is written past the byte cap on purpose. A journal that merely stopped
    would leave a recovered artifact silently short; the marker is what turns
    truncation into a declared limitation.
    """

    reason: str = REASON_JOURNAL_FULL


class JournalFinalize(JournalModel):
    """The recorder reached ``close()``. Written before validation or export."""

    status: str
    status_attributes: dict[str, str] = Field(default_factory=dict)
    ended: dict[str, Any]
    journal_complete: StrictBool = True
    first_limit_reason: str | None = None
    truncated_records: StrictInt = Field(default=0, ge=0)
    estimated_omitted_bytes: StrictInt = Field(default=0, ge=0)
    omitted_records_by_kind: tuple[tuple[str, StrictInt], ...] = ()
    omitted_records_by_capture_class: tuple[tuple[str, StrictInt], ...] = ()
    retained_classes: tuple[str, ...] = ()
    record_counts: dict[str, StrictInt] = Field(default_factory=dict)


JournalEntry = (
    JournalOpen
    | JournalRecordEntry
    | JournalLimitEntry
    | JournalOperationOpen
    | JournalExhausted
    | JournalFinalize
)

_ENTRY_TYPES: dict[str, type[JournalModel]] = {
    ENTRY_OPEN: JournalOpen,
    ENTRY_RECORD: JournalRecordEntry,
    ENTRY_LIMIT: JournalLimitEntry,
    ENTRY_OPERATION_OPEN: JournalOperationOpen,
    ENTRY_EXHAUSTED: JournalExhausted,
    ENTRY_FINALIZE: JournalFinalize,
}
_ENTRY_TAGS: dict[type[JournalModel], str] = {model: tag for tag, model in _ENTRY_TYPES.items()}


def governance_to_journal(governance: CaptureGovernance) -> JournalGovernance:
    """Snapshot declared governance so a replay rebuilds the same manifest."""

    return JournalGovernance(
        consent=None if governance.consent is None else dataclasses.asdict(governance.consent),
        redaction=(
            None if governance.redaction is None else dataclasses.asdict(governance.redaction)
        ),
        retention=(
            None if governance.retention is None else dataclasses.asdict(governance.retention)
        ),
        export=None if governance.export is None else dataclasses.asdict(governance.export),
    )


def _governance_from_journal(value: JournalGovernance) -> CaptureGovernance:
    export = value.export
    return CaptureGovernance(
        consent=None if value.consent is None else ConsentConfig(**value.consent),
        redaction=None if value.redaction is None else RedactionConfig(**value.redaction),
        retention=None if value.retention is None else RetentionConfig(**value.retention),
        export=(
            None
            if export is None
            else ExportConfig(**{**export, "destinations": tuple(export.get("destinations", ()))})
        ),
    )


def encode_entry(entry: JournalEntry) -> bytes:
    """Render one entry as deterministic compact JSON."""

    tag = _ENTRY_TAGS[type(entry)]
    document = {"k": tag, "v": entry.model_dump(mode="json")}
    return json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_entry(body: bytes) -> JournalEntry:
    """Parse one entry, raising :class:`JournalFormatError` on anything else."""

    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise JournalFormatError("journal entry is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != {"k", "v"}:
        raise JournalFormatError("journal entry envelope is malformed")
    model = _ENTRY_TYPES.get(document["k"])
    if model is None:
        raise JournalFormatError("unsupported journal entry kind")
    try:
        return model.model_validate(document["v"])  # type: ignore[return-value]
    except ValueError as error:
        raise JournalFormatError("journal entry violates its structure") from error
