"""The call projection job: which calls are stale, and the compare-and-set write.

**Stale.** A call with a revision needs projecting when it has no projection, when
its ``projection_seq`` moved past the projection's ``source_seq`` (a revision was
finished, or an analysis result written, in the same transaction that bumped it),
when its project's own price version or the built-in one moved past the
projection's stamps, or when ``CALL_PROJECTION_VERSION`` did. Calls upgraded from a
release without projections have no row, so they are projected too.

**One pass per call.** In one read snapshot the job reads its latest revision's
evidence: the spans and stored recordings that revision was built from
(``first_revision`` at most its number; evidence that arrived after it waits for the
next revision), the recordings' analyses, whether that revision is final (its own
close, never an end signal that came after it), and the price rows; it computes the
projection outside every lock
(``call_projection.compute_projection``, pure); then one short mutation writes it
**only if** the call's ``projection_seq``, its latest revision and its stored
projection are still what the snapshot read.
Otherwise the result is discarded, the row stays stale, and a later pass
recomputes it: a counter, not a flag, so a change landing mid-computation is
never lost. The row is stamped with the versions the
snapshot read. A projection whose ``input_digest`` is unchanged only has its stamps
moved. The projection, its tags and its transcript lines are replaced together.

**Reprice.** A projection that is stale only because prices changed is repriced
from its stored usage (``usage_json``) and detail: no span is read or decoded.

**Built-in prices** (``pricing_defaults``) are seeded by the job's first pass at
every start; seeding is idempotent.

**Bounds.** Before a projection is written, every value is checked against its
catalog column: an integer outside the column's range (``Integer`` is 32-bit on
Postgres, ``BigInteger`` 64-bit) or text holding U+0000 refuses the whole projection
(``EARSHOT_PROJECTION_OUT_OF_RANGE``), so SQLite and Postgres store the same rows and
nothing is clamped. ``compute_projection`` already makes the values it can reach
unknown; this check is the boundary.

**Failures are the call's, never the job's.** A call whose stored inputs cannot be
read or projected (``EARSHOT_PROJECTION_INPUT_INVALID``), whose projection is out of
range, or whose write the database refuses (``EARSHOT_PROJECTION_WRITE_REFUSED``) is
logged (``EARSHOT_PROJECTION_FAILED``, with the code) and set aside: tried again after
``PROJECTION_RETRY_SECONDS`` (1, 4, then every 16 minutes), or as soon as the call
changes. The job moves on to the next call. The record is the job's memory
(``CallProjector.failure``), so a restart tries the call once more. A busy,
unreachable or closed store is not the call's failure: the pass ends, and a later
tick resumes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .call_capture import stored_span
from .call_projection import (
    CALL_PROJECTION_VERSION,
    CallSource,
    Projection,
    RecordingSource,
    canonical_json,
    compact_json,
    compute_projection,
    reprice,
)
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .storage import DatabaseError, DatabaseUnavailableError, StorageBusyError, StorageError
from .storage_calls import call_spans_sql
from .storage_closer import SNAPSHOT_RESOURCES_SQL
from .storage_prices import PriceStore, price_rows

if TYPE_CHECKING:
    from .call_cost import PriceRow
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection

_LOGGER = logging.getLogger(__name__)
PROJECTION_BATCH_SIZE = 20
# How long a failed call is set aside before each retry (the last repeats).
PROJECTION_RETRY_SECONDS = (60, 240, 960)
_SECOND = 1_000_000_000
# Stable failure codes (module docstring).
PROJECTION_INPUT_INVALID = "EARSHOT_PROJECTION_INPUT_INVALID"
PROJECTION_OUT_OF_RANGE = "EARSHOT_PROJECTION_OUT_OF_RANGE"
PROJECTION_WRITE_REFUSED = "EARSHOT_PROJECTION_WRITE_REFUSED"
# The summary columns a projection row holds, in ``call_projections`` order.
_SUMMARY_COLUMNS = (
    "agent",
    "framework",
    "outcome",
    "started_unix_nano",
    "duration_ms",
    "turn_count",
    "reported_latency_p50_ms",
    "reported_latency_max_ms",
    "measurable_turns",
    "audible_gap_p50_ms",
    "audible_gap_max_ms",
    "talk_time_user_ms",
    "talk_time_agent_ms",
    "overlap_ms",
    "barge_in_count",
    "talk_over_count",
    "long_silence_count",
    "dead_air_ms",
    "cost_lower_bound_nano_usd",
    "cost_estimated_nano_usd",
    "cost_unknown_items",
    "provider_cost_nano_usd",
    "provider_cost_basis",
    "analysis_state",
)
# The range of each integer the projection writes, as ``storage_schema`` declares its
# column: ``Integer`` is 32-bit on Postgres, ``BigInteger`` 64-bit, and every one but
# the evidence start is CHECKed >= 0. Text columns cannot hold U+0000.
_INT32 = (0, 2**31 - 1)
_INT64 = (0, 2**63 - 1)
_SIGNED_INT64 = (-(2**63), 2**63 - 1)
COLUMN_RANGES: dict[str, tuple[int, int]] = {
    "started_unix_nano": _SIGNED_INT64,
    "duration_ms": _INT64,
    "turn_count": _INT32,
    "reported_latency_p50_ms": _INT64,
    "reported_latency_max_ms": _INT64,
    "measurable_turns": _INT32,
    "audible_gap_p50_ms": _INT64,
    "audible_gap_max_ms": _INT64,
    "talk_time_user_ms": _INT64,
    "talk_time_agent_ms": _INT64,
    "overlap_ms": _INT64,
    "barge_in_count": _INT32,
    "talk_over_count": _INT32,
    "long_silence_count": _INT32,
    "dead_air_ms": _INT64,
    "cost_lower_bound_nano_usd": _INT64,
    "cost_estimated_nano_usd": _INT64,
    "cost_unknown_items": _INT32,
    "provider_cost_nano_usd": _INT64,
}
TRANSCRIPT_TIME_RANGE = _SIGNED_INT64
_TEXT_COLUMNS = ("agent", "framework", "outcome", "provider_cost_basis", "analysis_state")
_STALE_SQL = (
    "SELECT c.project_id, c.call_id, c.projection_seq FROM calls c "
    "JOIN projects pr ON pr.project_id = c.project_id "
    "LEFT JOIN call_projections p ON p.project_id = c.project_id AND p.call_id = c.call_id "
    "WHERE c.latest_revision IS NOT NULL AND pr.lifecycle_state = 'active' AND ("
    "p.call_id IS NULL OR p.source_seq < c.projection_seq OR p.projection_version < ? "
    "OR p.price_version < pr.price_version OR p.builtin_price_version < ?) "
    "AND (c.project_id, c.call_id) > (?, ?) "
    "ORDER BY c.project_id, c.call_id LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one call's projection pass read, in one read snapshot."""

    project_id: str
    call_id: str
    projection_seq: int
    revision: int
    price_version: int
    builtin_price_version: int
    prices: tuple[PriceRow, ...]
    first_evidence_unix_nano: int
    # ``reprice``: only prices moved, so ``source`` is ``None`` and the stored
    # usage and detail are reused.
    kind: str
    source: CallSource | None
    stored_digest: str | None
    stored_usage: list[dict[str, Any]] | None = None
    stored_detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Computed:
    projection: Projection | None
    # For a reprice: the new cost columns and detail.
    cost_columns: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] | None = None
    digest: str = ""


