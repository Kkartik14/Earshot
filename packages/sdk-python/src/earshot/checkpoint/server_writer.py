"""An in-memory journal the server writes on a producer's behalf.

The crash journal (:mod:`earshot.checkpoint.writer`) is written by the *producer*
of a session -- an SDK recorder journaling its own facts to disk so a process
death loses nothing. The continuous browser-capture path has the inverse shape:
the server is handed a telemetry drain, projects it into governed facts through a
real recorder, and must accumulate those facts into one growing artifact without
ever holding a mutable incident. It does that by journaling -- the append-only
journal is the accumulator -- collecting the entries in the server rather than on
the producer's disk. Durability is added a layer out: when the call was given a
journal directory, the live registry writes the very frames it retains to an
append-only ``.eck`` file (:mod:`earshot.capture.durable`), so a backend restart
replays the call rather than losing it. This writer itself stays in memory and
metadata-only.

:class:`ServerJournalWriter` is that in-memory journal. It satisfies the exact
surface :class:`~earshot.recorder.IncidentRecorder` calls on its checkpoint
writer (``open_journal``, ``append_record``, ``append_limit``,
``append_operation_open``, ``finalize``, ``release``, ``status``, and the
``enabled`` flag), so a recorder journals to it byte-for-byte the way it journals
to disk. Instead of framing and writing bytes, it collects the *entries* in
admission order and hands the new ones to the live registry, which owns the
server-assigned sequence numbers and the retained frame buffer. One sequence
authority, in one place, is what keeps a re-sent drain from ever minting two
slots for the same fact.

Nothing here fsyncs, encrypts, or touches a descriptor: the durable copy of a
capture call is the live session's retained frames -- held in memory always, and
mirrored to disk when a journal directory is configured -- and its truncation
story is already the live session's (`frames_complete`). This writer is
metadata-only and bounded by the same recorder caps every other journal runs
under.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from pydantic import BaseModel

from ..privacy import CaptureClass, CapturePolicy
from .records import (
    JOURNAL_FORMAT_VERSION,
    JournalEntry,
    JournalFinalize,
    JournalLimitEntry,
    JournalOmission,
    JournalOpen,
    JournalOperationOpen,
    JournalRecordEntry,
    governance_to_journal,
)
from .writer import CheckpointStatus, RecordMutation


class ServerJournalWriter:
    """A checkpoint-writer-shaped, in-memory collector of journal entries.

    The recorder journals to this exactly as it would to a
    :class:`~earshot.checkpoint.writer.CheckpointWriter`. Rather than framing and
    writing bytes, it accumulates the entries; :meth:`take_new` hands the entries
    admitted since the last call to the live registry, which assigns their
    sequence numbers and retains their frames.
    """

    enabled = True

    def __init__(self) -> None:
        self.header: JournalOpen | None = None
        self._entries: list[JournalEntry] = []
        self._taken = 0
        self._closed = False
        self._journal_id: str | None = None

    def open_journal(
        self,
        *,
        producer_name: str,
        producer_version: str,
        bundle_id: str,
        session_id: str,
        clock_domain_id: str,
        started_wall: int,
        started_mono: int,
        manual_trace_id: str,
        capture_policy: CapturePolicy,
        max_records: int,
        max_capture_bytes: int,
        max_raw_otlp_bytes: int,
        max_value_bytes: int,
    ) -> None:
        """Record the header entry. Called once, before the first admitted fact."""

        if self.header is not None or self._closed:
            return
        # The journal id is derived from the bundle id (the deterministic call
        # key), not minted at random, so two reconstructions of the same call
        # carry the same journal identity and re-sealing stays idempotent.
        journal_id = hashlib.sha256(
            b"earshot.capture.journal:" + bundle_id.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:32]
        self._journal_id = journal_id
        header = JournalOpen(
            journal_format_version=JOURNAL_FORMAT_VERSION,
            journal_id=journal_id,
            producer_name=producer_name,
            producer_version=producer_version,
            bundle_id=bundle_id,
            session_id=session_id,
            clock_domain_id=clock_domain_id,
            started_wall=str(started_wall),
            started_mono=str(started_mono),
            manual_trace_id=manual_trace_id,
            policy_id=capture_policy.policy_id,
            policy_version=capture_policy.policy_version,
            enabled_classes=tuple(
                sorted(capture_class.value for capture_class in capture_policy.enabled)
            ),
            governance={
                capture_class.value: governance_to_journal(governance)
                for capture_class, governance in sorted(
                    capture_policy.governance.items(), key=lambda item: item[0].value
                )
            },
            max_records=max_records,
            max_capture_bytes=max_capture_bytes,
            max_raw_otlp_bytes=max_raw_otlp_bytes,
            max_value_bytes=max_value_bytes,
        )
        self.header = header
        self._entries.append(header)

    def append_record(self, mutation: RecordMutation) -> None:
        """Collect one admitted mutation as a journal record entry.

        The mapping from a :class:`RecordMutation` to a
        :class:`JournalRecordEntry` is exactly the on-disk writer's, so the
        entries a recorder produces do not depend on which journal it wrote to.
        """

        if self._closed:
            return
        value: dict[str, Any] | None = None
        if mutation.raw_payload is not None:
            value = (
                {}
                if mutation.record is None
                else mutation.record.model_dump(mode="json", exclude={"payload"})
            )
            value["payload_base64"] = base64.b64encode(mutation.raw_payload).decode("ascii")
        elif mutation.record is not None:
            value = mutation.record.model_dump(mode="json")
        self._entries.append(
            JournalRecordEntry(
                kind=mutation.kind,  # type: ignore[arg-type]
                value=value,
                omissions=tuple(
                    JournalOmission(
                        field_key_sha256=omission.field_key_sha256,
                        capture_class=omission.capture_class.value,
                        reason=omission.reason,
                    )
                    for omission in mutation.omissions
                ),
                retained_classes=tuple(
                    capture_class.value for capture_class in mutation.retained_classes
                ),
                replaces_index=mutation.replaces_index,
            )
        )

    def append_limit(
        self,
        *,
        reason: str,
        kind: str,
        capture_class: CaptureClass,
        estimated_bytes: int,
        whole_record: bool,
        freeze: bool,
    ) -> None:
        if self._closed:
            return
        self._entries.append(
            JournalLimitEntry(
                reason=reason,
                kind=kind,
                capture_class=capture_class.value,
                estimated_bytes=max(0, estimated_bytes),
                whole_record=whole_record,
                freeze=freeze,
            )
        )

    def append_operation_open(
        self,
        *,
        operation_id: str,
        operation_name: str,
        operation_name_sha256: str | None,
        started_at: BaseModel,
        participant_id: str | None,
        stream_id: str | None,
        turn_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        parent_span_id: str | None,
        parent_scope: str,
    ) -> None:
        if self._closed:
            return
        self._entries.append(
            JournalOperationOpen(
                operation_id=operation_id,
                operation_name=operation_name,
                operation_name_sha256=operation_name_sha256,
                started_at=started_at.model_dump(mode="json"),
                participant_id=participant_id,
                stream_id=stream_id,
                turn_id=turn_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                parent_scope=parent_scope,
            )
        )

    def finalize(
        self,
        *,
        status: str,
        status_attributes: dict[str, str],
        ended: BaseModel,
        first_limit_reason: str | None,
        truncated_records: int,
        estimated_omitted_bytes: int,
        omitted_records_by_kind: Any,
        omitted_records_by_capture_class: Any,
        retained_classes: Any,
        record_counts: dict[str, int],
    ) -> None:
        """Record the finalize entry. Phase 1 never closes; Phase 2 will."""

        if self.header is None or self._closed:
            return
        self._entries.append(
            JournalFinalize(
                status=status,
                status_attributes=dict(status_attributes),
                ended=ended.model_dump(mode="json"),
                journal_complete=True,
                first_limit_reason=first_limit_reason,
                truncated_records=truncated_records,
                estimated_omitted_bytes=estimated_omitted_bytes,
                omitted_records_by_kind=tuple(omitted_records_by_kind),
                omitted_records_by_capture_class=tuple(omitted_records_by_capture_class),
                retained_classes=tuple(
                    sorted(capture_class.value for capture_class in retained_classes)
                ),
                record_counts=dict(record_counts),
            )
        )

    def release(self, *, delivered: bool = False) -> None:
        self._closed = True

    def status(self) -> CheckpointStatus:
        return CheckpointStatus(
            state="closed" if self._closed else "open",
            journal_id=self._journal_id,
            path=None,
            last_sequence=len(self._entries),
            journal_bytes=0,
            degraded=False,
            journal_complete=True,
            dropped_records=0,
            last_failure=None,
        )

    def take_new(self) -> list[JournalEntry]:
        """The entries admitted since the last call, in admission order."""

        new = self._entries[self._taken :]
        self._taken = len(self._entries)
        return new


__all__ = ["ServerJournalWriter"]
