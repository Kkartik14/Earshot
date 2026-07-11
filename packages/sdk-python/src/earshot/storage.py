"""Immutable local incident storage backed by SQLite and content-addressed files.

Canonical protobuf artifacts own evidence content and graph truth. SQLite is the
durable catalog, tombstone, ordering, and publication authority; neither half is a
standalone backup. Derived analysis is separate and keyed by artifact digest and
analyzer version.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .contract import DerivedAnalysis

try:  # Unix file locking protects multiple local server processes sharing a store.
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to the process lock.
    fcntl = None

if TYPE_CHECKING:
    from collections.abc import Iterator

    from earshot.contract import IncidentBundle


class StorageError(RuntimeError):
    """Base class for storage failures safe to translate at the API boundary."""


class IncidentNotFoundError(StorageError):
    pass


class IncidentConflictError(StorageError):
    pass


class IncidentPurgedError(StorageError):
    pass


class ArtifactCorruptionError(StorageError):
    pass


class InvalidCursorError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class IncidentRecord:
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


_SCHEMA_VERSION = 4

_SCHEMA = """

CREATE TABLE IF NOT EXISTS incidents (
    bundle_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    object_digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    status TEXT NOT NULL,
    finality TEXT NOT NULL,
    completeness TEXT NOT NULL,
    framework TEXT,
    created_at_unix_nano TEXT NOT NULL,
    ingested_at_unix_nano INTEGER NOT NULL,
    expires_at_unix_nano TEXT,
    export_allowed_local_api INTEGER NOT NULL DEFAULT 1
        CHECK (export_allowed_local_api IN (0, 1)),
    export_allowed_local_cli INTEGER NOT NULL DEFAULT 1
        CHECK (export_allowed_local_cli IN (0, 1))
);

