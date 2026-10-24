"""Recordings in the catalog: create, upload attempts, the hold, the sweep, blob deletions.

A ``RecordingStore`` is the storage half of the recordings API. It runs on
an ``IncidentStore``'s database, write locks and clock, uses the ``CallStore`` for
everything a call is (identity, tombstones, the close rules), and the
server's ``BlobStore`` for bytes.

**Create.** ``(project_id, recording_id)`` names a recording. Creating one resolves
its session key to the key's current call, creating that call when needed
(``key_source = 'recording'``). The same ``recording_id`` with the same manifest
is the existing recording; a different manifest, or a recording that already
``failed``, is ``RecordingConflictError``. A ``metadata_only`` project stores no
media (``PresetForbidsMediaError``). A ``mixed`` recording may name each channel's
speaker (``channel_speakers``, stored as compact JSON); its file then has one
channel per entry, which the API checks against the uploaded header.

**Upload attempts.** Each upload streams into its own blob key,
``recordings/<project_id>/<attempt uuid>``, minted here; no client string is ever
part of a key. ``begin_upload`` journals the key in ``pending_blob_deletions``
before any byte is written and registers the attempt as in flight in this
process. ``finish_upload`` stores the recording with a conditional update
(``WHERE state = 'pending'``) and removes the winning key from the journal in the
same transaction, so a key stays journaled unless a stored recording owns it. A
losing attempt (another attempt won, or the sweep failed the recording) deletes
its own object (``discard_upload``, which never deletes an object a stored
recording names). Whatever a crash leaves (a committed object whose recording
never stored) is still journaled, and ``drain_blob_deletions`` deletes it once no
attempt in this process owns it. One server owns a catalog and its blob location
(``K_server`` on Postgres, ``.server.lock`` on SQLite refuse a second one), so
"in flight in this process" is "in flight".

**The pending deadline** is ``pending_deadline``: the latest of the
recording's creation, its call's last evidence and its call's end signal, plus
``UPLOAD_HOLD_CAP``. A call is held (the closer does not claim it) while any of its
pending recordings is before its deadline or has an upload streaming
(``uploading_calls``, read under the claim's write lock); the sweep fails a pending
recording once its deadline has passed and no upload of it is streaming. A call
that keeps receiving evidence keeps its pending recordings alive, however long it
runs, and so does an upload that is still sending bytes: the API ends an upload
that sends nothing for ``UPLOAD_IDLE_TIMEOUT_SECONDS`` (60 s), or that is still
sending ``UPLOAD_HOLD_CAP`` after it started, so neither a stalled nor a trickling
client can hold its call forever.

**A call holds at most** ``MAX_REVISION_RECORDINGS`` (16) recordings, in any
state: ``create`` refuses another (``RecordingLimitError``).

**Deletions** run after commit: ``drain_blob_deletions`` claims journaled keys
under the write lock, deletes their objects outside it, then removes their rows.
A failed delete is retried with exponential backoff (``retry_delay_seconds``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .call_capture import CAPTURE_PRESET_FULL
from .call_materializer import MAX_REVISION_RECORDINGS
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .recordings import valid_channel_speakers, valid_start_unix_nano
from .storage import StorageBusyError

if TYPE_CHECKING:
    from .blobstore import BlobStore
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore
    from .storage_calls import CallStore
    from .storage_database import DatabaseConnection

_LOGGER = logging.getLogger(__name__)
_SECOND = 1_000_000_000
BLOB_KEY_PREFIX = "recordings"
DELETION_BATCH_SIZE = 100
MAX_RETRY_DELAY_SECONDS = 300
# Recordings requests wait at most this long for the store's locks (then 503 +
# Retry-After), as OTLP export does.
REQUEST_LOCK_BUDGET_SECONDS = 2.0
# The blob sweep runs at startup, then hourly; after a failure (an unreachable S3,
# say) it tries again a minute later.
BLOB_SWEEP_INTERVAL_SECONDS = 3600.0
BLOB_SWEEP_RETRY_SECONDS = 60.0
_COLUMNS = (
    "project_id, recording_id, call_id, speaker, content_type, label, started_at_unix_nano, "
    "state, blob_key, size, sha256, duration_ms, created_unix_nano, stored_unix_nano, "
    "channel_speakers_json"
)


class RecordingError(Exception):
    """A recordings request the API refuses with ``status_code`` and a stable ``code``."""

    status_code = 400
    code = "EARSHOT_RECORDING_INVALID"
    message = "the recording request is invalid"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)


class RecordingNotFoundError(RecordingError):
    status_code = 404
    code = "EARSHOT_RECORDING_NOT_FOUND"
    message = "recording not found"


class RecordingConflictError(RecordingError):
    status_code = 409
    code = "EARSHOT_RECORDING_CONFLICT"
    message = "the recording exists with a different manifest, or it failed"


class PresetForbidsMediaError(RecordingError):
    status_code = 422
    code = "EARSHOT_PRESET_FORBIDS_MEDIA"
    message = "the project's capture preset stores no media"


class RecordingLimitError(RecordingError):
    status_code = 409
    code = "EARSHOT_RECORDING_LIMIT"
    message = f"a call holds at most {MAX_REVISION_RECORDINGS} recordings"


class CallPurgedError(RecordingError):
    status_code = 409
    code = "EARSHOT_CALL_PURGED"
    message = "the call for this session key was purged; its evidence is refused for now"


@dataclass(frozen=True, slots=True)
class RecordingManifest:
    """What a client declares when it creates a recording (validated by the API)."""

    recording_id: str
    session_key: str
    speaker: str
    content_type: str
    started_at_unix_nano: str
    label: str | None = None
    # A ``mixed`` recording's speaker per channel, in channel order (``None``: one
    # channel, the recording's ``speaker``).
    channel_speakers: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Recording:
    """One ``recordings`` row."""

    project_id: str
    recording_id: str
    call_id: str
    speaker: str
    content_type: str
    label: str | None
    started_at_unix_nano: str
    state: str
    blob_key: str | None
    size: int | None
    sha256: str | None
    duration_ms: int | None
    created_unix_nano: int
    stored_unix_nano: int | None
    channel_speakers: tuple[str, ...] | None = None

    @property
    def channels(self) -> int:
        """How many channels its file has: one per mapped speaker, else one."""

        return 1 if self.channel_speakers is None else len(self.channel_speakers)

    def declares(self, manifest: RecordingManifest, call_id: str) -> bool:
        """Whether this recording is the one ``manifest`` (naming ``call_id``) creates."""

        return (
            self.call_id,
            self.speaker,
            self.content_type,
            self.started_at_unix_nano,
            self.label,
            self.channel_speakers,
        ) == (
            call_id,
            manifest.speaker,
            manifest.content_type,
            manifest.started_at_unix_nano,
            manifest.label,
            manifest.channel_speakers,
        )


@dataclass(frozen=True, slots=True)
class UploadAttempt:
    """An upload's plan: stream into ``blob_key``, or (``None``) only verify the bytes
    against an already stored ``recording``."""

    recording: Recording
    blob_key: str | None


@dataclass(frozen=True, slots=True)
class BlobDeletions:
    """What one ``drain_blob_deletions`` pass did."""

    deleted: int = 0
    failed: int = 0


def pending_deadline(
    created_unix_nano: int,
    last_evidence_unix_nano: int,
    end_signal_unix_nano: int | None,
    *,
    hold_nano: int,
) -> int:
    """When a pending recording stops holding its call and may be failed.

    ``max(created, call.last_evidence, call.end_signal or 0) + UPLOAD_HOLD_CAP``:
    a recording is posted at a call's first audio and uploaded after the call, so
    the hold runs from the call's latest evidence, not from the post. The hold
    (held while ``now < deadline``, the closer's ``claim_candidate_queries`` restates
    it in SQL) and the sweep (failed once ``now > deadline``) both use it; an upload
    in flight also holds its call and is never failed, whatever the deadline.
    """

    return max(created_unix_nano, last_evidence_unix_nano, end_signal_unix_nano or 0) + hold_nano


def retry_delay_seconds(attempts: int) -> int:
    """How long a journaled deletion waits after ``attempts`` tries: 0, 2, 4, … 300 s."""

    return 0 if attempts <= 0 else min(2**attempts, MAX_RETRY_DELAY_SECONDS)


def _recording(row: Any) -> Recording:
    return Recording(
        project_id=row["project_id"],
        recording_id=row["recording_id"],
        call_id=row["call_id"],
        speaker=row["speaker"],
        content_type=row["content_type"],
        label=row["label"],
        started_at_unix_nano=row["started_at_unix_nano"],
        state=row["state"],
        blob_key=row["blob_key"],
        size=None if row["size"] is None else int(row["size"]),
        sha256=row["sha256"],
        duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
        created_unix_nano=int(row["created_unix_nano"]),
        stored_unix_nano=None if row["stored_unix_nano"] is None else int(row["stored_unix_nano"]),
        channel_speakers=(
            None
            if row["channel_speakers_json"] is None
            else tuple(json.loads(row["channel_speakers_json"]))
        ),
    )


def channel_speakers_json(channel_speakers: tuple[str, ...] | None) -> str | None:
    """A channel map as ``recordings.channel_speakers_json`` stores it: compact JSON."""

    if channel_speakers is None:
        return None
    return json.dumps(list(channel_speakers), separators=(",", ":"))


class RecordingStore:
    """Recording storage operations on one ``IncidentStore`` and one ``BlobStore``."""

    def __init__(self, store: IncidentStore, *, calls: CallStore, blobs: BlobStore | None) -> None:
        self.store = store
        self.calls = calls
        self.blobs = blobs
        self._uploads_lock = threading.Lock()
        # ``(project_id, blob_key)`` of each attempt in flight -> its recording and call.
        self._uploads_in_flight: dict[tuple[str, str], tuple[str, str]] = {}

    # --- reads ------------------------------------------------------------------------------

    @staticmethod
    def _row(
        connection: DatabaseConnection, project_id: str, recording_id: str
    ) -> Recording | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM recordings WHERE project_id = ? AND recording_id = ?",
            (project_id, recording_id),
        ).fetchone()
        return None if row is None else _recording(row)

    def get(self, project_id: str, recording_id: str) -> Recording | None:
        with self.store._connect() as connection:
            return self._row(connection, project_id, recording_id)

    @staticmethod
    def stored_for_call(
        connection: DatabaseConnection, project_id: str, call_id: str
    ) -> tuple[Recording, ...]:
        """The call's stored recordings (what a revision of it holds)."""

        return RecordingStore.for_call(connection, project_id, call_id, stored_only=True)

    @staticmethod
    def for_call(
        connection: DatabaseConnection, project_id: str, call_id: str, *, stored_only: bool = False
    ) -> tuple[Recording, ...]:
        """The call's recordings, by ID: every state, or only the stored ones."""

        state = " AND state = 'stored'" if stored_only else ""
        return tuple(
            _recording(row)
            for row in connection.execute(
                f"SELECT {_COLUMNS} FROM recordings WHERE project_id = ? AND call_id = ?"
                f"{state} ORDER BY recording_id",
                (project_id, call_id),
            ).fetchall()
        )

    def _now(self, now_unix_nano: int | None) -> int:
        return self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano

    # --- create ------------------------------------------------------------------------------

    def create(
        self, project_id: str, manifest: RecordingManifest, *, budget_s: float | None = None
    ) -> tuple[Recording, bool]:
        """The recording ``manifest`` declares, and whether this call created it."""

        from .call_capture import valid_session_key

        key = valid_session_key(manifest.session_key)
        if key is None:
            raise ValueError("a recording needs a valid session key")
        if not valid_start_unix_nano(manifest.started_at_unix_nano):
            # The API refuses it first (422); this is where the invariant lives.
            raise ValueError("a recording's start must be a Unix nanosecond time in [1, 2^63 - 1]")
        if manifest.channel_speakers is not None and (
            manifest.speaker != "mixed" or not valid_channel_speakers(manifest.channel_speakers)
        ):
            raise ValueError("a channel map names user and agent once at most, on a mixed one")
        with (
            self.store._mutation("create_recording", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            if self.calls._read_preset(connection, project_id) != CAPTURE_PRESET_FULL:
                raise PresetForbidsMediaError
            now = self.store._clock.unix_nano()
            existing = self._row(connection, project_id, manifest.recording_id)
            if existing is not None:
                fingerprint = self.calls.key_fingerprint(project_id, key)
                generation = self.calls._generation(connection, project_id, fingerprint)
                call_id = self.calls.call_id(project_id, key, generation)
                if existing.state == "failed" or not existing.declares(manifest, call_id):
                    raise RecordingConflictError
                return existing, False
            call_id = self.calls.recording_call(connection, project_id, key, now)
            if call_id is None:
                self.calls._bump_counters(connection, project_id, purged_call_evidence_dropped=1)
                connection.commit()
                raise CallPurgedError
            # A revision holds at most this many (one media clock domain each), in any
            # state; nothing is written when the call has them all.
            [held] = connection.execute(
                "SELECT count(*) FROM recordings WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ).fetchone()
            if held >= MAX_REVISION_RECORDINGS:
                raise RecordingLimitError
            connection.execute(
                "INSERT INTO recordings(project_id, recording_id, call_id, speaker, "
                "content_type, label, started_at_unix_nano, state, created_unix_nano, "
                "channel_speakers_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    project_id,
                    manifest.recording_id,
                    call_id,
                    manifest.speaker,
                    manifest.content_type,
                    manifest.label,
                    manifest.started_at_unix_nano,
                    now,
                    channel_speakers_json(manifest.channel_speakers),
                ),
            )
            created = self._row(connection, project_id, manifest.recording_id)
            connection.commit()
        assert created is not None
        return created, True

    # --- upload attempts ---------------------------------------------------------------------

    def begin_upload(
        self, project_id: str, recording_id: str, *, budget_s: float | None = None
    ) -> UploadAttempt:
        """Start one upload of a recording's bytes.

        A pending recording gets a new journaled attempt key; a stored one gets no
        key (the upload is only compared with it). A failed recording is a
        conflict; an unknown one is not found.
        """

        with (
            self.store._mutation("begin_recording_upload", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            preset = self.calls._read_preset(connection, project_id)
            recording = self._row(connection, project_id, recording_id)
            if recording is None:
                raise RecordingNotFoundError
            if preset != CAPTURE_PRESET_FULL:
                raise PresetForbidsMediaError
            if recording.state == "failed":
                raise RecordingConflictError
            if recording.state == "stored":
                return UploadAttempt(recording, None)
            blob_key = f"{BLOB_KEY_PREFIX}/{project_id}/{uuid.uuid4()}"
            # In flight before it is journaled: a journaled key that is not in flight
            # belongs to an attempt that has ended, so a drain may delete it.
            with self._uploads_lock:
                self._uploads_in_flight[(project_id, blob_key)] = (
                    recording_id,
                    recording.call_id,
                )
            try:
                connection.execute(
                    "INSERT INTO pending_blob_deletions(project_id, blob_key, "
                    "queued_at_unix_nano, attempts) VALUES (?, ?, ?, 0)",
                    (project_id, blob_key, self.store._clock.unix_nano()),
                )
                connection.commit()
            except BaseException:
                self.end_upload(project_id, blob_key)
                raise
        return UploadAttempt(recording, blob_key)

    def finish_upload(
        self,
        project_id: str,
        recording_id: str,
        blob_key: str,
        *,
        sha256: str,
        size: int,
        duration_ms: int | None,
        budget_s: float | None = None,
    ) -> Recording | None:
        """Store the recording from the committed object at ``blob_key``.

        Only a pending recording is stored; ``None`` means this attempt lost (the
        recording was stored by another attempt or failed meanwhile), and the
        caller deletes its own object (``discard_upload``). The call gains the
        recording as evidence in the same transaction.
        """

        with (
            self.store._mutation("store_recording", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            self.store._require_active_project(connection, project_id)
            now = self.store._clock.unix_nano()
            updated = connection.execute(
                "UPDATE recordings SET state = 'stored', blob_key = ?, sha256 = ?, size = ?, "
                "duration_ms = ?, stored_unix_nano = ? "
                "WHERE project_id = ? AND recording_id = ? AND state = 'pending'",
                (blob_key, sha256, size, duration_ms, now, project_id, recording_id),
            ).rowcount
            if not updated:
                connection.rollback()
                return None
            connection.execute(
                "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ?",
                (project_id, blob_key),
            )
            # A stored recording's analysis job is queued with it, never after it.
            from .storage_analysis import schedule_analysis

            schedule_analysis(connection, project_id, recording_id, now)
            stored = self._row(connection, project_id, recording_id)
            assert stored is not None
            self.calls.add_recording_evidence(connection, project_id, stored.call_id, now)
            connection.commit()
        return stored

    def discard_upload(
        self, project_id: str, blob_key: str, *, budget_s: float | None = None
    ) -> bool:
        """Delete a losing or abandoned attempt's object and its journal row.

        The ownership check comes first, under the write lock: a caller that did not
        see its ``finish_upload`` result (an ambiguous commit) may be discarding the
        object a stored recording now names, and that object stays (only its journal
        row goes). Best effort: on any failure the row stays journaled and a later
        drain deletes the object. Returns whether the attempt is cleaned up.
        """

        if self.blobs is None:
            return False
        try:
            with (
                self.store._mutation("check_recording_upload_owner", budget_s=budget_s),
                self.store._connect() as connection,
            ):
                connection.begin_write()
                owned = connection.execute(
                    "SELECT 1 FROM recordings WHERE project_id = ? AND blob_key = ?",
                    (project_id, blob_key),
                ).fetchone()
                if owned is not None:
                    connection.execute(
                        "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ?",
                        (project_id, blob_key),
                    )
                connection.commit()
            if owned is not None:
                return True
            self.blobs.delete(blob_key)
            with (
                self.store._mutation("discard_recording_upload", budget_s=budget_s),
                self.store._connect() as connection,
            ):
                connection.begin_write()
                connection.execute(
                    "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ? "
                    "AND NOT EXISTS (SELECT 1 FROM recordings WHERE project_id = ? "
                    "AND blob_key = ?)",
                    (project_id, blob_key, project_id, blob_key),
                )
                connection.commit()
        except Exception as error:
            _LOGGER.warning(
                "EARSHOT_RECORDING_DISCARD_DEFERRED: an upload attempt's object stays "
                "journaled for deletion (%s)",
                type(error).__name__,
            )
            return False
        return True

    def end_upload(self, project_id: str, blob_key: str) -> None:
        """The attempt is over (stored, lost, refused or broken): it no longer owns its key."""

        with self._uploads_lock:
            self._uploads_in_flight.pop((project_id, blob_key), None)

    def _in_flight(self, project_id: str, blob_key: str) -> bool:
        with self._uploads_lock:
            return (project_id, blob_key) in self._uploads_in_flight

    def _uploading(self) -> frozenset[tuple[str, str]]:
        """``(project_id, recording_id)`` of every recording with an attempt in flight."""

        with self._uploads_lock:
            return frozenset(
                (project_id, recording_id)
                for (project_id, _), (recording_id, _) in self._uploads_in_flight.items()
            )

    def uploading_calls(self) -> frozenset[tuple[str, str]]:
        """``(project_id, call_id)`` of every call with an upload in flight: the calls
        the closer's claim leaves alone. Read it under the store's write lock, where
        ``begin_upload`` registers an attempt, so no upload can begin between the
        read and the claim's commit. In-process state is every upload: one server
        owns a catalog (``K_server``, ``.server.lock``)."""

        with self._uploads_lock:
            return frozenset(
                (project_id, call_id)
                for (project_id, _), (_, call_id) in self._uploads_in_flight.items()
            )

    # --- the hold and the sweep ---------------------------------------------------------------

    def _pending(
        self, connection: DatabaseConnection, where: str = "", parameters: tuple[Any, ...] = ()
    ) -> list[Any]:
        return connection.execute(
            "SELECT r.project_id, r.recording_id, r.call_id, r.created_unix_nano, "
            "c.last_evidence_unix_nano, c.end_signal_unix_nano FROM recordings r "
            "JOIN calls c ON c.project_id = r.project_id AND c.call_id = r.call_id "
            f"WHERE r.state = 'pending'{where} "
            "ORDER BY r.created_unix_nano, r.project_id, r.recording_id",
            parameters,
        ).fetchall()

    def _deadline(self, row: Any) -> int:
        end_signal = row["end_signal_unix_nano"]
        return pending_deadline(
            int(row["created_unix_nano"]),
            int(row["last_evidence_unix_nano"]),
            None if end_signal is None else int(end_signal),
            hold_nano=self.calls.timings.upload_hold_nano,
        )

    # A pending recording the sweep may fail: created before the horizon, and not one
    # a live fetch job owns (that job retries past the deadline, and ends the
    # recording itself when it gives up).
    _SWEEPABLE = (
        " AND r.created_unix_nano < ? AND NOT EXISTS (SELECT 1 FROM recording_fetches f "
        "WHERE f.project_id = r.project_id AND f.recording_id = r.recording_id "
        "AND f.state IN ('pending', 'running'))"
    )

    def _expired(self, row: Any, now: int, uploading: frozenset[tuple[str, str]]) -> bool:
        """Whether the sweep fails the pending recording ``row`` at ``now``."""

        return now > self._deadline(row) and (row["project_id"], row["recording_id"]) not in (
            uploading
        )

    def fail_stale_uploads(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> int:
        """Fail every pending recording whose pending deadline has passed.

        A recording a live fetch job owns (a provider recording) is never failed
        here: its fetch retries past the deadline and fails it when it gives up.
        A recording with an upload still streaming is never failed, however late
        (its deadline cannot expire under a live attempt); it is judged again once
        the attempt ends. Uploads in flight are this process's, which is every
        upload: one server owns a catalog (``K_server``, ``.server.lock``). Its
        journaled attempt objects are deleted by ``drain_blob_deletions``. A read
        finds the candidates first, so an idle sweep takes no write lock.
        """

        now = self._now(now_unix_nano)
        horizon = now - self.calls.timings.upload_hold_nano
        with self.store._connect() as connection:
            candidates = self._pending(connection, self._SWEEPABLE, (horizon,))
        if not any(self._expired(row, now, self._uploading()) for row in candidates):
            return 0
        with (
            self.store._mutation("fail_stale_recording_uploads", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            # Read under the write lock: an attempt begins under it too, so none can
            # start between this snapshot and the update.
            uploading = self._uploading()
            expired = [
                (row["project_id"], row["recording_id"])
                for row in self._pending(connection, self._SWEEPABLE, (horizon,))
                if self._expired(row, now, uploading)
            ]
            connection.executemany(
                "UPDATE recordings SET state = 'failed' "
                "WHERE project_id = ? AND recording_id = ? AND state = 'pending'",
                expired,
            )
            connection.commit()
        if expired:
            _LOGGER.info(
                "EARSHOT_RECORDING_UPLOADS_FAILED: %d pending recording(s) passed their "
                "upload deadline",
                len(expired),
            )
        return len(expired)

    # --- blob deletions -------------------------------------------------------------------------

    def drain_blob_deletions(
        self,
        *,
        now_unix_nano: int | None = None,
        limit: int = DELETION_BATCH_SIZE,
        budget_s: float | None = None,
        project_id: str | None = None,
    ) -> BlobDeletions:
        """Delete up to ``limit`` journaled objects that nothing owns, after commit
        (only ``project_id``'s when given: project deletion waits for them).

        A key is due when no upload in this process is streaming into it and its
        retry delay has passed. Claimed keys get their attempt counted under the
        write lock; their objects are deleted outside it; their rows go once
        deleted. A key a stored recording still names loses its row, never its
        object.
        """

        if self.blobs is None:
            return BlobDeletions()
        now = self._now(now_unix_nano)
        project = "" if project_id is None else "project_id = ? AND "
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, blob_key, queued_at_unix_nano, attempts "
                f"FROM pending_blob_deletions WHERE {project}queued_at_unix_nano <= ? "
                "ORDER BY queued_at_unix_nano, project_id, blob_key LIMIT ?",
                (*(() if project_id is None else (project_id,)), now, limit),
            ).fetchall()
        due = [
            (row["project_id"], row["blob_key"])
            for row in rows
            if not self._in_flight(row["project_id"], row["blob_key"])
            and int(row["queued_at_unix_nano"])
            + retry_delay_seconds(int(row["attempts"])) * _SECOND
            <= now
        ]
        return self._delete_due(due, now, budget_s)

    def delete_blobs(
        self, project_id: str, blob_keys: Iterable[str], *, budget_s: float | None = None
    ) -> BlobDeletions:
        """Delete these journaled objects now, right after the commit that journaled
        them (a call purge). One that fails stays journaled, and the drain retries it."""

        if self.blobs is None:
            return BlobDeletions()
        due = [
            (project_id, blob_key)
            for blob_key in sorted(set(blob_keys))
            if not self._in_flight(project_id, blob_key)
        ]
        return self._delete_due(due, self._now(None), budget_s)

    def _delete_due(
        self, due: list[tuple[str, str]], now: int, budget_s: float | None
    ) -> BlobDeletions:
        if not due or self.blobs is None:
            return BlobDeletions()
        claimed = self._claim_deletions(due, now, budget_s)
        deleted: list[tuple[str, str]] = []
        for project_id, blob_key in claimed:
            try:
                self.blobs.delete(blob_key)
            except Exception as error:
                _LOGGER.warning(
                    "EARSHOT_BLOB_DELETE_RETRY: a blob deletion failed and will be retried (%s)",
                    type(error).__name__,
                )
                continue
            deleted.append((project_id, blob_key))
        if deleted:
            with (
                self.store._mutation("finish_blob_deletions", budget_s=budget_s),
                self.store._connect() as connection,
            ):
                connection.begin_write()
                # A row journaled again since its claim (``attempts`` back at 0) names
                # an object written after the delete above: it stays for a later drain.
                connection.executemany(
                    "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ? "
                    "AND attempts > 0",
                    deleted,
                )
                connection.commit()
        return BlobDeletions(deleted=len(deleted), failed=len(claimed) - len(deleted))

    def _claim_deletions(
        self, due: Iterable[tuple[str, str]], now: int, budget_s: float | None
    ) -> list[tuple[str, str]]:
        claimed: list[tuple[str, str]] = []
        with (
            self.store._mutation("claim_blob_deletions", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            for project_id, blob_key in due:
                if self._in_flight(project_id, blob_key):
                    continue
                if (
                    connection.execute(
                        "SELECT 1 FROM pending_blob_deletions WHERE project_id = ? "
                        "AND blob_key = ?",
                        (project_id, blob_key),
                    ).fetchone()
                    is None
                ):
                    continue  # stored or discarded since the read
                owned = connection.execute(
                    "SELECT 1 FROM recordings WHERE project_id = ? AND blob_key = ?",
                    (project_id, blob_key),
                ).fetchone()
                if owned is not None:
                    connection.execute(
                        "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ?",
                        (project_id, blob_key),
                    )
                    continue
                connection.execute(
                    "UPDATE pending_blob_deletions SET attempts = attempts + 1, "
                    "queued_at_unix_nano = ? WHERE project_id = ? AND blob_key = ?",
                    (now, project_id, blob_key),
                )
                claimed.append((project_id, blob_key))
            connection.commit()
        return claimed


def recording_upload_sweep_job(recordings: RecordingStore) -> MaintenanceJob:
    """A maintenance job: fail pending recordings past their deadline, at the tick's time."""

    async def sweep(now_unix_nano: int) -> None:
        try:
            await asyncio.to_thread(
                recordings.fail_stale_uploads,
                now_unix_nano=now_unix_nano,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
        except StorageBusyError:
            return None  # the next tick tries again

    return sweep


def blob_deletion_job(recordings: RecordingStore) -> MaintenanceJob:
    """A maintenance job: delete journaled blobs; a full batch asks to run again at once."""

    async def drain(now_unix_nano: int) -> float | None:
        try:
            result = await asyncio.to_thread(
                recordings.drain_blob_deletions,
                now_unix_nano=now_unix_nano,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
        except StorageBusyError:
            return None
        return 0.0 if result.deleted + result.failed >= DELETION_BATCH_SIZE else None

    return drain


def blob_sweep_job(blobs: BlobStore) -> MaintenanceJob:
    """A maintenance job: remove what crashed uploads left in the blob store.

    It runs when the server starts and hourly after that (``BlobStore.sweep``
    spares this process's own uploads in flight). A sweep that fails, for example
    while S3 is unreachable, is logged and retried a minute later; the server
    keeps serving meanwhile.
    """

    async def sweep(_now_unix_nano: int) -> float:
        try:
            removed = await asyncio.to_thread(blobs.sweep)
        except Exception as error:
            _LOGGER.warning(
                "EARSHOT_BLOB_SWEEP_FAILED: the blob store sweep failed and will be retried (%s)",
                type(error).__name__,
            )
            return BLOB_SWEEP_RETRY_SECONDS
        if removed:
            _LOGGER.info("EARSHOT_BLOB_SWEEP: removed %d interrupted upload(s)", removed)
        return BLOB_SWEEP_INTERVAL_SECONDS

    return sweep
