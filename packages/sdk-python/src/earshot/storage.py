"""Immutable incident storage: a SQLite or Postgres catalog plus canonical artifacts.

Canonical protobuf artifacts own evidence content and graph truth. The catalog is
the durable tombstone, ordering, and publication authority. On SQLite the
artifacts are content-addressed files beside the catalog and neither half is a
standalone backup; on Postgres they are ``bytea`` rows in the catalog itself,
written in the incident's transaction. Derived analysis is separate and keyed by
artifact digest and analyzer version.

SQL runs through the dialect-neutral connection shim in ``storage_database``;
dialect-specific statements go through ``connection.dialect.sql(...)``. Alembic
(``storage_migrations``) owns the schema and is imported when a store opens, so
importing this module needs no ``server`` extra but opening a store does. A
``postgresql://`` store also needs the ``postgres`` extra (``storage_postgres``).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .call_capture import CAPTURE_PRESETS, UNAVAILABLE_CAPTURE_PRESETS
from .clock import Clock, SystemClock
from .contract import DerivedAnalysis
from .credential_cache import CredentialVerdictCache
from .storage_database import DatabaseConnection, Row, SQLiteDatabase
from .storage_projection import ProjectionRows, compute_projection_rows, write_projection_rows
from .versions import (
    ELEVENLABS_NORMALIZER_VERSION,
    INCIDENT_PROJECTION_VERSION,
    RETELL_NORMALIZER_VERSION,
    RINGG_NORMALIZER_VERSION,
    VAPI_NORMALIZER_VERSION,
)
from .versions import (
    TURN_FACT_PROJECTION_VERSION as TURN_FACT_PROJECTION_VERSION,  # re-exported
)

try:  # Unix file locking protects multiple local server processes sharing a store.
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to the process lock.
    fcntl = None

_INCIDENT_STORES: weakref.WeakSet[Any] = weakref.WeakSet()
_INCIDENT_STORES_LOCK = threading.RLock()
_LOGGER = logging.getLogger(__name__)


def _open_advisory_lock(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        return os.fdopen(descriptor, "a+b", buffering=0)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def _lock_incident_stores_before_fork() -> None:
    _INCIDENT_STORES_LOCK.acquire()


def _unlock_incident_stores_after_fork() -> None:
    _INCIDENT_STORES_LOCK.release()


def _reset_incident_stores_after_fork() -> None:
    global _INCIDENT_STORES_LOCK
    _INCIDENT_STORES_LOCK = threading.RLock()
    for store in tuple(_INCIDENT_STORES):
        try:
            store._reset_after_fork()
        except Exception:
            # An at-fork hook must not leave the child with inherited locks.
            store._fork_reset_error = "the incident store could not reset after fork"


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_lock_incident_stores_before_fork,
        after_in_parent=_unlock_incident_stores_after_fork,
        after_in_child=_reset_incident_stores_after_fork,
    )

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from earshot.contract import IncidentBundle


class StorageError(RuntimeError):
    """Base class for storage failures safe to translate at the API boundary."""


class StorageCleanupPendingError(StorageError):
    """A fenced cleanup hit temporary storage contention and must be retried."""


class IncidentNotFoundError(StorageError):
    pass


class IncidentConflictError(StorageError):
    pass


class IncidentPurgedError(StorageError):
    pass


class IncidentBelongsToCallError(StorageError):
    """The incident is a revision of a call: it is purged only with its call."""

    code = "EARSHOT_INCIDENT_BELONGS_TO_CALL"

    def __init__(self, call_id: str) -> None:
        super().__init__("the incident is a revision of a call; purge the call")
        self.call_id = call_id


class ProjectInactiveError(StorageError):
    """A project lifecycle fence rejected a new or late write."""


class ProjectConflictError(StorageError):
    """A project already exists with other terms than the ones asked for."""


class ArtifactCorruptionError(StorageError):
    pass


class CapturePresetUnavailableError(StorageError):
    """A capture preset this release does not implement (``redacted``)."""

    code = "EARSHOT_PRESET_UNAVAILABLE"
    status_code = 422

    def __init__(self, preset: str) -> None:
        super().__init__(
            f"{self.code}: the {preset} capture preset is not available in this release; "
            f"use {' or '.join(CAPTURE_PRESETS)}"
        )


class InvalidCursorError(StorageError):
    pass


class StorageOpenError(StorageError):
    """A store refused to open, with a stable code and an operator-safe message."""

    code = "EARSHOT_STORAGE_OPEN_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class SchemaTooNewError(StorageOpenError):
    """The catalog was migrated by a newer Earshot: its revision is unknown here."""

    code = "EARSHOT_SCHEMA_TOO_NEW"


class SchemaBehindError(StorageOpenError):
    """The catalog needs a migration and automatic migration was turned off."""

    code = "EARSHOT_SCHEMA_BEHIND"


class CatalogIntegrityError(StorageOpenError):
    """A migration would have committed rows that break a foreign key."""

    code = "EARSHOT_CATALOG_INTEGRITY"


class StorageRuntimeUnavailableError(StorageOpenError):
    """The storage runtime (SQLAlchemy, Alembic) is not installed."""

    code = "EARSHOT_SERVER_EXTRA_REQUIRED"


class DatabaseInUseError(StorageOpenError):
    """Another server already holds this Postgres database (``K_server``) or this
    SQLite data directory (``.server.lock``)."""

    code = "EARSHOT_DATABASE_IN_USE"


class CorrelationKeyRequiredError(StorageOpenError):
    """A Postgres catalog needs a configured correlation key, and none is usable."""

    code = "EARSHOT_CORRELATION_KEY_REQUIRED"


class CorrelationKeyMismatchError(StorageOpenError):
    """The configured correlation key is not the one this catalog was written with."""

    code = "EARSHOT_CORRELATION_KEY_MISMATCH"


class DatabaseError(StorageError):
    """A database statement failed. The API answers it as storage unavailable (503)."""


class DatabaseUnavailableError(DatabaseError):
    """The database cannot serve now: connection, pool timeout, lock or serialization."""


class StorageBusyError(StorageError):
    """A mutation with a lock budget could not take the store's locks in time.

    The API answers it with 503 and ``Retry-After``: the store is healthy but
    another writer (or a SQLite compaction) holds it now.
    """

    code = "EARSHOT_STORAGE_BUSY"
    retry_after_seconds = 3


class DatabaseLockLostError(StorageError):
    """The server lost its ``K_server`` session, and with it the database."""

    code = "EARSHOT_DATABASE_LOCK_LOST"


# Every exception a catalog statement can raise, on either dialect. SQLite keeps
# raising the stdlib's own errors; Postgres errors arrive as ``DatabaseError``.
DATABASE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, DatabaseError)
CORRELATION_KEY_ENVIRONMENT = "EARSHOT_CORRELATION_KEY"
CORRELATION_KEY_FILE_ENVIRONMENT = "EARSHOT_CORRELATION_KEY_FILE"
_CORRELATION_KEY_ID_META = "correlation_key_id"


def _storage_migrations() -> Any:
    """Import the migration runner, which needs the ``server`` extra."""

    try:
        from . import storage_migrations
    except ModuleNotFoundError as error:
        if error.name not in {"sqlalchemy", "alembic"}:
            raise
        raise StorageRuntimeUnavailableError(
            "opening an incident store needs the server extra; install "
            "'earshot-observability[server]'"
        ) from None
    return storage_migrations


def _storage_postgres() -> Any:
    """Import the Postgres runtime, which needs the ``server`` and ``postgres`` extras."""

    _storage_migrations()
    try:
        from . import storage_postgres
    except ModuleNotFoundError as error:
        if error.name not in {"psycopg", "psycopg_binary"}:
            raise
        raise StorageRuntimeUnavailableError(
            "a postgresql:// database needs the postgres extra; install "
            "'earshot-observability[server,postgres]'"
        ) from None
    return storage_postgres


SERVER_LOCK_FILE = ".server.lock"


class SQLiteServerLock:
    """The one-server-per-data-directory lock on SQLite, held for the server's lifetime.

    An exclusive, non-blocking ``flock`` on ``<data-dir>/.server.lock``. The
    recordings and blob layer assume one server owns its catalog and blob location
    (uploads in flight are tracked in memory), so a second server or worker on the
    same data directory is refused, as ``K_server`` refuses one on Postgres. CLI
    commands never take it: they keep the per-mutation ``.store.lock`` and work
    while the server runs. The kernel drops the lock when the process ends, however
    it ends.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._pid = os.getpid()

    @classmethod
    def acquire(cls, data_dir: str | Path) -> SQLiteServerLock:
        directory = Path(data_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = _open_advisory_lock(directory / SERVER_LOCK_FILE)
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise DatabaseInUseError(
                    "another Earshot server is running on this data directory; run one "
                    "server (and one worker) per data directory"
                ) from None
            except OSError as error:
                handle.close()
                raise StorageOpenError(
                    f"could not take the server lock ({type(error).__name__})"
                ) from None
        return cls(handle)

    def verify(self) -> None:
        """Raise ``DatabaseLockLostError`` in a forked child: the lock is the parent's."""

        if os.getpid() != self._pid:
            raise DatabaseLockLostError("the server lock belongs to the parent process")

    def release(self) -> None:
        if os.getpid() == self._pid:
            with contextlib.suppress(Exception):
                self._handle.close()


def acquire_server_lock(database_url: str | None, data_dir: str | Path | None = None) -> Any | None:
    """Take the server lock: ``K_server`` on Postgres, ``.server.lock`` on SQLite.

    A second server, or a second worker, gets ``DatabaseInUseError``
    (``EARSHOT_DATABASE_IN_USE``). On SQLite the lock is on ``data_dir``; without
    one there is nothing to lock (``None``).
    """

    if database_url is not None:
        return _storage_postgres().PostgresServerLock.acquire(database_url)
    if data_dir is None:
        return None
    return SQLiteServerLock.acquire(data_dir)


def resolve_correlation_key() -> bytes:
    """Read the configured correlation key (``EARSHOT_CORRELATION_KEY`` or ``_FILE``).

    The value is 32 bytes: base64 in the variable; raw (an existing
    ``instance-correlation.key`` works as is) or base64 in the file, which must be
    mode 0600 and not a symbolic link.
    """

    from .checkpoint.keys import resolve_at_rest_key

    try:
        key = resolve_at_rest_key(
            None,
            env_var=CORRELATION_KEY_ENVIRONMENT,
            env_file_var=CORRELATION_KEY_FILE_ENVIRONMENT,
            label="correlation key",
        )
    except ValueError as error:
        raise CorrelationKeyRequiredError(str(error)) from None
    if key is None:
        raise CorrelationKeyRequiredError(
            f"a Postgres catalog needs {CORRELATION_KEY_ENVIRONMENT} or "
            f"{CORRELATION_KEY_FILE_ENVIRONMENT} (a 32-byte key); to keep the "
            "identities of a SQLite store, point the file variable at its "
            "instance-correlation.key"
        )
    return key


def _correlation_key_id(key: bytes) -> str:
    """A one-way identifier of the correlation key, safe to store beside the data."""

    return hmac.new(key, b"earshot.correlation-key-id.v1\x00instance", hashlib.sha256).hexdigest()


DEFAULT_PROJECT_ID = "default"
_CONNECTOR_NORMALIZER_VERSIONS = {
    "elevenlabs": ELEVENLABS_NORMALIZER_VERSION,
    "vapi": VAPI_NORMALIZER_VERSION,
    "retell": RETELL_NORMALIZER_VERSION,
    "ringg": RINGG_NORMALIZER_VERSION,
}
_PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_API_KEY_PREFIX = "earshot_sk_"
# This version owns the wide Turn Fact semantics, not only the underlying
# deterministic analyzer. Bump it whenever projection meanings or evidence
# selection rules change.


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    display_name: str
    created_at_unix_nano: str
    capture_preset: str = CAPTURE_PRESETS[0]
    lifecycle_state: str = "active"


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    project_id: str
    key_id: str
    label: str
    credential: str


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    project_id: str
    key_id: str


@dataclass(frozen=True, slots=True)
class TurnFact:
    project_id: str
    bundle_id: str
    session_id: str
    turn_id: str
    turn_index: int
    started_at_unix_nano: str | None
    framework: str | None
    provider: str | None
    model: str | None
    language: str | None
    status: str
    stt_finalization_ms: float | None
    stt_finalization_availability: str
    stt_finalization_basis: str
    stt_finalization_confidence: str
    stt_finalization_limitation: str | None
    eou_ms: float | None
    eou_availability: str
    eou_basis: str
    eou_confidence: str
    eou_limitation: str | None
    first_token_ms: float | None
    first_token_availability: str
    first_token_basis: str
    first_token_confidence: str
    first_token_limitation: str | None
    generated_response_ms: float | None
    generated_response_availability: str
    generated_response_basis: str
    generated_response_confidence: str
    generated_response_limitation: str | None
    sent_response_ms: float | None
    sent_response_availability: str
    sent_response_basis: str
    sent_response_confidence: str
    sent_response_limitation: str | None
    received_response_ms: float | None
    received_response_availability: str
    received_response_basis: str
    received_response_confidence: str
    received_response_limitation: str | None
    render_start_response_ms: float | None
    render_start_response_availability: str
    render_start_response_basis: str
    render_start_response_confidence: str
    render_start_response_limitation: str | None
    response_ms: float | None
    response_availability: str
    response_basis: str
    response_confidence: str
    response_limitation: str | None
    turn_duration_ms: float | None
    turn_duration_availability: str
    turn_duration_basis: str
    turn_duration_confidence: str
    turn_duration_limitation: str | None
    tool_operation_count: int
    tool_total_work_ms: float
    interruption_count: int | None
    interruption_availability: str
    interruption_basis: str
    interruption_confidence: str
    interruption_limitation: str | None
    projection_version: str
    contract_version: str


@dataclass(frozen=True, slots=True)
class TurnMetricSummary:
    group: str
    availability: str
    basis: str
    confidence: str
    limitation: str | None
    turn_count: int
    available_count: int
    average_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None
    p50_ms: float | None
    p95_ms: float | None


@dataclass(frozen=True, slots=True)
class TurnMetricFleetSummary:
    """Grouped percentiles for one metric, together with what they leave out.

    A percentile is only as trustworthy as the population behind it. A
    crash-recovered or operator-sealed artifact is ``provisional``: it is missing
    however much of the conversation the producer never durably observed, so its
    turn facts describe a fragment and cannot be pooled with cleanly closed ones
    without quietly biasing the fleet number.

    Dropping them without saying so would only trade one quiet lie for another,
    so the withheld counts travel with the numbers. ``groups`` empty while
    ``withheld_incident_count`` is non-zero is a refusal to aggregate, and reads
    as one; it is never a measured zero.
    """

    groups: tuple[TurnMetricSummary, ...]
    # Distinct final incidents whose turn facts these groups are computed from.
    incident_count: int
    # Distinct non-final incidents whose turn facts were kept out, and how many
    # turn facts that cost the aggregate.
    withheld_incident_count: int
    withheld_turn_count: int


# What a fleet percentile structurally cannot answer. Stated with the numbers
# rather than left for the reader to infer, in the same spirit as the live
# session collection: "not counted" and "counted as zero" are different claims.
TURN_METRIC_LIMITATIONS = (
    "only final incidents are aggregated: a provisional artifact covers an "
    "unknown fraction of its conversation, so pooling its turns would bias "
    "every value here",
    "withheld_incident_count above zero with no groups means the aggregate was "
    "refused for want of final evidence, never that the fleet measured zero",
    "a turn whose metric was never observed is counted in turn_count and "
    "excluded from available_count, never substituted with a value",
)


@dataclass(frozen=True, slots=True)
class ConnectorRecord:
    endpoint_id: str
    project_id: str
    provider: str
    secret_ref: str
    enabled: bool
    # The version of the translator the connector was created with; informational.
    normalizer_version: str
    # The connector's configuration as compact JSON (``None``: the defaults).
    config_json: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalReferenceRecord:
    """A project-scoped link from an incident to another product record."""

    namespace: str
    record_type: str
    external_id: str
    linked_at_unix_nano: str


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    project_id: str
    bundle_id: str
    session_id: str
    schema_version: str
    digest: str
    size_bytes: int
    status: str
    finality: str
    completeness: str
    framework: str | None
    created_at_unix_nano: str
    ingested_at_unix_nano: str

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "finality": self.finality,
            "completeness": self.completeness,
            "framework": self.framework,
            "created_at_unix_nano": self.created_at_unix_nano,
            "ingested_at_unix_nano": self.ingested_at_unix_nano,
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    record: IncidentRecord
    created: bool


@dataclass(frozen=True, slots=True)
class PreparedIncident:
    """An incident ready to store (``IncidentStore.ingest_prepared``): its canonical
    encoding, its projected rows and the catalog columns, derived from a valid
    bundle with no I/O by ``prepare_incident``. Plain data, so it can be built in
    another process (the call closer's materializer) and stored by this one."""

    project_id: str
    canonical_payload: bytes
    projection: ProjectionRows
    bundle_id: str
    session_id: str
    schema_version: str
    status: str
    finality: str
    completeness: str
    framework: str | None
    created_at_unix_nano: str


def prepare_incident(
    bundle: IncidentBundle, project_id: str, *, canonical_payload: bytes | None = None
) -> PreparedIncident:
    """Validate and encode ``bundle`` and derive its projections: the half of
    ``ingest`` that needs no lock and no database."""

    from .codec import encode_incident_protobuf
    from .validation import assert_valid_incident

    assert_valid_incident(bundle)
    expected_payload = encode_incident_protobuf(bundle)
    if canonical_payload is None:
        canonical_payload = expected_payload
    elif canonical_payload != expected_payload:
        raise StorageError("payload is not the canonical encoding of the incident")
    manifest = bundle.profile.manifest
    return PreparedIncident(
        project_id=project_id,
        canonical_payload=canonical_payload,
        projection=compute_projection_rows(bundle, project_id, canonical_payload=canonical_payload),
        bundle_id=manifest.bundle_id,
        session_id=manifest.session_id,
        schema_version=manifest.schema_version,
        status=bundle.profile.session.status,
        finality=manifest.finality,
        completeness=manifest.completeness,
        framework=manifest.adapters[0].framework if manifest.adapters else None,
        created_at_unix_nano=manifest.created_at_unix_nano,
    )


@dataclass(frozen=True, slots=True)
class IncidentPage:
    items: tuple[IncidentRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    bundle_id: str
    analyzer_version: str
    input_digest: str
    generated_at_unix_nano: str
    value: Any

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "analyzer_version": self.analyzer_version,
            "input_digest": self.input_digest,
            "generated_at_unix_nano": self.generated_at_unix_nano,
            "analysis": self.value,
        }


_MAX_CURSOR_ENCODED_CHARS = 4096
_MAX_CURSOR_DECODED_BYTES = 512
_SQLITE_INT64_MAX = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class MutationTimingStats:
    """Lock wait and hold durations of one operation's store mutations."""

    count: int
    last_wait_seconds: float
    last_hold_seconds: float
    max_wait_seconds: float
    max_hold_seconds: float


@dataclass(frozen=True, slots=True)
class AuthenticationStats:
    """API-key authentication work since the store opened."""

    secret_verifications: int
    verdict_cache_hits: int


# The retention loop compacts SQLite (checkpoint plus VACUUM) after a purge drain
# at most this often; a VACUUM blocks every writer while it runs.
COMPACTION_INTERVAL_NANO = 10 * 60 * 1_000_000_000

# ``api_keys.last_used_at_unix_nano`` is advisory: it is written at most this often
# per key, so a busy exporter does not take the write lock on every request.
KEY_USE_RECORD_INTERVAL_NANO = 60 * 1_000_000_000
# How long an authenticated request waits for the write lock to record a key's
# use. Past it the write is skipped and a later request retries it: the request
# already passed the revocation check, and it must not queue behind a long
# writer, a compaction or another process for a timestamp.
KEY_USE_RECORD_BUDGET_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class ReprojectionBatch:
    """The outcome of one ``reproject_stale`` batch."""

    reprojected: int
    failed: tuple[str, ...]
    resume_after: str | None


@dataclass(frozen=True, slots=True)
class ProjectionConvergence:
    """The outcome of ``converge_projections``: what was done and what is left."""

    reprojected: int
    failed: tuple[str, ...]
    pending: int


MAX_EXPIRED_PURGE_BATCH_SIZE = 10_000
# One reprojection batch decodes and analyses this many incidents outside the
# write lock, then writes them in one short mutation.
REPROJECTION_BATCH_SIZE = 100
# Holding the store write lock longer than this stalls every other writer, OTLP
# included, so it is logged with the operation that did it.
SLOW_MUTATION_SECONDS = 1.0
# How often a budgeted mutation retries a lock another process holds.
LOCK_POLL_SECONDS = 0.02
_STARTUP_EXPIRED_PURGE_BATCH_SIZE = 1000
_SQLITE_SAFE_BIND_LIMIT = 900


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tombstone_key(bundle_id: str) -> str:
    return hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory-entry change when the platform supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _default_catalog_path(data_dir: Path) -> Path:
    return data_dir / "earshot.sqlite3"


def describe_catalog_revision(
    data_dir: str | Path, *, database_url: str | None = None
) -> dict[str, object]:
    """Report the catalog's schema revision without opening the store or writing.

    The result has the head revision this binary ships, the catalog's revision
    (``None`` before Alembic manages it) and a state: ``empty``, ``legacy``,
    ``behind``, ``current`` or ``unsupported``. A SQLite catalog also reports its
    ``user_version``; Postgres has none.
    """

    migrations = _storage_migrations()
    if database_url is not None:
        database = _storage_postgres().PostgresDatabase(database_url, pool_size=2)
        try:
            return migrations.current_postgres_revision(database)
        finally:
            database.close()
    catalog = _default_catalog_path(Path(data_dir).expanduser().resolve())
    return migrations.current_sqlite_revision(SQLiteDatabase(str(catalog)))


def _load_or_create_instance_key(path: Path, *, allow_create: bool) -> bytes:
    """Return the stable local key used only for non-reversible correlation fingerprints."""

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_bytes()
        if len(value) != 32:
            raise StorageError("instance correlation key is missing or corrupt") from None
        os.chmod(path, 0o600)
        return value
    if not allow_create:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise StorageError(
            "existing store is missing its correlation key; restore "
            "instance-correlation.key from the same backup as the catalog"
        )
    value = secrets.token_bytes(32)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return value


def _not_expired_sql(column: str) -> str:
    """Build the indexed decimal-text comparison used at read boundaries."""

    return f"({column} IS NULL OR length({column}) > ? OR (length({column}) = ? AND {column} > ?))"


# What a project owns; project deletion empties each, and only then marks the
# project deleted. The call tables come last, in deletion order.
_PROJECT_CALL_KEY_TABLES = (
    "otlp_resources",
    "call_key_generations",
    "call_tombstones",
    "otlp_counters",
)
_PROJECT_OWNED_TABLES = (
    "incidents",
    "delivery_receipts",
    "external_identities",
    "connectors",
    "api_keys",
    "turn_metrics",
    "pending_object_deletions",
    "pending_ingests",
    "legacy_deletion_reviews",
    "spans",
    "call_traces",
    "recordings",
    "call_revisions",
    "calls",
    *_PROJECT_CALL_KEY_TABLES,
    "pending_blob_deletions",
)


# Call revisions are incidents. A call's earlier revisions stay readable but
# are superseded by its latest one, so turn metrics count each call once; and a
# revision is never reaped on its own, only with its call.
_CALL_REVISION_SQL = (
    "EXISTS (SELECT 1 FROM call_revisions WHERE call_revisions.bundle_id = incidents.bundle_id)"
)
_SUPERSEDED_REVISION_SQL = (
    "EXISTS (SELECT 1 FROM call_revisions JOIN calls "
    "ON calls.project_id = call_revisions.project_id AND calls.call_id = call_revisions.call_id "
    "WHERE call_revisions.bundle_id = incidents.bundle_id "
    "AND call_revisions.revision <> calls.latest_revision)"
)


def _revision_call_id(connection: DatabaseConnection, bundle_id: str) -> str | None:
    """The call whose revision this incident is, or ``None``."""

    row = connection.execute(
        "SELECT call_id FROM call_revisions WHERE bundle_id = ?", (bundle_id,)
    ).fetchone()
    return None if row is None else str(row["call_id"])


def _expired_alone(connection: DatabaseConnection, bundle_id: str, expiry: str | None) -> bool:
    """Whether an incident's own retention deadline has passed and it expires alone.

    A call revision never expires alone, not even when read: it expires with its
    call, once the call's latest revision has.
    """

    return (
        expiry is not None
        and _decimal_lte(expiry, str(time.time_ns()))
        and _revision_call_id(connection, bundle_id) is None
    )


def _expired_sql(column: str) -> str:
    """Build the indexed decimal-text comparison used by retention cleanup."""

    return (
        f"({column} IS NOT NULL AND (length({column}) < ? "
        f"OR (length({column}) = ? AND {column} <= ?)))"
    )


def _sqlite_lock_contention(error: sqlite3.OperationalError) -> bool:
    """Whether SQLite refused for lock contention (``SQLITE_BUSY`` or ``SQLITE_LOCKED``)."""

    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return "locked" in str(error)


def _decimal_lte(value: str, other: str) -> bool:
    return len(value) < len(other) or (len(value) == len(other) and value <= other)


def encode_page_cursor(unix_nano: int | str, identifier: str) -> str:
    """A keyset page cursor: the last item's ``(time, ID)`` sort key, opaque to clients."""

    raw = json.dumps([str(unix_nano), identifier], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encode_cursor(record: IncidentRecord) -> str:
    return encode_page_cursor(record.ingested_at_unix_nano, record.bundle_id)


def decode_page_cursor(cursor: str) -> tuple[int, str]:
    """The ``(time, ID)`` of an ``encode_page_cursor`` value; ``InvalidCursorError`` else."""

    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_ENCODED_CHARS:
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(cursor + padding)
        if len(decoded) > _MAX_CURSOR_DECODED_BYTES:
            raise ValueError
        value = json.loads(decoded.decode("utf-8"))
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            # ASCII digits only: ``isdigit`` and ``int`` also take other scripts' digits.
            or not (value[0].isascii() and value[0].isdigit())
            or (len(value[0]) > 1 and value[0].startswith("0"))
            or not isinstance(value[1], str)
            or not value[1]
            or len(value[1]) > 256
            # An ID is UTF-8 text without NUL, as every stored ID is; JSON can carry a
            # lone surrogate (not encodable) or a NUL (refused by Postgres text).
            or "\x00" in value[1]
        ):
            raise ValueError
        value[1].encode("utf-8")
        cursor_time = int(value[0])
        if cursor_time > _SQLITE_INT64_MAX:
            raise ValueError
        return cursor_time, value[1]
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        RecursionError,
    ) as error:
        raise InvalidCursorError("invalid pagination cursor") from error


