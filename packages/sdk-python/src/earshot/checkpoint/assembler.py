"""Reconstruct an incident from a checkpoint journal.

The assembler replays a journal into the same snapshot ``close()`` builds and
then calls the same profile builder. One construction path is the whole reason a
recovered artifact cannot drift from a closed one, and it is what makes the
strongest available guarantee testable: replaying a *finalized* journal produces
bytes identical to the ones ``close()`` produced.

That guarantee is also why a finalized replay carries no recovery declaration.
The evidence is complete and the close really was observed; only delivery was
interrupted, and delivery is not evidence. Declaring it recovered would change
the digest of an identical artifact and turn an idempotent re-ingest into a
conflict. Whether recovery ran is reported in :class:`AssemblyReport`, which is
operational output rather than part of the artifact.

A journal with no finalize entry is a different artifact and says so: provisional,
incomplete, ``session.status="interrupted"``, and no session end at all.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contract import (
    Adapter,
    AudioStream,
    ClockDomain,
    ClockRelation,
    Coverage,
    Event,
    Evidence,
    IncidentBundle,
    MediaRef,
    Operation,
    Participant,
    Producer,
    QualitySample,
    RawOtlpChunk,
    RecoveryRecord,
    TimePoint,
)
from ..privacy import CaptureClass, Omission
from ..versions import PACKAGE_VERSION
from .reader import JournalReader, JournalReplay
from .records import (
    JournalExhausted,
    JournalFinalize,
    JournalLimitEntry,
    JournalOperationOpen,
    JournalRecordEntry,
)

if TYPE_CHECKING:  # pragma: no cover - avoids a module-level import cycle
    from ..recorder import RecorderSnapshot

METHOD_CHECKPOINT_JOURNAL = "checkpoint_journal"
REASON_BEFORE_CLOSE = "process_terminated_before_close"
RECOVERED_SESSION_STATUS = "interrupted"

_RECORD_MODELS = {
    "adapter": Adapter,
    "participant": Participant,
    "stream": AudioStream,
    "coverage": Coverage,
    "operation": Operation,
    "event": Event,
    "quality_sample": QualitySample,
    "media": MediaRef,
    "clock_domain": ClockDomain,
    "clock_relation": ClockRelation,
}


class AssemblyError(ValueError):
    """Raised when a journal cannot be replayed into a coherent incident."""


@dataclass(frozen=True)
class AssemblyReport:
    """What the recovery run itself observed. Operational, never evidence."""

    journal_id: str
    session_id: str
    bundle_id: str
    last_sequence: int
    close_observed: bool
    torn_tail_bytes: int
    discarded_records: int
    journal_complete: bool
    counter_mismatch: bool
    unfinished_operations: int
    stop_reason: str | None


@dataclass(frozen=True)
class AssemblyResult:
    bundle: IncidentBundle
    report: AssemblyReport


class _ReplayState:
    """The recorder state a journal replays into, in journal order."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.adapters: list[Adapter] = []
        self.participants: list[Participant] = []
        self.audio_streams: list[AudioStream] = []
        self.coverage: list[Coverage] = []
        self.operations: list[Operation] = []
        self.events: list[Event] = []
        self.quality_samples: list[QualitySample] = []
        self.media_refs: list[MediaRef] = []
        self.clock_domains: list[ClockDomain] = []
        self.clock_relations: list[ClockRelation] = []
        self.raw_otlp_chunks: list[RawOtlpChunk] = []
        self.omissions: list[Omission] = []
        self.retained_classes: set[CaptureClass] = {CaptureClass.METADATA}
        self.open_operations: dict[str, JournalOperationOpen] = {}
        self.first_limit_reason: str | None = None
        self.truncated_records = 0
        self.estimated_omitted_bytes = 0
        self.omitted_by_kind: dict[str, int] = {}
        self.omitted_by_class: dict[str, int] = dict.fromkeys(
            (capture_class.value for capture_class in CaptureClass), 0
        )
        self.last_observation: TimePoint | None = None

    def append(self, kind: str, record: Any, replaces_index: int | None) -> None:
        target = {
            "adapter": self.adapters,
            "participant": self.participants,
            "stream": self.audio_streams,
            "coverage": self.coverage,
            "operation": self.operations,
            "event": self.events,
            "quality_sample": self.quality_samples,
            "media": self.media_refs,
            "clock_domain": self.clock_domains,
            "clock_relation": self.clock_relations,
        }[kind]
        if replaces_index is not None:
            if replaces_index >= len(target):
                raise AssemblyError("journal supersedes a record that was never admitted")
            target[replaces_index] = record
            return
        target.append(record)

    def counts(self) -> dict[str, int]:
        return {
            "adapters": len(self.adapters),
            "participants": len(self.participants),
            "audio_streams": len(self.audio_streams),
            "clock_domains": len(self.clock_domains),
            "clock_relations": len(self.clock_relations),
            "coverage": len(self.coverage),
            "operations": len(self.operations),
            "events": len(self.events),
            "quality_samples": len(self.quality_samples),
            "media_refs": len(self.media_refs),
            "omissions": len(self.omissions),
            "raw_otlp_chunks": len(self.raw_otlp_chunks),
        }


