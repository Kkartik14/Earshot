"""Reading calls: the call list, one call's detail and one span.

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
grows as its spans arrive. Each new span's line was extracted when it was stored
(``spans.text_json``); older upload/provider transcript-line spans predate that
extractor, so their payload is decoded only when this precomputed text is absent. The
read never decodes ordinary span payloads (an ``llm`` span's payload repeats the whole
conversation).

**Projections.** The list and the detail carry each call's projection
(``call_projection``) when it has one, with its freshness. The list's filters
compare projection columns, and a call without a projection, or with a NULL
column, matches no filter on it: an unknown is never a match. ``q`` is a
substring of the projection's transcript lines (``call_transcripts``) with ``%``,
``_`` and ``\\`` matched literally and ASCII case folded (SQLite's ``LIKE``;
Postgres's ``ILIKE`` folds other scripts' case too). It needs the project's
current preset to be ``full``: a ``metadata_only`` project may still hold lines
captured before its preset changed, and they are not searched.

**A span** is read only through the call its trace is bound to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .call_capture import CAPTURE_PRESET_FULL
from .call_materializer import (
    TranscriptLine,
    ordered_transcript_lines,
    stored_transcript_line,
    transcript_coverage,
    transcript_text_json_from_stored_span,
)
from .call_projection import ProjectionInputs, ProjectionStamps, projection_state
from .storage import decode_page_cursor, encode_page_cursor
from .storage_calls import call_spans_sql
from .storage_recordings import RecordingStore

if TYPE_CHECKING:
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    from .storage import IncidentStore
    from .storage_database import DatabaseConnection
    from .storage_recordings import Recording

CALL_STATES = ("open", "closed")
# Current transcript lines use ``text_json``. Older upload/provider transcript spans
# predate that extractor, so decode only their named line payloads as a fallback.
TRANSCRIPT_SQL = call_spans_sql(
    "s.trace_id, s.span_id, s.start_unix_nano, s.end_unix_nano, s.text_json, "
    "s.scope_name, NULL AS legacy_payload",
    condition="s.text_json IS NOT NULL",
)
LEGACY_TRANSCRIPT_SQL = call_spans_sql(
    "s.trace_id, s.span_id, s.start_unix_nano, s.end_unix_nano, s.text_json, "
    "s.scope_name, s.payload AS legacy_payload",
    condition=(
        "s.text_json IS NULL AND s.name = 'earshot.connector.transcript_line' AND "
        "(s.scope_name = 'earshot.upload' OR s.scope_name LIKE 'earshot.connector.%')"
    ),
)
# One stored span of a call, read through the call's binding of its trace.
SPAN_SQL = call_spans_sql("s.scope_name, s.payload", condition="t.trace_id = ? AND s.span_id = ?")
MAX_PAGE_SIZE = 100
# ``q``: the trigram floor (an optional trigram index serves 3 characters and up).
SEARCH_MIN_LENGTH = 3
SEARCH_MAX_LENGTH = 256
MAX_TAG_FILTERS = 10
_SUMMARY_COLUMNS = (
    "c.call_id, c.key_source, c.state, c.session_key, c.first_evidence_unix_nano, "
    "c.last_evidence_unix_nano, c.span_count, c.recording_count, c.materialize_error, "
    "c.evidence_after_latest_revision, r.revision, r.bundle_id, r.close_reason, r.finality, "
    "r.span_count AS revision_span_count, r.recording_count AS revision_recording_count, "
    "r.created_unix_nano AS revision_created_unix_nano, c.projection_seq, "
    "p.revision AS projection_revision, p.source_seq, "
    "p.price_version AS projection_price_version, "
    "p.builtin_price_version AS projection_builtin_price_version, p.projection_version, "
    "p.agent, p.framework, p.outcome, p.duration_ms, p.turn_count, p.measurable_turns, "
    "p.reported_latency_p50_ms, p.reported_latency_max_ms, p.audible_gap_p50_ms, "
    "p.audible_gap_max_ms, p.cost_lower_bound_nano_usd, p.cost_estimated_nano_usd, "
    "p.cost_unknown_items, p.analysis_state"
)
_SUMMARY_FROM = (
    "FROM calls c LEFT JOIN call_revisions r ON r.project_id = c.project_id "
    "AND r.call_id = c.call_id AND r.revision = c.latest_revision "
    "LEFT JOIN call_projections p ON p.project_id = c.project_id AND p.call_id = c.call_id"
)
# A cost within ``max_cost_nano_usd`` (bound twice): lower bound plus estimate, written
# so the sum is never computed (two large values would overflow a BIGINT).
_COST_WITHIN = (
    "p.cost_lower_bound_nano_usd <= ? "
    "AND COALESCE(p.cost_estimated_nano_usd, 0) <= ? - p.cost_lower_bound_nano_usd"
)


class ContentSearchForbiddenError(Exception):
    """``q`` was given for a project whose current preset stores no content."""

    code = "EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH"


@dataclass(frozen=True, slots=True)
class CallFilters:
    """The call list's filters; every one given must match.

    Latency and audible-gap bounds compare the call's p50; cost bounds are
    nano-USD. ``min_cost_nano_usd`` compares the lower bound. ``max_cost_nano_usd``
    matches only calls whose cost is complete (no unknown item), on the lower bound
    plus the estimate. ``tags`` must all be on the call.
    """

    agent: str | None = None
    outcome: str | None = None
    tags: tuple[str, ...] = ()
    min_reported_latency_ms: int | None = None
    max_reported_latency_ms: int | None = None
    min_audible_gap_ms: int | None = None
    max_audible_gap_ms: int | None = None
    min_cost_nano_usd: int | None = None
    max_cost_nano_usd: int | None = None
    q: str | None = None

    def __post_init__(self) -> None:
        if self.q is not None and not SEARCH_MIN_LENGTH <= len(self.q) <= SEARCH_MAX_LENGTH:
            raise ValueError(f"q must be {SEARCH_MIN_LENGTH} to {SEARCH_MAX_LENGTH} characters")
        if len(self.tags) > MAX_TAG_FILTERS:
            raise ValueError(f"at most {MAX_TAG_FILTERS} tags")


_RANGES = (
    ("min_reported_latency_ms", "p.reported_latency_p50_ms >= ?"),
    ("max_reported_latency_ms", "p.reported_latency_p50_ms <= ?"),
    ("min_audible_gap_ms", "p.audible_gap_p50_ms >= ?"),
    ("max_audible_gap_ms", "p.audible_gap_p50_ms <= ?"),
    ("min_cost_nano_usd", "p.cost_lower_bound_nano_usd >= ?"),
)


def _like_pattern(text: str) -> str:
    """``text`` as a ``LIKE … ESCAPE '\\'`` substring pattern that matches it literally."""

    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filter_sql(connection: DatabaseConnection, filters: CallFilters) -> tuple[str, list[Any]]:
    """The conditions every filter but ``max_cost_nano_usd`` adds, and their values."""

    clauses: list[str] = []
    values: list[Any] = []
    for name in ("agent", "outcome"):
        value = getattr(filters, name)
        if value is not None:
            clauses.append(f"p.{name} = ?")
            values.append(value)
    for tag in dict.fromkeys(filters.tags):
        clauses.append(
            "EXISTS (SELECT 1 FROM call_tags g WHERE g.project_id = c.project_id "
            "AND g.call_id = c.call_id AND g.tag = ?)"
        )
        values.append(tag)
    for name, clause in _RANGES:
        value = getattr(filters, name)
        if value is not None:
            clauses.append(clause)
            values.append(value)
    if filters.q is not None:
        like = connection.dialect.sql("transcript-search", sqlite="LIKE", postgresql="ILIKE")
        clauses.append(
            "EXISTS (SELECT 1 FROM call_transcripts x WHERE x.project_id = c.project_id "
            f"AND x.call_id = c.call_id AND x.text {like} ? ESCAPE '\\')"
        )
        values.append(_like_pattern(filters.q))
    return "".join(f" AND {clause}" for clause in clauses), values


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
class CallProjectionSummary:
    """A projection's summary columns (``call_projections``); unknown is ``None``."""

    revision: int
    agent: str | None
    framework: str | None
    outcome: str | None
    tags: tuple[str, ...]
    duration_ms: int | None
    turn_count: int
    measurable_turns: int
    reported_latency_p50_ms: int | None
    reported_latency_max_ms: int | None
    audible_gap_p50_ms: int | None
    audible_gap_max_ms: int | None
    cost_lower_bound_nano_usd: int | None
    cost_estimated_nano_usd: int | None
    cost_unknown_items: int
    analysis_state: str


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
    # ``call_projection.projection_state``; ``projection`` is ``None`` when ``none``.
    projection_state: str = "none"
    projection: CallProjectionSummary | None = None


