"""The closer: a due call's next revision, claimed, materialized and finished (FR-6).

A ``Closer`` runs on an ``IncidentStore``'s database, locks and clock, with the
``CallStore`` (the call rules) and the ``RecordingStore`` (the hold). The server's
maintenance loop runs ``call_closer_job`` every tick; tests call the three steps.

**Claim** (one short mutation, one call). A call is claimable when it is dirty and
due, or has a revision pending from a claim that never finished; its lease is free
or expired; no pending recording holds it (WS6's one pending deadline,
``storage_recordings.pending_deadline``, checked per candidate in the claim's
query) and no upload of its recordings is in flight
(``RecordingStore.uploading_calls``, read under the claim's write lock); and it is
not at the revision cap (``UNCAPPED_SQL``). The claim clears
``dirty``, sets ``pending_revision`` to the latest revision plus one, leases the
call for ``LEASE_SECONDS`` under a new ``lease_token``, and snapshots the call: its
span keys and stored recordings. Evidence that commits after the claim makes the
call dirty again, so it is in the next revision, never lost.

**Integrity.** A stored revision that is still pending cannot happen (finish is
atomic with the catalog write). If the claim ever sees one, it logs
``EARSHOT_CALL_INTEGRITY``, counts it (``integrity_errors``), and sets the call
aside (``materialize_error``); nothing repairs it.

**Materialize** reads the snapshot's stored spans outside every lock, as bytes
(``RevisionInput``), and hands them to the closer's materializer, which builds
the revision (``build_revision``: ``call_materializer.materialize``, then
``storage.prepare_incident``) and answers with the incident ready to store. The
server's materializer is a ``MaterializerProcess``: one persistent child process
(``spawn``) that holds no database connection, so a long call's materialization
never runs in the serving interpreter (its GIL starved OTLP acks for seconds).
A child that dies or stops answering is a failed attempt
(``EARSHOT_MATERIALIZE_WORKER_FAILED``); the next build starts a new child.

**Finish** runs inside ``IncidentStore.ingest_prepared``'s catalog transaction, so
the revision's incident and its ``call_revisions`` row commit together or not at
all. It is conditional on the claim's ``lease_token``: a holder whose lease was
taken over commits nothing (``StaleClaimError``). It records the revision, closes
the call, clears the pending revision and the lease, and never touches ``dirty``.

**Failure** counts an attempt, makes the call dirty again, and puts it off for
``min(2 ** attempts, LONG_QUIET)`` seconds with a stable ``materialize_error``:
evidence the materializer refuses, a worker that died, a revision ID taken by
another incident, and anything else a finish raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from .call_capture import stored_span
from .call_materializer import CallSnapshot, materialize, revision_bundle_id
from .crash_points import crash_point, fault_point, stall_point
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .storage import (
    DATABASE_ERRORS,
    IncidentConflictError,
    IncidentPurgedError,
    PreparedIncident,
    StorageBusyError,
    StorageError,
    prepare_incident,
)
from .storage_calls import UNCAPPED_SQL, call_spans_sql

if TYPE_CHECKING:
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore, IngestResult
    from .storage_calls import CallStore
    from .storage_database import DatabaseConnection
    from .storage_recordings import Recording, RecordingStore

_LOGGER = logging.getLogger(__name__)
_SECOND = 1_000_000_000
LEASE_SECONDS = 60
# A build that has not been answered this long after it began (sending the snapshot
# included: a child that stops reading blocks a send once the pipe is full) has its
# child killed, and the attempt fails: a bound, never a hang. A 1000-turn call
# builds in seconds.
MATERIALIZE_TIMEOUT_SECONDS = 600.0
# How long a new child may take to start (a fresh interpreter importing Earshot).
MATERIALIZER_START_TIMEOUT_SECONDS = 60.0
# How long closing waits for a build in progress before it kills the child.
MATERIALIZER_CLOSE_GRACE_SECONDS = 10.0

# A claim's snapshot: its span keys (read inside the claim's write mutation), the
# resources those spans name, and the spans' payloads (read outside every lock).
SNAPSHOT_KEYS_SQL = call_spans_sql("s.trace_id, s.span_id", order_by="s.trace_id, s.span_id")
SNAPSHOT_RESOURCES_SQL = (
    "SELECT resource_digest, payload FROM otlp_resources WHERE project_id = ? "
    f"AND resource_digest IN ({call_spans_sql('s.resource_digest')})"
)
SNAPSHOT_SPANS_SQL = call_spans_sql("s.trace_id, s.span_id, s.resource_digest, s.payload")

# Stable ``calls.materialize_error`` codes.
CALL_INTEGRITY = "EARSHOT_CALL_INTEGRITY"
MATERIALIZE_INVALID_EVIDENCE = "EARSHOT_MATERIALIZE_INVALID_EVIDENCE"
MATERIALIZE_SNAPSHOT_GONE = "EARSHOT_MATERIALIZE_SNAPSHOT_GONE"
MATERIALIZE_CONFLICT = "EARSHOT_MATERIALIZE_CONFLICT"
MATERIALIZE_WORKER_FAILED = "EARSHOT_MATERIALIZE_WORKER_FAILED"
MATERIALIZE_FINISH_FAILED = "EARSHOT_MATERIALIZE_FINISH_FAILED"


def claim_candidate_queries(
    now: int, horizon: int, *, limit: int
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    """The closer's candidate reads, in claim order, with their parameters.

    A call is a candidate when it has a revision pending from a claim that never
    finished (the first read), or is dirty and due (the second), each by due time,
    ``limit`` rows. Its lease must be free or expired; it is not set aside
    (``CALL_INTEGRITY``) or at the revision cap (``UNCAPPED_SQL``); its project is
    active (a project being deleted takes its calls with it, FR-14, unrevised); and
    no pending recording holds it. A pending recording holds its call while ``now``
    is before its pending deadline, ``max(created, last_evidence, end_signal or 0) +
    UPLOAD_HOLD_CAP`` (``storage_recordings.pending_deadline``), which is the
    predicate below with both sides less the hold (``horizon = now - hold``).

    Each read is a range of one ``calls`` index (``calls_pending_revision_idx``,
    ``calls_close_due_idx``) with one probe of the candidate's own recordings
    (``recordings_call_idx``). One query that OR-ed the two made SQLite walk every
    call of the catalog, once a tick and again inside the claim's write lock
    (tribunal round 2).
    """

    held = (
        "AND (c.lease_until_unix_nano IS NULL OR c.lease_until_unix_nano < ?) "
        "AND (c.materialize_error IS NULL OR c.materialize_error <> ?) "
        f"AND {UNCAPPED_SQL} "
        "AND EXISTS (SELECT 1 FROM projects p WHERE p.project_id = c.project_id "
        "AND p.lifecycle_state = 'active') "
        # The call's own recordings (``recordings_call_idx``). ``state`` is compared as
        # an expression: with no statistics SQLite otherwise picks the smaller
        # ``recordings_state_idx`` and walks every pending recording of every project.
        "AND NOT EXISTS (SELECT 1 FROM recordings r WHERE r.project_id = c.project_id "
        "AND r.call_id = c.call_id AND COALESCE(r.state, '') = 'pending' "
        "AND (r.created_unix_nano > ? OR c.last_evidence_unix_nano > ? "
        "OR COALESCE(c.end_signal_unix_nano, 0) > ?)) "
        "ORDER BY c.close_due_unix_nano, c.project_id, c.call_id LIMIT ?"
    )
    select = (
        "SELECT c.project_id, c.call_id, c.latest_revision, c.pending_revision, "
        "c.end_signal_unix_nano FROM calls c WHERE "
    )
    tail = (now, CALL_INTEGRITY, horizon, horizon, horizon, limit)
    return (
        (f"{select}c.pending_revision IS NOT NULL {held}", tail),
        (f"{select}c.dirty = 1 AND c.close_due_unix_nano <= ? {held}", (now, *tail)),
    )


class StaleClaimError(StorageError):
    """A finish whose claim's lease was taken over: it commits nothing."""