def _record(row: Row) -> IncidentRecord:
    return IncidentRecord(
        project_id=row["project_id"],
        bundle_id=row["bundle_id"],
        session_id=row["session_id"],
        schema_version=row["schema_version"],
        digest=row["object_digest"],
        size_bytes=row["size_bytes"],
        status=row["status"],
        finality=row["finality"],
        completeness=row["completeness"],
        framework=row["framework"],
        created_at_unix_nano=row["created_at_unix_nano"],
        ingested_at_unix_nano=str(row["ingested_at_unix_nano"]),
    )


def _turn_fact(row: Row) -> TurnFact:
    return TurnFact(
        project_id=row["project_id"],
        bundle_id=row["bundle_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        turn_index=row["turn_index"],
        started_at_unix_nano=row["started_at_unix_nano"],
        framework=row["framework"],
        provider=row["provider"],
        model=row["model"],
        language=row["language"],
        status=row["status"],
        stt_finalization_ms=row["stt_finalization_ms"],
        stt_finalization_availability=row["stt_finalization_availability"],
        stt_finalization_basis=row["stt_finalization_basis"],
        stt_finalization_confidence=row["stt_finalization_confidence"],
        stt_finalization_limitation=row["stt_finalization_limitation"],
        eou_ms=row["eou_ms"],
        eou_availability=row["eou_availability"],
        eou_basis=row["eou_basis"],
        eou_confidence=row["eou_confidence"],
        eou_limitation=row["eou_limitation"],
        first_token_ms=row["first_token_ms"],
        first_token_availability=row["first_token_availability"],
        first_token_basis=row["first_token_basis"],
        first_token_confidence=row["first_token_confidence"],
        first_token_limitation=row["first_token_limitation"],
        generated_response_ms=row["generated_response_ms"],
        generated_response_availability=row["generated_response_availability"],
        generated_response_basis=row["generated_response_basis"],
        generated_response_confidence=row["generated_response_confidence"],
        generated_response_limitation=row["generated_response_limitation"],
        sent_response_ms=row["sent_response_ms"],
        sent_response_availability=row["sent_response_availability"],
        sent_response_basis=row["sent_response_basis"],
        sent_response_confidence=row["sent_response_confidence"],
        sent_response_limitation=row["sent_response_limitation"],
        received_response_ms=row["received_response_ms"],
        received_response_availability=row["received_response_availability"],
        received_response_basis=row["received_response_basis"],
        received_response_confidence=row["received_response_confidence"],
        received_response_limitation=row["received_response_limitation"],
        render_start_response_ms=row["render_start_response_ms"],
        render_start_response_availability=row["render_start_response_availability"],
        render_start_response_basis=row["render_start_response_basis"],
        render_start_response_confidence=row["render_start_response_confidence"],
        render_start_response_limitation=row["render_start_response_limitation"],
        response_ms=row["response_ms"],
        response_availability=row["response_availability"],
        response_basis=row["response_basis"],
        response_confidence=row["response_confidence"],
        response_limitation=row["response_limitation"],
        turn_duration_ms=row["turn_duration_ms"],
        turn_duration_availability=row["turn_duration_availability"],
        turn_duration_basis=row["turn_duration_basis"],
        turn_duration_confidence=row["turn_duration_confidence"],
        turn_duration_limitation=row["turn_duration_limitation"],
        tool_operation_count=row["tool_operation_count"],
        tool_total_work_ms=row["tool_total_work_ms"],
        interruption_count=row["interruption_count"],
        interruption_availability=row["interruption_availability"],
        interruption_basis=row["interruption_basis"],
        interruption_confidence=row["interruption_confidence"],
        interruption_limitation=row["interruption_limitation"],
        projection_version=row["projection_version"],
        contract_version=row["contract_version"],
    )


def _project_record(row: Row) -> ProjectRecord:
    return ProjectRecord(
        project_id=row["project_id"],
        display_name=row["display_name"],
        created_at_unix_nano=str(row["created_at_unix_nano"]),
        capture_preset=row["capture_preset"],
        lifecycle_state=row["lifecycle_state"],
    )


def _connector_record(row: Row) -> ConnectorRecord:
    return ConnectorRecord(
        endpoint_id=row["endpoint_id"],
        project_id=row["project_id"],
        provider=row["provider"],
        secret_ref=row["secret_ref"],
        enabled=bool(row["enabled"]),
        normalizer_version=row["normalizer_version"],
        config_json=row["config_json"],
    )


class ContentAddressedObjects:
    """Small atomic SHA-256 object store.

    Objects are never named from user input.  A temporary file is flushed and
    atomically linked into its final content-addressed path.
    """

    def __init__(self, root: Path):
        self.root = root
        self.tmp = root.parent.parent / "tmp"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.tmp, 0o700)

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid SHA-256 digest")
        return self.root / digest[:2] / digest[2:]

    def put(self, payload: bytes) -> tuple[str, bool]:
        digest = _sha256(payload)
        destination = self.path_for(digest)
        if destination.exists():
            self._verify_path(destination, digest)
            return digest, False

        shard_existed = destination.parent.exists()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if not shard_existed:
            _fsync_directory(self.root)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix="object-", dir=self.tmp)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
                created = True
                _fsync_directory(destination.parent)
            except FileExistsError:
                created = False
            self._verify_path(destination, digest)
            return digest, created
        finally:
            temporary.unlink(missing_ok=True)
            _fsync_directory(self.tmp)

    def get(self, digest: str) -> bytes:
        path = self.path_for(digest)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactCorruptionError("incident artifact is missing") from error
        if _sha256(payload) != digest:
            raise ArtifactCorruptionError("incident artifact digest mismatch")
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
        return payload

    def delete(self, digest: str) -> None:
        self.delete_many((digest,))

    def delete_many(self, digests: tuple[str, ...]) -> None:
        """Unlink a proven-dead CAS batch and sync each changed shard once."""

        changed_directories: set[Path] = set()
        for digest in digests:
            path = self.path_for(digest)
            changed_directories.add(path.parent if path.parent.is_dir() else self.root)
            path.unlink(missing_ok=True)
        for directory in sorted(changed_directories):
            _fsync_directory(directory)

    def cleanup_temporary_files(self) -> int:
        removed = 0
        for path in self.tmp.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            _fsync_directory(self.tmp)
        return removed

    def has_objects(self) -> bool:
        return any(
            path.is_file()
            for directory in self.root.iterdir()
            if directory.is_dir()
            for path in directory.iterdir()
        )

    @staticmethod
    def _verify_path(path: Path, digest: str) -> None:
        try:
            actual = _sha256(path.read_bytes())
        except FileNotFoundError as error:
            raise ArtifactCorruptionError("content-addressed object disappeared") from error
        if actual != digest:
            raise ArtifactCorruptionError("content-addressed object is corrupt")
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)


