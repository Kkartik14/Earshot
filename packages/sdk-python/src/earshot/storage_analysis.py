"""Recording analyses in the catalog, and the maintenance job that runs them.

**Scheduling.** Storing a recording inserts its ``audio_analysis`` row (``pending``)
in the transaction that stores it (``schedule_analysis``, called by
``RecordingStore.finish_upload``); revision ``0005_projections`` inserted one for
every recording stored before it.

**Leases** follow the generic leased-job helper (``leased_jobs``) on the
``audio_analysis`` table. A job's timeout is ``30 s + 0.25 x`` the recording's
duration (15 minutes when the duration is unknown, as for MP3 and WebM), and its
lease is that timeout plus ``LEASE_MARGIN_SECONDS``, set at the claim, so a job
cannot be leased twice while its child may still run.

**States.** A recording the decoder cannot read is terminal ``unavailable`` with
its code, as is every job while the ``signals`` extra is missing
(``EARSHOT_SIGNALS_EXTRA_REQUIRED``). A child that crashes, runs out of memory or
times out is retried after 1, 4 and 16 minutes, then is terminal ``failed`` with its
last code. A result is ``stored``. Every terminal write stamps the current
``ALGORITHM_VERSION`` and bumps the recording's call's ``projection_seq`` (what the
call's projection reads changed).

**Outputs** (peaks, and a rendition) are blobs under server keys,
``analysis/<project_id>/<uuid>-…``. Before any byte is written, the claim reserves
their keys in ``pending_blob_deletions``, due only after its lease (plus a grace):
a process that dies mid-write leaves keys the deletion drain removes. The result
is written only while the claim's lease token still holds the row and the
reservations are untouched; it then owns its keys (their journal rows go). If the
lease was lost, for example because the recording's call was purged and the row
went with it, the same mutation makes the keys due at once, so no derived blob of
a purged recording survives. A replaced result's old peaks key is queued too.

**Mono pairs (one timeline).** Two stored mono recordings of one call, one user and
one agent, with equal ``started_at`` strings and known durations within
``ONE_TIMELINE_TOLERANCE_MS`` of each other, are one recorder timeline. A browser
plays them as one stereo FLAC rendition (channel 0 user, channel 1 agent), held by
the user recording's row (``rendition_blob_key``). Either job of the pair makes it
while the user row has none; the first stored wins, and the other is discarded.

**At start** the job sweeps its spool directory (``<data-dir>/analysis-tmp``),
returns leases a previous process left to ``pending``, and requeues in bounded
batches the rows an older algorithm computed and, once the extra imports, the
``EARSHOT_SIGNALS_EXTRA_REQUIRED`` rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import signals
from .blobstore import BlobNotFoundError
from .leased_jobs import FAILED, JobLease, LeasedJobs
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .storage import StorageBusyError

if TYPE_CHECKING:
    from .blobstore import BlobStore
    from .maintenance import MaintenanceJob
    from .signals.runner import AnalysisProcess
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection

_LOGGER = logging.getLogger(__name__)
_SECOND = 1_000_000_000
TIMEOUT_BASE_SECONDS = 30.0
TIMEOUT_PER_AUDIO_SECOND = 0.25
UNKNOWN_DURATION_TIMEOUT_SECONDS = 15 * 60.0
LEASE_MARGIN_SECONDS = 60
# Reserved output keys become due for deletion this long after their claim's lease.
OUTPUT_RESERVE_GRACE_SECONDS = 60
REQUEUE_BATCH_SIZE = 500
ONE_TIMELINE_TOLERANCE_MS = 20
BLOB_KEY_PREFIX = "analysis"
SPOOL_DIRECTORY = "analysis-tmp"
STORED = "stored"
UNAVAILABLE = "unavailable"
_EXTENSIONS = {
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}

ANALYSIS_JOBS = LeasedJobs("audio_analysis", ("project_id", "recording_id"))


def analysis_timeout_seconds(duration_ms: int | None) -> float:
    """How long one analysis child may run: ``30 s + 0.25 x`` the audio's duration."""

    if duration_ms is None:
        return UNKNOWN_DURATION_TIMEOUT_SECONDS
    return TIMEOUT_BASE_SECONDS + TIMEOUT_PER_AUDIO_SECOND * duration_ms / 1000