class SnapshotGoneError(StorageError):
    """A span of the claim's snapshot is no longer stored (the call was purged)."""


class MaterializerWorkerError(Exception):
    """The materializer's child process died or did not answer in time."""


class MaterializeRefusedError(Exception):
    """The materializer's child refused the evidence; ``error_type`` is the name of
    the exception it raised (never its message, which may quote the call)."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class MaterializerClosedError(RuntimeError):
    """A build after the materializer was closed (the server is stopping)."""


@dataclass(frozen=True, slots=True)
class RevisionInput:
    """A claim's snapshot as bytes: what a revision is built from, in a form that
    crosses a process boundary. ``spans`` are ``(resource_digest, payload)`` of the
    stored spans; ``resources`` the stored resource payloads by digest."""

    project_id: str
    call_id: str
    revision: int
    final: bool
    capture_preset: str
    recordings: tuple[Recording, ...]
    resources: dict[bytes, bytes]
    spans: tuple[tuple[bytes, bytes], ...] = ()


def build_revision(snapshot: RevisionInput) -> PreparedIncident:
    """The revision ``snapshot`` gives, ready to store. Pure and deterministic: the
    same snapshot gives the same bytes in any process."""

    spans = tuple(
        stored_span(payload, snapshot.resources[digest]) for digest, payload in snapshot.spans
    )
    bundle = materialize(
        CallSnapshot(
            call_id=snapshot.call_id,
            revision=snapshot.revision,
            final=snapshot.final,
            capture_preset=snapshot.capture_preset,
            spans=spans,
            recordings=snapshot.recordings,
        )
    )
    return prepare_incident(bundle, snapshot.project_id)


class RevisionMaterializer(Protocol):
    """Where a closer builds revisions: ``build`` takes the snapshot (its spans
    streamed) and answers with the incident ready to store."""

    def start(self) -> None: ...

    def build(
        self, snapshot: RevisionInput, spans: Iterable[tuple[bytes, bytes]]
    ) -> PreparedIncident: ...

    def close(self) -> None: ...


class InProcessMaterializer:
    """Revisions built in the calling thread (a closer used outside the server)."""

    def start(self) -> None:
        pass

    def build(
        self, snapshot: RevisionInput, spans: Iterable[tuple[bytes, bytes]]
    ) -> PreparedIncident:
        return build_revision(replace(snapshot, spans=tuple(spans)))

    def close(self) -> None:
        pass


# The worker protocol. Parent to child: the snapshot (pickled, without spans),
# then each span as two raw frames (its resource digest, its payload), then an
# empty frame, then ``_BUILD`` or ``_ABORT``; ``None`` instead of a snapshot stops
# the child. Child to parent: ``_READY`` once, then per build ``(_BUILT,
# PreparedIncident)`` or ``(_REFUSED, exception type name)``.
_READY = "ready"
_BUILD = "build"
_ABORT = "abort"
_BUILT = "built"
_REFUSED = "refused"


def _worker_main(connection: Any) -> None:
    """The materializer child: build what the parent sends until told to stop."""

    crash_point("materializer_starting")
    connection.send(_READY)
    while True:
        try:
            snapshot = connection.recv()
        except EOFError:  # the parent is gone
            return
        if snapshot is None:
            return
        stall_point("materializer_reading_snapshot")
        spans = []
        while digest := connection.recv_bytes():
            spans.append((digest, connection.recv_bytes()))
        if connection.recv() != _BUILD:
            continue
        crash_point("during_materialize")
        try:
            prepared = build_revision(replace(snapshot, spans=tuple(spans)))
        except Exception as error:
            reply: tuple[str, Any] = (_REFUSED, type(error).__name__)
        else:
            reply = (_BUILT, prepared)
        del spans
        connection.send(reply)


class MaterializerProcess:
    """Revisions built in one persistent child process, started with ``spawn``.

    The child holds no database connection and no lock: the closer sends it a
    claim's stored spans as bytes (the snapshot it already read) and stores what it
    answers. One build runs at a time, under one deadline (``timeout_s``) from the
    first byte sent to the answer: a watchdog kills the child at the deadline, so a
    child that is alive but not reading cannot block a send for good. ``start``
    launches the child (the server does it when its maintenance loop starts); a
    child that died is replaced by the next ``build``; ``close`` stops it (the
    loop's shutdown), giving a build in progress ``close_grace_s`` and then killing
    the child, so closing never waits on a stalled one.
    """

    def __init__(
        self,
        *,
        timeout_s: float = MATERIALIZE_TIMEOUT_SECONDS,
        start_timeout_s: float = MATERIALIZER_START_TIMEOUT_SECONDS,
        close_grace_s: float = MATERIALIZER_CLOSE_GRACE_SECONDS,
    ) -> None:
        self.timeout_s = timeout_s
        self.start_timeout_s = start_timeout_s
        self.close_grace_s = close_grace_s
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._process: Any = None
        self._connection: Any = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        """The child's process ID while one runs."""

        process = self._process
        return None if process is None or not process.is_alive() else int(process.pid)

    def start(self) -> None:
        with self._lock:
            self._ensure_started()

    def _ensure_started(self) -> None:
        if self._closed:
            raise MaterializerClosedError("the materializer is closed")
        if self._process is not None and self._process.is_alive():
            return
        self._discard()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_main, args=(child,), name="earshot-materializer", daemon=True
        )
        ready = False
        cause: str | None = None
        try:
            process.start()
        except Exception as error:
            # Its type and errno only: an OSError's message can quote paths.
            cause = f"{type(error).__name__}, errno {getattr(error, 'errno', None)}"
        else:
            self._process = process
        finally:
            # The child holds its own copy now. Ours, kept open, would hide a child
            # that dies before it is ready until the start timeout (tribunal round 2).
            child.close()
        if self._process is process:
            try:
                ready = parent.poll(self.start_timeout_s) and parent.recv() == _READY
            except (EOFError, OSError):
                ready = False
        self._connection = parent
        if not ready:
            self._discard()
            if cause is None:
                _LOGGER.error(
                    "EARSHOT_MATERIALIZER_START_FAILED: the closer's materializer process did "
                    "not start, so calls are not closed; a program that starts the server "
                    'itself must guard its entry point with `if __name__ == "__main__":` (the '
                    "process is spawned, which imports the program's main module again)"
                )
            else:
                _LOGGER.error(
                    "EARSHOT_MATERIALIZER_START_FAILED: the closer's materializer process could "
                    "not be started (%s), so calls are not closed; check the host's limits on "
                    "processes and open files",
                    cause,
                )
            raise MaterializerWorkerError(
                "EARSHOT_MATERIALIZER_START_FAILED: the materializer process did not start"
                + ("" if cause is None else f" ({cause})")
            )

    def _discard(self) -> None:
        """Kill the child (if any) and forget it."""

        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=10)
            if process.exitcode is not None:
                process.close()

    def build(
        self, snapshot: RevisionInput, spans: Iterable[tuple[bytes, bytes]]
    ) -> PreparedIncident:
        with self._lock:
            self._ensure_started()
            connection = self._connection
            deadline = time.monotonic() + self.timeout_s
            # Killing the child is the only way to free a send it no longer reads;
            # its pipe then breaks, and the send or the wait below ends.
            watchdog = threading.Timer(self.timeout_s, _kill, args=(self._process,))
            watchdog.daemon = True
            watchdog.start()
            try:
                connection.send(snapshot)
                complete = False
                try:
                    for digest, payload in spans:
                        connection.send_bytes(digest)
                        connection.send_bytes(payload)
                    complete = True
                finally:
                    # Even when reading the snapshot failed: the child then discards it.
                    connection.send_bytes(b"")
                    connection.send(_BUILD if complete else _ABORT)
                if not connection.poll(max(0.0, deadline - time.monotonic())):
                    self._discard()
                    raise MaterializerWorkerError("the materializer process did not answer")
                kind, value = connection.recv()
            except (EOFError, OSError) as error:
                self._discard()
                if self._closed:  # ``close`` killed it: the server is stopping
                    raise MaterializerClosedError("the materializer was closed") from error
                raise MaterializerWorkerError(
                    "the materializer process died or did not answer in time"
                ) from error
            finally:
                watchdog.cancel()
        if kind == _REFUSED:
            raise MaterializeRefusedError(value)
        return value

    def close(self) -> None:
        """Stop the child. A build in progress gets ``close_grace_s`` to finish; then
        the child is killed, which ends that build with ``MaterializerClosedError``
        (its call's lease expires and the next server retries it)."""

        self._closed = True  # a build that starts from now on is refused
        if not self._lock.acquire(timeout=self.close_grace_s):
            _LOGGER.warning(
                "EARSHOT_MATERIALIZER_STOPPED: a revision build did not finish within %g s "
                "of shutdown; its child process is killed",
                self.close_grace_s,
            )
            _kill(self._process)
            if not self._lock.acquire(timeout=self.close_grace_s):
                return  # the child is dead; the build ends without us
        try:
            process, connection = self._process, self._connection
            if process is None:
                return
            with contextlib.suppress(OSError):
                connection.send(None)
            process.join(timeout=self.close_grace_s)
            self._discard()
        finally:
            self._lock.release()