@dataclass(frozen=True, slots=True)
class ProjectionPass:
    projected: int
    more: bool


@dataclass(frozen=True, slots=True)
class ProjectionFailure:
    """Why a call's projection failed at ``projection_seq``, how many passes in a row
    failed there, and when the job tries it again."""

    projection_seq: int
    code: str
    attempts: int
    retry_unix_nano: int


class ProjectionOutOfRangeError(ValueError):
    """A computed value its catalog column cannot hold (``column`` names it)."""

    def __init__(self, column: str) -> None:
        super().__init__(f"{column} is outside its catalog column's range")
        self.column = column


def _in_range(value: Any, bounds: tuple[int, int]) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and bounds[0] <= value <= bounds[1]
    )


def _check_columns(columns: dict[str, Any]) -> None:
    for name, bounds in COLUMN_RANGES.items():
        if name in columns and not _in_range(columns[name], bounds):
            raise ProjectionOutOfRangeError(name)
    for name in _TEXT_COLUMNS:
        if "\x00" in (columns.get(name) or ""):
            raise ProjectionOutOfRangeError(name)


def check_bounds(projection: Projection) -> None:
    """Raise ``ProjectionOutOfRangeError`` unless the catalog can store every value
    of ``projection`` as it is (module docstring)."""

    _check_columns(projection.columns)
    if any("\x00" in tag for tag in projection.tags):
        raise ProjectionOutOfRangeError("call_tags.tag")
    for row in projection.transcript:
        if "\x00" in row.text:
            raise ProjectionOutOfRangeError("call_transcripts.text")
        if not (
            _in_range(row.start_ms, TRANSCRIPT_TIME_RANGE)
            and _in_range(row.end_ms, TRANSCRIPT_TIME_RANGE)
        ):
            raise ProjectionOutOfRangeError("call_transcripts.start_ms")