@dataclass(frozen=True, slots=True)
class CallPage:
    items: tuple[CallSummary, ...]
    next_cursor: str | None
    # With a ``max_cost_nano_usd`` filter: the calls every other filter matches whose
    # cost as known is within it, left out because their cost has unknown items.
    excluded_incomplete: int | None = None


@dataclass(frozen=True, slots=True)
class CallDetail:
    """A call with its revisions, recordings and transcript."""

    summary: CallSummary
    revisions: tuple[CallRevision, ...]
    recordings: tuple[Recording, ...]
    transcript: tuple[TranscriptLine, ...]
    # ``call_materializer.transcript_coverage``.
    transcript_coverage: str
    # The projection's detail members (``call_projections.detail_json``), or ``None``.
    projection_detail: dict[str, Any] | None = field(default=None)


@dataclass(frozen=True, slots=True)
class CallSpan:
    """One stored span of a call: its scope and the preset-filtered span itself."""

    call_id: str
    scope_name: str
    span: Span


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


def _projection(row: Any, tags: tuple[str, ...]) -> CallProjectionSummary | None:
    if row["projection_revision"] is None:
        return None

    def known(name: str) -> int | None:
        value = row[name]
        return None if value is None else int(value)

    return CallProjectionSummary(
        revision=int(row["projection_revision"]),
        agent=row["agent"],
        framework=row["framework"],
        outcome=row["outcome"],
        tags=tags,
        duration_ms=known("duration_ms"),
        turn_count=int(row["turn_count"]),
        measurable_turns=int(row["measurable_turns"]),
        reported_latency_p50_ms=known("reported_latency_p50_ms"),
        reported_latency_max_ms=known("reported_latency_max_ms"),
        audible_gap_p50_ms=known("audible_gap_p50_ms"),
        audible_gap_max_ms=known("audible_gap_max_ms"),
        cost_lower_bound_nano_usd=known("cost_lower_bound_nano_usd"),
        cost_estimated_nano_usd=known("cost_estimated_nano_usd"),
        cost_unknown_items=int(row["cost_unknown_items"]),
        analysis_state=row["analysis_state"],
    )