def schedule_analysis(
    connection: DatabaseConnection, project_id: str, recording_id: str, now: int
) -> None:
    """Queue a stored recording's analysis, in the caller's transaction."""

    connection.execute(
        "INSERT INTO audio_analysis(project_id, recording_id, state, attempts, lease_token, "
        "algorithm_version, updated_unix_nano) VALUES (?, ?, 'pending', 0, 0, 0, ?) "
        "ON CONFLICT (project_id, recording_id) DO NOTHING",
        (project_id, recording_id, now),
    )


@dataclass(frozen=True, slots=True)
class SourceRecording:
    """A stored recording an analysis child reads."""

    recording_id: str
    blob_key: str
    size: int
    content_type: str
    duration_ms: int | None
    speakers: tuple[str, ...]

    @property
    def extension(self) -> str:
        return _EXTENSIONS.get(self.content_type, ".bin")


@dataclass(frozen=True, slots=True)
class RenditionPlan:
    """A mono pair's stereo rendition: its sources (user, then agent) and its holder."""

    owner_recording_id: str
    sources: tuple[SourceRecording, SourceRecording]
    speakers: tuple[str, str] = ("user", "agent")


@dataclass(frozen=True, slots=True)
class AnalysisClaim:
    """One leased analysis job and what its child needs."""

    lease: JobLease
    project_id: str
    recording_id: str
    call_id: str
    recording: SourceRecording
    rendition: RenditionPlan | None
    timeout_seconds: float

    @property
    def speakers(self) -> tuple[str, ...]:
        return self.recording.speakers


@dataclass(frozen=True, slots=True)
class OutputKeys:
    """The blob keys one claim's outputs are written under."""

    peaks: str
    rendition: str | None

    def all(self) -> tuple[str, ...]:
        return (self.peaks,) if self.rendition is None else (self.peaks, self.rendition)


@dataclass(frozen=True, slots=True)
class Rendition:
    """A mono pair's stereo FLAC (channel 0 user, channel 1 agent) and its halves."""

    blob_key: str
    recording_ids: tuple[str, str]
    peaks_blob_keys: tuple[str | None, str | None]


@dataclass(frozen=True, slots=True)
class Analysis:
    """One ``audio_analysis`` row, as the API reads it."""

    project_id: str
    recording_id: str
    state: str
    error_code: str | None
    algorithm_version: int
    peaks_blob_key: str | None
    rendition_blob_key: str | None
    updated_unix_nano: int


def _speakers(row: Any) -> tuple[str, ...]:
    mapped = row["channel_speakers_json"]
    return (row["speaker"],) if mapped is None else tuple(json.loads(mapped))


def _source(row: Any) -> SourceRecording:
    return SourceRecording(
        recording_id=row["recording_id"],
        blob_key=row["blob_key"],
        size=int(row["size"]),
        content_type=row["content_type"],
        duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
        speakers=_speakers(row),
    )


_RECORDING_COLUMNS = (
    "recording_id, call_id, speaker, channel_speakers_json, content_type, blob_key, size, "
    "duration_ms, started_at_unix_nano"
)