def assemble_incident(
    journal: Path | str,
    *,
    key: bytes | None = None,
    recoverer: Producer | None = None,
    best_effort: bool = False,
    bundle_id_suffix: str | None = None,
) -> AssemblyResult:
    """Rebuild an incident from one journal file.

    ``best_effort`` downgrades a counter cross-check failure from an error to a
    reported flag. It never invents a record and never turns an unclosed session
    into a closed one.
    """

    replay = JournalReader(journal, key=key).read()
    state = _ReplayState(replay.header.session_id)
    discarded = 0
    journal_exhausted = False
    for entry in replay.entries:
        if isinstance(entry, JournalRecordEntry):
            discarded += _apply_record(state, entry)
        elif isinstance(entry, JournalLimitEntry):
            _apply_limit(state, entry)
        elif isinstance(entry, JournalOperationOpen):
            state.open_operations[entry.operation_id] = entry
            state.last_observation = TimePoint.model_validate(entry.started_at)
        elif isinstance(entry, JournalExhausted):
            journal_exhausted = True
        elif isinstance(entry, JournalFinalize):
            break
    finalize = replay.finalize
    journal_complete = (
        not journal_exhausted
        and replay.torn_tail_bytes == 0
        and (finalize is None or finalize.journal_complete)
    )
    counter_mismatch = False
    if finalize is not None:
        counter_mismatch = _cross_check(state, finalize)
        if counter_mismatch and not best_effort:
            raise AssemblyError(
                "journal replay disagrees with the recorder's own totals; "
                "re-run with best_effort to accept the replayed values"
            )

    bundle_id = replay.header.bundle_id + (bundle_id_suffix or "")
    recovery: RecoveryRecord | None = None
    unfinished: list[Operation] = []
    if finalize is not None:
        # The close was observed, so the evidence is complete and this replay
        # reproduces exactly what ``close()`` produced — no declaration, no new
        # digest, and an idempotent re-ingest.
        status = finalize.status
        status_attributes = dict(finalize.status_attributes)
        ended_at: TimePoint | None = TimePoint.model_validate(finalize.ended)
    else:
        # No close was observed. Everything the artifact says about its own
        # completeness has to reflect that, and ``ended_at`` stays absent because
        # the real end genuinely was not seen.
        status = RECOVERED_SESSION_STATUS
        status_attributes = {}
        ended_at = None
        unfinished = _unfinished_operations(state)
        state.operations.extend(unfinished)
        recovery = RecoveryRecord(
            method=METHOD_CHECKPOINT_JOURNAL,
            reason=REASON_BEFORE_CLOSE,
            close_observed=False,
            journal_id=replay.header.journal_id,
            last_sequence=replay.last_sequence,
            last_observation=state.last_observation,
            torn_tail_bytes=replay.torn_tail_bytes,
            discarded_records=discarded,
            journal_complete=journal_complete,
            recoverer=recoverer or _default_recoverer(),
        )

    snapshot = _snapshot(
        replay, state, status, status_attributes, ended_at, bundle_id, len(unfinished)
    )
    from ..recorder import build_incident_profile  # lazy: keeps the import graph acyclic

    profile = build_incident_profile(snapshot, recovery=recovery)
    bundle = IncidentBundle(profile=profile, raw_otlp_chunks=tuple(state.raw_otlp_chunks))
    report = AssemblyReport(
        journal_id=replay.header.journal_id,
        session_id=replay.header.session_id,
        bundle_id=bundle_id,
        last_sequence=replay.last_sequence,
        close_observed=finalize is not None,
        torn_tail_bytes=replay.torn_tail_bytes,
        discarded_records=discarded,
        journal_complete=journal_complete,
        counter_mismatch=counter_mismatch,
        unfinished_operations=len(unfinished),
        stop_reason=replay.stop_reason,
    )
    return AssemblyResult(bundle=bundle, report=report)