def _state(row: Any, inputs: _ProjectInputs) -> str:
    stamps = (
        None
        if row["projection_revision"] is None
        else ProjectionStamps(
            source_seq=int(row["source_seq"]),
            price_version=int(row["projection_price_version"]),
            builtin_price_version=int(row["projection_builtin_price_version"]),
            projection_version=int(row["projection_version"]),
        )
    )
    return projection_state(
        stamps,
        ProjectionInputs(
            projection_seq=int(row["projection_seq"]),
            price_version=inputs.price_version,
            builtin_price_version=inputs.builtin_price_version,
        ),
    )


@dataclass(frozen=True, slots=True)
class _ProjectInputs:
    """The project's own price version, the built-in one, and its current preset."""

    price_version: int
    builtin_price_version: int
    capture_preset: str


def _project_inputs(connection: DatabaseConnection, project_id: str) -> _ProjectInputs:
    row = connection.execute(
        "SELECT price_version, capture_preset, "
        "(SELECT version FROM builtin_price_version WHERE singleton = 1) AS builtin_version "
        "FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return _ProjectInputs(
        price_version=int(row["price_version"]),
        builtin_price_version=int(row["builtin_version"]),
        capture_preset=row["capture_preset"],
    )


def _tags(
    connection: DatabaseConnection, project_id: str, call_ids: list[str]
) -> dict[str, tuple[str, ...]]:
    """The tags of the projected calls in ``call_ids``, sorted."""

    tags: dict[str, list[str]] = {}
    if call_ids:
        marks = ", ".join("?" * len(call_ids))
        for row in connection.execute(
            "SELECT call_id, tag FROM call_tags "
            f"WHERE project_id = ? AND call_id IN ({marks}) ORDER BY call_id, tag",
            (project_id, *call_ids),
        ).fetchall():
            tags.setdefault(row["call_id"], []).append(row["tag"])
    return {call_id: tuple(values) for call_id, values in tags.items()}