def _transient(error: BaseException) -> bool:
    """Whether ``error`` is the store's (busy, unreachable, closed) rather than the
    call's own (its inputs, or a statement the database refused for its values)."""

    if isinstance(error, sqlite3.OperationalError | DatabaseUnavailableError):
        return True
    if isinstance(error, sqlite3.Error | DatabaseError):
        return False
    return isinstance(error, StorageError)


def _recordings(connection: DatabaseConnection, project_id: str, call_id: str, revision: int):
    rows = connection.execute(
        "SELECT r.recording_id, r.speaker, r.channel_speakers_json, r.content_type, "
        "r.started_at_unix_nano, r.duration_ms, a.state, a.error_code, "
        "a.peaks_blob_key, a.rendition_blob_key, "
        "CASE WHEN a.state = 'stored' THEN a.result_json END AS result_json "
        "FROM recordings r LEFT JOIN audio_analysis a ON a.project_id = r.project_id "
        "AND a.recording_id = r.recording_id "
        "WHERE r.project_id = ? AND r.call_id = ? AND r.state = 'stored' "
        "AND r.first_revision <= ? ORDER BY r.recording_id",
        (project_id, call_id, revision),
    ).fetchall()
    return tuple(
        RecordingSource(
            recording_id=row["recording_id"],
            speaker=row["speaker"],
            channel_speakers=(
                None
                if row["channel_speakers_json"] is None
                else tuple(json.loads(row["channel_speakers_json"]))
            ),
            content_type=row["content_type"],
            started_at_unix_nano=int(row["started_at_unix_nano"]),
            duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
            analysis_state=row["state"],
            analysis_error_code=row["error_code"],
            result=None if row["result_json"] is None else json.loads(row["result_json"]),
            has_peaks=row["peaks_blob_key"] is not None,
            holds_rendition=row["rendition_blob_key"] is not None,
        )
        for row in rows
    )


_REVISION_SPANS_SQL = call_spans_sql(
    "s.trace_id, s.span_id, s.resource_digest, s.payload", condition="s.first_revision <= ?"
)


def _spans(connection: DatabaseConnection, project_id: str, call_id: str, revision: int):
    resources = {
        bytes(row["resource_digest"]): bytes(row["payload"])
        for row in connection.execute(
            SNAPSHOT_RESOURCES_SQL, (project_id, project_id, call_id)
        ).fetchall()
    }
    return tuple(
        stored_span(bytes(row["payload"]), resources[bytes(row["resource_digest"])])
        for row in connection.execute(
            _REVISION_SPANS_SQL, (project_id, call_id, revision)
        ).fetchall()
    )