CREATE INDEX IF NOT EXISTS incidents_session_idx
    ON incidents(session_id, ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_digest_idx ON incidents(object_digest);
CREATE INDEX IF NOT EXISTS incidents_expiry_idx
    ON incidents(length(expires_at_unix_nano), expires_at_unix_nano)
    WHERE expires_at_unix_nano IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_export_local_api_idx
    ON incidents(export_allowed_local_api, ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_export_local_cli_idx
    ON incidents(export_allowed_local_cli, ingested_at_unix_nano DESC, bundle_id DESC);

CREATE TABLE IF NOT EXISTS analyses (
    bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
    analyzer_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    generated_at_unix_nano TEXT NOT NULL,
    output_json BLOB NOT NULL,
    PRIMARY KEY (bundle_id, analyzer_version, input_digest)
);

CREATE TABLE IF NOT EXISTS tombstones (
    bundle_id_sha256 TEXT PRIMARY KEY CHECK (length(bundle_id_sha256) = 64),
    purged_at_unix_nano INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    status TEXT NOT NULL,
    participant_id TEXT,
    stream_id TEXT,
    turn_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    parent_scope TEXT NOT NULL,
    started_source_time_unix_nano TEXT,
    started_observed_time_unix_nano TEXT,
    started_monotonic_time_nano TEXT,
    started_clock_domain_id TEXT,
    ended_source_time_unix_nano TEXT,
    ended_observed_time_unix_nano TEXT,
    ended_monotonic_time_nano TEXT,
    ended_clock_domain_id TEXT,
    evidence_source TEXT,
    evidence_confidence TEXT,
    evidence_availability TEXT,
    capture_class TEXT NOT NULL,
    PRIMARY KEY (bundle_id, operation_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS operations_otel_identity_idx
    ON operations(bundle_id, trace_id, span_id)
    WHERE trace_id IS NOT NULL AND span_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS operations_turn_idx
    ON operations(bundle_id, turn_id, operation_name);
CREATE INDEX IF NOT EXISTS operations_participant_stream_idx
    ON operations(bundle_id, participant_id, stream_id);
CREATE INDEX IF NOT EXISTS operations_parent_idx
    ON operations(bundle_id, trace_id, parent_span_id);

CREATE TABLE IF NOT EXISTS causal_links (
    bundle_id TEXT NOT NULL,
    source_operation_id TEXT NOT NULL,
    link_index INTEGER NOT NULL CHECK (link_index >= 0),
    relationship TEXT NOT NULL,
    target_scope TEXT NOT NULL,
    target_operation_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    PRIMARY KEY (bundle_id, source_operation_id, link_index),
    FOREIGN KEY (bundle_id, source_operation_id)
        REFERENCES operations(bundle_id, operation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS causal_links_target_idx
    ON causal_links(bundle_id, target_operation_id);
CREATE INDEX IF NOT EXISTS causal_links_otel_target_idx
    ON causal_links(bundle_id, trace_id, span_id);

CREATE TABLE IF NOT EXISTS events (
    bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    operation_id TEXT,
    participant_id TEXT,
    stream_id TEXT,
    turn_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    source_time_unix_nano TEXT,
    observed_time_unix_nano TEXT,
    monotonic_time_nano TEXT,
    clock_domain_id TEXT,
    evidence_source TEXT,
    evidence_confidence TEXT,
    evidence_availability TEXT,
    capture_class TEXT NOT NULL,
    PRIMARY KEY (bundle_id, event_id),
    FOREIGN KEY (bundle_id, operation_id)
        REFERENCES operations(bundle_id, operation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS events_turn_idx
    ON events(bundle_id, turn_id, event_name);
CREATE INDEX IF NOT EXISTS events_operation_idx
    ON events(bundle_id, operation_id);
CREATE INDEX IF NOT EXISTS events_participant_stream_idx
    ON events(bundle_id, participant_id, stream_id);
"""


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


def _effective_expiry(bundle: IncidentBundle) -> str | None:
    """Return the earliest retention deadline governing retained evidence.

    A class-level TTL is relative to the immutable bundle creation timestamp. An
    immutable bundle cannot selectively delete one class in place, so its most
    restrictive captured-class deadline governs the complete artifact.
    """

    created = int(bundle.profile.manifest.created_at_unix_nano)
    deadlines: list[int] = []
    for policy in bundle.profile.privacy.capture_classes:
        if not policy.captured or policy.retention is None:
            continue
        retention = policy.retention
        if retention.expires_at_unix_nano is not None:
            deadlines.append(int(retention.expires_at_unix_nano))
        if retention.ttl_nano is not None:
            deadlines.append(created + int(retention.ttl_nano))
    return str(min(deadlines)) if deadlines else None


def _export_allowed(bundle: IncidentBundle, destination: str) -> bool:
    for policy in bundle.profile.privacy.capture_classes:
        if not policy.captured or policy.export is None:
            continue
        export = policy.export
        if not export.allowed:
            return False
        if export.destinations and destination not in export.destinations:
            return False
    return True


def _evidence_columns(evidence: Any | None) -> tuple[str | None, str | None, str | None]:
    if evidence is None:
        return None, None, None
    return evidence.source, evidence.confidence, evidence.availability


def _decimal_lte(value: str, other: str) -> bool:
    return len(value) < len(other) or (len(value) == len(other) and value <= other)


def _encode_cursor(record: IncidentRecord) -> str:
    raw = json.dumps(
        [record.ingested_at_unix_nano, record.bundle_id],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or not value[0].isdigit()
            or not isinstance(value[1], str)
            or not value[1]
        ):
            raise ValueError
        return int(value[0]), value[1]
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidCursorError("invalid incident pagination cursor") from error


def _record(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
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
        path = self.path_for(digest)
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            _fsync_directory(path.parent)

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
    """Thread/process-safe facade over SQLite and content-addressed evidence.

    Purge uses SQLite ``secure_delete``, WAL checkpoint/truncation, ``VACUUM``,
    fsync, and CAS unlinking. This removes ordinary live/free-page remnants but is
    necessarily best effort: copy-on-write filesystems, SSD wear levelling,
    snapshots, and external backups can retain historical blocks. Deployments that
    require cryptographic erasure should encrypt evidence with disposable keys.
    """

    def __init__(self, data_dir: str | Path, *, database_path: str | Path | None = None):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self._database_uri = False
        self._anchor_connection: sqlite3.Connection | None = None
        self._write_lock = threading.RLock()
        self._mutation_depth = 0
        lock_path = self.data_dir / ".store.lock"
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        self._lock_handle = os.fdopen(lock_descriptor, "a+b", buffering=0)
        if database_path is not None and str(database_path) == ":memory:":
            # Each operation uses its own connection. A named shared-memory URI
            # plus an anchor keeps one logical database alive across those calls.
            self.database_path = f"file:earshot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._database_uri = True
            self._anchor_connection = sqlite3.connect(
                self.database_path,
                timeout=10.0,
                uri=True,
            )
            self._configure_connection(self._anchor_connection)
        else:
            database = (
                Path(database_path).expanduser().resolve()
                if database_path is not None
                else self.data_dir / "earshot.sqlite3"
            )
            database.parent.mkdir(parents=True, exist_ok=True)
            self.database_path = str(database)
        self.objects = ContentAddressedObjects(self.data_dir / "objects" / "sha256")
        try:
            if (
                not self._database_uri
                and self.objects.has_objects()
                and not self._existing_catalog_is_valid()
            ):
                raise StorageError(
                    "incident catalog is missing or invalid while CAS evidence exists; "
                    "restore the SQLite catalog before opening this store"
                )
            self._initialize()
            self._reconcile()
        except Exception:
            if self._anchor_connection is not None:
                self._anchor_connection.close()
                self._anchor_connection = None
            self._lock_handle.close()
            raise

    def _existing_catalog_is_valid(self) -> bool:
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

    @contextlib.contextmanager
    def _mutation(self) -> Iterator[None]:
        """Serialize filesystem+database mutations across threads and processes."""

        with self._write_lock:
            outermost = self._mutation_depth == 0
            if outermost and fcntl is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
            self._mutation_depth += 1
            try:
                yield
            finally:
                self._mutation_depth -= 1
                if outermost and fcntl is not None:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA wal_autocheckpoint = 1000")
        connection.execute("PRAGMA journal_size_limit = 0")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10.0,
            uri=self._database_uri,
        )
        self._configure_connection(connection)
        return connection

    def _initialize(self) -> None:
        with self._mutation(), self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise StorageError("incident database schema is newer than this binary")
            connection.execute("PRAGMA journal_mode = WAL")
            incidents_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'incidents'"
            ).fetchone()
            columns = (
                {row["name"] for row in connection.execute("PRAGMA table_info(incidents)")}
                if incidents_exists
                else set()
            )
            additions = {
                "expires_at_unix_nano": "TEXT",
                "export_allowed_local_api": (
                    "INTEGER NOT NULL DEFAULT 1 CHECK (export_allowed_local_api IN (0, 1))"
                ),
                "export_allowed_local_cli": (
                    "INTEGER NOT NULL DEFAULT 1 CHECK (export_allowed_local_cli IN (0, 1))"
                ),
            }
            if incidents_exists:
                for column, declaration in additions.items():
                    if column not in columns:
                        connection.execute(
                            f"ALTER TABLE incidents ADD COLUMN {column} {declaration}"
                        )
            analyses_columns = {
                row["name"]: str(row["type"]).upper()
                for row in connection.execute("PRAGMA table_info(analyses)")
            }
            if analyses_columns.get("generated_at_unix_nano") == "INTEGER":
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                connection.execute("ALTER TABLE analyses RENAME TO analyses_integer_time")
                connection.execute(
                    """
                    CREATE TABLE analyses (
                        bundle_id TEXT NOT NULL
                            REFERENCES incidents(bundle_id) ON DELETE CASCADE,
                        analyzer_version TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        generated_at_unix_nano TEXT NOT NULL,
                        output_json BLOB NOT NULL,
                        PRIMARY KEY (bundle_id, analyzer_version, input_digest)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO analyses (
                        bundle_id, analyzer_version, input_digest,
                        generated_at_unix_nano, output_json
                    )
                    SELECT
                        bundle_id, analyzer_version, input_digest,
                        CAST(generated_at_unix_nano AS TEXT), output_json
                    FROM analyses_integer_time
                    """
                )
                connection.execute("DROP TABLE analyses_integer_time")
            tombstone_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tombstones)")
            }
            if "bundle_id" in tombstone_columns:
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                legacy_tombstones = connection.execute(
                    "SELECT bundle_id, purged_at_unix_nano FROM tombstones"
                ).fetchall()
                connection.execute("ALTER TABLE tombstones RENAME TO tombstones_plaintext")
                connection.execute(
                    """
                    CREATE TABLE tombstones (
                        bundle_id_sha256 TEXT PRIMARY KEY
                            CHECK (length(bundle_id_sha256) = 64),
                        purged_at_unix_nano INTEGER NOT NULL
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO tombstones(bundle_id_sha256, purged_at_unix_nano) VALUES (?, ?)",
                    (
                        (_tombstone_key(row["bundle_id"]), row["purged_at_unix_nano"])
                        for row in legacy_tombstones
                    ),
                )
                connection.execute("DROP TABLE tombstones_plaintext")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        self._harden_database_permissions()
        if not self._database_uri:
            _fsync_directory(Path(self.database_path).parent)

    def _harden_database_permissions(self) -> None:
        if self._database_uri:
            return
        database = Path(self.database_path)
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    def _reconcile(self) -> None:
        """Repair derived indexes without guessing whether unreferenced evidence is disposable."""

        from .codec import IncidentCodecError, decode_incident_protobuf

        with self._mutation():
            self.objects.cleanup_temporary_files()
            # Honor already-indexed retention before decoding artifacts. Expired
            # evidence must remain erasable even if its bytes were later corrupted.
            expired = self._purge_all_expired_locked(str(time.time_ns()))
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT bundle_id, object_digest FROM incidents ORDER BY bundle_id"
                ).fetchall()
                has_tombstones = (
                    connection.execute("SELECT 1 FROM tombstones LIMIT 1").fetchone() is not None
                )
                connection.execute("BEGIN IMMEDIATE")
                for row in rows:
                    payload = self.objects.get(row["object_digest"])
                    try:
                        bundle = decode_incident_protobuf(payload)
                    except IncidentCodecError as error:
                        raise ArtifactCorruptionError(
                            "stored incident cannot be decoded during reconciliation"
                        ) from error
                    if bundle.profile.manifest.bundle_id != row["bundle_id"]:
                        raise ArtifactCorruptionError(
                            "stored incident identity does not match its index"
                        )
                    self._replace_graph_projection(connection, bundle)
                    connection.execute(
                        """
                        UPDATE incidents
                        SET expires_at_unix_nano = ?,
                            export_allowed_local_api = ?,
                            export_allowed_local_cli = ?
                        WHERE bundle_id = ?
                        """,
                        (
                            _effective_expiry(bundle),
                            int(_export_allowed(bundle, "local_api")),
                            int(_export_allowed(bundle, "local_cli")),
                            row["bundle_id"],
                        ),
                    )
                connection.commit()
            # The decode pass may have backfilled deadlines for a v1 database.
            expired += self._purge_all_expired_locked(str(time.time_ns()))
            if has_tombstones and expired == 0:
                # A process may have crashed after logical deletion/CAS unlink but
                # before checkpoint/VACUUM. Repeating the scrub is safe.
                self._scrub_deleted_pages()
        self._harden_database_permissions()

    def close(self) -> None:
        with self._write_lock:
            if self._anchor_connection is not None:
                self._anchor_connection.close()
                self._anchor_connection = None
            if not self._lock_handle.closed:
                self._lock_handle.close()

    def __enter__(self) -> IncidentStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return os.access(self.objects.root, os.W_OK) and os.access(self.objects.tmp, os.W_OK)
        except (OSError, sqlite3.Error):
            return False

    @staticmethod
    def _replace_graph_projection(
        connection: sqlite3.Connection,
        bundle: IncidentBundle,
    ) -> None:
        """Replace derived graph rows inside the caller's incident transaction."""

        bundle_id = bundle.profile.manifest.bundle_id
        connection.execute("DELETE FROM events WHERE bundle_id = ?", (bundle_id,))
        connection.execute("DELETE FROM causal_links WHERE bundle_id = ?", (bundle_id,))
        connection.execute("DELETE FROM operations WHERE bundle_id = ?", (bundle_id,))

        operation_rows: list[tuple[object, ...]] = []
        link_rows: list[tuple[object, ...]] = []
        for operation in bundle.profile.operations:
            evidence_source, evidence_confidence, evidence_availability = _evidence_columns(
                operation.evidence
            )
            ended = operation.ended_at
            operation_rows.append(
                (
                    bundle_id,
                    operation.operation_id,
                    operation.operation_name,
                    operation.status,
                    operation.participant_id,
                    operation.stream_id,
                    operation.turn_id,
                    operation.trace_id,
                    operation.span_id,
                    operation.parent_span_id,
                    operation.parent_scope,
                    operation.started_at.source_time_unix_nano,
                    operation.started_at.observed_time_unix_nano,
                    operation.started_at.monotonic_time_nano,
                    operation.started_at.clock_domain_id,
                    ended.source_time_unix_nano if ended else None,
                    ended.observed_time_unix_nano if ended else None,
                    ended.monotonic_time_nano if ended else None,
                    ended.clock_domain_id if ended else None,
                    evidence_source,
                    evidence_confidence,
                    evidence_availability,
                    operation.capture_class,
                )
            )
            link_rows.extend(
                (
                    bundle_id,
                    operation.operation_id,
                    link_index,
                    link.relationship,
                    link.target_scope,
                    link.target_operation_id,
                    link.trace_id,
                    link.span_id,
                )
                for link_index, link in enumerate(operation.links)
            )

        connection.executemany(
            """
            INSERT INTO operations (
                bundle_id, operation_id, operation_name, status,
                participant_id, stream_id, turn_id, trace_id, span_id,
                parent_span_id, parent_scope,
                started_source_time_unix_nano, started_observed_time_unix_nano,
                started_monotonic_time_nano, started_clock_domain_id,
                ended_source_time_unix_nano, ended_observed_time_unix_nano,
                ended_monotonic_time_nano, ended_clock_domain_id,
                evidence_source, evidence_confidence, evidence_availability,
                capture_class
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            operation_rows,
        )
        connection.executemany(
            """
            INSERT INTO causal_links (
                bundle_id, source_operation_id, link_index, relationship,
                target_scope, target_operation_id, trace_id, span_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            link_rows,
        )

        event_rows: list[tuple[object, ...]] = []
        for event in bundle.profile.events:
            evidence_source, evidence_confidence, evidence_availability = _evidence_columns(
                event.evidence
            )
            event_rows.append(
                (
                    bundle_id,
                    event.event_id,
                    event.event_name,
                    event.operation_id,
                    event.participant_id,
                    event.stream_id,
                    event.turn_id,
                    event.trace_id,
                    event.span_id,
                    event.time.source_time_unix_nano,
                    event.time.observed_time_unix_nano,
                    event.time.monotonic_time_nano,
                    event.time.clock_domain_id,
                    evidence_source,
                    evidence_confidence,
                    evidence_availability,
                    event.capture_class,
                )
            )
        connection.executemany(
            """
            INSERT INTO events (
                bundle_id, event_id, event_name, operation_id,
                participant_id, stream_id, turn_id, trace_id, span_id,
                source_time_unix_nano, observed_time_unix_nano,
                monotonic_time_nano, clock_domain_id,
                evidence_source, evidence_confidence, evidence_availability,
                capture_class
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            event_rows,
        )

    def ingest(
        self,
        bundle: IncidentBundle,
        canonical_payload: bytes | None = None,
    ) -> IngestResult:
        """Persist an already validated bundle atomically.

        Validation and encoding are delegated to the contract modules so direct
        storage callers receive the same guarantees as HTTP ingest callers.
        """

        from .codec import encode_incident_protobuf
        from .validation import assert_valid_incident

        assert_valid_incident(bundle)
        expected_payload = encode_incident_protobuf(bundle)
        if canonical_payload is None:
            canonical_payload = expected_payload
        elif canonical_payload != expected_payload:
            raise StorageError("payload is not the canonical encoding of the incident")

        manifest = bundle.profile.manifest
        framework = manifest.adapters[0].framework if manifest.adapters else None
        ingested_at = time.time_ns()
        expiry = _effective_expiry(bundle)
        export_local_api = int(_export_allowed(bundle, "local_api"))
        export_local_cli = int(_export_allowed(bundle, "local_cli"))

        with self._mutation():
            # CAS publication and index insertion are one critical section. This
            # prevents cleanup from mistaking a just-published object for an orphan.
            digest, object_created = self.objects.put(canonical_payload)
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute(
                        "SELECT 1 FROM tombstones WHERE bundle_id_sha256 = ?",
                        (_tombstone_key(manifest.bundle_id),),
                    ).fetchone():
                        raise IncidentPurgedError("purged incident identifiers cannot be reused")

                    existing = connection.execute(
                        "SELECT * FROM incidents WHERE bundle_id = ?", (manifest.bundle_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["object_digest"] != digest:
                            raise IncidentConflictError(
                                "bundle identifier already exists with different content"
                            )
                        self._replace_graph_projection(connection, bundle)
                        connection.execute(
                            """
                            UPDATE incidents
                            SET expires_at_unix_nano = ?,
                                export_allowed_local_api = ?,
                                export_allowed_local_cli = ?
                            WHERE bundle_id = ?
                            """,
                            (
                                expiry,
                                export_local_api,
                                export_local_cli,
                                manifest.bundle_id,
                            ),
                        )
                        connection.commit()
                        refreshed = connection.execute(
                            "SELECT * FROM incidents WHERE bundle_id = ?",
                            (manifest.bundle_id,),
                        ).fetchone()
                        assert refreshed is not None
                        return IngestResult(record=_record(refreshed), created=False)

                    connection.execute(
                        """
                        INSERT INTO incidents (
                            bundle_id, session_id, schema_version, object_digest,
                            size_bytes, status, finality, completeness, framework,
                            created_at_unix_nano, ingested_at_unix_nano,
                            expires_at_unix_nano, export_allowed_local_api,
                            export_allowed_local_cli
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            manifest.bundle_id,
                            manifest.session_id,
                            manifest.schema_version,
                            digest,
                            len(canonical_payload),
                            bundle.profile.session.status,
                            manifest.finality,
                            manifest.completeness,
                            framework,
                            manifest.created_at_unix_nano,
                            ingested_at,
                            expiry,
                            export_local_api,
                            export_local_cli,
                        ),
                    )
                    self._replace_graph_projection(connection, bundle)
                    row = connection.execute(
                        "SELECT * FROM incidents WHERE bundle_id = ?", (manifest.bundle_id,)
                    ).fetchone()
                    assert row is not None
                    connection.commit()
                    self._harden_database_permissions()
                    return IngestResult(record=_record(row), created=True)
            except Exception:
                if object_created:
                    try:
                        with self._connect() as cleanup_connection:
                            referenced = cleanup_connection.execute(
                                "SELECT 1 FROM incidents WHERE object_digest = ? LIMIT 1",
                                (digest,),
                            ).fetchone()
                        if referenced is None:
                            self.objects.delete(digest)
                    except (OSError, sqlite3.Error):
                        # Reconciliation removes any crash-left unindexed CAS object.
                        pass
                raise

    def get_record(self, bundle_id: str) -> IncidentRecord:
        with self._mutation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM incidents WHERE bundle_id = ?", (bundle_id,)
                ).fetchone()
                tombstoned = (
                    connection.execute(
                        "SELECT 1 FROM tombstones WHERE bundle_id_sha256 = ?",
                        (_tombstone_key(bundle_id),),
                    ).fetchone()
                    is not None
                )
            if row is not None:
                expiry = row["expires_at_unix_nano"]
                if expiry is not None and _decimal_lte(expiry, str(time.time_ns())):
                    self._purge_existing_locked((bundle_id,), time.time_ns())
                    raise IncidentPurgedError("incident was purged")
                return _record(row)
            if tombstoned:
                raise IncidentPurgedError("incident was purged")
            raise IncidentNotFoundError("incident not found")

    def get_artifact(self, bundle_id: str) -> tuple[IncidentRecord, bytes]:
        # Keep the index lookup and object read coherent with concurrent purge.
        with self._mutation():
            record = self.get_record(bundle_id)
            return record, self.objects.get(record.digest)

    def list_incidents(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        destination: str | None = None,
    ) -> IncidentPage:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if destination not in {None, "local_api", "local_cli"}:
            raise ValueError("destination must be local_api or local_cli")

        clauses: list[str] = []
        parameters: list[object] = []
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
            cursor_time, cursor_bundle_id = _decode_cursor(cursor)
            clauses.append(
                "(ingested_at_unix_nano < ? OR (ingested_at_unix_nano = ? AND bundle_id < ?))"
            )
            parameters.extend((cursor_time, cursor_time, cursor_bundle_id))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit + 1)
        with self._mutation():
            self._purge_all_expired_locked(str(time.time_ns()))
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM incidents"
                    + where
                    + " ORDER BY ingested_at_unix_nano DESC, bundle_id DESC LIMIT ?",
                    parameters,
                ).fetchall()
            has_more = len(rows) > limit
            records = tuple(_record(row) for row in rows[:limit])
            next_cursor = _encode_cursor(records[-1]) if has_more and records else None
            return IncidentPage(items=records, next_cursor=next_cursor)

    def get_analysis(self, bundle_id: str, analyzer_version: str) -> StoredAnalysis | None:
        with self._mutation():
            record = self.get_record(bundle_id)
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
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
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

            _, artifact_payload = self.get_artifact(bundle_id)
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
        with self._mutation():
            record, artifact_payload = self.get_artifact(bundle_id)
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
                connection.execute("BEGIN IMMEDIATE")
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
            stored = self.get_analysis(bundle_id, analyzer_version)
            assert stored is not None
            return stored

    def purge(self, bundle_id: str) -> None:
        """Best-effort physically remove evidence and leave a minimal tombstone."""

        with self._mutation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM incidents WHERE bundle_id = ?", (bundle_id,)
                ).fetchone()
                if row is None:
                    if connection.execute(
                        "SELECT 1 FROM tombstones WHERE bundle_id_sha256 = ?",
                        (_tombstone_key(bundle_id),),
                    ).fetchone():
                        # Retry both filesystem cleanup and SQLite free-page scrubbing.
                        try:
                            self._cleanup_unreferenced_objects_locked()
                            self._scrub_deleted_pages()
                        except (OSError, sqlite3.Error) as error:
                            raise StorageError("artifact purge requires retry") from error
                        return
                    raise IncidentNotFoundError("incident not found")
            self._purge_existing_locked((bundle_id,), time.time_ns())

    def purge_expired(
        self,
        now_unix_nano: int | str | None = None,
        *,
        limit: int = 1000,
    ) -> int:
        """Purge up to ``limit`` incidents whose earliest retention deadline passed."""

        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now = str(time.time_ns() if now_unix_nano is None else now_unix_nano)
        if (
            not now
            or any(character not in "0123456789" for character in now)
            or (len(now) > 1 and now.startswith("0"))
        ):
            raise ValueError("now_unix_nano must be a canonical non-negative decimal")

        with self._mutation():
            return self._purge_expired_batch_locked(now, limit)

    def _purge_all_expired_locked(self, now: str) -> int:
        removed = 0
        while True:
            count = self._purge_expired_batch_locked(now, 10_000)
            removed += count
            if count < 10_000:
                return removed

    def _purge_expired_batch_locked(self, now: str, limit: int) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bundle_id
                FROM incidents
                WHERE expires_at_unix_nano IS NOT NULL
                  AND (
                    length(expires_at_unix_nano) < ?
                    OR (
                      length(expires_at_unix_nano) = ?
                      AND expires_at_unix_nano <= ?
                    )
                  )
                ORDER BY length(expires_at_unix_nano), expires_at_unix_nano, bundle_id
                LIMIT ?
                """,
                (len(now), len(now), now, limit),
            ).fetchall()
        bundle_ids = tuple(row["bundle_id"] for row in rows)
        if not bundle_ids:
            return 0
        self._purge_existing_locked(bundle_ids, time.time_ns())
        return len(bundle_ids)

    def _purge_existing_locked(
        self,
        bundle_ids: tuple[str, ...],
        purged_at_unix_nano: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows: list[sqlite3.Row] = []
            for offset in range(0, len(bundle_ids), 500):
                batch = bundle_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        "SELECT bundle_id, object_digest FROM incidents "
                        f"WHERE bundle_id IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            if len(rows) != len(bundle_ids):
                raise IncidentNotFoundError("incident not found")
            connection.executemany(
                "DELETE FROM incidents WHERE bundle_id = ?",
                ((bundle_id,) for bundle_id in bundle_ids),
            )
            connection.executemany(
                """
                INSERT INTO tombstones(bundle_id_sha256, purged_at_unix_nano)
                VALUES (?, ?)
                ON CONFLICT(bundle_id_sha256) DO NOTHING
                """,
                ((_tombstone_key(bundle_id), purged_at_unix_nano) for bundle_id in bundle_ids),
            )
            digests = {row["object_digest"] for row in rows}
            referenced: set[str] = set()
            digest_values = tuple(digests)
            for offset in range(0, len(digest_values), 500):
                batch = digest_values[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                referenced.update(
                    row["object_digest"]
                    for row in connection.execute(
                        "SELECT DISTINCT object_digest FROM incidents "
                        f"WHERE object_digest IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            connection.commit()

        try:
            for digest in digests - referenced:
                self.objects.delete(digest)
            self._cleanup_unreferenced_objects_locked()
            self._scrub_deleted_pages()
        except (OSError, sqlite3.Error) as error:
            # Tombstones are already durable; repeating purge safely retries scrub.
            raise StorageError("artifact purge requires retry") from error

    def _scrub_deleted_pages(self) -> None:
        """Compact secure-deleted pages and truncate WAL remnants.

        This is a best-effort file-level scrub, not a guarantee against filesystem
        snapshots, copy-on-write history, SSD remapping, or external backups.
        """

        if self._database_uri:
            return
        with self._connect() as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and checkpoint[0] != 0:
                raise StorageError("SQLite WAL is busy; purge requires retry")
            connection.execute("VACUUM")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and checkpoint[0] != 0:
                raise StorageError("SQLite WAL is busy; purge requires retry")
        self._harden_database_permissions()
        database = Path(self.database_path)
        _fsync_file(database)
        for suffix in ("-wal", "-shm"):
            path = Path(f"{database}{suffix}")
            if path.exists():
                _fsync_file(path)
        _fsync_directory(database.parent)

    def cleanup_unreferenced_objects(self) -> int:
        """Remove CAS leftovers without retaining evidence in tombstones."""

        with self._mutation():
            return self._cleanup_unreferenced_objects_locked()

    def _cleanup_unreferenced_objects_locked(self) -> int:
        with self._connect() as connection:
            referenced = {
                row["object_digest"]
                for row in connection.execute(
                    "SELECT DISTINCT object_digest FROM incidents"
                ).fetchall()
            }
        removed = 0
        for directory in self.objects.root.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                digest = directory.name + path.name
                if digest not in referenced:
                    path.unlink(missing_ok=True)
                    removed += 1
            if removed:
                _fsync_directory(directory)
        return removed

    def iter_referenced_digests(self) -> Iterator[str]:
        """Yield live object keys, primarily for maintenance and integrity checks."""

        with self._connect() as connection:
            rows = connection.execute("SELECT DISTINCT object_digest FROM incidents").fetchall()
        yield from (row["object_digest"] for row in rows)
