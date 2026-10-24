"""Purging calls: a call purge, call retention, and a project's call evidence.

**A call purge** (``CallPurger.purge_call``: ``DELETE /v1/calls/{call_id}`` and
``earshot call purge``) runs in one ``_mutation`` and one write transaction. It
deletes the ``calls`` row, its traces and spans, its recordings rows and its
``call_revisions``; purges every revision incident through the incident purge
(``tombstones``); journals the stored recordings' blobs in
``pending_blob_deletions``; deletes the project's resources no span references
any more; increments the key's generation in ``call_key_generations``; writes
``call_tombstones`` for the key and for each of the call's traces, live for
``LONG_QUIET + 24 h``; and on SQLite records a scrub (``purge_generation``).
After commit it deletes the revision artifacts and the journaled blobs (a failed
blob delete stays journaled; the ``blob-deletions`` job retries it), then scrubs
SQLite's deleted pages.

**Purged keys**. While a live tombstone covers a key, its evidence is
dropped and counted (``purged_call_evidence_dropped``) by ``CallStore`` and
``RecordingStore``; so are spans of the purged call's traces. Because the purge
already incremented the key's generation, evidence after the tombstone expires
starts a new call with a new ID; the purged call and its tombstoned revision IDs
are never rebuilt.

**Retention**. A revision never expires alone (the incident reaper and
the reads that expire an incident skip call revisions). A closed call whose
**latest** revision's retention deadline has passed is purged whole, as above,
in bounded batches (``purge_expired_calls``, ``call_retention_job``). An open
call has no revision, so it never expires. Call revisions carry no retention
policy (their deadline is NULL), so a call lives until it is purged or its
project is deleted. The same job deletes expired ``call_tombstones``
(``collect_expired_tombstones``); key generations stay.

**Project deletion** calls ``delete_project_calls`` from
``IncidentStore.delete_project_data`` once the project's incidents are gone: it
deletes calls with all their evidence, then parked traces, in bounded batches;
``delete_project_data`` then deletes the project's resources, generations,
tombstones and counters, and waits for its journaled blobs to be deleted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .storage import DATABASE_ERRORS, StorageBusyError, StorageError, _expired_sql

if TYPE_CHECKING:
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore
    from .storage_calls import CallStore
    from .storage_database import DatabaseConnection
    from .storage_recordings import RecordingStore

_SECOND = 1_000_000_000
# A purged key stays tombstoned this long after LONG_QUIET: enough to absorb a
# client's retries and late batches of the purged call.
TOMBSTONE_GRACE_NANO = 24 * 3600 * _SECOND
# Calls one retention batch purges at most; each is its own transaction.
CALL_EXPIRY_BATCH_SIZE = 20
# How long the retention job waits after a batch that left no expired call.
CALL_RETENTION_INTERVAL_SECONDS = 5.0
# Expired ``call_tombstones`` one collection deletes at most, and how often the
# retention job looks for them when the last collection was not a full batch.
TOMBSTONE_COLLECTION_BATCH_SIZE = 1000
TOMBSTONE_COLLECTION_INTERVAL_SECONDS = 60.0
# Bound parameters per IN list (under SQLite's oldest default limit, 999).
_IN_LIST_SIZE = 400
# Closed calls whose latest revision's incident has passed its retention deadline
# (binds: the deadline comparison's three ``_expired_sql`` parameters).
_EXPIRED_CALLS = (
    "FROM incidents i JOIN call_revisions r ON r.bundle_id = i.bundle_id "
    "JOIN calls c ON c.project_id = r.project_id AND c.call_id = r.call_id "
    "AND c.latest_revision = r.revision "
    f"WHERE {_expired_sql('i.expires_at_unix_nano')} AND c.state = 'closed'"
)


class CallNotFoundError(StorageError):
    """The project has no call with that ID."""

    code = "EARSHOT_CALL_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("call not found")


@dataclass(frozen=True, slots=True)
class DeletedCalls:
    """What ``delete_calls`` removed and still owes after commit."""

    # ``(key_fingerprint, generation)`` of each deleted call.
    keys: tuple[tuple[str, int], ...] = ()
    trace_ids: tuple[bytes, ...] = ()
    # The revision incidents of the deleted calls.
    bundle_ids: tuple[str, ...] = ()
    # Blobs of stored recordings, journaled in ``pending_blob_deletions``.
    blob_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CallPurge:
    """One purged call."""

    project_id: str
    call_id: str
    revisions: int
    blob_keys: tuple[str, ...]


def _chunks(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), _IN_LIST_SIZE):
        yield values[start : start + _IN_LIST_SIZE]


def delete_calls(
    connection: DatabaseConnection, project_id: str, call_ids: Sequence[str], *, now: int
) -> DeletedCalls:
    """Delete the calls and every row of their evidence, in FK-safe order.

    Spans, traces, recordings, revisions, then the calls; the stored recordings'
    blobs are journaled (``pending_blob_deletions``), and the project's resources
    no span references any more are deleted. Revision incidents are the caller's
    (the incident purge, or project deletion's incident batches). Runs in the
    caller's write transaction.
    """

    from .storage_calls import CallStore

    keys: list[tuple[str, int]] = []
    trace_ids: list[bytes] = []
    bundle_ids: list[str] = []
    blob_keys: list[str] = []
    digests: set[bytes] = set()
    for chunk in _chunks(sorted(set(call_ids))):
        marks = ", ".join("?" * len(chunk))
        scope = (project_id, *chunk)
        in_calls = f"project_id = ? AND call_id IN ({marks})"
        keys.extend(
            (row["key_fingerprint"], int(row["generation"]))
            for row in connection.execute_unprepared(
                f"SELECT key_fingerprint, generation FROM calls WHERE {in_calls} ORDER BY call_id",
                scope,
            ).fetchall()
        )
        traces = [
            bytes(row["trace_id"])
            for row in connection.execute_unprepared(
                f"SELECT trace_id FROM call_traces WHERE {in_calls} ORDER BY trace_id", scope
            ).fetchall()
        ]
        trace_ids.extend(traces)
        bundle_ids.extend(
            row["bundle_id"]
            for row in connection.execute_unprepared(
                f"SELECT bundle_id FROM call_revisions WHERE {in_calls} ORDER BY bundle_id", scope
            ).fetchall()
        )
        blob_keys.extend(
            row["blob_key"]
            for row in connection.execute_unprepared(
                f"SELECT blob_key FROM recordings WHERE {in_calls} AND blob_key IS NOT NULL "
                "ORDER BY blob_key",
                scope,
            ).fetchall()
        )
        # What their analyses derived (peaks, renditions): the rows go with the
        # recordings (ON DELETE CASCADE), so their keys are journaled first.
        blob_keys.extend(
            key
            for row in connection.execute_unprepared(
                "SELECT a.peaks_blob_key, a.rendition_blob_key FROM audio_analysis a "
                "JOIN recordings r ON r.project_id = a.project_id "
                "AND r.recording_id = a.recording_id "
                f"WHERE r.project_id = ? AND r.call_id IN ({marks})",
                scope,
            ).fetchall()
            for key in (row["peaks_blob_key"], row["rendition_blob_key"])
            if key is not None
        )
        digests.update(_delete_traces(connection, project_id, traces))
        for table in ("recordings", "call_revisions", "calls"):
            connection.execute_unprepared(f"DELETE FROM {table} WHERE {in_calls}", scope)
    connection.executemany(
        "INSERT INTO pending_blob_deletions(project_id, blob_key, queued_at_unix_nano, attempts) "
        "VALUES (?, ?, ?, 0) ON CONFLICT(project_id, blob_key) DO NOTHING",
        [(project_id, key, now) for key in blob_keys],
    )
    CallStore._delete_unreferenced_resources(connection, project_id, digests)
    return DeletedCalls(tuple(keys), tuple(trace_ids), tuple(bundle_ids), tuple(blob_keys))


def _delete_traces(
    connection: DatabaseConnection, project_id: str, trace_ids: Sequence[bytes]
) -> set[bytes]:
    """Delete the traces and their spans; the resource digests their spans used."""

    digests: set[bytes] = set()
    for chunk in _chunks(trace_ids):
        marks = ", ".join("?" * len(chunk))
        scope = (project_id, *chunk)
        in_traces = f"project_id = ? AND trace_id IN ({marks})"
        digests.update(
            bytes(row["resource_digest"])
            for row in connection.execute_unprepared(
                f"SELECT DISTINCT resource_digest FROM spans WHERE {in_traces}", scope
            ).fetchall()
        )
        connection.execute_unprepared(f"DELETE FROM spans WHERE {in_traces}", scope)
        connection.execute_unprepared(f"DELETE FROM call_traces WHERE {in_traces}", scope)
    return digests


def delete_project_calls(
    connection: DatabaseConnection, project_id: str, *, limit: int, now: int
) -> bool:
    """One bounded batch of a deleted project's calls (with all their evidence:
    their projections, tags and transcript lines go with them), then of its parked
    traces, then of its own prices (never the built-in ones). ``True`` once none is
    left. Runs in the caller's write transaction, after the project's incidents are
    gone."""

    call_ids = [
        row["call_id"]
        for row in connection.execute(
            "SELECT call_id FROM calls WHERE project_id = ? ORDER BY call_id LIMIT ?",
            (project_id, limit),
        ).fetchall()
    ]
    if call_ids:
        delete_calls(connection, project_id, call_ids, now=now)
    parked = [
        bytes(row["trace_id"])
        for row in connection.execute(
            "SELECT trace_id FROM call_traces WHERE project_id = ? AND call_id IS NULL "
            "ORDER BY trace_id LIMIT ?",
            (project_id, limit),
        ).fetchall()
    ]
    _delete_traces(connection, project_id, parked)
    connection.execute(
        "DELETE FROM prices WHERE id IN "
        "(SELECT id FROM prices WHERE project_id = ? ORDER BY id LIMIT ?)",
        (project_id, limit),
    )
    return not any(
        connection.execute(
            f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1", (project_id,)
        ).fetchone()
        for table in ("calls", "call_traces", "prices")
    )


class CallPurger:
    """Call purge and call retention on one ``IncidentStore``."""

    def __init__(
        self,
        store: IncidentStore,
        *,
        calls: CallStore,
        recordings: RecordingStore | None = None,
    ) -> None:
        self.store = store
        self.calls = calls
        self.recordings = recordings

    def purge_call(
        self, project_id: str, call_id: str, *, budget_s: float | None = None
    ) -> CallPurge:
        """Purge the call and all its evidence; ``CallNotFoundError`` if the
        project has no such call."""

        with self.store._mutation("purge_call", budget_s=budget_s):
            purge = self._purge_locked(project_id, call_id, expired_at=None)
            if purge is None:
                raise CallNotFoundError
            self._cleanup_artifacts_locked(1)
            scrub = self.store._scrub_required_locked()
        self._delete_blobs((purge,))
        if scrub:
            self._scrub(compact=True)
        return purge

    def purge_expired_calls(
        self,
        *,
        now_unix_nano: int | None = None,
        limit: int = CALL_EXPIRY_BATCH_SIZE,
        budget_s: float | None = None,
    ) -> int:
        """Purge up to ``limit`` closed calls whose latest revision has expired,
        each in its own transaction; how many were purged.

        A read finds the candidates first, so an idle pass takes no write lock;
        each is checked again under the lock before it is purged.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        now = str(self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano)
        with self.store._connect() as connection:
            candidates = self._expired_calls(connection, now, limit)
        if not candidates:
            return 0
        purged: list[CallPurge] = []
        with self.store._mutation("purge_expired_calls", budget_s=budget_s):
            for project_id, call_id in candidates:
                purge = self._purge_locked(project_id, call_id, expired_at=now)
                if purge is not None:
                    purged.append(purge)
            self._cleanup_artifacts_locked(max(1, len(purged)))
            scrub = self.store._scrub_required_locked()
        self._delete_blobs(purged)
        if scrub:
            # VACUUM once the backlog drains (and at most every compaction
            # interval), as the incident reaper does; until then, checkpoint.
            self._scrub(compact=len(candidates) < limit and self.store._compaction_due())
        return len(purged)

    def collect_expired_tombstones(
        self,
        *,
        now_unix_nano: int | None = None,
        limit: int = TOMBSTONE_COLLECTION_BATCH_SIZE,
        budget_s: float | None = None,
    ) -> int:
        """Delete up to ``limit`` expired ``call_tombstones``, key and trace ones; how many.

        An expired tombstone protects nothing: lookups ignore it, and the purge
        already moved the key to its next generation (``call_key_generations`` rows
        are never collected, so a purged call is never rebuilt). A read finds them
        first, so an idle pass takes no write lock.
        """

        now = self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano
        with self.store._connect() as connection:
            expired = [
                (row["project_id"], row["key_fingerprint"], int(row["generation"]), now)
                for row in connection.execute(
                    "SELECT project_id, key_fingerprint, generation FROM call_tombstones "
                    "WHERE expires_at_unix_nano <= ? ORDER BY expires_at_unix_nano LIMIT ?",
                    (now, limit),
                ).fetchall()
            ]
        if not expired:
            return 0
        with (
            self.store._mutation("collect_call_tombstones", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            # Still expired: a purge since the read may have renewed one.
            connection.executemany(
                "DELETE FROM call_tombstones WHERE project_id = ? AND key_fingerprint = ? "
                "AND generation = ? AND expires_at_unix_nano <= ?",
                expired,
            )
            connection.commit()
        return len(expired)

    @staticmethod
    def _expired_calls(connection: DatabaseConnection, now: str, limit: int) -> list[Any]:
        """Closed calls whose latest revision's incident has passed its deadline,
        earliest deadline first (read from the incidents' expiry index)."""

        return [
            (row["project_id"], row["call_id"])
            for row in connection.execute(
                f"SELECT c.project_id, c.call_id {_EXPIRED_CALLS} "
                "ORDER BY length(i.expires_at_unix_nano), i.expires_at_unix_nano, "
                "c.project_id, c.call_id LIMIT ?",
                (len(now), len(now), now, limit),
            ).fetchall()
        ]

    @staticmethod
    def _call_expired(
        connection: DatabaseConnection, project_id: str, call_id: str, now: str
    ) -> bool:
        return (
            connection.execute(
                f"SELECT 1 {_EXPIRED_CALLS} AND c.project_id = ? AND c.call_id = ?",
                (len(now), len(now), now, project_id, call_id),
            ).fetchone()
            is not None
        )

    def _purge_locked(
        self, project_id: str, call_id: str, *, expired_at: str | None
    ) -> CallPurge | None:
        """One call purge transaction; ``None`` (nothing written) if the call does not
        exist, or with ``expired_at``, if it has not expired by then."""

        with self.store._connect() as connection:
            connection.begin_write()
            if expired_at is not None and not self._call_expired(
                connection, project_id, call_id, expired_at
            ):
                connection.rollback()
                return None
            now = self.store._clock.unix_nano()
            deleted = delete_calls(connection, project_id, [call_id], now=now)
            if not deleted.keys:
                connection.rollback()
                return None
            stored = self._stored_incidents(connection, deleted.bundle_ids)
            self.store._purge_incident_rows(connection, stored, now)
            expires = now + self.calls.timings.long_quiet_nano + TOMBSTONE_GRACE_NANO
            [(fingerprint, generation)] = deleted.keys
            # The key's next call is a new call: bump its generation now.
            connection.execute(
                "INSERT INTO call_key_generations(project_id, key_fingerprint, generation) "
                "VALUES (?, ?, ?) ON CONFLICT(project_id, key_fingerprint) "
                "DO UPDATE SET generation = call_key_generations.generation + 1",
                (project_id, fingerprint, generation + 1),
            )
            tombstones = [(fingerprint, generation)] + [
                (self.calls.trace_key_fingerprint(project_id, trace_id), 0)
                for trace_id in deleted.trace_ids
            ]
            connection.executemany(
                "INSERT INTO call_tombstones(project_id, key_fingerprint, generation, "
                "expires_at_unix_nano) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, key_fingerprint, generation) "
                "DO UPDATE SET expires_at_unix_nano = excluded.expires_at_unix_nano",
                [(project_id, key, number, expires) for key, number in dict.fromkeys(tombstones)],
            )
            if not self.store._database_uri and not self.store._catalog_artifacts:
                # SQLite: the deleted spans' pages need a scrub (L2(e)).
                self.store._mark_scrub_required(connection)
            connection.commit()
        return CallPurge(project_id, call_id, len(stored), deleted.blob_keys)

    @staticmethod
    def _stored_incidents(
        connection: DatabaseConnection, bundle_ids: Sequence[str]
    ) -> tuple[str, ...]:
        stored: list[str] = []
        for chunk in _chunks(bundle_ids):
            marks = ", ".join("?" * len(chunk))
            stored.extend(
                row["bundle_id"]
                for row in connection.execute(
                    f"SELECT bundle_id FROM incidents WHERE bundle_id IN ({marks})", tuple(chunk)
                ).fetchall()
            )
        return tuple(sorted(stored))

    def _cleanup_artifacts_locked(self, limit: int) -> None:
        try:
            self.store._cleanup_pending_object_deletions_locked(limit=max(limit, 100))
        except (OSError, *DATABASE_ERRORS) as error:
            # The purge committed; its artifacts stay journaled and are retried.
            raise StorageError("artifact purge requires retry") from error

    def _delete_blobs(self, purges: Sequence[CallPurge]) -> None:
        if self.recordings is None:
            return  # journaled: the blob-deletions job deletes them
        for purge in purges:
            if purge.blob_keys:
                self.recordings.delete_blobs(purge.project_id, purge.blob_keys)

    def _scrub(self, *, compact: bool) -> None:
        try:
            self.store._scrub_deleted_pages(compact=compact, skip_if_current=True)
        except (OSError, *DATABASE_ERRORS) as error:
            raise StorageError("call purge requires a scrub retry") from error


def call_retention_job(
    purger: CallPurger, *, batch_size: int = CALL_EXPIRY_BATCH_SIZE
) -> MaintenanceJob:
    """A maintenance job: purge calls whose latest revision expired, a bounded
    batch per run, and collect expired call tombstones at most every
    ``TOMBSTONE_COLLECTION_INTERVAL_SECONDS``. A full batch of either asks to run
    again at once."""

    next_collection = 0

    async def expire(now_unix_nano: int) -> float | None:
        nonlocal next_collection
        collected = 0
        try:
            purged = await asyncio.to_thread(
                purger.purge_expired_calls,
                now_unix_nano=now_unix_nano,
                limit=batch_size,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
            if now_unix_nano >= next_collection:
                collected = await asyncio.to_thread(
                    purger.collect_expired_tombstones,
                    now_unix_nano=now_unix_nano,
                    budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
                )
                if collected < TOMBSTONE_COLLECTION_BATCH_SIZE:
                    next_collection = now_unix_nano + int(
                        TOMBSTONE_COLLECTION_INTERVAL_SECONDS * _SECOND
                    )
        except StorageBusyError:
            return None
        full = purged >= batch_size or collected >= TOMBSTONE_COLLECTION_BATCH_SIZE
        return 0.0 if full else CALL_RETENTION_INTERVAL_SECONDS

    return expire