def _summaries(
    connection: DatabaseConnection, project_id: str, rows: list[Any], inputs: _ProjectInputs
) -> tuple[CallSummary, ...]:
    projected = [row["call_id"] for row in rows if row["projection_revision"] is not None]
    tags = _tags(connection, project_id, projected)
    return tuple(
        _summary(
            row,
            projection_state=_state(row, inputs),
            projection=_projection(row, tags.get(row["call_id"], ())),
        )
        for row in rows
    )


def _summary(
    row: Any, *, projection_state: str = "none", projection: CallProjectionSummary | None = None
) -> CallSummary:
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
        projection_state=projection_state,
        projection=projection,
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
        filters: CallFilters | None = None,
    ) -> CallPage:
        """One page of the project's calls that match ``filters``, newest first
        evidence first.

        ``cursor`` is the previous page's ``next_cursor``; a malformed one raises
        ``InvalidCursorError``. ``filters.q`` on a project whose current preset is
        not ``full`` raises ``ContentSearchForbiddenError``.
        """

        filters = filters or CallFilters()
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if state is not None and state not in CALL_STATES:
            raise ValueError("state must be open or closed")
        where = "c.project_id = ?"
        parameters: list[Any] = [project_id]
        if state is not None:
            where += " AND c.state = ?"
            parameters.append(state)
        page_where, page_parameters = where, list(parameters)
        if cursor is not None:
            first_evidence, call_id = decode_page_cursor(cursor)
            page_where += " AND (c.first_evidence_unix_nano, c.call_id) < (?, ?)"
            page_parameters.extend((first_evidence, call_id))
        cap = filters.max_cost_nano_usd
        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            inputs = _project_inputs(connection, project_id)
            if filters.q is not None and inputs.capture_preset != CAPTURE_PRESET_FULL:
                raise ContentSearchForbiddenError
            matched, matched_parameters = _filter_sql(connection, filters)
            if cap is not None:
                complete = f" AND p.cost_unknown_items = 0 AND {_COST_WITHIN}"
                page_where += matched + complete
                page_parameters += [*matched_parameters, cap, cap]
            else:
                page_where += matched
                page_parameters += matched_parameters
            rows = connection.execute(
                f"SELECT {_SUMMARY_COLUMNS} {_SUMMARY_FROM} WHERE {page_where} "
                "ORDER BY c.first_evidence_unix_nano DESC, c.call_id DESC LIMIT ?",
                (*page_parameters, limit + 1),
            ).fetchall()
            excluded = None
            if cap is not None:
                [excluded] = connection.execute(
                    f"SELECT count(*) {_SUMMARY_FROM} WHERE {where}{matched} "
                    f"AND p.cost_unknown_items > 0 AND {_COST_WITHIN}",
                    (*parameters, *matched_parameters, cap, cap),
                ).fetchone()
            items = _summaries(connection, project_id, rows[:limit], inputs)
        last = items[-1] if len(rows) > limit else None
        return CallPage(
            items=items,
            next_cursor=(
                None
                if last is None
                else encode_page_cursor(last.first_evidence_unix_nano, last.call_id)
            ),
            excluded_incomplete=None if excluded is None else int(excluded),
        )

    def get_call(self, project_id: str, call_id: str) -> CallDetail | None:
        """The call with its revisions, recordings, transcript and projection;
        ``None`` if the project has no such call."""

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
        lines_list = []
        for query in (TRANSCRIPT_SQL, LEGACY_TRANSCRIPT_SQL):
            for item in connection.execute(query, (project_id, call_id)).fetchall():
                text_json = item["text_json"]
                if text_json is None:
                    payload = item["legacy_payload"]
                    if payload is None:
                        continue
                    text_json = transcript_text_json_from_stored_span(
                        item["scope_name"], bytes(payload)
                    )
                if text_json is None:
                    continue
                lines_list.append(
                    stored_transcript_line(
                        text_json,
                        start_unix_nano=int(item["start_unix_nano"]),
                        end_unix_nano=int(item["end_unix_nano"]),
                        trace_id=bytes(item["trace_id"]),
                        span_id=bytes(item["span_id"]),
                    )
                )
        lines = tuple(ordered_transcript_lines(lines_list))
        inputs = _project_inputs(connection, project_id)
        [summary] = _summaries(connection, project_id, [row], inputs)
        detail = None
        if summary.projection is not None:
            [detail_json] = connection.execute(
                "SELECT detail_json FROM call_projections WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ).fetchone()
            detail = json.loads(detail_json)
        return CallDetail(
            summary=summary,
            revisions=revisions,
            recordings=RecordingStore.for_call(connection, project_id, call_id),
            transcript=lines,
            transcript_coverage=transcript_coverage(lines, inputs.capture_preset),
            projection_detail=detail,
        )

    def get_span(
        self, project_id: str, call_id: str, trace_id: bytes, span_id: bytes
    ) -> CallSpan | None:
        """One stored span of the call; ``None`` unless its trace is bound to the call."""

        from opentelemetry.proto.trace.v1.trace_pb2 import Span

        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            row = connection.execute(SPAN_SQL, (project_id, call_id, trace_id, span_id)).fetchone()
        if row is None:
            return None
        return CallSpan(
            call_id=call_id, scope_name=row["scope_name"], span=Span.FromString(row["payload"])
        )


# Optional ``pg_trgm`` index; keep it out of migrations when server roles lack extension privileges.
TRANSCRIPT_INDEX = "earshot_opt_call_transcripts_text_trgm"


class TranscriptIndexError(Exception):
    """The transcript index cannot be created here (its message says why)."""


def enable_transcript_index(store: IncidentStore) -> str:
    """Create the trigram index on ``call_transcripts.text`` (idempotent, under the
    store's write lock); its name."""

    from .storage import DATABASE_ERRORS

    with store._mutation("enable_transcript_index"), store._connect() as connection:
        if connection.dialect.name != "postgresql":
            raise TranscriptIndexError(
                "the transcript index is for Postgres catalogs; SQLite searches with LIKE"
            )
        connection.begin_write()
        try:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {TRANSCRIPT_INDEX} ON call_transcripts "
                "USING gin (text gin_trgm_ops)"
            )
        except DATABASE_ERRORS as error:
            connection.rollback()
            raise TranscriptIndexError(
                "Postgres refused pg_trgm or the index (the role needs CREATE on the "
                f"database, or an administrator to create the extension): {type(error).__name__}"
            ) from error
        connection.commit()
    return TRANSCRIPT_INDEX