def _default_recoverer() -> Producer:
    return Producer(name="earshot", version=PACKAGE_VERSION, sdk_version=PACKAGE_VERSION)


def _apply_record(state: _ReplayState, entry: JournalRecordEntry) -> int:
    """Apply one journaled mutation. Returns the number of records discarded."""

    discarded = 0
    if entry.value is not None:
        if entry.kind == "raw_otlp":
            chunk = _decode_raw_otlp(entry.value)
            if chunk is None:
                return 1
            state.raw_otlp_chunks.append(chunk)
        else:
            model = _RECORD_MODELS.get(entry.kind)
            if model is None:
                return 1
            try:
                record = model.model_validate(entry.value)
            except ValueError as error:
                raise AssemblyError("journal holds a record that is not a contract record") from (
                    error
                )
            state.append(entry.kind, record, entry.replaces_index)
            _observe_time(state, record)
    for omission in entry.omissions:
        try:
            state.omissions.append(
                Omission(
                    field_key_sha256=omission.field_key_sha256,
                    capture_class=CaptureClass(omission.capture_class),
                    reason=omission.reason,
                )
            )
        except ValueError:
            discarded += 1
    for name in entry.retained_classes:
        try:
            state.retained_classes.add(CaptureClass(name))
        except ValueError:
            discarded += 1
    return discarded


def _apply_limit(state: _ReplayState, entry: JournalLimitEntry) -> None:
    """Re-run ``_note_omission_locked`` so counters are derived, not copied."""

    if state.first_limit_reason is None:
        state.first_limit_reason = entry.reason
    if entry.whole_record:
        state.truncated_records += 1
        state.omitted_by_kind[entry.kind] = state.omitted_by_kind.get(entry.kind, 0) + 1
    state.omitted_by_class[entry.capture_class] = (
        state.omitted_by_class.get(entry.capture_class, 0) + 1
    )
    state.estimated_omitted_bytes += entry.estimated_bytes


def _decode_raw_otlp(value: dict[str, Any]) -> RawOtlpChunk | None:
    payload_base64 = value.get("payload_base64")
    if not isinstance(payload_base64, str):
        return None
    try:
        payload = base64.b64decode(payload_base64, validate=True)
    except ValueError:
        return None
    try:
        return RawOtlpChunk(
            chunk_id=value["chunk_id"],
            signal=value["signal"],
            content_type=value.get("content_type", "application/x-protobuf"),
            compression=value.get("compression", "identity"),
            payload=payload,
            sha256=value.get("sha256"),
        )
    except (KeyError, ValueError):
        return None


def _observe_time(state: _ReplayState, record: Any) -> None:
    """Track the last coordinate the journal durably observed."""

    candidate = getattr(record, "ended_at", None) or getattr(record, "started_at", None)
    if candidate is None:
        candidate = getattr(record, "time", None)
    if isinstance(candidate, TimePoint):
        state.last_observation = candidate


