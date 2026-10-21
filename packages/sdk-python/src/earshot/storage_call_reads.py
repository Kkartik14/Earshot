"""Reading calls (FR-10): the call list and one call's detail.

Reads take no write lock. Each runs in one read snapshot of the catalog, so a
call's row, its revisions, recordings and spans agree with one another.

**The list** is ordered by ``(first_evidence_unix_nano DESC, call_id DESC)``, a
key that never changes once a call exists (``storage_calls``: a call's first
evidence is fixed when it is created), so keyset pages stay stable while calls
are created and while listed calls receive evidence. ``span_count`` and
``recording_count`` are the counts kept on ``calls``; no page aggregates.

**The detail** adds the call's revisions, its recordings (every state), whether
it holds evidence its latest revision does not show
(``evidence_after_latest_revision``: a call at the revision cap), and its
transcript, read from its spans at request time, so an open call's transcript
grows as its spans arrive. Each span's line was extracted when the span was
stored (``spans.text_json``); the read selects only spans that carry one and
never reads a payload, so its cost follows the transcript, not the spans (an
``llm`` span's payload repeats the whole conversation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .call_materializer import (
    TranscriptLine,
    stored_transcript_line,
    transcript_coverage,
    transcript_order,
)
from .storage import decode_page_cursor, encode_page_cursor
from .storage_calls import call_spans_sql
from .storage_recordings import RecordingStore

if TYPE_CHECKING:
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection
    from .storage_recordings import Recording

CALL_STATES = ("open", "closed")
# A call's transcript lines: its spans' ``text_json``, never a payload.
TRANSCRIPT_SQL = call_spans_sql(
    "s.trace_id, s.span_id, s.start_unix_nano, s.end_unix_nano, s.text_json",
    condition="s.text_json IS NOT NULL",
)
MAX_PAGE_SIZE = 100
_SUMMARY_COLUMNS = (
    "c.call_id, c.key_source, c.state, c.session_key, c.first_evidence_unix_nano, "
    "c.last_evidence_unix_nano, c.span_count, c.recording_count, c.materialize_error, "
    "c.evidence_after_latest_revision, r.revision, r.bundle_id, r.close_reason, r.finality, "
    "r.span_count AS revision_span_count, r.recording_count AS revision_recording_count, "
    "r.created_unix_nano AS revision_created_unix_nano"
)
_SUMMARY_FROM = (
    "FROM calls c LEFT JOIN call_revisions r ON r.project_id = c.project_id "
    "AND r.call_id = c.call_id AND r.revision = c.latest_revision"
)


@dataclass(frozen=True, slots=True)
class CallRevision:
    """One ``call_revisions`` row: a revision bundle of a call."""

    revision: int
    bundle_id: str
    close_reason: str
    finality: str
    span_count: int
    recording_count: int
    created_unix_nano: int


@dataclass(frozen=True, slots=True)
class CallSummary:
    """A call as the list shows it."""

    call_id: str
    key_source: str
    state: str
    # ``None`` when the key was not stored (``metadata_only``, trace-keyed calls).
    session_key: str | None
    first_evidence_unix_nano: int
    last_evidence_unix_nano: int
    span_count: int
    recording_count: int
    latest_revision: CallRevision | None
    materialize_error: str | None
    evidence_after_latest_revision: bool


@dataclass(frozen=True, slots=True)
class CallPage:
    items: tuple[CallSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CallDetail:
    """A call with its revisions, recordings and transcript."""

    summary: CallSummary
    revisions: tuple[CallRevision, ...]
    recordings: tuple[Recording, ...]
    transcript: tuple[TranscriptLine, ...]
    # ``call_materializer.transcript_coverage``.
    transcript_coverage: str


def _revision(row: Any, prefix: str = "") -> CallRevision:
    return CallRevision(
        revision=int(row["revision"]),
        bundle_id=row["bundle_id"],
        close_reason=row["close_reason"],
        finality=row["finality"],
        span_count=int(row[f"{prefix}span_count"]),
        recording_count=int(row[f"{prefix}recording_count"]),
        created_unix_nano=int(row[f"{prefix}created_unix_nano"]),
    )


def _summary(row: Any) -> CallSummary:
    return CallSummary(
        call_id=row["call_id"],
        key_source=row["key_source"],
        state=row["state"],
        session_key=row["session_key"],
        first_evidence_unix_nano=int(row["first_evidence_unix_nano"]),
        last_evidence_unix_nano=int(row["last_evidence_unix_nano"]),
        span_count=int(row["span_count"]),
        recording_count=int(row["recording_count"]),
        latest_revision=None if row["revision"] is None else _revision(row, "revision_"),
        materialize_error=row["materialize_error"],
        evidence_after_latest_revision=bool(row["evidence_after_latest_revision"]),
    )


class CallReads:
    """Call read operations on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store

    def list_calls(
        self,
        project_id: str,
        *,
        state: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CallPage:
        """One page of the project's calls, newest first evidence first.

        ``cursor`` is the previous page's ``next_cursor``; a malformed one raises
        ``InvalidCursorError``.
        """

        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if state is not None and state not in CALL_STATES:
            raise ValueError("state must be open or closed")
        where = "c.project_id = ?"
        parameters: list[Any] = [project_id]
        if state is not None:
            where += " AND c.state = ?"
            parameters.append(state)
        if cursor is not None:
            first_evidence, call_id = decode_page_cursor(cursor)
            where += " AND (c.first_evidence_unix_nano, c.call_id) < (?, ?)"
            parameters.extend((first_evidence, call_id))
        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            rows = connection.execute(
                f"SELECT {_SUMMARY_COLUMNS} {_SUMMARY_FROM} WHERE {where} "
                "ORDER BY c.first_evidence_unix_nano DESC, c.call_id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        items = tuple(_summary(row) for row in rows[:limit])
        last = items[-1] if len(rows) > limit else None
        return CallPage(
            items=items,
            next_cursor=(
                None
                if last is None
                else encode_page_cursor(last.first_evidence_unix_nano, last.call_id)
            ),
        )

    def get_call(self, project_id: str, call_id: str) -> CallDetail | None:
        """The call with its revisions, recordings and transcript; ``None`` if the
        project has no such call."""

        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            return self._detail(connection, project_id, call_id)

    def _detail(
        self, connection: DatabaseConnection, project_id: str, call_id: str
    ) -> CallDetail | None:
        row = connection.execute(
            f"SELECT {_SUMMARY_COLUMNS} {_SUMMARY_FROM} WHERE c.project_id = ? AND c.call_id = ?",
            (project_id, call_id),
        ).fetchone()
        if row is None:
            return None
        revisions = tuple(
            _revision(item)
            for item in connection.execute(
                "SELECT revision, bundle_id, close_reason, finality, span_count, "
                "recording_count, created_unix_nano FROM call_revisions "
                "WHERE project_id = ? AND call_id = ? ORDER BY revision",
                (project_id, call_id),
            ).fetchall()
        )
        lines = tuple(
            sorted(
                (
                    stored_transcript_line(
                        item["text_json"],
                        start_unix_nano=int(item["start_unix_nano"]),
                        end_unix_nano=int(item["end_unix_nano"]),
                        trace_id=bytes(item["trace_id"]),
                        span_id=bytes(item["span_id"]),
                    )
                    for item in connection.execute(TRANSCRIPT_SQL, (project_id, call_id)).fetchall()
                ),
                key=transcript_order,
            )
        )
        preset = connection.execute(
            "SELECT capture_preset FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()["capture_preset"]
        return CallDetail(
            summary=_summary(row),
            revisions=revisions,
            recordings=RecordingStore.for_call(connection, project_id, call_id),
            transcript=lines,
            transcript_coverage=transcript_coverage(lines, preset),
        )