class AnalysisStore:
    """Analysis jobs on one ``IncidentStore``'s catalog, locks and clock."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store

    def _now(self, now_unix_nano: int | None) -> int:
        return self.store._clock.unix_nano() if now_unix_nano is None else now_unix_nano

    # --- reads ------------------------------------------------------------------------------

    def get(self, project_id: str, recording_id: str) -> Analysis | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT state, error_code, algorithm_version, peaks_blob_key, "
                "rendition_blob_key, updated_unix_nano FROM audio_analysis "
                "WHERE project_id = ? AND recording_id = ?",
                (project_id, recording_id),
            ).fetchone()
        if row is None:
            return None
        return Analysis(
            project_id=project_id,
            recording_id=recording_id,
            state=row["state"],
            error_code=row["error_code"],
            algorithm_version=int(row["algorithm_version"]),
            peaks_blob_key=row["peaks_blob_key"],
            rendition_blob_key=row["rendition_blob_key"],
            updated_unix_nano=int(row["updated_unix_nano"]),
        )

    @staticmethod
    def _recording(connection: DatabaseConnection, project_id: str, recording_id: str) -> Any:
        return connection.execute(
            f"SELECT {_RECORDING_COLUMNS} FROM recordings WHERE project_id = ? "
            "AND recording_id = ? AND state = 'stored'",
            (project_id, recording_id),
        ).fetchone()

    @staticmethod
    def _pair(connection: DatabaseConnection, project_id: str, recording: Any) -> tuple | None:
        """``(user, agent)``: the mono pair on one timeline ``recording`` is half of."""

        speakers = _speakers(recording)
        if len(speakers) != 1 or speakers[0] not in ("user", "agent"):
            return None
        if recording["duration_ms"] is None:
            return None
        other = "agent" if speakers[0] == "user" else "user"
        candidates = [
            row
            for row in connection.execute(
                f"SELECT {_RECORDING_COLUMNS} FROM recordings WHERE project_id = ? "
                "AND call_id = ? AND recording_id <> ? AND state = 'stored' "
                "AND started_at_unix_nano = ? ORDER BY recording_id",
                (
                    project_id,
                    recording["call_id"],
                    recording["recording_id"],
                    recording["started_at_unix_nano"],
                ),
            ).fetchall()
            if _speakers(row) == (other,)
        ]
        if len(candidates) != 1:  # none, or not one clear partner
            return None
        partner = candidates[0]
        if (
            partner["duration_ms"] is None
            or abs(int(partner["duration_ms"]) - int(recording["duration_ms"]))
            > ONE_TIMELINE_TOLERANCE_MS
        ):
            return None
        return (recording, partner) if speakers[0] == "user" else (partner, recording)

    @classmethod
    def _rendition(
        cls, connection: DatabaseConnection, project_id: str, recording: Any
    ) -> RenditionPlan | None:
        """The stereo rendition this recording's job should make: one for its mono pair
        on one timeline, while the user recording holds none."""

        pair = cls._pair(connection, project_id, recording)
        if pair is None:
            return None
        user, agent = pair
        held = connection.execute(
            "SELECT rendition_blob_key FROM audio_analysis WHERE project_id = ? "
            "AND recording_id = ?",
            (project_id, user["recording_id"]),
        ).fetchone()
        if held is None or held["rendition_blob_key"] is not None:
            return None
        return RenditionPlan(user["recording_id"], (_source(user), _source(agent)))

    def rendition(self, project_id: str, recording_id: str) -> Rendition | None:
        """The stereo rendition ``recording_id`` holds (it is the user half of a mono
        pair), with both halves' peaks keys (``None`` until stored)."""

        with self.store._connect() as connection:
            recording = self._recording(connection, project_id, recording_id)
            if recording is None:
                return None
            held = connection.execute(
                "SELECT rendition_blob_key FROM audio_analysis WHERE project_id = ? "
                "AND recording_id = ?",
                (project_id, recording_id),
            ).fetchone()
            if held is None or held["rendition_blob_key"] is None:
                return None
            pair = self._pair(connection, project_id, recording)
            if pair is None:
                return None
            peaks = {
                row["recording_id"]: row["peaks_blob_key"]
                for row in connection.execute(
                    "SELECT recording_id, peaks_blob_key FROM audio_analysis "
                    "WHERE project_id = ? AND recording_id IN (?, ?)",
                    (project_id, pair[0]["recording_id"], pair[1]["recording_id"]),
                ).fetchall()
            }
        user, agent = (row["recording_id"] for row in pair)
        return Rendition(
            blob_key=held["rendition_blob_key"],
            recording_ids=(user, agent),
            peaks_blob_keys=(peaks.get(user), peaks.get(agent)),
        )

    # --- claim --------------------------------------------------------------------------------

    def claim(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> AnalysisClaim | None:
        """Lease the next due analysis job, or ``None`` (no write lock is taken then)."""

        now = self._now(now_unix_nano)
        with self.store._connect() as connection:
            if not ANALYSIS_JOBS.due(connection, now):
                return None
        with (
            self.store._mutation("claim_recording_analysis", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()

            def lease_nano(key: tuple) -> int:
                row = self._recording(connection, *key)
                duration = None if row is None or row["duration_ms"] is None else row["duration_ms"]
                timeout = analysis_timeout_seconds(duration)
                return round((timeout + LEASE_MARGIN_SECONDS) * _SECOND)

            lease = ANALYSIS_JOBS.claim(connection, now, lease_nano=lease_nano)
            claim = None
            if lease is not None:
                project_id, recording_id = lease.key
                row = self._recording(connection, project_id, recording_id)
                if row is None or row["blob_key"] is None:
                    # A row whose recording is not stored cannot happen (the row is
                    # inserted when it is stored, and goes with it); say so, once.
                    ANALYSIS_JOBS.complete(
                        connection,
                        lease,
                        now,
                        state=UNAVAILABLE,
                        error_code=signals.ANALYSIS_SOURCE_MISSING,
                        columns={"algorithm_version": signals.ALGORITHM_VERSION},
                    )
                else:
                    source = _source(row)
                    claim = AnalysisClaim(
                        lease=lease,
                        project_id=project_id,
                        recording_id=recording_id,
                        call_id=row["call_id"],
                        recording=source,
                        rendition=self._rendition(connection, project_id, row),
                        timeout_seconds=analysis_timeout_seconds(source.duration_ms),
                    )
            connection.commit()
        return claim

    @staticmethod
    def output_keys(claim: AnalysisClaim) -> OutputKeys:
        """New, unique blob keys for this claim's outputs."""

        name = f"{BLOB_KEY_PREFIX}/{claim.project_id}/{uuid.uuid4()}"
        return OutputKeys(
            peaks=f"{name}-peaks.dat",
            rendition=None if claim.rendition is None else f"{name}-rendition.flac",
        )

    # --- outputs and results ------------------------------------------------------------------

    def reserve_outputs(
        self,
        claim: AnalysisClaim,
        keys: OutputKeys,
        *,
        now_unix_nano: int | None = None,
        budget_s: float | None = None,
    ) -> bool:
        """Journal ``keys`` before their bytes are written, due only after the claim's
        lease; ``False`` (nothing journaled) when the claim no longer holds its job."""

        with (
            self.store._mutation("reserve_analysis_outputs", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            if not ANALYSIS_JOBS.holds(connection, claim.lease):
                connection.rollback()
                return False
            due = claim.lease.lease_until_unix_nano + OUTPUT_RESERVE_GRACE_SECONDS * _SECOND
            connection.executemany(
                "INSERT INTO pending_blob_deletions(project_id, blob_key, queued_at_unix_nano, "
                "attempts) VALUES (?, ?, ?, 0)",
                [(claim.project_id, key, max(due, self._now(now_unix_nano))) for key in keys.all()],
            )
            connection.commit()
        return True

    @staticmethod
    def _reserved(connection: DatabaseConnection, project_id: str, keys: Iterable[str]) -> bool:
        """Whether every key is still journaled and untouched by the deletion drain."""

        return all(
            connection.execute(
                "SELECT 1 FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ? "
                "AND attempts = 0",
                (project_id, key),
            ).fetchone()
            is not None
            for key in keys
        )

    @staticmethod
    def _make_due(
        connection: DatabaseConnection, project_id: str, keys: Iterable[str], now: int
    ) -> None:
        connection.executemany(
            "UPDATE pending_blob_deletions SET queued_at_unix_nano = ? "
            "WHERE project_id = ? AND blob_key = ? AND attempts = 0",
            [(now, project_id, key) for key in keys],
        )

    @staticmethod
    def _touch_call(connection: DatabaseConnection, claim: AnalysisClaim) -> None:
        """What the call's projection reads changed: bump its ``projection_seq``."""

        connection.execute(
            "UPDATE calls SET projection_seq = projection_seq + 1 "
            "WHERE project_id = ? AND call_id = ?",
            (claim.project_id, claim.call_id),
        )

    def store_result(
        self,
        claim: AnalysisClaim,
        keys: OutputKeys,
        result: dict[str, Any],
        *,
        rendition: bool,
        now_unix_nano: int | None = None,
        budget_s: float | None = None,
    ) -> bool:
        """Store the claim's result and its peaks (and rendition, when ``rendition``)
        in one mutation; ``False`` when the claim lost its job, and then its keys are
        due for deletion at once."""

        now = self._now(now_unix_nano)
        project_id = claim.project_id
        with (
            self.store._mutation("store_recording_analysis", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            previous = connection.execute(
                "SELECT peaks_blob_key FROM audio_analysis WHERE project_id = ? "
                "AND recording_id = ?",
                (project_id, claim.recording_id),
            ).fetchone()
            stored = self._reserved(connection, project_id, keys.all()) and ANALYSIS_JOBS.complete(
                connection,
                claim.lease,
                now,
                state=STORED,
                columns={
                    "algorithm_version": signals.ALGORITHM_VERSION,
                    "peaks_blob_key": keys.peaks,
                    "result_json": json.dumps(result, separators=(",", ":"), sort_keys=True),
                },
            )
            if not stored:
                self._make_due(connection, project_id, keys.all(), now)
                connection.commit()
                return False
            owned = [keys.peaks]
            if rendition and keys.rendition is not None and claim.rendition is not None:
                attached = connection.execute(
                    "UPDATE audio_analysis SET rendition_blob_key = ? WHERE project_id = ? "
                    "AND recording_id = ? AND rendition_blob_key IS NULL",
                    (keys.rendition, project_id, claim.rendition.owner_recording_id),
                ).rowcount
                if attached == 1:
                    owned.append(keys.rendition)
            connection.executemany(
                "DELETE FROM pending_blob_deletions WHERE project_id = ? AND blob_key = ?",
                [(project_id, key) for key in owned],
            )
            self._make_due(connection, project_id, set(keys.all()) - set(owned), now)
            replaced = None if previous is None else previous["peaks_blob_key"]
            if replaced is not None and replaced != keys.peaks:
                connection.execute(
                    "INSERT INTO pending_blob_deletions(project_id, blob_key, "
                    "queued_at_unix_nano, attempts) VALUES (?, ?, ?, 0) "
                    "ON CONFLICT (project_id, blob_key) DO NOTHING",
                    (project_id, replaced, now),
                )
            self._touch_call(connection, claim)
            connection.commit()
        return True

    def fail(
        self,
        claim: AnalysisClaim,
        code: str,
        *,
        terminal: bool,
        now_unix_nano: int | None = None,
        budget_s: float | None = None,
    ) -> str | None:
        """Record a failed attempt: terminal ``unavailable`` (``terminal``), or a retry
        that becomes ``failed`` past the last backoff. Returns the job's new state
        (``None``: the claim had lost its job, and nothing was written)."""

        now = self._now(now_unix_nano)
        stamp = {"algorithm_version": signals.ALGORITHM_VERSION}
        with (
            self.store._mutation("fail_recording_analysis", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            if terminal:
                state = (
                    UNAVAILABLE
                    if ANALYSIS_JOBS.complete(
                        connection, claim.lease, now, state=UNAVAILABLE, error_code=code,
                        columns=stamp,
                    )
                    else None
                )  # fmt: skip
            else:
                state = ANALYSIS_JOBS.retry(
                    connection, claim.lease, now, error_code=code, columns=stamp
                )
            if state in (UNAVAILABLE, FAILED):
                self._touch_call(connection, claim)
            connection.commit()
        return state

    # --- start -------------------------------------------------------------------------------

    def release_leases(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> int:
        """Return every running job to pending: at a server's start no child survives."""

        with (
            self.store._mutation("release_analysis_leases", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            released = ANALYSIS_JOBS.release(connection, self._now(now_unix_nano))
            connection.commit()
        return released

    def requeue(
        self,
        *,
        extra_available: bool,
        limit: int = REQUEUE_BATCH_SIZE,
        now_unix_nano: int | None = None,
        budget_s: float | None = None,
    ) -> int:
        """Requeue up to ``limit`` terminal jobs an older algorithm computed, and (when
        ``extra_available``) those that waited for the ``signals`` extra."""

        now = self._now(now_unix_nano)
        where = (
            "(state IN ('stored', 'failed', 'unavailable') AND algorithm_version < ? "
            "AND COALESCE(error_code, '') <> ?)"
        )
        parameters: tuple[Any, ...] = (signals.ALGORITHM_VERSION, signals.SIGNALS_EXTRA_REQUIRED)
        if extra_available:
            where += " OR (state = 'unavailable' AND error_code = ?)"
            parameters += (signals.SIGNALS_EXTRA_REQUIRED,)
        select = f"SELECT project_id, recording_id FROM audio_analysis WHERE {where} LIMIT ?"
        with self.store._connect() as connection:
            if not connection.execute(select, (*parameters, 1)).fetchall():
                return 0
        with (
            self.store._mutation("requeue_recording_analyses", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            keys = [
                (row["project_id"], row["recording_id"])
                for row in connection.execute(select, (*parameters, limit)).fetchall()
            ]
            connection.executemany(
                "UPDATE audio_analysis SET state = 'pending', attempts = 0, error_code = NULL, "
                "lease_until_unix_nano = NULL, next_attempt_unix_nano = NULL, "
                "updated_unix_nano = ? WHERE project_id = ? AND recording_id = ?",
                [(now, project_id, recording_id) for project_id, recording_id in keys],
            )
            connection.executemany(
                "UPDATE calls SET projection_seq = projection_seq + 1 WHERE project_id = ? "
                "AND call_id = (SELECT call_id FROM recordings WHERE project_id = ? "
                "AND recording_id = ?)",
                [(project_id, project_id, recording_id) for project_id, recording_id in keys],
            )
            connection.commit()
        return len(keys)


# --- the maintenance job ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """What one ``AnalysisRunner.run_next`` did: ``stored``, ``unavailable``,
    ``pending`` (retried later), ``failed``, or ``lost`` (the claim lost its job)."""

    project_id: str
    recording_id: str
    result: str
    error_code: str | None = None


class AnalysisRunner:
    """Run analysis jobs one at a time: claim, spool, child, store.

    The recording (and, for a rendition, its partner) is copied from the blob store
    into ``<spool_root>/<uuid>/`` (removed in ``finally``); the child reads and
    writes only there. Nothing here runs under a store lock except the short claim,
    reserve and result mutations.
    """

    def __init__(
        self,
        analyses: AnalysisStore,
        *,
        blobs: BlobStore,
        spool_root: Path,
        process: AnalysisProcess | None = None,
        extra_available: Callable[[], bool] = signals.extra_available,
    ) -> None:
        from .signals.runner import AnalysisProcess

        self.analyses = analyses
        self.blobs = blobs
        self.spool_root = spool_root
        self.process = process or AnalysisProcess()
        self.extra_available = extra_available

    def sweep_spool(self) -> int:
        """Remove what a previous process left in the spool directory (its audio copies)."""

        removed = 0
        if self.spool_root.is_dir():
            for entry in self.spool_root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        return removed

    def _spool(self, claim: AnalysisClaim, job_dir: Path) -> dict[str, Any]:
        """Copy the claim's sources into ``job_dir``; the child's job description."""

        def copy(source: SourceRecording, name: str) -> str:
            path = job_dir / f"{name}{source.extension}"
            with (
                self.blobs.open_range(source.blob_key, 0, source.size) as reader,
                path.open("wb") as handle,
            ):
                for chunk in reader:
                    handle.write(chunk)
            return path.name

        job: dict[str, Any] = {
            "input": copy(claim.recording, "source"),
            "content_type": claim.recording.content_type,
            "speakers": list(claim.speakers),
            "rendition": None,
        }
        if claim.rendition is not None:
            names = [
                job["input"]
                if source.recording_id == claim.recording_id
                else copy(source, "partner")
                for source in claim.rendition.sources
            ]
            job["rendition"] = {
                "inputs": names,
                "content_types": [source.content_type for source in claim.rendition.sources],
                "tolerance_ms": ONE_TIMELINE_TOLERANCE_MS,
            }
        return job

    def run_next(
        self, *, now_unix_nano: int | None = None, budget_s: float | None = None
    ) -> AnalysisOutcome | None:
        """Claim and run one analysis; ``None`` when no job is due."""

        from .signals.runner import AnalysisClosedError

        claim = self.analyses.claim(now_unix_nano=now_unix_nano, budget_s=budget_s)
        if claim is None:
            return None

        def failed(code: str, *, terminal: bool) -> AnalysisOutcome:
            state = self.analyses.fail(claim, code, terminal=terminal, budget_s=budget_s)
            _LOGGER.warning(
                "EARSHOT_ANALYSIS_FAILED: a recording's analysis failed with %s (%s)",
                code,
                state or "lease lost",
            )
            return AnalysisOutcome(claim.project_id, claim.recording_id, state or "lost", code)

        if not self.extra_available():
            return failed(signals.SIGNALS_EXTRA_REQUIRED, terminal=True)
        job_dir = self.spool_root / uuid.uuid4().hex
        try:
            # The spool holds copies of call audio: only this server's user reads it.
            self.spool_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.spool_root, 0o700)
            job_dir.mkdir(mode=0o700)
            try:
                job = self._spool(claim, job_dir)
            except BlobNotFoundError:
                return failed(signals.ANALYSIS_SOURCE_MISSING, terminal=True)
            except Exception as error:  # an unreachable blob store: try again later
                _LOGGER.warning("analysis spool failed (%s)", type(error).__name__)
                return failed(signals.ANALYSIS_SOURCE_UNREADABLE, terminal=False)
            (job_dir / "job.json").write_text(json.dumps(job))
            try:
                outcome = self.process.run(job_dir, timeout_s=claim.timeout_seconds)
            except AnalysisClosedError:
                raise  # the server is stopping; the next start releases the lease
            if outcome.error_code is not None:
                return failed(outcome.error_code, terminal=outcome.terminal)
            result = json.loads((job_dir / "result.json").read_text())
            rendition_file = job_dir / "rendition.flac"
            rendition = claim.rendition is not None and rendition_file.is_file()
            keys = self.analyses.output_keys(claim)
            if not self.analyses.reserve_outputs(claim, keys, budget_s=budget_s):
                return AnalysisOutcome(claim.project_id, claim.recording_id, "lost")
            try:
                self._put(job_dir / "peaks.dat", keys.peaks)
                if rendition and keys.rendition is not None:
                    self._put(rendition_file, keys.rendition)
            except Exception as error:
                # The reserved keys stay journaled; the drain deletes them after the lease.
                _LOGGER.warning("analysis outputs not stored (%s)", type(error).__name__)
                return failed(signals.ANALYSIS_STORE_FAILED, terminal=False)
            if not self.analyses.store_result(
                claim, keys, result, rendition=rendition, budget_s=budget_s
            ):
                return AnalysisOutcome(claim.project_id, claim.recording_id, "lost")
            return AnalysisOutcome(claim.project_id, claim.recording_id, STORED)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _put(self, path: Path, key: str) -> None:
        with path.open("rb") as handle, self.blobs.begin_put(key) as writer:
            while chunk := handle.read(1024 * 1024):
                writer.write(chunk)
            writer.commit()

    def close(self) -> None:
        self.process.close()


class _AnalysisJob:
    """``analysis_job``'s job: a maintenance job the loop also closes."""

    def __init__(self, runner: AnalysisRunner) -> None:
        self.runner = runner
        self._started = False
        self._requeueing = True

    async def __call__(self, now_unix_nano: int) -> float | None:
        from .signals.runner import AnalysisClosedError

        runner = self.runner
        try:
            if not self._started:
                await asyncio.to_thread(runner.sweep_spool)
                await asyncio.to_thread(
                    runner.analyses.release_leases,
                    now_unix_nano=now_unix_nano,
                    budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
                )
                self._started = True
            if self._requeueing:
                requeued = await asyncio.to_thread(
                    runner.analyses.requeue,
                    extra_available=runner.extra_available(),
                    now_unix_nano=now_unix_nano,
                    budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
                )
                if requeued:
                    return 0.0
                self._requeueing = False
            outcome = await asyncio.to_thread(
                runner.run_next,
                now_unix_nano=now_unix_nano,
                budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS,
            )
        except (StorageBusyError, AnalysisClosedError):
            return None
        return None if outcome is None else 0.0

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.runner.close)


def analysis_job(runner: AnalysisRunner) -> MaintenanceJob:
    """A maintenance job: analyse stored recordings one at a time, each in a child.

    Its first runs sweep the spool, release the leases a previous process left and
    requeue in batches; while jobs are due it asks to run again at once. The loop's
    shutdown kills a running child (``aclose``); its lease is released at the next
    start.
    """

    return _AnalysisJob(runner)