def _kill(process: Any) -> None:
    """Kill a materializer child, if it still runs (from any thread)."""

    if process is None:
        return
    with contextlib.suppress(ValueError, OSError):  # closed by now, or already gone
        if process.is_alive():
            process.kill()


@dataclass(frozen=True, slots=True)
class CallClaim:
    """One claimed call: which revision is pending, under which lease, of what."""

    project_id: str
    call_id: str
    revision: int
    lease_token: int
    close_reason: str
    final: bool
    capture_preset: str
    span_keys: tuple[tuple[bytes, bytes], ...]
    recordings: tuple[Recording, ...]

    @property
    def bundle_id(self) -> str:
        return revision_bundle_id(self.call_id, self.revision)


@dataclass(frozen=True, slots=True)
class MaterializedRevision:
    """A claim and the revision built from its snapshot, ready to store (``None``
    when the call holds no evidence: it closes without a revision)."""

    claim: CallClaim
    prepared: PreparedIncident | None


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What one ``close_next`` did for one call."""

    project_id: str
    call_id: str
    revision: int
    # "revision", "closed_without_revision", "failed" or "stale".
    result: str
    error: str | None = None


class Closer:
    """Revision closing for the calls of one ``IncidentStore``."""

    def __init__(
        self,
        store: IncidentStore,
        *,
        calls: CallStore,
        recordings: RecordingStore,
        materializer: RevisionMaterializer | None = None,
    ) -> None:
        self.store = store
        self.calls = calls
        self.recordings = recordings
        self.materializer: RevisionMaterializer = materializer or InProcessMaterializer()
        # Calls this closer found holding a stored revision that is still pending.
        self.integrity_errors = 0

    def start(self) -> None:
        """Start the materializer (the server's child process)."""

        self.materializer.start()

    def close(self) -> None:
        """Stop the materializer; a build in progress finishes first."""

        self.materializer.close()

    def _now(self, now_unix_nano: int | None) -> int:
        return self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano

    # --- claim ---------------------------------------------------------------------------

    def _next_claimable(
        self, connection: DatabaseConnection, now: int, uploading: frozenset[tuple[str, str]]
    ) -> Any | None:
        """The next claimable call: pending revisions first, then by due time; never
        one in ``uploading`` (a call with an upload in flight is held, FR-6). Each
        read takes one row more than ``uploading`` has calls, so a held call never
        hides a claimable one behind it."""

        horizon = now - self.recordings.timings.upload_hold_nano
        for sql, parameters in claim_candidate_queries(now, horizon, limit=len(uploading) + 1):
            for row in connection.execute(sql, parameters).fetchall():
                if (row["project_id"], row["call_id"]) not in uploading:
                    return row
        return None

    @staticmethod
    def _revision_stored(connection: DatabaseConnection, row: Any) -> bool:
        return (
            row["pending_revision"] is not None
            and connection.execute(
                "SELECT 1 FROM call_revisions WHERE project_id = ? AND call_id = ? "
                "AND revision = ?",
                (row["project_id"], row["call_id"], row["pending_revision"]),
            ).fetchone()
            is not None
        )

    def _set_aside(self, connection: DatabaseConnection, row: Any) -> None:
        """AC6.4's invariant broke: log, count, set aside; never repair."""

        self.integrity_errors += 1
        _LOGGER.error(
            "%s: a call holds a stored revision %d that is still pending; the call is "
            "set aside and not repaired",
            CALL_INTEGRITY,
            row["pending_revision"],
        )
        connection.execute(
            "UPDATE calls SET materialize_error = ? WHERE project_id = ? AND call_id = ?",
            (CALL_INTEGRITY, row["project_id"], row["call_id"]),
        )

    def claim(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> CallClaim | None:
        """Claim the next claimable call, or ``None`` (no write lock is taken then)."""

        now = self._now(now_unix_nano)
        with self.store._connect() as connection:
            if self._next_claimable(connection, now, self.recordings.uploading_calls()) is None:
                return None
        with (
            self.store._mutation("claim_call", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            # Read under the write lock: ``begin_upload`` registers an attempt under it
            # too, so no upload can begin between this read and the claim's commit.
            uploading = self.recordings.uploading_calls()
            row = self._next_claimable(connection, now, uploading)
            while row is not None and self._revision_stored(connection, row):
                self._set_aside(connection, row)
                row = self._next_claimable(connection, now, uploading)
            claim = None if row is None else self._claim(connection, row, now)
            connection.commit()
        if claim is None:
            return None
        crash_point("after_claim_commit")
        return claim

    def _claim(self, connection: DatabaseConnection, row: Any, now: int) -> CallClaim:
        project_id, call_id = row["project_id"], row["call_id"]
        latest = row["latest_revision"]
        revision = 1 if latest is None else int(latest) + 1
        connection.execute(
            "UPDATE calls SET dirty = 0, pending_revision = ?, lease_until_unix_nano = ?, "
            "lease_token = lease_token + 1 WHERE project_id = ? AND call_id = ?",
            (revision, now + LEASE_SECONDS * _SECOND, project_id, call_id),
        )
        lease_token = int(
            connection.execute(
                "SELECT lease_token FROM calls WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ).fetchone()["lease_token"]
        )
        has_final = (
            connection.execute(
                "SELECT 1 FROM call_revisions WHERE project_id = ? AND call_id = ? "
                "AND finality = 'final' LIMIT 1",
                (project_id, call_id),
            ).fetchone()
            is not None
        )
        final = row["end_signal_unix_nano"] is not None
        if final and not has_final:
            close_reason = "end_signal"
        elif latest is not None:
            close_reason = "late_data"
        else:
            close_reason = "quiet_period"
        span_keys = tuple(
            (bytes(item["trace_id"]), bytes(item["span_id"]))
            for item in connection.execute(SNAPSHOT_KEYS_SQL, (project_id, call_id)).fetchall()
        )
        return CallClaim(
            project_id=project_id,
            call_id=call_id,
            revision=revision,
            lease_token=lease_token,
            close_reason=close_reason,
            final=final,
            capture_preset=self.calls._read_preset(connection, project_id),
            span_keys=span_keys,
            recordings=self.recordings.stored_for_call(connection, project_id, call_id),
        )

    # --- materialize ---------------------------------------------------------------------

    def materialize(self, claim: CallClaim) -> MaterializedRevision:
        """Build the claim's revision from its snapshot; reads only, takes no lock.

        The snapshot's stored spans are read as bytes and the connection given
        back before the materializer builds; a span of the snapshot that is gone
        (a purge raced the claim) is a ``SnapshotGoneError``, and nothing is built.
        """

        if not claim.span_keys and not claim.recordings:
            return MaterializedRevision(claim, None)
        with self.store._connect() as connection:
            resources = {
                bytes(row["resource_digest"]): bytes(row["payload"])
                for row in connection.execute(
                    SNAPSHOT_RESOURCES_SQL, (claim.project_id, claim.project_id, claim.call_id)
                ).fetchall()
            }
            spans = tuple(self._snapshot_spans(connection, claim))
        snapshot = RevisionInput(
            project_id=claim.project_id,
            call_id=claim.call_id,
            revision=claim.revision,
            final=claim.final,
            capture_preset=claim.capture_preset,
            recordings=claim.recordings,
            resources=resources,
        )
        return MaterializedRevision(claim, self.materializer.build(snapshot, spans))

    @staticmethod
    def _snapshot_spans(
        connection: DatabaseConnection, claim: CallClaim
    ) -> Iterator[tuple[bytes, bytes]]:
        """``(resource_digest, payload)`` of each span of the claim's snapshot."""

        wanted = set(claim.span_keys)
        found = 0
        for row in connection.execute(SNAPSHOT_SPANS_SQL, (claim.project_id, claim.call_id)):
            if (bytes(row["trace_id"]), bytes(row["span_id"])) in wanted:
                found += 1
                yield bytes(row["resource_digest"]), bytes(row["payload"])
        if found != len(wanted):
            raise SnapshotGoneError("a span of the claimed snapshot is no longer stored")

    # --- finish --------------------------------------------------------------------------

    def finish(
        self, materialized: MaterializedRevision, *, now_unix_nano: int | None = None
    ) -> IngestResult | None:
        """Store the revision and close the call in one transaction (AC6.3)."""

        claim = materialized.claim
        now = self._now(now_unix_nano)
        prepared = materialized.prepared
        if prepared is None:
            with (
                self.store._mutation("close_call_without_revision"),
                self.store._connect() as connection,
            ):
                connection.begin_write()
                self._close(connection, claim, latest_revision=None)
                connection.commit()
            return None

        def record_revision(connection: DatabaseConnection) -> None:
            self._close(connection, claim, latest_revision=claim.revision)
            connection.execute(
                "INSERT INTO call_revisions(project_id, call_id, revision, bundle_id, "
                "close_reason, finality, span_count, recording_count, created_unix_nano) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim.project_id,
                    claim.call_id,
                    claim.revision,
                    claim.bundle_id,
                    claim.close_reason,
                    prepared.finality,
                    len(claim.span_keys),
                    len(claim.recordings),
                    now,
                ),
            )
            # Evidence that arrived since the claim and that the cap now keeps out.
            self.calls.note_evidence_past_cap(connection, claim.project_id, claim.call_id)

        result = self.store.ingest_prepared(prepared, before_commit=record_revision)
        crash_point("after_catalog_commit")
        fault_point("after_catalog_commit")
        return result

    @staticmethod
    def _close(
        connection: DatabaseConnection, claim: CallClaim, *, latest_revision: int | None
    ) -> None:
        updated = connection.execute(
            "UPDATE calls SET state = 'closed', "
            "latest_revision = COALESCE(?, latest_revision), pending_revision = NULL, "
            "lease_until_unix_nano = NULL, materialize_attempts = 0, materialize_error = NULL, "
            "evidence_after_latest_revision = 0 "
            "WHERE project_id = ? AND call_id = ? AND lease_token = ? AND pending_revision = ?",
            (
                latest_revision,
                claim.project_id,
                claim.call_id,
                claim.lease_token,
                claim.revision,
            ),
        ).rowcount
        if updated != 1:
            raise StaleClaimError("the claim's lease was taken over; its finish commits nothing")

    # --- failure -------------------------------------------------------------------------

    def fail(self, claim: CallClaim, code: str, *, now_unix_nano: int | None = None) -> bool:
        """Record a failed attempt and put the call off (AC6.5).

        Only while the claim still holds its call: the same lease token, its revision
        still pending, and not stored. ``False`` otherwise: the lease was taken over,
        or the claim's finish committed before the error was seen (a post-commit
        step raised, or a commit's answer was lost), and a failure there would make
        the stored revision dirty again and build it twice (tribunal round 2, Lamport LAM2-1).
        """

        now = self._now(now_unix_nano)
        claimed = (
            "project_id = ? AND call_id = ? AND lease_token = ? AND pending_revision = ? "
            "AND NOT EXISTS (SELECT 1 FROM call_revisions r WHERE r.project_id = calls.project_id "
            "AND r.call_id = calls.call_id AND r.revision = calls.pending_revision)"
        )
        scope = (claim.project_id, claim.call_id, claim.lease_token, claim.revision)
        with self.store._mutation("fail_call_revision"), self.store._connect() as connection:
            connection.begin_write()
            row = connection.execute(
                f"SELECT materialize_attempts FROM calls WHERE {claimed}", scope
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            attempts = int(row["materialize_attempts"]) + 1
            delay = min(2**attempts * _SECOND, self.calls.timings.long_quiet_nano)
            connection.execute(
                "UPDATE calls SET dirty = 1, pending_revision = NULL, "
                "lease_until_unix_nano = NULL, materialize_attempts = ?, "
                f"close_due_unix_nano = ?, materialize_error = ? WHERE {claimed}",
                (attempts, now + delay, code, *scope),
            )
            connection.commit()
        return True

    # --- the loop's step -----------------------------------------------------------------

    def close_next(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> CloseOutcome | None:
        """Claim, materialize and finish one call; ``None`` when none is claimable."""

        claim = self.claim(now_unix_nano=now_unix_nano, budget_s=budget_s)
        if claim is None:
            return None
        outcome = CloseOutcome(claim.project_id, claim.call_id, claim.revision, "revision")
        try:
            materialized = self.materialize(claim)
        except SnapshotGoneError as error:
            return self._failed(claim, outcome, MATERIALIZE_SNAPSHOT_GONE, error, now_unix_nano)
        except MaterializerWorkerError as error:
            return self._failed(claim, outcome, MATERIALIZE_WORKER_FAILED, error, now_unix_nano)
        except (StorageError, MaterializerClosedError, *DATABASE_ERRORS):
            raise  # the store, not the evidence: the lease expires and the call is retried
        except Exception as error:
            # The adapter, the recorder or the contract refused the evidence.
            return self._failed(claim, outcome, MATERIALIZE_INVALID_EVIDENCE, error, now_unix_nano)
        try:
            self.finish(materialized, now_unix_nano=now_unix_nano)
        except StaleClaimError:
            _LOGGER.warning(
                "EARSHOT_CALL_LEASE_LOST: revision %d of a call was finished by a newer claim",
                claim.revision,
            )
            return CloseOutcome(claim.project_id, claim.call_id, claim.revision, "stale")
        except (IncidentConflictError, IncidentPurgedError) as error:
            return self._failed(claim, outcome, MATERIALIZE_CONFLICT, error, now_unix_nano)
        except Exception as error:
            # Anything else a finish raises (the store refusing the revision, a lost
            # connection) is a failed attempt with its backoff and a stable code
            # (AC6.5), never a silent retry at every lease expiry. If the database
            # cannot even record that, ``fail`` raises and the lease expiry retries.
            return self._failed(claim, outcome, MATERIALIZE_FINISH_FAILED, error, now_unix_nano)
        if materialized.prepared is None:
            return CloseOutcome(
                claim.project_id, claim.call_id, claim.revision, "closed_without_revision"
            )
        return outcome

    def _failed(
        self,
        claim: CallClaim,
        outcome: CloseOutcome,
        code: str,
        error: Exception,
        now_unix_nano: int | None,
    ) -> CloseOutcome:
        # The exception's type only: its message may quote the call's content.
        error_type = (
            error.error_type if isinstance(error, MaterializeRefusedError) else type(error).__name__
        )
        if self.fail(claim, code, now_unix_nano=now_unix_nano):
            _LOGGER.warning(
                "EARSHOT_MATERIALIZE_FAILED: revision %d of a call failed with %s (%s)",
                claim.revision,
                code,
                error_type,
            )
            return CloseOutcome(
                outcome.project_id, outcome.call_id, outcome.revision, "failed", code
            )
        # ``fail`` recorded nothing: the claim no longer holds its call. Report what
        # became of it (tribunal round 3, R3-4).
        result = self._settled(claim)
        if result == "stale":
            _LOGGER.warning(
                "EARSHOT_CALL_LEASE_LOST: revision %d of a call was finished by a newer claim",
                claim.revision,
            )
        else:
            _LOGGER.warning(
                "EARSHOT_REVISION_STORED_DESPITE_ERROR: revision %d of a call was finished "
                "(%s); a step after its commit raised %s",
                claim.revision,
                result,
                error_type,
            )
        return CloseOutcome(outcome.project_id, outcome.call_id, outcome.revision, result)

    def _settled(self, claim: CallClaim) -> str:
        """What became of a claim whose failure ``fail`` did not record: its finish
        committed (``revision``, or ``closed_without_revision``: the call still has the
        claim's lease token and nothing pending), or another claim took the call
        (``stale``)."""

        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT c.pending_revision, EXISTS (SELECT 1 FROM call_revisions r "
                "WHERE r.project_id = c.project_id AND r.call_id = c.call_id "
                "AND r.revision = ?) AS stored "
                "FROM calls c WHERE c.project_id = ? AND c.call_id = ? AND c.lease_token = ?",
                (claim.revision, claim.project_id, claim.call_id, claim.lease_token),
            ).fetchone()
        if row is None:
            return "stale"
        if row["stored"]:
            return "revision"
        return "closed_without_revision" if row["pending_revision"] is None else "stale"

    def release_leases(self, *, budget_s: float | None = None) -> int:
        """Free every lease: at server start, no closer holds one (one server owns a
        catalog), so a lease left by a process that died is released at once."""

        with (
            self.store._mutation("release_call_leases", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            released = connection.execute(
                "UPDATE calls SET lease_until_unix_nano = NULL "
                "WHERE lease_until_unix_nano IS NOT NULL"
            ).rowcount
            connection.commit()
        return int(released)


class _CloserJob:
    """``call_closer_job``'s job: a maintenance job the loop also closes."""

    def __init__(self, closer: Closer) -> None:
        self.closer = closer
        self._started = False

    async def __call__(self, now_unix_nano: int) -> float | None:
        try:
            if not self._started:
                await asyncio.to_thread(
                    self.closer.release_leases, budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS
                )
                self._started = True
                # The server's lifespan has started the child already (and refused to
                # start without one). A loop a host composes itself starts it here; a
                # child that cannot start is logged, and each build tries again under
                # its call's backoff.
                with contextlib.suppress(MaterializerWorkerError):
                    await asyncio.to_thread(self.closer.start)
            outcome = await asyncio.to_thread(
                self.closer.close_next,
                now_unix_nano=now_unix_nano,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
        except (StorageBusyError, MaterializerClosedError):
            # Busy: the next tick tries again. Closed: the server is stopping, and a
            # claim left leased is released when the next server starts.
            return None
        return None if outcome is None else 0.0

    async def aclose(self) -> None:
        await asyncio.to_thread(self.closer.close)


def call_closer_job(closer: Closer) -> MaintenanceJob:
    """A maintenance job: close due calls one at a time, at the tick's time.

    Its first run releases the leases a previous server process left and starts
    the closer's materializer if nothing has (the app's lifespan does, before it
    serves); the loop's shutdown stops it (``aclose``). While
    calls are claimable it asks to run again at once; each call is its own claim
    and its own ingest, so no mutation batches several revisions.
    """

    return _CloserJob(closer)