class CallProjector:
    """The projection job's operations on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store
        # The calls whose last pass failed, by (project, call).
        self._failed: dict[tuple[str, str], ProjectionFailure] = {}

    def failure(self, project_id: str, call_id: str) -> ProjectionFailure | None:
        """Why the call's last projection pass failed; ``None`` when it did not."""

        return self._failed.get((project_id, call_id))

    # --- stale calls ------------------------------------------------------------------------

    def stale(self, *, limit: int, after: tuple[str, str] = ("", "")) -> list[tuple[str, str, int]]:
        """Up to ``limit`` calls needing a projection, after ``after`` in key order, as
        ``(project_id, call_id, projection_seq)``."""

        with self.store._listing_connection() as connection:
            connection.begin_read()
            [builtin] = connection.execute(
                "SELECT version FROM builtin_price_version WHERE singleton = 1"
            ).fetchone()
            return [
                (row["project_id"], row["call_id"], int(row["projection_seq"]))
                for row in connection.execute(
                    _STALE_SQL, (CALL_PROJECTION_VERSION, int(builtin), *after, limit)
                ).fetchall()
            ]

    # --- one call ---------------------------------------------------------------------------

    def snapshot(self, project_id: str, call_id: str) -> Snapshot | None:
        """Everything the call's projection reads, in one read snapshot; ``None`` when
        the call has no revision (or no longer exists)."""

        with self.store._listing_connection() as connection:
            connection.begin_read()
            call = connection.execute(
                "SELECT c.projection_seq, c.latest_revision, c.first_evidence_unix_nano, "
                "r.finality, pr.price_version, "
                "(SELECT version FROM builtin_price_version WHERE singleton = 1) AS builtin, "
                "p.source_seq, p.projection_version, p.input_digest "
                "FROM calls c JOIN projects pr ON pr.project_id = c.project_id "
                "LEFT JOIN call_revisions r ON r.project_id = c.project_id "
                "AND r.call_id = c.call_id AND r.revision = c.latest_revision "
                "LEFT JOIN call_projections p ON p.project_id = c.project_id "
                "AND p.call_id = c.call_id "
                "WHERE c.project_id = ? AND c.call_id = ?",
                (project_id, call_id),
            ).fetchone()
            if call is None or call["latest_revision"] is None:
                return None
            seq = int(call["projection_seq"])
            prices = tuple(price_rows(connection, project_id))
            common = {
                "project_id": project_id,
                "call_id": call_id,
                "projection_seq": seq,
                "revision": int(call["latest_revision"]),
                "price_version": int(call["price_version"]),
                "builtin_price_version": int(call["builtin"]),
                "prices": prices,
                "first_evidence_unix_nano": int(call["first_evidence_unix_nano"]),
                "stored_digest": call["input_digest"],
            }
            only_prices = (
                call["source_seq"] is not None
                and int(call["source_seq"]) == seq
                and int(call["projection_version"]) == CALL_PROJECTION_VERSION
            )
            if only_prices:
                stored = connection.execute(
                    "SELECT usage_json, detail_json FROM call_projections "
                    "WHERE project_id = ? AND call_id = ?",
                    (project_id, call_id),
                ).fetchone()
                if stored["usage_json"] is not None:
                    return Snapshot(
                        **common,
                        kind="reprice",
                        source=None,
                        stored_usage=json.loads(stored["usage_json"]),
                        stored_detail=json.loads(stored["detail_json"]),
                    )
            source = CallSource(
                project_id=project_id,
                call_id=call_id,
                revision=int(call["latest_revision"]),
                first_evidence_unix_nano=int(call["first_evidence_unix_nano"]),
                ended=call["finality"] == "final",
                spans=_spans(connection, project_id, call_id, int(call["latest_revision"])),
                recordings=_recordings(
                    connection, project_id, call_id, int(call["latest_revision"])
                ),
            )
            return Snapshot(**common, kind="full", source=source)

    @staticmethod
    def compute(snapshot: Snapshot) -> Computed:
        """The projection a snapshot gives (pure; outside every lock)."""

        if snapshot.kind == "reprice":
            assert snapshot.stored_usage is not None and snapshot.stored_detail is not None
            columns, detail = reprice(
                snapshot.stored_usage,
                snapshot.stored_detail,
                snapshot.prices,
                project_id=snapshot.project_id,
                at_unix_nano=snapshot.first_evidence_unix_nano,
            )
            _check_columns(columns)
            priced = canonical_json([columns, detail["cost_items"]])
            digest = hashlib.sha256(f"{snapshot.stored_digest}:{priced}".encode()).hexdigest()
            return Computed(projection=None, cost_columns=columns, detail=detail, digest=digest)
        assert snapshot.source is not None
        projection = compute_projection(snapshot.source, snapshot.prices)
        check_bounds(projection)
        return Computed(projection=projection, digest=projection.input_digest())

    def write(
        self, snapshot: Snapshot, computed: Computed, *, budget_s: float | None = None
    ) -> bool:
        """Store the projection if the call is still as the snapshot read it and its
        project is not deleting."""

        now = self.store._clock.unix_nano()
        stamps = {
            "source_seq": snapshot.projection_seq,
            "price_version": snapshot.price_version,
            "builtin_price_version": snapshot.builtin_price_version,
            "projection_version": CALL_PROJECTION_VERSION,
            "input_digest": computed.digest,
            "updated_unix_nano": now,
        }
        scope = (snapshot.project_id, snapshot.call_id)
        with (
            self.store._mutation("write_call_projection", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            # A project deleting since the snapshot gets no projection.
            call = connection.execute(
                "SELECT c.projection_seq, c.latest_revision FROM calls c "
                "JOIN projects p ON p.project_id = c.project_id "
                "WHERE c.project_id = ? AND c.call_id = ? AND p.lifecycle_state = 'active'",
                scope,
            ).fetchone()
            if (
                call is None
                or int(call["projection_seq"]) != snapshot.projection_seq
                or call["latest_revision"] != snapshot.revision
            ):
                connection.rollback()
                return False
            stored = connection.execute(
                "SELECT input_digest FROM call_projections WHERE project_id = ? AND call_id = ?",
                scope,
            ).fetchone()
            if (None if stored is None else stored["input_digest"]) != snapshot.stored_digest:
                # Another projection was written since the snapshot.
                connection.rollback()
                return False
            if computed.projection is None:  # a reprice
                values = {
                    **stamps,
                    **computed.cost_columns,
                    "detail_json": compact_json(computed.detail),
                }
                connection.execute(
                    f"UPDATE call_projections SET {', '.join(f'{name} = ?' for name in values)} "
                    "WHERE project_id = ? AND call_id = ?",
                    (*values.values(), *scope),
                )
            elif stored is not None and stored["input_digest"] == computed.digest:
                connection.execute(
                    f"UPDATE call_projections SET {', '.join(f'{name} = ?' for name in stamps)} "
                    "WHERE project_id = ? AND call_id = ?",
                    (*stamps.values(), *scope),
                )
            else:
                self._replace(connection, snapshot, computed.projection, stamps)
            connection.commit()
        return True

    @staticmethod
    def _replace(
        connection: DatabaseConnection,
        snapshot: Snapshot,
        projection: Projection,
        stamps: dict[str, Any],
    ) -> None:
        scope = (snapshot.project_id, snapshot.call_id)
        # Its tags and transcript lines go with it (ON DELETE CASCADE).
        connection.execute(
            "DELETE FROM call_projections WHERE project_id = ? AND call_id = ?", scope
        )
        row = {
            "project_id": snapshot.project_id,
            "call_id": snapshot.call_id,
            "revision": snapshot.revision,
            **{name: projection.columns[name] for name in _SUMMARY_COLUMNS},
            **stamps,
            "usage_json": compact_json(projection.usage),
            "detail_json": compact_json(projection.detail),
        }
        connection.execute(
            f"INSERT INTO call_projections({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        connection.executemany(
            "INSERT INTO call_tags(project_id, call_id, tag) VALUES (?, ?, ?)",
            [(*scope, tag) for tag in projection.tags],
        )
        connection.executemany(
            "INSERT INTO call_transcripts(project_id, call_id, line_no, speaker, timing, "
            "start_ms, end_ms, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    *scope,
                    line.line_no,
                    line.speaker,
                    line.timing,
                    line.start_ms,
                    line.end_ms,
                    line.text,
                )
                for line in projection.transcript
            ],
        )

    def project_call(
        self,
        project_id: str,
        call_id: str,
        *,
        seq: int | None = None,
        budget_s: float | None = None,
    ) -> bool:
        """One pass for one call; ``True`` if a projection was written.

        A failure that is the call's own (module docstring) is recorded against its
        ``projection_seq`` (``seq``, as ``stale`` read it, else the snapshot's) and
        logged, and the pass returns ``False``. The store's own errors propagate.
        """

        snapshot = None
        try:
            snapshot = self.snapshot(project_id, call_id)
            if snapshot is None:
                return False
            computed = self.compute(snapshot)
        except Exception as error:
            if _transient(error):
                raise
            code = (
                PROJECTION_OUT_OF_RANGE
                if isinstance(error, ProjectionOutOfRangeError)
                else PROJECTION_INPUT_INVALID
            )
            self._fail(project_id, call_id, seq, snapshot, code, error)
            return False
        try:
            written = self.write(snapshot, computed, budget_s=budget_s)
        except Exception as error:
            if _transient(error):
                raise
            self._fail(project_id, call_id, seq, snapshot, PROJECTION_WRITE_REFUSED, error)
            return False
        self._failed.pop((project_id, call_id), None)
        return written

    def _fail(
        self,
        project_id: str,
        call_id: str,
        seq: int | None,
        snapshot: Snapshot | None,
        code: str,
        error: BaseException,
    ) -> None:
        if seq is None and snapshot is not None:
            seq = snapshot.projection_seq
        delay = PROJECTION_RETRY_SECONDS[0]
        if seq is not None:
            previous = self._failed.get((project_id, call_id))
            attempts = (
                previous.attempts + 1
                if previous is not None and previous.projection_seq == seq
                else 1
            )
            delay = PROJECTION_RETRY_SECONDS[min(attempts, len(PROJECTION_RETRY_SECONDS)) - 1]
            self._failed[(project_id, call_id)] = ProjectionFailure(
                projection_seq=seq,
                code=code,
                attempts=attempts,
                retry_unix_nano=self.store._clock.unix_nano() + delay * _SECOND,
            )
        _LOGGER.error(
            "EARSHOT_PROJECTION_FAILED: call %s could not be projected (%s, %s); "
            "tried again in %d s or when it changes",
            call_id,
            code,
            type(error).__name__,
            delay,
        )

    def _set_aside(self, project_id: str, call_id: str, seq: int, now: int) -> bool:
        failure = self._failed.get((project_id, call_id))
        return (
            failure is not None and failure.projection_seq == seq and now < failure.retry_unix_nano
        )

    def run_pending(
        self, *, limit: int = PROJECTION_BATCH_SIZE, budget_s: float | None = None
    ) -> ProjectionPass:
        """Project up to ``limit`` stale calls; ``more`` when others may be waiting."""

        projected = 0
        after = ("", "")
        examined = 0
        now = self.store._clock.unix_nano()
        while True:
            batch = self.stale(limit=limit, after=after)
            if not batch:
                return ProjectionPass(projected, more=False)
            for project_id, call_id, seq in batch:
                after = (project_id, call_id)
                # A call that failed is skipped, unread, until its retry is due or
                # it changes.
                if self._set_aside(project_id, call_id, seq, now):
                    continue
                if self.project_call(project_id, call_id, seq=seq, budget_s=budget_s):
                    projected += 1
                examined += 1
                if examined >= limit:
                    return ProjectionPass(projected, more=True)


class _ProjectionJob:
    """``projection_job``'s job: seeds the built-in prices once, then projects."""

    def __init__(self, projector: CallProjector, builtin_prices: tuple) -> None:
        self.projector = projector
        self.builtin_prices = builtin_prices
        self._seeded = False

    async def __call__(self, now_unix_nano: int) -> float | None:
        try:
            if not self._seeded:
                await asyncio.to_thread(
                    PriceStore(self.projector.store).seed_builtins,
                    self.builtin_prices,
                    budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
                )
                self._seeded = True
            done = await asyncio.to_thread(
                self.projector.run_pending, budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS
            )
        except StorageBusyError:
            return None
        return 0.0 if done.more else None


def projection_job(projector: CallProjector) -> MaintenanceJob:
    """A maintenance job: seed the built-in prices at start, then keep every call's
    projection current, a bounded batch at a time."""

    from .pricing_defaults import BUILTIN_PRICES

    return _ProjectionJob(projector, BUILTIN_PRICES)