def _unfinished_operations(state: _ReplayState) -> list[Operation]:
    """Turn every started-but-never-completed operation into an honest record.

    The start *was* observed and durably recorded; the end genuinely was not. So
    the operation appears with no end and status ``unknown``, which the analyzer
    already reports as an unavailable interval rather than as zero work.
    """

    completed = {operation.operation_id for operation in state.operations}
    unfinished: list[Operation] = []
    for operation_id, opened in state.open_operations.items():
        if operation_id in completed:
            continue
        attributes: dict[str, str] = {}
        if opened.operation_name_sha256 is not None:
            attributes["earshot.source.name_sha256"] = opened.operation_name_sha256
        unfinished.append(
            Operation(
                operation_id=operation_id,
                session_id=state.session_id,
                operation_name=opened.operation_name,
                status="unknown",
                started_at=TimePoint.model_validate(opened.started_at),
                ended_at=None,
                participant_id=opened.participant_id,
                stream_id=opened.stream_id,
                turn_id=opened.turn_id,
                trace_id=opened.trace_id,
                span_id=opened.span_id,
                parent_span_id=opened.parent_span_id,
                parent_scope=opened.parent_scope,
                evidence=Evidence(
                    source="earshot.sdk",
                    observer="earshot.recorder",
                    method="checkpoint_journal",
                    confidence="observed",
                    availability="partial",
                ),
                capture_class="metadata",
                attributes=attributes,
            )
        )
    return unfinished


def _cross_check(state: _ReplayState, finalize: JournalFinalize) -> bool:
    """Compare the replay against the recorder's own totals.

    The replayed values stay authoritative either way; this only reports whether
    the two disagree, because a disagreement means a bug rather than damage.
    """

    if state.first_limit_reason != finalize.first_limit_reason:
        return True
    if state.truncated_records != finalize.truncated_records:
        return True
    if state.estimated_omitted_bytes != finalize.estimated_omitted_bytes:
        return True
    expected_kind = {kind: count for kind, count in finalize.omitted_records_by_kind if count}
    if {kind: count for kind, count in state.omitted_by_kind.items() if count} != expected_kind:
        return True
    expected_class = {
        name: count for name, count in finalize.omitted_records_by_capture_class if count
    }
    if {name: count for name, count in state.omitted_by_class.items() if count} != expected_class:
        return True
    if sorted(item.value for item in state.retained_classes) != list(finalize.retained_classes):
        return True
    return state.counts() != dict(finalize.record_counts)


def _snapshot(
    replay: JournalReplay,
    state: _ReplayState,
    status: str,
    status_attributes: dict[str, str],
    ended_at: TimePoint | None,
    bundle_id: str,
    unfinished_operations: int,
) -> RecorderSnapshot:
    from ..recorder import RecorderSnapshot  # lazy: keeps the import graph acyclic

    header = replay.header
    return RecorderSnapshot(
        producer_name=header.producer_name,
        producer_version=header.producer_version,
        bundle_id=bundle_id,
        session_id=header.session_id,
        clock_domain_id=header.clock_domain_id,
        started_wall=int(header.started_wall),
        started_mono=int(header.started_mono),
        capture_policy=header.capture_policy(),
        adapters=tuple(state.adapters),
        status=status,
        status_attributes=status_attributes,
        ended_at=ended_at,
        participants=tuple(state.participants),
        audio_streams=tuple(state.audio_streams),
        extra_clock_domains=tuple(state.clock_domains),
        clock_relations=tuple(state.clock_relations),
        coverage=tuple(state.coverage),
        operations=tuple(state.operations),
        events=tuple(state.events),
        quality_samples=tuple(state.quality_samples),
        media_refs=tuple(state.media_refs),
        omissions=tuple(state.omissions),
        retained_classes=frozenset(state.retained_classes),
        first_limit_reason=state.first_limit_reason,
        omitted_records_by_class=tuple(state.omitted_by_class.items()),
        unfinished_operations=unfinished_operations,
    )


__all__ = [
    "METHOD_CHECKPOINT_JOURNAL",
    "REASON_BEFORE_CLOSE",
    "AssemblyError",
    "AssemblyReport",
    "AssemblyResult",
    "assemble_incident",
]