class IncidentStore:
    """Thread/process-safe facade over the catalog and its canonical artifacts.

    On SQLite, purge uses ``secure_delete``, WAL checkpoint/truncation, ``VACUUM``,
    fsync, and CAS unlinking. This removes ordinary live/free-page remnants but is
    necessarily best effort: copy-on-write filesystems, SSD wear levelling,
    snapshots, and external backups can retain historical blocks. On Postgres,
    purge deletes the rows (artifact bytes included) and nothing more: the server
    reclaims the pages later (autovacuum), and its WAL, replicas and backups keep
    copies until they age out. Deployments that require cryptographic erasure
    should encrypt evidence with disposable keys.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        database_path: str | Path | None = None,
        database_url: str | None = None,
        correlation_key: bytes | None = None,
        auto_migrate: bool = True,
        clock: Clock | None = None,
    ):
        # Fail before touching the filesystem when the storage runtime is missing.
        self._migrations = _storage_migrations()
        self._auto_migrate = auto_migrate
        # The store's time source for its own schedules (credential verdicts,
        # usage records, compaction); tests inject a ``ManualClock``.
        self._clock: Clock = clock or SystemClock()
        self._credential_verdicts = CredentialVerdictCache(clock=self._clock)
        self._authentication_lock = threading.Lock()
        self._secret_verifications = 0
        self._verdict_cache_hits = 0
        self._key_use_recorded: dict[str, int] = {}
        self._last_compaction_monotonic: int | None = None
        self._write_lock = threading.RLock()
        self._compaction_thread_lock = threading.Lock()
        self._mutation_depth = 0
        self._mutation_thread: int | None = None
        self._mutation_deadline: float | None = None
        self._timing_lock = threading.Lock()
        self._mutation_timings: dict[str, MutationTimingStats] = {}
        self._closed = False
        self._process_owner_pid = os.getpid()
        self._fork_reset_error: str | None = None
        self._identity_key = b""
        self._anchor_connection: sqlite3.Connection | None = None
        self._database_uri = False
        if database_url is not None:
            if database_path is not None:
                raise ValueError("database_path and database_url are alternatives")
            self._open_postgres(data_dir, database_url, correlation_key)
            return
        if correlation_key is not None:
            raise ValueError("a SQLite store keeps its correlation key in its data directory")
        if data_dir is None:
            raise ValueError("a SQLite store needs a data directory")
        self._catalog_artifacts = False
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self._lock_path = self.data_dir / ".store.lock"
        self._compaction_lock_path = self.data_dir / ".compaction.lock"
        if database_path is not None and str(database_path) == ":memory:":
            # Each operation uses its own connection. A named shared-memory URI
            # plus an anchor keeps one logical database alive across those calls.
            self.database_path = f"file:earshot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._database_uri = True
            self._database = SQLiteDatabase(self.database_path, uri=True)
            self._anchor_connection = self._database.connect()
        else:
            database = (
                Path(database_path).expanduser().resolve()
                if database_path is not None
                else _default_catalog_path(self.data_dir)
            )
            database.parent.mkdir(parents=True, exist_ok=True)
            self.database_path = str(database)
            self._database = SQLiteDatabase(self.database_path)
        self.objects = ContentAddressedObjects(self.data_dir / "objects" / "sha256")
        self._lock_handle = _open_advisory_lock(self._lock_path)
        try:
            self._compaction_lock_handle = _open_advisory_lock(self._compaction_lock_path)
        except Exception:
            self._lock_handle.close()
            raise
        try:
            with self._mutation("open"):
                existing_catalog = not self._database_uri and self._existing_catalog_is_valid()
                existing_objects = self.objects.has_objects()
                existing_correlation_state = self._catalog_has_correlation_state()
                if not self._database_uri and existing_objects and not existing_catalog:
                    raise StorageError(
                        "incident catalog is missing or invalid while CAS evidence exists; "
                        "restore the SQLite catalog before opening this store"
                    )
                self._initialize()
                self._identity_key = _load_or_create_instance_key(
                    self.data_dir / "instance-correlation.key",
                    allow_create=not existing_correlation_state and not existing_objects,
                )
            self._reconcile()
            with _INCIDENT_STORES_LOCK:
                _INCIDENT_STORES.add(self)
        except Exception:
            if self._anchor_connection is not None:
                self._anchor_connection.close()
                self._anchor_connection = None
            self._lock_handle.close()
            self._compaction_lock_handle.close()
            raise

    def _open_postgres(
        self,
        data_dir: str | Path | None,
        database_url: str,
        correlation_key: bytes | None,
    ) -> None:
        """Open a Postgres catalog. Nothing is written to the local filesystem.

        Artifacts are catalog rows, the write lock is ``K_write`` and the
        correlation key comes from configuration, so ``data_dir`` is only
        recorded, never created.
        """

        postgres = _storage_postgres()
        self._catalog_artifacts = True
        self.data_dir = None if data_dir is None else Path(data_dir).expanduser().resolve()
        self.database_path = None
        self.objects = None
        key = resolve_correlation_key() if correlation_key is None else correlation_key
        if len(key) != 32:
            raise CorrelationKeyRequiredError("the correlation key must be 32 bytes")
        self._database = postgres.PostgresDatabase(database_url)
        try:
            with self._mutation("open"):
                self._initialize()
                self._identity_key = self._bind_correlation_key(key)
            self._reconcile()
            with _INCIDENT_STORES_LOCK:
                _INCIDENT_STORES.add(self)
        except BaseException:
            self._database.close()
            raise

    def _bind_correlation_key(self, key: bytes) -> bytes:
        """Record the key's ID on first open, and refuse any other key afterwards.

        Only a one-way key ID is stored. A catalog that already holds correlated
        rows but no key ID cannot prove which key wrote them, so it is refused
        rather than silently bound to whatever key is configured now.
        """

        key_id = _correlation_key_id(key)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM earshot_meta WHERE key = ?", (_CORRELATION_KEY_ID_META,)
            ).fetchone()
            if row is None:
                if self._catalog_has_correlation_state():
                    raise CorrelationKeyMismatchError(
                        "the catalog holds correlated data but no correlation key ID; "
                        "it cannot be matched to the configured key"
                    )
                connection.begin_write()
                connection.execute(
                    "INSERT INTO earshot_meta(key, value) VALUES (?, ?)",
                    (_CORRELATION_KEY_ID_META, key_id),
                )
            elif row["value"] != key_id:
                raise CorrelationKeyMismatchError(
                    "the configured correlation key is not the key this catalog was "
                    "written with; configure the original key"
                )
        return key

    def _reset_after_fork(self) -> None:
        """Give a child process its own thread and advisory-lock state."""

        child_pid = os.getpid()
        if self._process_owner_pid == child_pid:
            return
        self._write_lock = threading.RLock()
        self._compaction_thread_lock = threading.Lock()
        self._mutation_depth = 0
        self._mutation_thread = None
        self._mutation_deadline = None
        self._timing_lock = threading.Lock()
        # Another thread may have held these at fork; the child starts clean.
        self._authentication_lock = threading.Lock()
        self._credential_verdicts = CredentialVerdictCache(clock=self._clock)
        self._process_owner_pid = child_pid
        if self._catalog_artifacts:
            # Postgres: drop the inherited pool and lock connection (never closing
            # the parent's sessions); the child connects afresh on first use.
            if self._closed:
                self._fork_reset_error = "incident store is closed"
                return
            self._database.reset_after_fork()
            self._fork_reset_error = None
            return
        if self._closed:
            self._fork_reset_error = "incident store is closed"
            with contextlib.suppress(OSError):
                self._lock_handle.close()
            with contextlib.suppress(OSError):
                self._compaction_lock_handle.close()
            return
        if self._database_uri:
            self._fork_reset_error = "an in-memory incident store cannot be reused after fork"
            with contextlib.suppress(OSError):
                self._lock_handle.close()
            with contextlib.suppress(OSError):
                self._compaction_lock_handle.close()
            return
        if self._lock_handle.closed or self._compaction_lock_handle.closed:
            self._fork_reset_error = "the inherited incident store is closed"
            with contextlib.suppress(OSError):
                self._lock_handle.close()
            with contextlib.suppress(OSError):
                self._compaction_lock_handle.close()
            return
        replacement_store_lock = None
        replacement_compaction_lock = None
        try:
            replacement_store_lock = _open_advisory_lock(self._lock_path)
            replacement_compaction_lock = _open_advisory_lock(self._compaction_lock_path)
        except Exception:
            for replacement_handle in (replacement_store_lock, replacement_compaction_lock):
                if replacement_handle is not None:
                    with contextlib.suppress(OSError):
                        replacement_handle.close()
            self._fork_reset_error = "the incident store lock could not be reopened after fork"
            with contextlib.suppress(OSError):
                self._lock_handle.close()
            with contextlib.suppress(OSError):
                self._compaction_lock_handle.close()
            return

        inherited_store_lock = self._lock_handle
        inherited_compaction_lock = self._compaction_lock_handle
        self._lock_handle = replacement_store_lock
        self._compaction_lock_handle = replacement_compaction_lock
        self._fork_reset_error = None
        for inherited_handle in (inherited_store_lock, inherited_compaction_lock):
            with contextlib.suppress(OSError):
                inherited_handle.close()

    def _ensure_process_ownership(self) -> None:
        if self._closed:
            raise StorageError("incident store is closed")
        if self._process_owner_pid != os.getpid():
            self._reset_after_fork()
        if self._fork_reset_error is not None:
            raise StorageError(self._fork_reset_error)

    def _existing_catalog_is_valid(self) -> bool:
        """Whether the SQLite catalog file exists, passes ``quick_check`` and has incidents.

        It guards the file CAS against a lost catalog file, so only SQLite runs it:
        a Postgres catalog has no file to lose and keeps its artifacts inside.
        """

        self._database.dialect.only_sqlite("catalog-file-validity-probe")
        if self._database_uri:
            return True
        database = Path(self.database_path)
        if not database.is_file() or database.stat().st_size == 0:
            return False
        try:
            uri = database.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, timeout=1.0, uri=True) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    return False
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                return "incidents" in tables
        except (OSError, sqlite3.Error, ValueError):
            return False

    def _catalog_has_correlation_state(self) -> bool:
        if self._catalog_artifacts:
            # Postgres: the schema is current (this runs after migration) and any
            # read failure propagates, so the open fails closed.
            with self._connect() as connection:
                return any(
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                    for table in (
                        "incidents",
                        "delivery_receipts",
                        "external_identities",
                        "tombstones",
                    )
                )
        self._database.dialect.only_sqlite("catalog-file-correlation-probe")
        if self._database_uri:
            return False
        database = Path(self.database_path)
        if not database.is_file() or database.stat().st_size == 0:
            return False
        try:
            uri = database.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, timeout=1.0, uri=True) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                for table in (
                    "incidents",
                    "delivery_receipts",
                    "external_identities",
                    "tombstones",
                ):
                    if table == "tombstones" and schema_version <= 3:
                        columns = {
                            row[1] for row in connection.execute("PRAGMA table_info(tombstones)")
                        }
                        # Only the old plaintext shape predates hosted replay IDs.
                        # A version rollback must not exempt current hashed tombstones.
                        if "bundle_id" in columns and "bundle_id_sha256" not in columns:
                            continue
                    if (
                        table in tables
                        and connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    ):
                        return True
        except (OSError, sqlite3.Error, ValueError):
            # Unreadable catalog state is not proof that the correlation key is
            # disposable. Fail closed if key recovery depends on this result.
            return True
        return False

    @contextlib.contextmanager
    def _mutation(self, operation: str, *, budget_s: float | None = None) -> Iterator[None]:
        """Serialize filesystem+database mutations across threads and processes.

        The outermost mutation records how long it waited for the locks and how
        long it held them under ``operation`` (``mutation_timing_stats``), and a
        hold longer than ``SLOW_MUTATION_SECONDS`` logs ``EARSHOT_SLOW_MUTATION``.

        Without ``budget_s`` a mutation waits for its locks as long as it takes.
        With it, the total wait for every lock is bounded: the in-process lock,
        then ``K_write`` (Postgres) or ``.store.lock`` (SQLite), then on SQLite the
        database's own write lock, whose ``busy_timeout`` is clamped to what is
        left. Past the budget it raises ``StorageBusyError`` and writes nothing.
        """

        self._ensure_process_ownership()
        started = time.perf_counter()
        deadline = None if budget_s is None else started + budget_s
        if deadline is None:
            self._write_lock.acquire()
        elif not self._write_lock.acquire(timeout=max(0.0, deadline - started)):
            raise StorageBusyError(f"{operation}: the store is busy in this process")
        try:
            if self._closed:
                raise StorageError("incident store is closed")
            if self._mutation_depth > 0:
                # Nested: the outermost mutation already holds every lock.
                self._mutation_depth += 1
                try:
                    yield
                finally:
                    self._mutation_depth -= 1
                return
            with self._store_write_lock(operation, deadline):
                acquired = time.perf_counter()
                self._mutation_depth += 1
                self._mutation_thread = threading.get_ident()
                self._mutation_deadline = deadline
                try:
                    yield
                except sqlite3.OperationalError as error:
                    if deadline is not None and _sqlite_lock_contention(error):
                        raise StorageBusyError(
                            f"{operation}: the SQLite write lock stayed busy past the budget"
                        ) from error
                    raise
                finally:
                    self._mutation_deadline = None
                    self._mutation_thread = None
                    self._mutation_depth -= 1
                    released = time.perf_counter()
                    self._record_mutation_timing(operation, acquired - started, released - acquired)
        finally:
            self._write_lock.release()

    @contextlib.contextmanager
    def _store_write_lock(self, operation: str, deadline: float | None) -> Iterator[None]:
        """The cross-process write lock: ``K_write`` on Postgres, ``.store.lock`` on SQLite."""

        if self._catalog_artifacts:
            # Postgres: K_write, held across every transaction of this mutation.
            with self._database.write_lock(deadline=deadline):
                yield
            return
        if fcntl is not None:
            self._lock_store_file(operation, deadline)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)

    def _lock_store_file(self, operation: str, deadline: float | None) -> None:
        descriptor = self._lock_handle.fileno()
        if deadline is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise StorageBusyError(
                        f"{operation}: another process holds the store write lock"
                    ) from None
                time.sleep(min(LOCK_POLL_SECONDS, remaining))

    def _record_mutation_timing(self, operation: str, wait: float, hold: float) -> None:
        with self._timing_lock:
            previous = self._mutation_timings.get(operation)
            self._mutation_timings[operation] = MutationTimingStats(
                count=1 if previous is None else previous.count + 1,
                last_wait_seconds=wait,
                last_hold_seconds=hold,
                max_wait_seconds=wait if previous is None else max(previous.max_wait_seconds, wait),
                max_hold_seconds=hold if previous is None else max(previous.max_hold_seconds, hold),
            )
        if hold > SLOW_MUTATION_SECONDS:
            _LOGGER.warning(
                "EARSHOT_SLOW_MUTATION: %s held the store write lock for %.3f s "
                "(waited %.3f s for it)",
                operation,
                hold,
                wait,
            )

    def mutation_timing_stats(self) -> dict[str, MutationTimingStats]:
        """Lock wait and hold durations of this store's mutations, by operation name.

        Only outermost mutations are counted; a nested one runs inside its
        caller's hold. The names are fixed by the code, so the map stays small.
        """

        with self._timing_lock:
            return dict(self._mutation_timings)

    def _connect(self) -> DatabaseConnection:
        connection = self._database.connect()
        deadline = self._mutation_deadline
        if (
            deadline is not None
            and not self._catalog_artifacts
            and self._mutation_thread == threading.get_ident()
        ):
            # A budgeted mutation waits for SQLite's write lock only as long as
            # its budget has left (the connection default is 10 s).
            remaining_ms = max(0, int((deadline - time.perf_counter()) * 1000))
            connection.execute(f"PRAGMA busy_timeout = {remaining_ms}")
        return connection

    @contextlib.contextmanager
    def _listing_connection(self) -> Iterator[DatabaseConnection]:
        """Open a listing snapshot with the locking its SQLite mode requires."""

        # WAL readers do not block file-backed writers, so only hold the local
        # guard while opening their snapshot connection. Shared-cache in-memory
        # databases have table locks instead; serialize the complete read with
        # this store's writes to avoid SQLITE_LOCKED races.
        serialization = self._write_lock if self._database_uri else contextlib.nullcontext()
        with serialization:
            with self._write_lock:
                self._ensure_process_ownership()
                connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        """Bring the catalog schema to the head revision, or refuse to open it."""

        with self._mutation("migrate"):
            if self._catalog_artifacts:
                self._migrations.prepare_postgres_catalog(
                    self._database,
                    auto_migrate=self._auto_migrate,
                )
                self._migrations.require_postgres_head_shape(self._database)
                return
            self._migrations.prepare_sqlite_catalog(
                self._database,
                auto_migrate=self._auto_migrate,
            )
            self._migrations.require_sqlite_head_shape(self._database)
        self._harden_database_permissions()
        if not self._database_uri:
            _fsync_directory(Path(self.database_path).parent)

    def _harden_database_permissions(self) -> None:
        if self._database_uri or self._catalog_artifacts:
            return
        database = Path(self.database_path)
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    def _reconcile(self) -> None:
        """Recover interrupted writes and honor retention at open; never re-project.

        Every step here is bounded by recovery or retention work, not by the size
        of the catalog. Incidents whose projections are older than this release
        (``projections_pending``) are re-projected later by ``reproject_stale``,
        which the server runs in the background once it is serving.
        """

        if not self._catalog_artifacts:
            with self._mutation("startup_cleanup_temporary_files"):
                self.objects.cleanup_temporary_files()
        while True:
            with self._mutation("startup_recover_ingests"):
                recovered_ingests = self._cleanup_pending_ingests_locked(
                    limit=_STARTUP_EXPIRED_PURGE_BATCH_SIZE
                )
            if recovered_ingests < _STARTUP_EXPIRED_PURGE_BATCH_SIZE:
                break
        # Honor indexed retention before anything is served. Expiry cleanup uses
        # bounded lock intervals so a startup backlog does not monopolize a store
        # shared with already-running workers.
        self._purge_expired_for_reconciliation(str(time.time_ns()))
        with self._mutation("startup_scrub_check"):
            scrub_required = self._scrub_required_locked()
        if scrub_required:
            # A process may have crashed after logical deletion/CAS unlink but
            # before checkpoint/VACUUM. The generation makes this retry precise.
            # Keep SQLite compaction outside the cross-process mutation lock.
            try:
                self._scrub_deleted_pages(skip_if_current=True)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error
        self._harden_database_permissions()

    def projections_pending(self) -> int:
        """How many incidents still have projections from an older release.

        They are readable, but their graph rows, turn facts, and expiry and export
        columns may be missing or out of date until ``reproject_stale`` reaches
        them. Fresh ingests are always current. The count uses an index.
        """

        self._ensure_process_ownership()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE projection_version IS NULL OR projection_version < ?",
                (INCIDENT_PROJECTION_VERSION,),
            ).fetchone()
        return int(row[0])

    def reproject_stale(
        self,
        *,
        limit: int = REPROJECTION_BATCH_SIZE,
        after: str | None = None,
    ) -> ReprojectionBatch:
        """Re-project up to ``limit`` stale incidents, in bundle order after ``after``.

        Artifacts are read, decoded and analysed without the write lock; one
        short mutation then writes the batch, skipping any incident that was
        purged, replaced or re-projected in the meantime. An incident whose
        artifact cannot be read, decoded or analysed stays stale and is named in
        ``failed``; it never holds up the others. Expired incidents are left to
        the retention reaper. ``resume_after`` continues this pass and is
        ``None`` once the pass has reached the last stale incident.
        """

        from .codec import decode_incident_protobuf

        if not 1 <= limit <= MAX_EXPIRED_PURGE_BATCH_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_EXPIRED_PURGE_BATCH_SIZE}")
        self._ensure_process_ownership()
        now = str(time.time_ns())
        parameters: tuple[object, ...] = (INCIDENT_PROJECTION_VERSION, len(now), len(now), now)
        cursor_filter = ""
        if after is not None:
            cursor_filter = " AND bundle_id > ?"
            parameters += (after,)
        computed: list[tuple[str, ProjectionRows]] = []
        failed: list[str] = []
        failure_kinds: set[str] = set()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT bundle_id, project_id, object_digest FROM incidents "
                "WHERE (projection_version IS NULL OR projection_version < ?) AND "
                + _not_expired_sql("expires_at_unix_nano")
                + cursor_filter
                + " ORDER BY bundle_id LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            for row in rows:
                try:
                    payload = self._read_artifact(connection, row["object_digest"])
                    bundle = decode_incident_protobuf(payload)
                    if bundle.profile.manifest.bundle_id != row["bundle_id"]:
                        raise ArtifactCorruptionError(
                            "stored incident identity does not match its index"
                        )
                    projection = compute_projection_rows(
                        bundle, row["project_id"], canonical_payload=payload
                    )
                except DATABASE_ERRORS:
                    raise
                except Exception as error:
                    # This incident cannot be projected; the rest of the batch can.
                    failed.append(row["bundle_id"])
                    failure_kinds.add(type(error).__name__)
                    continue
                computed.append((row["object_digest"], projection))
        reprojected = 0
        if computed:
            with self._mutation("reproject_stale"), self._connect() as connection:
                connection.begin_write()
                bundle_ids = [projection.bundle_id for _, projection in computed]
                current: dict[str, tuple[str, int | None]] = {}
                for offset in range(0, len(bundle_ids), _SQLITE_SAFE_BIND_LIMIT):
                    chunk = bundle_ids[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
                    placeholders = ", ".join("?" for _ in chunk)
                    current.update(
                        (row["bundle_id"], (row["object_digest"], row["projection_version"]))
                        for row in connection.execute(
                            "SELECT bundle_id, object_digest, projection_version "
                            f"FROM incidents WHERE bundle_id IN ({placeholders})",
                            chunk,
                        ).fetchall()
                    )
                # Skip anything purged, replaced or re-projected since it was read.
                still_stale = [
                    projection
                    for digest, projection in computed
                    if projection.bundle_id in current
                    and current[projection.bundle_id][0] == digest
                    and (
                        current[projection.bundle_id][1] is None
                        or current[projection.bundle_id][1] < INCIDENT_PROJECTION_VERSION
                    )
                ]
                write_projection_rows(connection, still_stale)
                connection.commit()
                reprojected = len(still_stale)
        if failed:
            _LOGGER.warning(
                "EARSHOT_PROJECTION_FAILED: %d stale incidents could not be re-projected "
                "(%s); they stay pending",
                len(failed),
                ", ".join(sorted(failure_kinds)),
            )
        return ReprojectionBatch(
            reprojected=reprojected,
            failed=tuple(failed),
            resume_after=rows[-1]["bundle_id"] if len(rows) == limit else None,
        )

    def mark_projections_stale(self) -> int:
        """Mark every incident's projections stale, so the next pass rebuilds them all.

        An operator tool: after restoring a backup, or to repair projections lost
        in some other way, it makes re-projection verify every artifact's digest
        and identity and rebuild every projected row. Returns how many incidents.
        """

        with self._mutation("mark_projections_stale"), self._connect() as connection:
            connection.begin_write()
            marked = connection.execute("UPDATE incidents SET projection_version = NULL").rowcount
            connection.commit()
        return int(marked)

    def converge_projections(
        self,
        *,
        batch_size: int = REPROJECTION_BATCH_SIZE,
        time_budget_seconds: float | None = None,
    ) -> ProjectionConvergence:
        """Make one full pass of ``reproject_stale`` over every stale incident.

        This is the path for a process without the server's background task (the
        CLI). With ``time_budget_seconds`` it stops after the batch that crosses
        the budget; ``pending`` says what is left either way.
        """

        if self.projections_pending() == 0:
            return ProjectionConvergence(reprojected=0, failed=(), pending=0)
        deadline = (
            None if time_budget_seconds is None else time.perf_counter() + time_budget_seconds
        )
        reprojected = 0
        failed: list[str] = []
        after: str | None = None
        while True:
            batch = self.reproject_stale(limit=batch_size, after=after)
            reprojected += batch.reprojected
            failed.extend(batch.failed)
            after = batch.resume_after
            if after is None or (deadline is not None and time.perf_counter() >= deadline):
                break
        return ProjectionConvergence(
            reprojected=reprojected,
            failed=tuple(failed),
            pending=self.projections_pending(),
        )

    def _read_artifact(self, connection: DatabaseConnection, digest: str) -> bytes:
        """Return verified canonical bytes: a CAS file on SQLite, a catalog row on Postgres."""

        if not self._catalog_artifacts:
            return self.objects.get(digest)
        row = connection.execute(
            "SELECT payload FROM artifacts WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise ArtifactCorruptionError("incident artifact is missing")
        payload = bytes(row["payload"])
        if _sha256(payload) != digest:
            raise ArtifactCorruptionError("incident artifact digest mismatch")
        return payload

    def _delete_artifact_files(self, digests: tuple[str, ...]) -> None:
        """Unlink proven-dead CAS files. A Postgres catalog never journals any."""

        if not digests:
            return
        if self._catalog_artifacts:
            raise StorageError("a Postgres catalog has no artifact files to delete")
        self.objects.delete_many(digests)

    def _purge_expired_for_reconciliation(self, now: str) -> int:
        # The ingest-order high-water mark defines an exact, finite startup
        # workset without counting or materializing every expired row. New
        # arrivals receive larger sequence values and remain for the reaper.
        with self._mutation("startup_expiry_high_water"), self._connect() as connection:
            row = connection.execute(
                connection.dialect.sql(
                    "ingest-order-high-water",
                    sqlite="SELECT seq FROM sqlite_sequence WHERE name = 'incident_ingest_order'",
                    # A sequence is never reused, so the largest stored value bounds
                    # every row that exists now, exactly as the AUTOINCREMENT mark does.
                    postgresql="SELECT COALESCE(MAX(sequence), 0) FROM incident_ingest_order",
                )
            ).fetchone()
            high_water = int(row[0]) if row is not None else 0

        removed = 0
        while True:
            bundle_ids = self._expired_bundle_ids(
                now,
                _STARTUP_EXPIRED_PURGE_BATCH_SIZE,
                ingest_sequence_high_water=high_water,
            )
            if not bundle_ids:
                self._finish_reconciliation_expiry_cleanup()
                break
            batch_removed = self._purge_expired_reconciliation_batch(
                now,
                bundle_ids,
                ingest_sequence_high_water=high_water,
            )
            removed += batch_removed
        return removed

    def close(self) -> None:
        with self._write_lock, self._compaction_thread_lock:
            if not self._closed and self._catalog_artifacts:
                self._closed = True
                if self._process_owner_pid == os.getpid():
                    self._database.close()
            elif not self._closed:
                self._closed = True
                if self._anchor_connection is not None:
                    self._anchor_connection.close()
                    self._anchor_connection = None
                if not self._lock_handle.closed:
                    self._lock_handle.close()
                if not self._compaction_lock_handle.closed:
                    self._compaction_lock_handle.close()
        with _INCIDENT_STORES_LOCK:
            _INCIDENT_STORES.discard(self)

    def __enter__(self) -> IncidentStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ready(self) -> bool:
        try:
            self._ensure_process_ownership()
            with self._write_lock:
                if self._closed:
                    return False
                with self._connect() as connection:
                    connection.execute("SELECT 1").fetchone()
                if self._catalog_artifacts:
                    return True
                return os.access(self.objects.root, os.W_OK) and os.access(
                    self.objects.tmp, os.W_OK
                )
        except (StorageError, OSError, *DATABASE_ERRORS):
            return False

    @staticmethod
    def _require_active_project(connection: DatabaseConnection, project_id: str) -> None:
        row = connection.execute(
            "SELECT lifecycle_state FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValueError("project does not exist")
        if row["lifecycle_state"] != "active":
            raise ProjectInactiveError("project is not accepting writes")

    def project_lifecycle(self, project_id: str) -> str | None:
        """Return the durable lifecycle state, or ``None`` if never provisioned."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT lifecycle_state FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return None if row is None else str(row["lifecycle_state"])

    def project_is_active(self, project_id: str) -> bool:
        return self.project_lifecycle(project_id) == "active"

    @contextlib.contextmanager
    def project_access_scope(self, project_id: str) -> Iterator[None]:
        """Hold the store lock while a bounded in-memory access uses project data.

        The store lock is process and file locked. Holding it across a bounded
        registry operation makes deletion's durable state change wait for work
        that already entered, while new work sees the deleting marker and is
        refused.
        """

        with self._mutation("project_access_scope"):
            with self._connect() as connection:
                self._require_active_project(connection, project_id)
            yield

    @contextlib.contextmanager
    def project_write_scope(self, project_id: str) -> Iterator[None]:
        """Fence an in-memory or file-backed write against project deletion."""

        with self.project_access_scope(project_id):
            yield

    def begin_project_deletion(self, project_id: str) -> str:
        """Persist the write fence; repeated calls preserve deletion progress."""

        if project_id == DEFAULT_PROJECT_ID:
            raise ValueError("the built-in default project cannot be deleted")
        with self._mutation("begin_project_deletion"), self._connect() as connection:
            connection.begin_write()
            project = connection.execute(
                "SELECT lifecycle_state FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise ValueError("project does not exist")
            state = str(project["lifecycle_state"])
            if state == "active":
                connection.execute(
                    """
                    UPDATE projects
                    SET lifecycle_state = 'deleting', delete_started_at_unix_nano = ?
                    WHERE project_id = ? AND lifecycle_state = 'active'
                    """,
                    (time.time_ns(), project_id),
                )
                state = "deleting"
            connection.commit()
            return state

    def mark_project_deleted(self, project_id: str) -> None:
        """Complete the lifecycle only after all owned data cleanup has succeeded."""

        with self._mutation("mark_project_deleted"), self._connect() as connection:
            connection.begin_write()
            project = connection.execute(
                "SELECT lifecycle_state FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise ValueError("project does not exist")
            if project["lifecycle_state"] == "deleted":
                connection.commit()
                return
            if project["lifecycle_state"] != "deleting":
                raise StorageError("project deletion has not been started")
            for table in _PROJECT_OWNED_TABLES:
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                    (project_id,),
                ).fetchone():
                    raise StorageError("project still has owned data pending cleanup")
            connection.execute(
                """
                UPDATE projects
                SET display_name = '', lifecycle_state = 'deleted', deleted_at_unix_nano = ?
                WHERE project_id = ? AND lifecycle_state = 'deleting'
                """,
                (time.time_ns(), project_id),
            )
            connection.commit()

    def delete_project_data(
        self,
        project_id: str,
        *,
        batch_size: int = 500,
        finalize: bool = True,
        compact: bool = True,
    ) -> bool:
        """Fence a project, erase its catalog and CAS data, and retry failed cleanup.

        Each call removes at most ``batch_size`` selected incident rows, then
        applies the same limit separately to each auxiliary table.
        ``False`` means the durable fence is in place and another bounded batch
        or physical cleanup retry is required. Returning between batches releases
        the store-wide lock. The project row and hashed incident tombstones remain
        so the same signed project token can safely resume deletion and old bundle
        IDs cannot be reused.
        """

        if batch_size < 1 or batch_size > 500:
            raise ValueError("project deletion batch size must be between 1 and 500")
        lifecycle = self.begin_project_deletion(project_id)
        if lifecycle == "deleted":
            return True
        try:
            with self._mutation("delete_project_data"):
                with self._connect() as connection:
                    connection.begin_write()
                    rows = connection.execute(
                        """
                        SELECT bundle_id, object_digest FROM incidents
                        WHERE project_id = ?
                        ORDER BY bundle_id
                        LIMIT ?
                        """,
                        (project_id, batch_size),
                    ).fetchall()
                    if rows:
                        now = time.time_ns()
                        connection.executemany(
                            """
                            INSERT INTO tombstones(
                                bundle_id_sha256, project_id, purged_at_unix_nano
                            ) VALUES (?, ?, ?)
                            ON CONFLICT(bundle_id_sha256) DO NOTHING
                            """,
                            ((_tombstone_key(row["bundle_id"]), project_id, now) for row in rows),
                        )
                        connection.executemany(
                            "DELETE FROM incidents WHERE project_id = ? AND bundle_id = ?",
                            ((project_id, row["bundle_id"]) for row in rows),
                        )
                        self._queue_unreferenced_object_deletions(
                            connection,
                            project_id,
                            {row["object_digest"] for row in rows},
                        )

                    incidents_remain = (
                        connection.execute(
                            "SELECT 1 FROM incidents WHERE project_id = ? LIMIT 1",
                            (project_id,),
                        ).fetchone()
                        is not None
                    )
                    if not incidents_remain:
                        from .storage_call_purge import delete_project_calls

                        # Calls and all their evidence, then parked traces;
                        # their blobs are journaled for deletion after commit.
                        calls_remain = not delete_project_calls(
                            connection,
                            project_id,
                            limit=batch_size,
                            now=self._clock.unix_nano(),
                        )
                        for table in (
                            "delivery_receipts",
                            "external_identities",
                            "connectors",
                            "api_keys",
                            "turn_metrics",
                            # What only calls referred to, once no call is left.
                            *(() if calls_remain else _PROJECT_CALL_KEY_TABLES),
                        ):
                            connection.execute(
                                connection.dialect.sql(
                                    "batched-project-row-delete",
                                    sqlite=f"""
                                DELETE FROM {table}
                                WHERE rowid IN (
                                    SELECT rowid FROM {table}
                                    WHERE project_id = ? LIMIT ?
                                )
                                """,
                                    # ctid is the row's physical address; it is
                                    # stable within this statement, and every
                                    # writer waits on K_write meanwhile.
                                    postgresql=f"""
                                DELETE FROM {table}
                                WHERE ctid = ANY(ARRAY(
                                    SELECT ctid FROM {table}
                                    WHERE project_id = ? LIMIT ?
                                ))
                                """,
                                ),
                                (project_id, batch_size),
                            )
                            if connection.execute(
                                f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                                (project_id,),
                            ).fetchone():
                                break
                    connection.commit()

                self._cleanup_pending_object_deletions_locked(
                    project_id=project_id,
                    limit=batch_size,
                )
                self._cleanup_pending_ingests_locked(
                    project_id=project_id,
                    limit=batch_size,
                )
                with self._connect() as connection:
                    # Journaled recording blobs are deleted after commit by the
                    # RecordingStore (``drain_blob_deletions``); until they are,
                    # deletion stays pending.
                    pending = any(
                        connection.execute(
                            f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                            (project_id,),
                        ).fetchone()
                        is not None
                        for table in _PROJECT_OWNED_TABLES
                    )
                if pending:
                    return False
        except StorageCleanupPendingError:
            return False
        except (OSError, *DATABASE_ERRORS):
            return False

        if compact:
            try:
                # SQLite page compaction is global. Run it after releasing the
                # application mutation lock so other projects are not held behind
                # deletion bookkeeping; SQLite itself arbitrates file-level access.
                self._scrub_deleted_pages()
            except StorageCleanupPendingError:
                return False
            except (OSError, *DATABASE_ERRORS):
                return False

        if finalize:
            self.mark_project_deleted(project_id)
        return True

    def _queue_unreferenced_object_deletions(
        self,
        connection: DatabaseConnection,
        project_id: str,
        digests: set[str],
    ) -> None:
        """Release the artifacts of just-deleted incidents that nothing references now.

        SQLite journals the CAS files for deletion after commit. Postgres deletes
        the ``artifacts`` rows in the caller's transaction: liveness is derived
        from ``incidents.object_digest`` (``incidents_digest_idx``), never counted.
        """

        digest_values = tuple(sorted(digests))
        for offset in range(0, len(digest_values), _SQLITE_SAFE_BIND_LIMIT):
            batch = digest_values[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
            placeholders = ",".join("?" for _ in batch)
            if self._catalog_artifacts:
                connection.execute(
                    f"DELETE FROM artifacts WHERE digest IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM incidents "
                    "WHERE incidents.object_digest = artifacts.digest)",
                    batch,
                )
                continue
            referenced = {
                row["object_digest"]
                for row in connection.execute(
                    "SELECT DISTINCT object_digest FROM incidents "
                    f"WHERE object_digest IN ({placeholders})",
                    batch,
                ).fetchall()
            }
            deletions = tuple(
                (project_id, digest, time.time_ns()) for digest in batch if digest not in referenced
            )
            insert_batch_size = _SQLITE_SAFE_BIND_LIMIT // 3
            for deletion_offset in range(0, len(deletions), insert_batch_size):
                deletion_batch = deletions[deletion_offset : deletion_offset + insert_batch_size]
                values = ",".join("(?, ?, ?)" for _ in deletion_batch)
                parameters = tuple(value for row in deletion_batch for value in row)
                connection.execute(
                    """
                    INSERT INTO pending_object_deletions(
                        project_id, object_digest, queued_at_unix_nano
                    ) VALUES """
                    + values
                    + " ON CONFLICT(project_id, object_digest) DO NOTHING",
                    parameters,
                )

    def _cleanup_pending_ingests_locked(
        self,
        *,
        project_id: str | None = None,
        intent_id: str | None = None,
        limit: int = 500,
    ) -> int:
        """Remove crash-interrupted CAS publications with recorded project ownership."""

        if limit < 1:
            raise ValueError("pending ingest cleanup limit must be positive")
        with self._connect() as connection:
            connection.begin_write()
            if intent_id is not None:
                if project_id is None:
                    rows = connection.execute(
                        "SELECT intent_id, project_id, object_digest FROM pending_ingests "
                        "WHERE intent_id = ? LIMIT ?",
                        (intent_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT intent_id, project_id, object_digest FROM pending_ingests "
                        "WHERE project_id = ? AND intent_id = ? LIMIT ?",
                        (project_id, intent_id, limit),
                    ).fetchall()
            elif project_id is None:
                rows = connection.execute(
                    "SELECT intent_id, project_id, object_digest FROM pending_ingests "
                    "ORDER BY started_at_unix_nano, intent_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT intent_id, project_id, object_digest FROM pending_ingests "
                    "WHERE project_id = ? ORDER BY started_at_unix_nano, intent_id LIMIT ?",
                    (project_id, limit),
                ).fetchall()

            digests = tuple(dict.fromkeys(row["object_digest"] for row in rows))
            referenced: set[str] = set()
            for offset in range(0, len(digests), _SQLITE_SAFE_BIND_LIMIT):
                batch = digests[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
                placeholders = ",".join("?" for _ in batch)
                referenced.update(
                    row["object_digest"]
                    for row in connection.execute(
                        "SELECT DISTINCT object_digest FROM incidents "
                        f"WHERE object_digest IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            self._delete_artifact_files(
                tuple(digest for digest in digests if digest not in referenced)
            )
            for offset in range(0, len(rows), _SQLITE_SAFE_BIND_LIMIT):
                batch = rows[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    f"DELETE FROM pending_ingests WHERE intent_id IN ({placeholders})",
                    tuple(row["intent_id"] for row in batch),
                )
            if rows and not self._catalog_artifacts:
                self._mark_scrub_required(connection)
            connection.commit()
        return len(rows)

    def _cleanup_pending_object_deletions_locked(
        self,
        *,
        project_id: str | None = None,
        limit: int = 500,
    ) -> int:
        """Delete only CAS objects recorded by an artifact erasure transaction."""

        if limit < 1:
            raise ValueError("pending object cleanup limit must be positive")
        with self._connect() as connection:
            connection.begin_write()
            if project_id is None:
                rows = connection.execute(
                    """
                    SELECT project_id, object_digest FROM pending_object_deletions
                    ORDER BY queued_at_unix_nano, project_id, object_digest LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT project_id, object_digest FROM pending_object_deletions
                    WHERE project_id = ?
                    ORDER BY queued_at_unix_nano, object_digest LIMIT ?
                    """,
                    (project_id, limit),
                ).fetchall()
            digests = tuple(dict.fromkeys(row["object_digest"] for row in rows))
            referenced: set[str] = set()
            for offset in range(0, len(digests), _SQLITE_SAFE_BIND_LIMIT):
                batch = digests[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
                placeholders = ",".join("?" for _ in batch)
                referenced.update(
                    row["object_digest"]
                    for row in connection.execute(
                        "SELECT DISTINCT object_digest FROM incidents "
                        f"WHERE object_digest IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            self._delete_artifact_files(
                tuple(digest for digest in digests if digest not in referenced)
            )
            delete_batch_size = _SQLITE_SAFE_BIND_LIMIT // 2
            for offset in range(0, len(rows), delete_batch_size):
                batch = rows[offset : offset + delete_batch_size]
                values = ",".join("(?, ?)" for _ in batch)
                parameters = tuple(
                    value for row in batch for value in (row["project_id"], row["object_digest"])
                )
                connection.execute(
                    "DELETE FROM pending_object_deletions "
                    f"WHERE (project_id, object_digest) IN ({values})",
                    parameters,
                )
            if rows and not self._database_uri:
                self._mark_scrub_required(connection)
            connection.commit()
        return len(rows)

    @staticmethod
    def _mark_scrub_required(connection: DatabaseConnection) -> None:
        connection.execute(
            "UPDATE storage_maintenance "
            "SET purge_generation = purge_generation + 1 WHERE singleton = 1"
        )

    def _scrub_required_locked(self) -> bool:
        # Neither an in-memory SQLite catalog nor a Postgres one has pages for this
        # process to scrub; Postgres purges never record a scrub generation.
        if self._database_uri or self._catalog_artifacts:
            return False
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT purge_generation > scrubbed_generation
                    FROM storage_maintenance WHERE singleton = 1
                    """
                ).fetchone()[0]
                == 1
            )

    def create_project(self, project_id: str, *, display_name: str) -> ProjectRecord:
        """Create an authorization scope without exposing storage details to callers."""

        if not _PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be a lowercase portable identifier")
        if not display_name or len(display_name) > 128:
            raise ValueError("display_name must contain between 1 and 128 characters")
        created_at = time.time_ns()
        with self._mutation("create_project"), self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, display_name, created_at_unix_nano)
                VALUES (?, ?, ?)
                """,
                (project_id, display_name, created_at),
            )
            connection.commit()
        return ProjectRecord(project_id, display_name, str(created_at))

    def provision_project(
        self, project_id: str, *, display_name: str, capture_preset: str
    ) -> tuple[ProjectRecord, bool]:
        """Create a project on these terms, or confirm one that already has them.

        Returns the project and whether this call created it. A project with other terms
        raises ``ProjectConflictError``: its preset or name is never changed this way. A
        deleting or deleted project raises ``ProjectInactiveError``: it is never brought
        back. Concurrent first calls create it once.
        """

        if not _PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be a lowercase portable identifier")
        if not display_name or len(display_name) > 128:
            raise ValueError("display_name must contain between 1 and 128 characters")
        if capture_preset not in CAPTURE_PRESETS:
            raise ValueError(f"capture_preset must be one of {', '.join(CAPTURE_PRESETS)}")
        with self._mutation("provision_project"), self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO projects(project_id, display_name, created_at_unix_nano, capture_preset)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id, display_name, time.time_ns(), capture_preset),
            ).rowcount
            row = connection.execute(
                """
                SELECT project_id, display_name, created_at_unix_nano, capture_preset,
                       lifecycle_state
                FROM projects WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            connection.commit()
        project = _project_record(row)
        if project.lifecycle_state != "active":
            raise ProjectInactiveError("project is not accepting requests")
        if (project.display_name, project.capture_preset) != (display_name, capture_preset):
            raise ProjectConflictError("project exists with other terms")
        return project, inserted == 1

    def get_project(self, project_id: str) -> ProjectRecord | None:
        """Return a project's record, or ``None`` if it was never provisioned."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, display_name, created_at_unix_nano, capture_preset,
                       lifecycle_state
                FROM projects WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        return None if row is None else _project_record(row)

    def is_call_revision(self, bundle_id: str, *, project_id: str) -> bool:
        """Whether this project's incident ``bundle_id`` is the revision of a call."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM call_revisions WHERE project_id = ? AND bundle_id = ?",
                (project_id, bundle_id),
            ).fetchone()
        return row is not None

    def set_capture_preset(self, project_id: str, preset: str) -> ProjectRecord:
        """Choose what the OTLP receiver keeps of this project's future evidence.

        ``full`` keeps content; ``metadata_only`` drops content, identity and the
        session-key attributes before anything is stored. Evidence already stored
        is not rewritten. ``redacted`` raises ``CapturePresetUnavailableError``.
        """

        if preset in UNAVAILABLE_CAPTURE_PRESETS:
            raise CapturePresetUnavailableError(preset)
        if preset not in CAPTURE_PRESETS:
            raise ValueError(f"capture preset must be {' or '.join(CAPTURE_PRESETS)}")
        with self._mutation("set_capture_preset"), self._connect() as connection:
            connection.begin_write()
            self._require_active_project(connection, project_id)
            connection.execute(
                "UPDATE projects SET capture_preset = ? WHERE project_id = ?",
                (preset, project_id),
            )
            row = connection.execute(
                """
                SELECT project_id, display_name, created_at_unix_nano, capture_preset,
                       lifecycle_state
                FROM projects WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            connection.commit()
        return _project_record(row)

    def project_exists(self, project_id: str) -> bool:
        """Return whether an authorization scope has been provisioned."""

        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                is not None
            )

    def issue_api_key(self, project_id: str, *, label: str) -> IssuedApiKey:
        """Issue a project credential; the returned secret is never persisted."""

        if not label or len(label) > 128:
            raise ValueError("label must contain between 1 and 128 characters")
        key_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        secret_hash = hashlib.scrypt(
            secret.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=32
        )
        with self._mutation("issue_api_key"), self._connect() as connection:
            self._require_active_project(connection, project_id)
            connection.execute(
                """
                INSERT INTO api_keys(
                    key_id, project_id, label, secret_salt, secret_hash,
                    created_at_unix_nano
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key_id, project_id, label, salt, secret_hash, time.time_ns()),
            )
            connection.commit()
        credential = f"{_API_KEY_PREFIX}{key_id}.{secret}"
        return IssuedApiKey(project_id, key_id, label, credential)

    def has_active_api_keys(self) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM api_keys AS api_key
                    JOIN projects AS project ON project.project_id = api_key.project_id
                    WHERE api_key.revoked_at_unix_nano IS NULL
                      AND project.lifecycle_state = 'active'
                    LIMIT 1
                    """
                ).fetchone()
                is not None
            )

    def authenticate_api_key(self, credential: str) -> ApiPrincipal | None:
        """Resolve a credential to its Project using a memory-hard secret hash."""

        if not credential.startswith(_API_KEY_PREFIX):
            return None
        encoded = credential.removeprefix(_API_KEY_PREFIX)
        key_id, separator, secret = encoded.partition(".")
        if not separator or len(key_id) != 16 or not secret or len(secret) > 128:
            return None
        # Every request reads the key's row: an indexed, lock-free SELECT that sees a
        # revocation or a project deletion from any process at once.
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT api_key.project_id, api_key.secret_salt, api_key.secret_hash
                FROM api_keys AS api_key
                JOIN projects AS project ON project.project_id = api_key.project_id
                WHERE api_key.key_id = ? AND api_key.revoked_at_unix_nano IS NULL
                  AND project.lifecycle_state = 'active'
                """,
                (key_id,),
            ).fetchone()
            if row is None:
                return None
        # Only the secret check is remembered, by the credential's digest.
        credential_digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        if self._credential_verdicts.verdict(credential_digest) == key_id:
            with self._authentication_lock:
                self._verdict_cache_hits += 1
        else:
            supplied_hash = hashlib.scrypt(
                secret.encode("utf-8"),
                salt=bytes(row["secret_salt"]),
                n=1 << 14,
                r=8,
                p=1,
                dklen=32,
            )
            with self._authentication_lock:
                self._secret_verifications += 1
            if not hmac.compare_digest(supplied_hash, bytes(row["secret_hash"])):
                return None
            self._credential_verdicts.remember(credential_digest, key_id)
        if not self._record_key_use(key_id):
            # Revocation won after the read above; never authenticate that race.
            self._credential_verdicts.forget(credential_digest)
            return None
        return ApiPrincipal(project_id=row["project_id"], key_id=key_id)

    def _record_key_use(self, key_id: str) -> bool:
        """Write ``last_used_at`` at most once a minute per key, in its own mutation.

        Returns ``False`` only when the write found the key revoked or its project
        inactive. A store busy past ``KEY_USE_RECORD_BUDGET_SECONDS`` skips the write
        (the next request retries it) and returns ``True``.
        """

        now = self._clock.monotonic_nano()
        with self._authentication_lock:
            previous = self._key_use_recorded.get(key_id)
            if previous is not None and now - previous < KEY_USE_RECORD_INTERVAL_NANO:
                return True
            # Claim this minute's write so concurrent requests do not queue for it.
            self._key_use_recorded[key_id] = now
        try:
            with (
                self._mutation("record_api_key_use", budget_s=KEY_USE_RECORD_BUDGET_SECONDS),
                self._connect() as connection,
            ):
                updated = connection.execute(
                    """
                    UPDATE api_keys SET last_used_at_unix_nano = ?
                    WHERE key_id = ? AND revoked_at_unix_nano IS NULL
                      AND EXISTS (
                          SELECT 1 FROM projects
                          WHERE projects.project_id = api_keys.project_id
                            AND projects.lifecycle_state = 'active'
                      )
                    """,
                    (self._clock.unix_nano(), key_id),
                )
                if updated.rowcount != 1:
                    return False
                connection.commit()
        except BaseException as error:
            with self._authentication_lock:
                if self._key_use_recorded.get(key_id) == now:
                    if previous is None:
                        del self._key_use_recorded[key_id]
                    else:
                        self._key_use_recorded[key_id] = previous
            if isinstance(error, StorageBusyError):
                return True
            raise
        return True

    def authentication_stats(self) -> AuthenticationStats:
        """How often API-key secrets were verified, and how often a verdict was reused."""

        with self._authentication_lock:
            return AuthenticationStats(
                secret_verifications=self._secret_verifications,
                verdict_cache_hits=self._verdict_cache_hits,
            )

    def api_key_is_active(self, project_id: str, key_id: str) -> bool:
        """Check whether a key-backed browser session still has an active issuer."""

        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM api_keys AS api_key
                    JOIN projects AS project ON project.project_id = api_key.project_id
                    WHERE api_key.project_id = ? AND api_key.key_id = ?
                      AND api_key.revoked_at_unix_nano IS NULL
                      AND project.lifecycle_state = 'active'
                    """,
                    (project_id, key_id),
                ).fetchone()
                is not None
            )

    def revoke_api_key(self, project_id: str, key_id: str) -> None:
        with self._mutation("revoke_api_key"), self._connect() as connection:
            result = connection.execute(
                """
                UPDATE api_keys SET revoked_at_unix_nano = ?
                WHERE project_id = ? AND key_id = ? AND revoked_at_unix_nano IS NULL
                """,
                (time.time_ns(), project_id, key_id),
            )
            if result.rowcount != 1:
                raise ValueError("active API key does not exist")
            connection.commit()

    def create_connector(
        self,
        project_id: str,
        *,
        provider: str,
        secret_ref: str,
        endpoint_id: str | None = None,
        normalizer_version: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> ConnectorRecord:
        """Create a connector. ``config`` is its configuration
        (``connectors.config.ConnectorConfig``; ``None``: the defaults)."""

        from .connectors.config import config_json

        if provider not in _CONNECTOR_NORMALIZER_VERSIONS:
            raise ValueError("unsupported connector provider")
        stored_config = None if config is None else config_json(config)
        if not re.fullmatch(r"env:[A-Z][A-Z0-9_]{0,127}", secret_ref):
            raise ValueError("secret_ref must name a portable environment secret")
        identifier = endpoint_id or f"connector_{secrets.token_urlsafe(18)}"
        selected_normalizer = normalizer_version or _CONNECTOR_NORMALIZER_VERSIONS[provider]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", identifier):
            raise ValueError("endpoint_id must be an opaque portable identifier")
        with self._mutation("create_connector"), self._connect() as connection:
            self._require_active_project(connection, project_id)
            connection.execute(
                """
                INSERT INTO connectors(
                    endpoint_id, project_id, provider, secret_ref, normalizer_version,
                    created_at_unix_nano, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    provider,
                    secret_ref,
                    selected_normalizer,
                    time.time_ns(),
                    stored_config,
                ),
            )
            row = connection.execute(
                "SELECT * FROM connectors WHERE endpoint_id = ?", (identifier,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return _connector_record(row)

    def get_connector(self, endpoint_id: str) -> ConnectorRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connectors WHERE endpoint_id = ?", (endpoint_id,)
            ).fetchone()
        return _connector_record(row) if row is not None else None

    def fingerprint(self, namespace: str, value: str) -> str:
        if not namespace or not value:
            raise ValueError("fingerprint namespace and value are required")
        return hmac.new(
            self._identity_key,
            namespace.encode("utf-8") + b"\x00" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def hosted_bundle_id(self, project_id: str, idempotency_key: str) -> str:
        """Mint a stable opaque Earshot ID for one hosted artifact submission."""

        if not project_id or not idempotency_key:
            raise ValueError("project_id and idempotency_key are required")
        value = f"{project_id}\x00{idempotency_key}"
        return f"bundle-{self.fingerprint('hosted-artifact-id-v1', value)}"

    def ingest(
        self,
        bundle: IncidentBundle,
        canonical_payload: bytes | None = None,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        before_commit: Callable[[DatabaseConnection], None] | None = None,
    ) -> IngestResult:
        """Persist an already validated bundle atomically.

        Validation and encoding are delegated to the contract modules so direct
        storage callers receive the same guarantees as HTTP ingest callers.

        ``before_commit`` runs inside the transaction that writes the catalog
        row (or finds it already written), just before it commits; whatever it
        writes commits with the incident, and if it raises nothing commits. The
        call closer finishes a revision this way.
        """

        return self.ingest_prepared(
            prepare_incident(bundle, project_id, canonical_payload=canonical_payload),
            before_commit=before_commit,
        )

    def ingest_prepared(
        self,
        prepared: PreparedIncident,
        *,
        before_commit: Callable[[DatabaseConnection], None] | None = None,
    ) -> IngestResult:
        """Persist an incident ``prepare_incident`` built: the locked half of
        ``ingest``. ``before_commit`` is as for ``ingest``."""

        canonical_payload = prepared.canonical_payload
        project_id = prepared.project_id
        projection = prepared.projection
        framework = prepared.framework
        ingested_at = time.time_ns()

        with self._mutation("ingest"):
            digest = _sha256(canonical_payload)
            intent_id = uuid.uuid4().hex
            intent_staged = False
            try:
                with self._connect() as connection:
                    connection.begin_write()
                    self._require_active_project(connection, project_id)
                    tombstone = connection.execute(
                        "SELECT project_id FROM tombstones WHERE bundle_id_sha256 = ?",
                        (_tombstone_key(prepared.bundle_id),),
                    ).fetchone()
                    if tombstone is not None:
                        if tombstone["project_id"] == project_id:
                            raise IncidentPurgedError(
                                "purged incident identifiers cannot be reused"
                            )
                        raise IncidentConflictError(
                            "bundle identifier is unavailable in the global namespace"
                        )

                    existing = connection.execute(
                        "SELECT * FROM incidents WHERE bundle_id = ?", (prepared.bundle_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["project_id"] != project_id:
                            raise IncidentConflictError(
                                "bundle identifier already belongs to another project"
                            )
                        if existing["object_digest"] != digest:
                            raise IncidentConflictError(
                                "bundle identifier already exists with different content"
                            )

                    if not self._catalog_artifacts:
                        # Persist project provenance before publishing the CAS object.
                        # If the process stops between the filesystem link and index
                        # commit, deletion/reconciliation can still erase the orphan.
                        # A Postgres artifact is a row in the incident's own
                        # transaction, so it can never be orphaned.
                        connection.execute(
                            """
                            INSERT INTO pending_ingests(
                                intent_id, project_id, object_digest, started_at_unix_nano
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (intent_id, project_id, digest, time.time_ns()),
                        )
                        intent_staged = True
                    connection.commit()

                if not self._catalog_artifacts:
                    published_digest, _ = self.objects.put(canonical_payload)
                    if published_digest != digest:
                        raise StorageError("content-addressed object digest changed during ingest")

                with self._connect() as connection:
                    connection.begin_write()
                    self._require_active_project(connection, project_id)
                    existing = connection.execute(
                        "SELECT * FROM incidents WHERE bundle_id = ?", (prepared.bundle_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["project_id"] != project_id:
                            raise IncidentConflictError(
                                "bundle identifier already belongs to another project"
                            )
                        if existing["object_digest"] != digest:
                            raise IncidentConflictError(
                                "bundle identifier already exists with different content"
                            )
                        write_projection_rows(connection, [projection])
                        if intent_staged:
                            connection.execute(
                                "DELETE FROM pending_ingests WHERE intent_id = ?",
                                (intent_id,),
                            )
                        if before_commit is not None:
                            before_commit(connection)
                        connection.commit()
                        refreshed = connection.execute(
                            "SELECT * FROM incidents WHERE bundle_id = ?",
                            (prepared.bundle_id,),
                        ).fetchone()
                        assert refreshed is not None
                        return IngestResult(record=_record(refreshed), created=False)

                    if self._catalog_artifacts:
                        connection.execute(
                            "INSERT INTO artifacts(digest, payload) VALUES (?, ?) "
                            "ON CONFLICT (digest) DO NOTHING",
                            (digest, canonical_payload),
                        )
                    connection.execute(
                        """
                        INSERT INTO incidents (
                            bundle_id, project_id, session_id, schema_version, object_digest,
                            size_bytes, status, finality, completeness, framework,
                            created_at_unix_nano, ingested_at_unix_nano,
                            expires_at_unix_nano, export_allowed_local_api,
                            export_allowed_local_cli, projection_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            prepared.bundle_id,
                            project_id,
                            prepared.session_id,
                            prepared.schema_version,
                            digest,
                            len(canonical_payload),
                            prepared.status,
                            prepared.finality,
                            prepared.completeness,
                            framework,
                            prepared.created_at_unix_nano,
                            ingested_at,
                            projection.expires_at_unix_nano,
                            projection.export_allowed_local_api,
                            projection.export_allowed_local_cli,
                            INCIDENT_PROJECTION_VERSION,
                        ),
                    )
                    # Record the arrival order in the same transaction. This is the
                    # schema-v19 trigger incidents_track_ingest_order, made explicit
                    # so every dialect runs the same statement.
                    connection.execute(
                        """
                        INSERT INTO incident_ingest_order(bundle_id)
                        SELECT bundle_id FROM incidents
                        WHERE bundle_id = ?
                            AND NOT EXISTS (
                                SELECT 1 FROM incident_ingest_order
                                WHERE incident_ingest_order.bundle_id = incidents.bundle_id
                            )
                        """,
                        (prepared.bundle_id,),
                    )
                    write_projection_rows(connection, [projection], replace=False)
                    row = connection.execute(
                        "SELECT * FROM incidents WHERE bundle_id = ?", (prepared.bundle_id,)
                    ).fetchone()
                    assert row is not None
                    if intent_staged:
                        connection.execute(
                            "DELETE FROM pending_ingests WHERE intent_id = ?",
                            (intent_id,),
                        )
                    if before_commit is not None:
                        before_commit(connection)
                    connection.commit()
                    self._harden_database_permissions()
                    return IngestResult(record=_record(row), created=True)
            except Exception:
                if intent_staged:
                    # Failed physical cleanup leaves the durable intent for project
                    # deletion or startup recovery to retry.
                    try:
                        self._cleanup_pending_ingests_locked(
                            project_id=project_id,
                            intent_id=intent_id,
                            limit=1,
                        )
                    except Exception as cleanup_error:
                        _LOGGER.warning(
                            "pending ingest cleanup failed; retry is deferred "
                            "(intent_id=%s, error=%s)",
                            intent_id,
                            type(cleanup_error).__name__,
                        )
                raise

    def bundle_identity_exists(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> bool:
        """Whether this project has stored or purged this immutable bundle ID."""

        with self._mutation("bundle_identity_exists"), self._connect() as connection:
            self._require_active_project(connection, project_id)
            incident = connection.execute(
                "SELECT 1 FROM incidents WHERE bundle_id = ? AND project_id = ?",
                (bundle_id, project_id),
            ).fetchone()
            if incident is not None:
                return True
            tombstone = connection.execute(
                """
                    SELECT 1 FROM tombstones
                    WHERE bundle_id_sha256 = ? AND project_id = ?
                    """,
                (_tombstone_key(bundle_id), project_id),
            ).fetchone()
            return tombstone is not None

    def get_record(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> IncidentRecord:
        expired = False
        record: IncidentRecord | None = None
        with self._mutation("get_record"):
            with self._connect() as connection:
                self._require_active_project(connection, project_id)
                row = connection.execute(
                    "SELECT * FROM incidents WHERE bundle_id = ? AND project_id = ?",
                    (bundle_id, project_id),
                ).fetchone()
                tombstoned = (
                    connection.execute(
                        """
                        SELECT 1 FROM tombstones
                        WHERE bundle_id_sha256 = ? AND project_id = ?
                        """,
                        (_tombstone_key(bundle_id), project_id),
                    ).fetchone()
                    is not None
                )
                expires = row is not None and _expired_alone(
                    connection, bundle_id, row["expires_at_unix_nano"]
                )
            if row is not None:
                if expires:
                    self._purge_existing_locked((bundle_id,), time.time_ns())
                    expired = True
                else:
                    record = _record(row)
            elif tombstoned:
                raise IncidentPurgedError("incident was purged")
            else:
                raise IncidentNotFoundError("incident not found")
        if expired:
            raise IncidentPurgedError("incident was purged")
        assert record is not None
        return record

    def set_external_reference(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        namespace: str,
        record_type: str,
        external_id: str,
    ) -> ExternalReferenceRecord:
        """Set one catalog link without changing the immutable incident bytes."""

        linked_at = time.time_ns()
        with self._mutation("set_external_reference"), self._connect() as connection:
            connection.begin_write()
            self._require_active_project(connection, project_id)
            incident = connection.execute(
                "SELECT expires_at_unix_nano FROM incidents WHERE bundle_id = ? AND project_id = ?",
                (bundle_id, project_id),
            ).fetchone()
            if incident is None:
                tombstoned = connection.execute(
                    """
                    SELECT 1 FROM tombstones
                    WHERE bundle_id_sha256 = ? AND project_id = ?
                    """,
                    (_tombstone_key(bundle_id), project_id),
                ).fetchone()
                connection.rollback()
                if tombstoned:
                    raise IncidentPurgedError("incident was purged")
                raise IncidentNotFoundError("incident not found")
            if _expired_alone(connection, bundle_id, incident["expires_at_unix_nano"]):
                # Release SQLite's write transaction before purge opens its own.
                # The outer mutation lock remains held, so no other writer can
                # recreate the incident or attach a reference in this interval.
                connection.rollback()
                self._purge_existing_locked((bundle_id,), time.time_ns())
                raise IncidentPurgedError("incident was purged")
            existing = connection.execute(
                """
                SELECT external_id, linked_at_unix_nano
                FROM incident_external_references
                WHERE project_id = ? AND bundle_id = ? AND namespace = ? AND record_type = ?
                """,
                (project_id, bundle_id, namespace, record_type),
            ).fetchone()
            if existing is not None and existing["external_id"] == external_id:
                linked_at = int(existing["linked_at_unix_nano"])
            else:
                connection.execute(
                    """
                    INSERT INTO incident_external_references(
                        project_id, bundle_id, namespace, record_type,
                        external_id, linked_at_unix_nano
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, bundle_id, namespace, record_type)
                    DO UPDATE SET external_id = excluded.external_id,
                                  linked_at_unix_nano = excluded.linked_at_unix_nano
                    """,
                    (project_id, bundle_id, namespace, record_type, external_id, linked_at),
                )
            connection.commit()
        return ExternalReferenceRecord(
            namespace=namespace,
            record_type=record_type,
            external_id=external_id,
            linked_at_unix_nano=str(linked_at),
        )

    def get_external_references(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> tuple[ExternalReferenceRecord, ...]:
        """Read references only through the incident's authenticated project."""

        # Keep the existence/retention check and catalog read in one serialized
        # operation so a concurrent purge cannot turn a successful read into a
        # misleading empty list.
        with self._mutation("get_external_references"):
            self.get_record(bundle_id, project_id=project_id)
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT namespace, record_type, external_id, linked_at_unix_nano
                    FROM incident_external_references
                    WHERE project_id = ? AND bundle_id = ?
                    ORDER BY namespace, record_type
                    """,
                    (project_id, bundle_id),
                ).fetchall()
        return tuple(
            ExternalReferenceRecord(
                namespace=row["namespace"],
                record_type=row["record_type"],
                external_id=row["external_id"],
                linked_at_unix_nano=str(row["linked_at_unix_nano"]),
            )
            for row in rows
        )

    def delete_external_reference(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        namespace: str,
        record_type: str,
    ) -> bool:
        """Remove one link; return whether a row existed."""

        with self._mutation("delete_external_reference"), self._connect() as connection:
            connection.begin_write()
            self._require_active_project(connection, project_id)
            incident = connection.execute(
                "SELECT expires_at_unix_nano FROM incidents WHERE bundle_id = ? AND project_id = ?",
                (bundle_id, project_id),
            ).fetchone()
            if incident is None:
                tombstoned = connection.execute(
                    """
                    SELECT 1 FROM tombstones
                    WHERE bundle_id_sha256 = ? AND project_id = ?
                    """,
                    (_tombstone_key(bundle_id), project_id),
                ).fetchone()
                connection.rollback()
                if tombstoned:
                    raise IncidentPurgedError("incident was purged")
                raise IncidentNotFoundError("incident not found")
            if _expired_alone(connection, bundle_id, incident["expires_at_unix_nano"]):
                # Retention is checked in the same serialized operation as the
                # catalog delete, so a deadline crossing cannot mutate a
                # reference on an incident that should already be purged.
                connection.rollback()
                self._purge_existing_locked((bundle_id,), time.time_ns())
                raise IncidentPurgedError("incident was purged")
            result = connection.execute(
                """
                DELETE FROM incident_external_references
                WHERE project_id = ? AND bundle_id = ? AND namespace = ? AND record_type = ?
                """,
                (project_id, bundle_id, namespace, record_type),
            )
            connection.commit()
        return result.rowcount == 1

    def get_artifact(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> tuple[IncidentRecord, bytes]:
        with self._mutation("get_artifact"):
            record = self.get_record(bundle_id, project_id=project_id)
            if not self._catalog_artifacts:
                return record, self.objects.get(record.digest)
            with self._connect() as connection:
                return record, self._read_artifact(connection, record.digest)

    def list_incidents(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        destination: str | None = None,
        include_call_revisions: bool = True,
    ) -> IncidentPage:
        """A page of the project's incidents, newest first. ``include_call_revisions=False``
        leaves out the revisions of calls, for a reader that may not read calls."""

        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if destination not in {None, "local_api", "local_cli"}:
            raise ValueError("destination must be local_api or local_cli")

        clauses: list[str] = ["project_id = ?"]
        parameters: list[object] = [project_id]
        if destination is not None:
            column = (
                "export_allowed_local_api"
                if destination == "local_api"
                else "export_allowed_local_cli"
            )
            clauses.append(f"{column} = 1")
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if cursor is not None:
            cursor_time, cursor_bundle_id = decode_page_cursor(cursor)
            clauses.append("(ingested_at_unix_nano, bundle_id) < (?, ?)")
            parameters.extend((cursor_time, cursor_bundle_id))
        if not include_call_revisions:
            clauses.append(f"NOT {_CALL_REVISION_SQL}")
        now = str(time.time_ns())
        clauses.append(_not_expired_sql("expires_at_unix_nano"))
        parameters.extend((len(now), len(now), now))
        index_name = (
            "incidents_session_idx"
            if session_id is not None
            else (
                f"incidents_export_{destination}_idx"
                if destination is not None
                else "incidents_project_idx"
            )
        )
        query = (
            self._database.dialect.sql(
                "listing-index-hint",
                sqlite=f"SELECT * FROM incidents INDEXED BY {index_name} ",
                # Postgres has no index hints; its planner picks the same
                # (project, ingested_at DESC, bundle_id DESC) indexes for this order.
                postgresql="SELECT * FROM incidents ",
            )
            + f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ingested_at_unix_nano DESC, bundle_id DESC LIMIT ?"
        )
        # The chosen index carries page order and avoids a temporary sort. SQLite
        # stops after limit+1 visible rows, but may scan an unbounded number of
        # expired catalog rows first. Listing never decodes artifacts or mutates
        # retention state.
        with self._listing_connection() as connection:
            connection.begin_read()
            self._require_active_project(connection, project_id)
            rows = connection.execute(query, (*parameters, limit + 1)).fetchall()
            has_more = len(rows) > limit
            records = tuple(_record(row) for row in rows[:limit])
            next_cursor = _encode_cursor(records[-1]) if has_more and records else None
            return IncidentPage(items=records, next_cursor=next_cursor)

    def list_turn_facts(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        session_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TurnFact, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = (
            "SELECT turn_metrics.* FROM turn_metrics "
            "JOIN incidents ON incidents.bundle_id = turn_metrics.bundle_id "
            "AND incidents.project_id = turn_metrics.project_id "
            "WHERE turn_metrics.project_id = ?"
            f" AND NOT {_SUPERSEDED_REVISION_SQL}"
        )
        parameters: list[object] = [project_id]
        if session_id is not None:
            query += " AND turn_metrics.session_id = ?"
            parameters.append(session_id)
        query += f" AND {_not_expired_sql('incidents.expires_at_unix_nano')}"
        query += " ORDER BY turn_metrics.started_at_unix_nano, "
        query += "turn_metrics.bundle_id, turn_metrics.turn_index LIMIT ?"
        with self._mutation("list_turn_facts"):
            with self._connect() as connection:
                self._require_active_project(connection, project_id)
            now = str(time.time_ns())
            query_parameters = (*parameters, len(now), len(now), now, limit)
            with self._connect() as connection:
                rows = connection.execute(query, query_parameters).fetchall()
        return tuple(_turn_fact(row) for row in rows)

    def summarize_turn_metric(
        self,
        metric: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        group_by: str = "framework",
    ) -> tuple[TurnMetricSummary, ...]:
        """The grouped percentiles on their own, for callers that state coverage elsewhere.

        These groups already exclude provisional incidents, but on their own they
        cannot say how many were withheld, so an empty result here is ambiguous
        between "nothing stored" and "nothing final". Anything that publishes
        these numbers should use ``summarize_turn_metric_fleet``, which carries
        that count with them.
        """

        return self.summarize_turn_metric_fleet(
            metric, project_id=project_id, group_by=group_by
        ).groups

    def summarize_turn_metric_fleet(
        self,
        metric: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        group_by: str = "framework",
        include_call_revisions: bool = True,
    ) -> TurnMetricFleetSummary:
        """Aggregate one turn metric across a project's *final* incidents only.

        ``turn_metrics`` is a projection of every stored incident, including the
        crash-recovered and operator-sealed ones that declare themselves
        ``provisional``. Those artifacts are knowingly missing an unknown part of
        their conversation, so their turns would move a fleet percentile without
        anything on the number saying why -- exactly the quiet kind of wrong this
        project exists to refuse. Finality lives on ``incidents``, so the
        population is fixed by joining there rather than by trusting the
        projection, and the join is the only change to a query whose grouping,
        ranking and nearest-rank percentiles stay byte-identical for final data.

        What the join removed is counted and returned, never dropped silently.

        A call's revisions each project the whole call, so only its latest
        revision counts; the earlier ones are superseded, not withheld.
        """

        metric_columns = {
            "first_token_ms": (
                "first_token_ms",
                "first_token_availability",
                "first_token_basis",
                "first_token_confidence",
                "first_token_limitation",
            ),
            "generated_response_ms": (
                "generated_response_ms",
                "generated_response_availability",
                "generated_response_basis",
                "generated_response_confidence",
                "generated_response_limitation",
            ),
            "sent_response_ms": (
                "sent_response_ms",
                "sent_response_availability",
                "sent_response_basis",
                "sent_response_confidence",
                "sent_response_limitation",
            ),
            "received_response_ms": (
                "received_response_ms",
                "received_response_availability",
                "received_response_basis",
                "received_response_confidence",
                "received_response_limitation",
            ),
            "render_start_response_ms": (
                "render_start_response_ms",
                "render_start_response_availability",
                "render_start_response_basis",
                "render_start_response_confidence",
                "render_start_response_limitation",
            ),
            "response_ms": (
                "response_ms",
                "response_availability",
                "response_basis",
                "response_confidence",
                "response_limitation",
            ),
            "stt_finalization_ms": (
                "stt_finalization_ms",
                "stt_finalization_availability",
                "stt_finalization_basis",
                "stt_finalization_confidence",
                "stt_finalization_limitation",
            ),
            "eou_ms": (
                "eou_ms",
                "eou_availability",
                "eou_basis",
                "eou_confidence",
                "eou_limitation",
            ),
            "turn_duration_ms": (
                "turn_duration_ms",
                "turn_duration_availability",
                "turn_duration_basis",
                "turn_duration_confidence",
                "turn_duration_limitation",
            ),
        }
        group_columns = {
            "framework": "framework",
            "provider": "provider",
            "model": "model",
            "language": "language",
            "status": "status",
        }
        if metric not in metric_columns:
            raise ValueError("unsupported turn metric")
        if group_by not in group_columns:
            raise ValueError("unsupported turn metric grouping")
        (
            metric_column,
            availability_column,
            basis_column,
            confidence_column,
            limitation_column,
        ) = metric_columns[metric]
        group_column = group_columns[group_by]
        # A call counts once (its latest revision), or not at all for a reader that may not
        # read calls.
        revisions_left_out = (
            _SUPERSEDED_REVISION_SQL if include_call_revisions else _CALL_REVISION_SQL
        )
        query = f"""
            WITH final_turns AS (
                SELECT turn_metrics.*
                FROM turn_metrics
                JOIN incidents ON incidents.bundle_id = turn_metrics.bundle_id
                    AND incidents.project_id = turn_metrics.project_id
                WHERE turn_metrics.project_id = ?
                  AND incidents.finality = 'final'
                  AND NOT {revisions_left_out}
                  AND {_not_expired_sql("incidents.expires_at_unix_nano")}
            ), totals AS (
                SELECT COALESCE({group_column}, 'unknown') AS group_value,
                       {availability_column} AS availability_value,
                       {basis_column} AS basis_value,
                       {confidence_column} AS confidence_value,
                       COALESCE({limitation_column}, '') AS limitation_value,
                       COUNT(*) AS turn_count
                FROM final_turns
                GROUP BY COALESCE({group_column}, 'unknown'),
                         {availability_column}, {basis_column},
                         {confidence_column}, COALESCE({limitation_column}, '')
            ), ranked AS (
                SELECT COALESCE({group_column}, 'unknown') AS group_value,
                       {availability_column} AS availability_value,
                       {basis_column} AS basis_value,
                       {confidence_column} AS confidence_value,
                       COALESCE({limitation_column}, '') AS limitation_value,
                       {metric_column} AS value,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE({group_column}, 'unknown'),
                                        {availability_column}, {basis_column},
                                        {confidence_column},
                                        COALESCE({limitation_column}, '')
                           ORDER BY {metric_column}
                       ) AS value_rank,
                       COUNT(*) OVER (
                           PARTITION BY COALESCE({group_column}, 'unknown'),
                                        {availability_column}, {basis_column},
                                        {confidence_column},
                                        COALESCE({limitation_column}, '')
                       ) AS available_count
                FROM final_turns
                WHERE {metric_column} IS NOT NULL
            )
            SELECT totals.group_value, totals.availability_value,
                   totals.basis_value, totals.confidence_value,
                   totals.limitation_value,
                   totals.turn_count,
                   COALESCE(MAX(ranked.available_count), 0) AS available_count,
                   AVG(ranked.value) AS average_ms,
                   MIN(ranked.value) AS minimum_ms,
                   MAX(ranked.value) AS maximum_ms,
                   MIN(CASE
                       WHEN ranked.value_rank >= ((ranked.available_count + 1) / 2)
                       THEN ranked.value
                   END) AS p50_ms,
                   MIN(CASE
                       WHEN ranked.value_rank >= ((ranked.available_count * 95 + 99) / 100)
                       THEN ranked.value
                   END) AS p95_ms
            FROM totals
            LEFT JOIN ranked ON ranked.group_value = totals.group_value
                AND ranked.availability_value = totals.availability_value
                AND ranked.basis_value = totals.basis_value
                AND ranked.confidence_value = totals.confidence_value
                AND ranked.limitation_value = totals.limitation_value
            GROUP BY totals.group_value, totals.availability_value,
                     totals.basis_value, totals.confidence_value,
                     totals.limitation_value, totals.turn_count
            ORDER BY totals.group_value, totals.availability_value,
                     totals.basis_value, totals.confidence_value,
                     totals.limitation_value
        """
        # The same population split the aggregation made, counted so the caller
        # can see what the number covers and what it refused. Read under the one
        # lock that produced the groups, so the declaration cannot drift from
        # them. Both counts are metric-independent, like ``turn_count``.
        coverage_query = f"""
            SELECT
                COUNT(DISTINCT CASE WHEN incidents.finality = 'final'
                      THEN turn_metrics.bundle_id END) AS incident_count,
                COUNT(DISTINCT CASE WHEN incidents.finality <> 'final'
                      THEN turn_metrics.bundle_id END) AS withheld_incident_count,
                COUNT(CASE WHEN incidents.finality <> 'final'
                      THEN 1 END) AS withheld_turn_count
            FROM turn_metrics
            JOIN incidents ON incidents.bundle_id = turn_metrics.bundle_id
                AND incidents.project_id = turn_metrics.project_id
            WHERE turn_metrics.project_id = ?
              AND NOT {revisions_left_out}
              AND {_not_expired_sql("incidents.expires_at_unix_nano")}
        """
        with self._mutation("summarize_turn_metric_fleet"):
            with self._connect() as connection:
                self._require_active_project(connection, project_id)
            now = str(time.time_ns())
            query_parameters = (project_id, len(now), len(now), now)
            with self._connect() as connection:
                rows = connection.execute(query, query_parameters).fetchall()
                coverage = connection.execute(coverage_query, query_parameters).fetchone()
        return TurnMetricFleetSummary(
            groups=tuple(
                TurnMetricSummary(
                    group=row["group_value"],
                    availability=row["availability_value"],
                    basis=row["basis_value"],
                    confidence=row["confidence_value"],
                    limitation=row["limitation_value"] or None,
                    turn_count=row["turn_count"],
                    available_count=row["available_count"],
                    average_ms=row["average_ms"],
                    minimum_ms=row["minimum_ms"],
                    maximum_ms=row["maximum_ms"],
                    p50_ms=row["p50_ms"],
                    p95_ms=row["p95_ms"],
                )
                for row in rows
            ),
            incident_count=coverage["incident_count"],
            withheld_incident_count=coverage["withheld_incident_count"],
            withheld_turn_count=coverage["withheld_turn_count"],
        )

    def get_analysis(
        self,
        bundle_id: str,
        analyzer_version: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> StoredAnalysis | None:
        with self._mutation("get_analysis"):
            record = self.get_record(bundle_id, project_id=project_id)
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM analyses
                    WHERE bundle_id = ? AND analyzer_version = ? AND input_digest = ?
                    """,
                    (bundle_id, analyzer_version, record.digest),
                ).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row["output_json"])
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
                TypeError,
                RecursionError,
            ) as error:
                raise ArtifactCorruptionError("stored analysis is corrupt") from error
            try:
                analysis = DerivedAnalysis.model_validate(value)
            except ValidationError as error:
                raise ArtifactCorruptionError("stored analysis violates its contract") from error
            if (
                analysis.input_sha256 != row["input_digest"]
                or analysis.analyzer_version != row["analyzer_version"]
                or str(analysis.generated_at_unix_nano) != str(row["generated_at_unix_nano"])
            ):
                raise ArtifactCorruptionError("stored analysis binding is inconsistent")
            from .codec import decode_incident_protobuf
            from .validation import validate_derived_analysis

            _, artifact_payload = self.get_artifact(bundle_id, project_id=project_id)
            source_bundle = decode_incident_protobuf(artifact_payload)
            if not validate_derived_analysis(source_bundle, analysis).ok:
                raise ArtifactCorruptionError("stored analysis evidence binding is invalid")
            return StoredAnalysis(
                bundle_id=bundle_id,
                analyzer_version=row["analyzer_version"],
                input_digest=row["input_digest"],
                generated_at_unix_nano=str(row["generated_at_unix_nano"]),
                value=analysis.model_dump(mode="json", exclude_none=True),
            )

    def put_analysis(
        self,
        bundle_id: str,
        analyzer_version: str,
        value: Any,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> StoredAnalysis:
        source = (
            value.model_dump(mode="python", warnings=False)
            if hasattr(value, "model_dump")
            else value
        )
        try:
            analysis = DerivedAnalysis.model_validate(source)
        except ValidationError as error:
            raise StorageError("analysis output violates the DerivedAnalysis contract") from error
        try:
            output = json.dumps(
                analysis.model_dump(mode="json", exclude_none=True),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise StorageError("analysis output is not strict JSON") from error
        with self._mutation("put_analysis"):
            with self._connect() as connection:
                self._require_active_project(connection, project_id)
            record, artifact_payload = self.get_artifact(bundle_id, project_id=project_id)
            if analysis.input_sha256 != record.digest:
                raise StorageError("analysis input digest does not match the stored artifact")
            if analysis.analyzer_version != analyzer_version:
                raise StorageError("analysis version does not match the cache key")
            from .codec import decode_incident_protobuf
            from .validation import validate_derived_analysis

            source_bundle = decode_incident_protobuf(artifact_payload)
            if not validate_derived_analysis(source_bundle, analysis).ok:
                raise StorageError("analysis output is inconsistent with source evidence")
            generated_at = analysis.generated_at_unix_nano
            with self._connect() as connection:
                connection.begin_write()
                connection.execute(
                    """
                    INSERT INTO analyses (
                        bundle_id, analyzer_version, input_digest,
                        generated_at_unix_nano, output_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bundle_id, analyzer_version, input_digest)
                    DO NOTHING
                    """,
                    (bundle_id, analyzer_version, record.digest, generated_at, output),
                )
                connection.commit()
            stored = self.get_analysis(bundle_id, analyzer_version, project_id=project_id)
            assert stored is not None
            return stored

    def purge(self, bundle_id: str, *, project_id: str = DEFAULT_PROJECT_ID) -> None:
        """Best-effort physically remove evidence and leave a minimal tombstone."""

        scrub_required = False
        with self._mutation("purge"), self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM incidents WHERE bundle_id = ? AND project_id = ?",
                (bundle_id, project_id),
            ).fetchone()
            if row is None:
                tombstoned = connection.execute(
                    """
                        SELECT 1 FROM tombstones
                        WHERE bundle_id_sha256 = ? AND project_id = ?
                        """,
                    (_tombstone_key(bundle_id), project_id),
                ).fetchone()
                if tombstoned is None:
                    raise IncidentNotFoundError("incident not found")
                try:
                    self._cleanup_pending_object_deletions_locked(
                        project_id=project_id,
                        limit=500,
                    )
                except (OSError, *DATABASE_ERRORS) as error:
                    raise StorageError("artifact purge requires retry") from error
                if connection.execute(
                    "SELECT 1 FROM pending_object_deletions WHERE project_id = ? LIMIT 1",
                    (project_id,),
                ).fetchone():
                    raise StorageCleanupPendingError(
                        "artifact purge requires another cleanup retry"
                    )
            else:
                call_id = _revision_call_id(connection, bundle_id)
                if call_id is not None:
                    raise IncidentBelongsToCallError(call_id)
                self._purge_existing_locked((bundle_id,), time.time_ns())
            scrub_required = self._scrub_required_locked()

        if scrub_required:
            try:
                self._scrub_deleted_pages(skip_if_current=True)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error

    def purge_expired(
        self,
        now_unix_nano: int | str | None = None,
        *,
        limit: int = 1000,
        compact_when_drained: bool = False,
    ) -> int:
        """Purge expired evidence and retry bounded CAS cleanup journals.

        With ``compact_when_drained=True`` (the server's retention loop),
        intermediate batches checkpoint and truncate the WAL; the full VACUUM is
        deferred until a batch drains the current expiry and pending-ingest
        backlogs, and runs at most once every ``COMPACTION_INTERVAL_NANO`` on the
        store clock. A VACUUM holds SQLite's write lock, so this keeps it from
        stalling writers after every drained purge; a deferred scrub stays
        recorded and runs on a later drain. Pending ingest cleanup removes
        only objects with a durable project owner and no incident reference.
        SQLite compaction runs after releasing the cross-process mutation lock so
        unrelated store operations can proceed.
        """

        if limit < 1 or limit > MAX_EXPIRED_PURGE_BATCH_SIZE:
            raise ValueError("limit must be between 1 and 10000")
        now = str(time.time_ns() if now_unix_nano is None else now_unix_nano)
        if (
            not now
            or any(character not in "0123456789" for character in now)
            or (len(now) > 1 and now.startswith("0"))
        ):
            raise ValueError("now_unix_nano must be a canonical non-negative decimal")

        return self._purge_expired_batch(
            now,
            limit,
            compact_when_drained=compact_when_drained,
        )

    def _purge_expired_reconciliation_batch(
        self,
        now: str,
        bundle_ids: tuple[str, ...],
        *,
        ingest_sequence_high_water: int,
    ) -> int:
        # Selection walks the expiry index and may skip many late arrivals. Keep
        # that read outside the mutation lock; recheck this bounded candidate set
        # after taking the lock before deleting anything.
        with self._mutation("startup_expiry_purge"):
            eligible_ids = self._eligible_expired_bundle_ids_locked(bundle_ids, now)
            if eligible_ids:
                self._purge_existing_locked(eligible_ids, time.time_ns())
            scrub_required = self._scrub_required_locked()
        if scrub_required:
            has_more = bool(
                self._expired_bundle_ids(
                    now,
                    1,
                    ingest_sequence_high_water=ingest_sequence_high_water,
                )
            )
            try:
                self._scrub_deleted_pages(compact=not has_more, skip_if_current=True)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error
        return len(eligible_ids)

    def _finish_reconciliation_expiry_cleanup(self) -> None:
        with self._mutation("startup_expiry_cleanup"):
            try:
                self._cleanup_pending_object_deletions_locked(
                    limit=_STARTUP_EXPIRED_PURGE_BATCH_SIZE
                )
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error
            scrub_required = self._scrub_required_locked()
        if scrub_required:
            try:
                self._scrub_deleted_pages(skip_if_current=True)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error

    def _eligible_expired_bundle_ids_locked(
        self,
        candidate_ids: tuple[str, ...],
        now: str,
    ) -> tuple[str, ...]:
        eligible: set[str] = set()
        ids_per_query = _SQLITE_SAFE_BIND_LIMIT - 3
        with self._connect() as connection:
            for offset in range(0, len(candidate_ids), ids_per_query):
                batch = candidate_ids[offset : offset + ids_per_query]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT bundle_id FROM incidents WHERE bundle_id IN ({placeholders}) "
                    f"AND {_expired_sql('expires_at_unix_nano')}",
                    (*batch, len(now), len(now), now),
                ).fetchall()
                eligible.update(row["bundle_id"] for row in rows)
        return tuple(bundle_id for bundle_id in candidate_ids if bundle_id in eligible)

    def _purge_expired_batch(
        self,
        now: str,
        limit: int,
        *,
        compact_when_drained: bool,
    ) -> int:
        with self._mutation("purge_expired"):
            removed = self._purge_expired_batch_locked(now, limit)
            scrub_required = self._scrub_required_locked()
            if compact_when_drained:
                expired_remains = bool(self._expired_bundle_ids(now, 1))
                with self._connect() as connection:
                    pending_ingests_remain = (
                        connection.execute("SELECT 1 FROM pending_ingests LIMIT 1").fetchone()
                        is not None
                    )
                compact = (
                    not expired_remains and not pending_ingests_remain and self._compaction_due()
                )
            else:
                compact = True
        if scrub_required:
            try:
                self._scrub_deleted_pages(compact=compact, skip_if_current=True)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error
        return removed

    def _purge_expired_batch_locked(
        self,
        now: str,
        limit: int,
    ) -> int:
        bundle_ids = self._expired_bundle_ids(now, limit)
        if not bundle_ids:
            try:
                self._cleanup_pending_object_deletions_locked(limit=limit)
            except (OSError, *DATABASE_ERRORS) as error:
                raise StorageError("artifact purge requires retry") from error
            removed = 0
        else:
            self._purge_existing_locked(bundle_ids, time.time_ns())
            removed = len(bundle_ids)
        try:
            self._cleanup_pending_ingests_locked(
                limit=min(limit, _STARTUP_EXPIRED_PURGE_BATCH_SIZE)
            )
        except (OSError, *DATABASE_ERRORS) as error:
            raise StorageError("pending ingest cleanup requires retry") from error
        return removed

    def _expired_bundle_ids(
        self,
        now: str,
        limit: int,
        *,
        project_id: str | None = None,
        ingest_sequence_high_water: int | None = None,
    ) -> tuple[str, ...]:
        sequence_join = ""
        sequence_filter = ""
        project_filter = " AND project_id = ?" if project_id is not None else ""
        if ingest_sequence_high_water is not None:
            sequence_join = (
                " JOIN incident_ingest_order AS ingest_order "
                "ON ingest_order.bundle_id = incidents.bundle_id"
            )
            sequence_filter = " AND ingest_order.sequence <= ?"
        parameters: tuple[object, ...] = (len(now), len(now), now)
        if ingest_sequence_high_water is not None:
            parameters += (ingest_sequence_high_water,)
        if project_id is not None:
            parameters += (project_id,)
        parameters += (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT incidents.bundle_id
                FROM incidents{sequence_join}
                WHERE {_expired_sql("incidents.expires_at_unix_nano")}
                    AND NOT {_CALL_REVISION_SQL}
                    {sequence_filter}{project_filter}
                ORDER BY length(incidents.expires_at_unix_nano),
                    incidents.expires_at_unix_nano
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(row["bundle_id"] for row in rows)

    def _purge_existing_locked(
        self,
        bundle_ids: tuple[str, ...],
        purged_at_unix_nano: int,
    ) -> None:
        with self._connect() as connection:
            connection.begin_write()
            purged = self._purge_incident_rows(connection, bundle_ids, purged_at_unix_nano)
            connection.commit()

        try:
            self._cleanup_pending_object_deletions_locked(limit=max(1, purged))
        except (OSError, *DATABASE_ERRORS) as error:
            # Tombstones and cleanup queue are durable; repeating purge safely retries.
            raise StorageError("artifact purge requires retry") from error

    def _purge_incident_rows(
        self,
        connection: DatabaseConnection,
        bundle_ids: tuple[str, ...],
        purged_at_unix_nano: int,
    ) -> int:
        """Delete stored incidents, tombstone their IDs and queue their artifacts.

        Runs in the caller's write transaction (under the store's write lock); the
        caller commits, then runs the artifact cleanup. Returns how many incidents
        were purged.
        """

        rows: list[Row] = []
        for offset in range(0, len(bundle_ids), _SQLITE_SAFE_BIND_LIMIT):
            batch = bundle_ids[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                connection.execute(
                    "SELECT bundle_id, project_id, object_digest FROM incidents "
                    f"WHERE bundle_id IN ({placeholders})",
                    batch,
                ).fetchall()
            )
        if len(rows) != len(bundle_ids):
            raise IncidentNotFoundError("incident not found")
        for offset in range(0, len(bundle_ids), _SQLITE_SAFE_BIND_LIMIT):
            batch = bundle_ids[offset : offset + _SQLITE_SAFE_BIND_LIMIT]
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"DELETE FROM incidents WHERE bundle_id IN ({placeholders})",
                batch,
            )
        insert_batch_size = _SQLITE_SAFE_BIND_LIMIT // 3
        for offset in range(0, len(rows), insert_batch_size):
            batch = rows[offset : offset + insert_batch_size]
            values = ",".join("(?, ?, ?)" for _ in batch)
            parameters = tuple(
                value
                for row in batch
                for value in (
                    _tombstone_key(row["bundle_id"]),
                    row["project_id"],
                    purged_at_unix_nano,
                )
            )
            connection.execute(
                """
                INSERT INTO tombstones(
                    bundle_id_sha256, project_id, purged_at_unix_nano
                )
                VALUES """
                + values
                + " ON CONFLICT(bundle_id_sha256) DO NOTHING",
                parameters,
            )
        project_digests: dict[str, set[str]] = {}
        for row in rows:
            project_digests.setdefault(row["project_id"], set()).add(row["object_digest"])
        for project_id, digests in project_digests.items():
            self._queue_unreferenced_object_deletions(connection, project_id, digests)
        if not self._database_uri and not self._catalog_artifacts:
            self._mark_scrub_required(connection)
        return len(rows)

    def _scrub_deleted_pages(
        self,
        *,
        compact: bool = True,
        skip_if_current: bool = False,
    ) -> None:
        """Scrub deleted data and optionally compact the database.

        This is a best-effort file-level scrub, not a guarantee against filesystem
        snapshots, copy-on-write history, SSD remapping, or external backups.
        """

        self._ensure_process_ownership()
        # WAL checkpoint, VACUUM and file fsyncs act on a SQLite file. Postgres has
        # no honest equivalent: purge already deleted the rows, the server reuses
        # their pages after autovacuum, and WAL and backups keep copies until they
        # age out. So there is nothing to do here, and no scrub is recorded.
        if self._catalog_artifacts or self._database_uri:
            return
        with self._compaction_thread_lock:
            if self._closed:
                raise StorageError("incident store is closed")
            if fcntl is not None:
                fcntl.flock(self._compaction_lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                self._scrub_deleted_pages_locked(
                    compact=compact,
                    skip_if_current=skip_if_current,
                )
            finally:
                if fcntl is not None:
                    fcntl.flock(self._compaction_lock_handle.fileno(), fcntl.LOCK_UN)

    def _scrub_deleted_pages_locked(
        self,
        *,
        compact: bool,
        skip_if_current: bool,
    ) -> None:
        """Run scrub work after acquiring the dedicated maintenance lock."""

        with self._connect() as connection:
            generation = None
            if compact:
                state = connection.execute(
                    """
                    SELECT purge_generation, scrubbed_generation
                    FROM storage_maintenance WHERE singleton = 1
                    """
                ).fetchone()
                if skip_if_current and state["purge_generation"] <= state["scrubbed_generation"]:
                    return
                generation = state["purge_generation"]
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and checkpoint[0] != 0:
                raise StorageCleanupPendingError("SQLite WAL is busy; cleanup requires retry")
            if compact:
                connection.execute("VACUUM")
                self._last_compaction_monotonic = self._clock.monotonic_nano()
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and checkpoint[0] != 0:
                    raise StorageCleanupPendingError("SQLite WAL is busy; cleanup requires retry")
        self._harden_database_permissions()
        database = Path(self.database_path)
        _fsync_file(database)
        for suffix in ("-wal", "-shm"):
            path = Path(f"{database}{suffix}")
            if path.exists():
                _fsync_file(path)
        _fsync_directory(database.parent)
        if generation is not None:
            # Persist only the generation included in this VACUUM. If another
            # process purged data while compaction ran, its newer generation
            # remains pending for the next maintenance pass.
            with self._connect() as connection:
                connection.execute(
                    connection.dialect.sql(
                        "scalar-max-scrubbed-generation",
                        sqlite="""
                    UPDATE storage_maintenance
                    SET scrubbed_generation = MAX(scrubbed_generation, ?)
                    WHERE singleton = 1
                    """,
                        postgresql="""
                    UPDATE storage_maintenance
                    SET scrubbed_generation = GREATEST(scrubbed_generation, ?)
                    WHERE singleton = 1
                    """,
                    ),
                    (generation,),
                )

    def _compaction_due(self) -> bool:
        last = self._last_compaction_monotonic
        return last is None or self._clock.monotonic_nano() - last >= COMPACTION_INTERVAL_NANO

    def cleanup_unreferenced_objects(self) -> int:
        """Sweep unreferenced CAS objects after an explicit maintenance decision.

        This also resolves projects whose pre-v13 deletion lost its per-artifact
        CAS ownership journal. The sweep is intentionally explicit because only
        maintenance may delete objects whose former owner cannot be proven.
        """

        with self._mutation("cleanup_unreferenced_objects"):
            removed = self._cleanup_unreferenced_objects_locked()
            with self._connect() as connection:
                connection.begin_write()
                connection.execute("DELETE FROM legacy_deletion_reviews")
                connection.commit()
            return removed

    def _cleanup_unreferenced_objects_locked(self) -> int:
        if self._catalog_artifacts:
            # Normally a no-op: purge deletes unreferenced rows in its transaction.
            with self._connect() as connection:
                connection.begin_write()
                return connection.execute(
                    "DELETE FROM artifacts WHERE NOT EXISTS (SELECT 1 FROM incidents "
                    "WHERE incidents.object_digest = artifacts.digest)"
                ).rowcount
        with self._connect() as connection:
            referenced = {
                row["object_digest"]
                for row in connection.execute(
                    "SELECT DISTINCT object_digest FROM incidents"
                ).fetchall()
            }
        unreferenced: list[Path] = []
        shard_directories: list[Path] = []
        try:
            root_stat = self.objects.root.lstat()
        except OSError as error:
            raise StorageError("CAS object root is unavailable") from error
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StorageError("CAS object root is not a real directory")

        for directory in self.objects.root.iterdir():
            try:
                directory_stat = directory.lstat()
            except OSError as error:
                raise StorageError("CAS shard could not be inspected") from error
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or re.fullmatch(r"[0-9a-f]{2}", directory.name) is None
            ):
                raise StorageError("CAS shard is not a real directory")
            shard_directories.append(directory)

            for path in directory.iterdir():
                try:
                    object_stat = path.lstat()
                except OSError as error:
                    raise StorageError("CAS object could not be inspected") from error
                if (
                    not stat.S_ISREG(object_stat.st_mode)
                    or re.fullmatch(r"[0-9a-f]{62}", path.name) is None
                ):
                    raise StorageError("CAS entry is not a regular content-addressed object")
                digest = directory.name + path.name
                if digest not in referenced:
                    unreferenced.append(path)

        # Validate the entire tree before unlinking any object. A malformed late
        # entry must not leave an otherwise recoverable prefix partially swept.
        removed = 0
        for path in unreferenced:
            path.unlink()
            removed += 1
        # An earlier sweep may have unlinked an object and then failed while
        # syncing its shard. Sync every validated shard on each explicit sweep so
        # the next retry can durably complete even when that object is now absent.
        for directory in shard_directories:
            _fsync_directory(directory)
        return removed

    def iter_referenced_digests(self) -> Iterator[str]:
        """Yield live object keys, primarily for maintenance and integrity checks."""

        with self._connect() as connection:
            rows = connection.execute("SELECT DISTINCT object_digest FROM incidents").fetchall()
        yield from (row["object_digest"] for row in rows)
