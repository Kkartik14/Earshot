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
import hmac
import json
import os
import re
import secrets
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
from .versions import (
    ELEVENLABS_NORMALIZER_VERSION,
    RETELL_NORMALIZER_VERSION,
    RINGG_NORMALIZER_VERSION,
    TURN_FACT_PROJECTION_VERSION,
    VAPI_NORMALIZER_VERSION,
)

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


class DeliveryReceiptConflictError(StorageError):
    pass


class DeliveryInProgressError(StorageError):
    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


DEFAULT_PROJECT_ID = "default"
_CONNECTOR_NORMALIZER_VERSIONS = {
    "elevenlabs": ELEVENLABS_NORMALIZER_VERSION,
    "vapi": VAPI_NORMALIZER_VERSION,
    "retell": RETELL_NORMALIZER_VERSION,
    "ringg": RINGG_NORMALIZER_VERSION,
}
_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_API_KEY_PREFIX = "earshot_sk_"
# This version owns the wide Turn Fact semantics, not only the underlying
# deterministic analyzer. Bump it whenever projection meanings or evidence
# selection rules change.


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    project_id: str
    display_name: str
    created_at_unix_nano: str


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
class ConnectorRecord:
    endpoint_id: str
    project_id: str
    provider: str
    secret_ref: str
    enabled: bool
    normalizer_version: str


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    receipt_id: str
    disposition: str
    bundle_id: str | None
    canonical_sha256: str | None
    lease_token: int | None


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


_SCHEMA_VERSION = 10
_MAX_CURSOR_ENCODED_CHARS = 4096
_MAX_CURSOR_DECODED_BYTES = 512
_SQLITE_INT64_MAX = (1 << 63) - 1

_SCHEMA = """

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL
);

INSERT OR IGNORE INTO projects(project_id, display_name, created_at_unix_nano)
VALUES ('default', 'Default', 0);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    secret_salt BLOB NOT NULL,
    secret_hash BLOB NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    last_used_at_unix_nano INTEGER,
    revoked_at_unix_nano INTEGER
);

CREATE INDEX IF NOT EXISTS api_keys_project_idx
    ON api_keys(project_id, revoked_at_unix_nano, created_at_unix_nano DESC);

CREATE TABLE IF NOT EXISTS incidents (
    bundle_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'default'
        REFERENCES projects(project_id),
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
    ON incidents(project_id, session_id, ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_project_idx
    ON incidents(project_id, ingested_at_unix_nano DESC, bundle_id DESC);
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
    project_id TEXT NOT NULL REFERENCES projects(project_id),
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

CREATE TABLE IF NOT EXISTS turn_metrics (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
    started_at_unix_nano TEXT,
    framework TEXT,
    provider TEXT,
    model TEXT,
    language TEXT,
    status TEXT NOT NULL,
    stt_finalization_ms REAL,
    stt_finalization_availability TEXT NOT NULL,
    stt_finalization_basis TEXT NOT NULL,
    stt_finalization_confidence TEXT NOT NULL,
    stt_finalization_limitation TEXT,
    eou_ms REAL,
    eou_availability TEXT NOT NULL,
    eou_basis TEXT NOT NULL,
    eou_confidence TEXT NOT NULL,
    eou_limitation TEXT,
    first_token_ms REAL,
    first_token_availability TEXT NOT NULL,
    first_token_basis TEXT NOT NULL,
    first_token_confidence TEXT NOT NULL,
    first_token_limitation TEXT,
    generated_response_ms REAL,
    generated_response_availability TEXT NOT NULL,
    generated_response_basis TEXT NOT NULL,
    generated_response_confidence TEXT NOT NULL,
    generated_response_limitation TEXT,
    sent_response_ms REAL,
    sent_response_availability TEXT NOT NULL,
    sent_response_basis TEXT NOT NULL,
    sent_response_confidence TEXT NOT NULL,
    sent_response_limitation TEXT,
    received_response_ms REAL,
    received_response_availability TEXT NOT NULL,
    received_response_basis TEXT NOT NULL,
    received_response_confidence TEXT NOT NULL,
    received_response_limitation TEXT,
    render_start_response_ms REAL,
    render_start_response_availability TEXT NOT NULL,
    render_start_response_basis TEXT NOT NULL,
    render_start_response_confidence TEXT NOT NULL,
    render_start_response_limitation TEXT,
    response_ms REAL,
    response_availability TEXT NOT NULL,
    response_basis TEXT NOT NULL,
    response_confidence TEXT NOT NULL,
    response_limitation TEXT,
    turn_duration_ms REAL,
    turn_duration_availability TEXT NOT NULL,
    turn_duration_basis TEXT NOT NULL,
    turn_duration_confidence TEXT NOT NULL,
    turn_duration_limitation TEXT,
    tool_operation_count INTEGER NOT NULL CHECK (tool_operation_count >= 0),
    tool_total_work_ms REAL NOT NULL CHECK (tool_total_work_ms >= 0),
    interruption_count INTEGER CHECK (interruption_count >= 0),
    interruption_availability TEXT NOT NULL,
    interruption_basis TEXT NOT NULL,
    interruption_confidence TEXT NOT NULL,
    interruption_limitation TEXT,
    provider_measurements_json BLOB NOT NULL,
    projection_version TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    PRIMARY KEY (bundle_id, turn_id)
);

CREATE INDEX IF NOT EXISTS turn_metrics_project_time_idx
    ON turn_metrics(project_id, started_at_unix_nano, bundle_id, turn_index);
CREATE INDEX IF NOT EXISTS turn_metrics_project_dimensions_idx
    ON turn_metrics(project_id, framework, provider, model, language, status);

CREATE TABLE IF NOT EXISTS connectors (
    endpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    normalizer_version TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS connectors_project_idx
    ON connectors(project_id, provider, enabled);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    receipt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL REFERENCES connectors(endpoint_id) ON DELETE CASCADE,
    delivery_key_hmac TEXT NOT NULL CHECK (length(delivery_key_hmac) = 64),
    body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    first_received_at_unix_nano INTEGER NOT NULL,
    completed_at_unix_nano INTEGER,
    lease_until_unix_nano INTEGER,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
    bundle_id TEXT REFERENCES incidents(bundle_id) ON DELETE SET NULL,
    canonical_sha256 TEXT,
    failure_code TEXT,
    UNIQUE(endpoint_id, delivery_key_hmac)
);

CREATE INDEX IF NOT EXISTS delivery_receipts_project_state_idx
    ON delivery_receipts(project_id, state, first_received_at_unix_nano DESC);

CREATE TABLE IF NOT EXISTS external_identities (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL REFERENCES connectors(endpoint_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    key_kind TEXT NOT NULL,
    value_hmac TEXT NOT NULL CHECK (length(value_hmac) = 64),
    bundle_id TEXT NOT NULL REFERENCES incidents(bundle_id) ON DELETE CASCADE,
    sensitivity TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    PRIMARY KEY (project_id, endpoint_id, key_kind, value_hmac, bundle_id)
);
"""


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute a static SQL script without sqlite3.executescript's implicit commit."""

    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            pending.clear()
    if "".join(pending).strip():
        raise StorageError("incident database schema script is incomplete")


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
            or not value[0].isdigit()
            or (len(value[0]) > 1 and value[0].startswith("0"))
            or not isinstance(value[1], str)
            or not value[1]
            or len(value[1]) > 256
        ):
            raise ValueError
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
        raise InvalidCursorError("invalid incident pagination cursor") from error


def _record(row: sqlite3.Row) -> IncidentRecord:
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


def _turn_fact(row: sqlite3.Row) -> TurnFact:
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


def _connector_record(row: sqlite3.Row) -> ConnectorRecord:
    return ConnectorRecord(
        endpoint_id=row["endpoint_id"],
        project_id=row["project_id"],
        provider=row["provider"],
        secret_ref=row["secret_ref"],
        enabled=bool(row["enabled"]),
        normalizer_version=row["normalizer_version"],
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
        self._identity_key = b""
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

    def _catalog_has_correlation_state(self) -> bool:
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
                for table in ("incidents", "delivery_receipts", "external_identities"):
                    if (
                        table in tables
                        and connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    ):
                        return True
        except (OSError, sqlite3.Error, ValueError):
            return False
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
            connection.execute("BEGIN IMMEDIATE")
            # Project ownership is needed by later migrations (notably scoped
            # tombstones), so establish the authorization root before inspecting
            # or rebuilding any legacy table that now references it.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at_unix_nano INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO projects(
                    project_id, display_name, created_at_unix_nano
                ) VALUES ('default', 'Default', 0);
                """
            )
            incidents_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'incidents'"
            ).fetchone()
            columns = (
                {row["name"] for row in connection.execute("PRAGMA table_info(incidents)")}
                if incidents_exists
                else set()
            )
            additions = {
                "project_id": "TEXT NOT NULL DEFAULT 'default'",
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
                session_index = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'incidents_session_idx'"
                ).fetchone()
                if session_index is not None and "project_id" not in str(session_index["sql"]):
                    connection.execute("DROP INDEX incidents_session_idx")
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
                        project_id TEXT NOT NULL REFERENCES projects(project_id),
                        purged_at_unix_nano INTEGER NOT NULL
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO tombstones(
                        bundle_id_sha256, project_id, purged_at_unix_nano
                    ) VALUES (?, 'default', ?)
                    """,
                    (
                        (_tombstone_key(row["bundle_id"]), row["purged_at_unix_nano"])
                        for row in legacy_tombstones
                    ),
                )
                connection.execute("DROP TABLE tombstones_plaintext")
            elif tombstone_columns and "project_id" not in tombstone_columns:
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                connection.execute("ALTER TABLE tombstones RENAME TO tombstones_unscoped")
                connection.execute(
                    """
                    CREATE TABLE tombstones (
                        bundle_id_sha256 TEXT PRIMARY KEY
                            CHECK (length(bundle_id_sha256) = 64),
                        project_id TEXT NOT NULL REFERENCES projects(project_id),
                        purged_at_unix_nano INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tombstones(
                        bundle_id_sha256, project_id, purged_at_unix_nano
                    )
                    SELECT bundle_id_sha256, 'default', purged_at_unix_nano
                    FROM tombstones_unscoped
                    """
                )
                connection.execute("DROP TABLE tombstones_unscoped")
            turn_metric_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(turn_metrics)")
            }
            if turn_metric_columns and not {
                "stt_finalization_ms",
                "language",
            }.issubset(turn_metric_columns):
                # Turn Facts are a fully rebuildable read model. Recreating the
                # projection is safer than manufacturing evidence fields for old
                # rows during a migration; reconciliation below repopulates it
                # from the canonical Incident artifacts.
                connection.execute("DROP TABLE turn_metrics")
            _execute_sql_script(connection, _SCHEMA)
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

    def create_project(self, project_id: str, *, display_name: str) -> ProjectRecord:
        """Create an authorization scope without exposing storage details to callers."""

        if not _PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be a lowercase portable identifier")
        if not display_name or len(display_name) > 128:
            raise ValueError("display_name must contain between 1 and 128 characters")
        created_at = time.time_ns()
        with self._mutation(), self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, display_name, created_at_unix_nano)
                VALUES (?, ?, ?)
                """,
                (project_id, display_name, created_at),
            )
            connection.commit()
        return ProjectRecord(project_id, display_name, str(created_at))

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
        with self._mutation(), self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("project does not exist")
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
                    "SELECT 1 FROM api_keys WHERE revoked_at_unix_nano IS NULL LIMIT 1"
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, secret_salt, secret_hash
                FROM api_keys
                WHERE key_id = ? AND revoked_at_unix_nano IS NULL
                """,
                (key_id,),
            ).fetchone()
            if row is None:
                return None
        supplied_hash = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=bytes(row["secret_salt"]),
            n=1 << 14,
            r=8,
            p=1,
            dklen=32,
        )
        if not hmac.compare_digest(supplied_hash, bytes(row["secret_hash"])):
            return None
        with self._mutation(), self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE api_keys SET last_used_at_unix_nano = ?
                WHERE key_id = ? AND revoked_at_unix_nano IS NULL
                """,
                (time.time_ns(), key_id),
            )
            if updated.rowcount != 1:
                # Revocation may win after the hash check but before usage is
                # recorded. Never authenticate that race.
                return None
            connection.commit()
        return ApiPrincipal(project_id=row["project_id"], key_id=key_id)

    def api_key_is_active(self, project_id: str, key_id: str) -> bool:
        """Check whether a key-backed browser session still has an active issuer."""

        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM api_keys
                    WHERE project_id = ? AND key_id = ? AND revoked_at_unix_nano IS NULL
                    """,
                    (project_id, key_id),
                ).fetchone()
                is not None
            )

    def revoke_api_key(self, project_id: str, key_id: str) -> None:
        with self._mutation(), self._connect() as connection:
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
    ) -> ConnectorRecord:
        if provider not in _CONNECTOR_NORMALIZER_VERSIONS:
            raise ValueError("unsupported connector provider")
        if not re.fullmatch(r"env:[A-Z][A-Z0-9_]{0,127}", secret_ref):
            raise ValueError("secret_ref must name a portable environment secret")
        identifier = endpoint_id or f"connector_{secrets.token_urlsafe(18)}"
        selected_normalizer = normalizer_version or _CONNECTOR_NORMALIZER_VERSIONS[provider]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", identifier):
            raise ValueError("endpoint_id must be an opaque portable identifier")
        with self._mutation(), self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("project does not exist")
            connection.execute(
                """
                INSERT INTO connectors(
                    endpoint_id, project_id, provider, secret_ref, normalizer_version,
                    created_at_unix_nano
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    provider,
                    secret_ref,
                    selected_normalizer,
                    time.time_ns(),
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

    def claim_delivery(
        self,
        connector: ConnectorRecord,
        *,
        delivery_key_hmac: str,
        body_sha256: str,
        event_type: str,
        now_unix_nano: int,
        lease_nano: int = 30_000_000_000,
    ) -> DeliveryClaim:
        with self._mutation(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored_connector_row = connection.execute(
                "SELECT * FROM connectors WHERE endpoint_id = ?",
                (connector.endpoint_id,),
            ).fetchone()
            if (
                stored_connector_row is None
                or _connector_record(stored_connector_row) != connector
                or not connector.enabled
            ):
                raise StorageError("delivery connector does not match stored configuration")
            row = connection.execute(
                """
                SELECT * FROM delivery_receipts
                WHERE endpoint_id = ? AND delivery_key_hmac = ?
                """,
                (connector.endpoint_id, delivery_key_hmac),
            ).fetchone()
            if row is not None:
                if row["body_sha256"] != body_sha256:
                    raise DeliveryReceiptConflictError("delivery key was reused with new content")
                if row["state"] in {"applied", "ignored"}:
                    return DeliveryClaim(
                        receipt_id=row["receipt_id"],
                        disposition="replayed",
                        bundle_id=row["bundle_id"],
                        canonical_sha256=row["canonical_sha256"],
                        lease_token=None,
                    )
                lease_until = row["lease_until_unix_nano"]
                if lease_until is not None and lease_until > now_unix_nano:
                    remaining_nano = lease_until - now_unix_nano
                    raise DeliveryInProgressError(
                        "delivery is already being processed",
                        retry_after_seconds=max(
                            1,
                            (remaining_nano + 999_999_999) // 1_000_000_000,
                        ),
                    )
                next_attempt = int(row["attempt_count"]) + 1
                connection.execute(
                    """
                    UPDATE delivery_receipts
                    SET state = 'processing', lease_until_unix_nano = ?,
                        attempt_count = attempt_count + 1,
                        failure_code = NULL
                    WHERE receipt_id = ?
                    """,
                    (now_unix_nano + lease_nano, row["receipt_id"]),
                )
                connection.commit()
                return DeliveryClaim(row["receipt_id"], "claimed", None, None, next_attempt)

            receipt_id = f"receipt_{secrets.token_urlsafe(18)}"
            connection.execute(
                """
                INSERT INTO delivery_receipts(
                    receipt_id, project_id, endpoint_id, delivery_key_hmac,
                    body_sha256, event_type, state, first_received_at_unix_nano,
                    lease_until_unix_nano, attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, 1)
                """,
                (
                    receipt_id,
                    connector.project_id,
                    connector.endpoint_id,
                    delivery_key_hmac,
                    body_sha256,
                    event_type,
                    now_unix_nano,
                    now_unix_nano + lease_nano,
                ),
            )
            connection.commit()
            return DeliveryClaim(receipt_id, "claimed", None, None, 1)

    def complete_delivery(
        self,
        receipt_id: str,
        *,
        state: str,
        completed_at_unix_nano: int,
        lease_token: int,
        bundle_id: str | None = None,
        canonical_sha256: str | None = None,
    ) -> None:
        if state not in {"applied", "ignored"}:
            raise ValueError("delivery completion state must be applied or ignored")
        if state == "applied" and (bundle_id is None or canonical_sha256 is None):
            raise ValueError("applied delivery requires a canonical incident binding")
        if state == "ignored" and (bundle_id is not None or canonical_sha256 is not None):
            raise ValueError("ignored delivery cannot bind a canonical incident")
        with self._mutation(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if state == "applied":
                binding = connection.execute(
                    """
                    SELECT receipt.project_id AS receipt_project_id,
                           incident.project_id AS incident_project_id,
                           incident.object_digest AS object_digest
                    FROM delivery_receipts AS receipt
                    JOIN incidents AS incident ON incident.bundle_id = ?
                    WHERE receipt.receipt_id = ?
                    """,
                    (bundle_id, receipt_id),
                ).fetchone()
                if (
                    binding is None
                    or binding["receipt_project_id"] != binding["incident_project_id"]
                    or binding["object_digest"] != canonical_sha256
                ):
                    raise StorageError(
                        "delivery receipt cannot bind an incident from another project"
                    )
            result = connection.execute(
                """
                UPDATE delivery_receipts
                SET state = ?, completed_at_unix_nano = ?, lease_until_unix_nano = NULL,
                    bundle_id = ?, canonical_sha256 = ?, failure_code = NULL
                WHERE receipt_id = ? AND state = 'processing' AND attempt_count = ?
                """,
                (
                    state,
                    completed_at_unix_nano,
                    bundle_id,
                    canonical_sha256,
                    receipt_id,
                    lease_token,
                ),
            )
            if result.rowcount != 1:
                raise StorageError("delivery receipt is not processing")
            connection.commit()

    def fail_delivery(
        self,
        receipt_id: str,
        *,
        lease_token: int,
        failure_code: str,
    ) -> None:
        with self._mutation(), self._connect() as connection:
            result = connection.execute(
                """
                UPDATE delivery_receipts
                SET state = 'failed', lease_until_unix_nano = NULL, failure_code = ?
                WHERE receipt_id = ? AND state = 'processing' AND attempt_count = ?
                """,
                (failure_code, receipt_id, lease_token),
            )
            if result.rowcount != 1:
                raise StorageError("delivery receipt lease is no longer owned")
            connection.commit()

    def record_external_identity(
        self,
        connector: ConnectorRecord,
        *,
        key_kind: str,
        value_hmac: str,
        bundle_id: str,
        sensitivity: str = "identity",
    ) -> None:
        with self._mutation(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored_connector = connection.execute(
                """
                SELECT project_id, provider FROM connectors WHERE endpoint_id = ?
                """,
                (connector.endpoint_id,),
            ).fetchone()
            incident = connection.execute(
                "SELECT project_id FROM incidents WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
            if (
                stored_connector is None
                or incident is None
                or stored_connector["project_id"] != connector.project_id
                or stored_connector["provider"] != connector.provider
                or incident["project_id"] != connector.project_id
            ):
                raise StorageError("external identity cannot cross connector and incident projects")
            connection.execute(
                """
                INSERT OR IGNORE INTO external_identities(
                    project_id, endpoint_id, provider, key_kind, value_hmac,
                    bundle_id, sensitivity, created_at_unix_nano
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connector.project_id,
                    connector.endpoint_id,
                    connector.provider,
                    key_kind,
                    value_hmac,
                    bundle_id,
                    sensitivity,
                    time.time_ns(),
                ),
            )
            connection.commit()

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
        IncidentStore._replace_turn_projection(connection, bundle)

    @staticmethod
    def _replace_turn_projection(
        connection: sqlite3.Connection,
        bundle: IncidentBundle,
    ) -> None:
        """Replace the fleet read model from the same canonical Incident."""

        from .analysis import _ClockAligner, analyze_incident, comparable_delta
        from .codec import analysis_input_sha256

        bundle_id = bundle.profile.manifest.bundle_id
        incident_row = connection.execute(
            "SELECT project_id FROM incidents WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if incident_row is None:
            raise StorageError("turn projection requires a cataloged incident")
        project_id = str(incident_row["project_id"])
        connection.execute("DELETE FROM turn_metrics WHERE bundle_id = ?", (bundle_id,))

        analysis = analyze_incident(
            bundle,
            input_sha256=analysis_input_sha256(bundle),
            generated_at_unix_nano="0",
        )
        aligner = _ClockAligner(bundle.profile.clock_relations)
        operation_by_id = {
            operation.operation_id: operation for operation in bundle.profile.operations
        }
        event_by_id = {event.event_id: event for event in bundle.profile.events}
        framework = (
            bundle.profile.manifest.adapters[0].framework
            if bundle.profile.manifest.adapters
            else None
        )
        coverage_by_signal = {item.signal: item for item in bundle.profile.coverage}

        def metric_values(metric: Any) -> tuple[object, ...]:
            value_ms: float | None = None
            if metric.availability == "available":
                if metric.unit == "ns":
                    value_ms = float(metric.value) / 1_000_000
                elif metric.unit == "ms":
                    value_ms = float(metric.value)
                elif metric.unit == "s":
                    value_ms = float(metric.value) * 1_000
                else:
                    raise StorageError("analysis metric uses an unsupported latency unit")
            return (
                value_ms,
                metric.availability,
                metric.basis,
                metric.confidence,
                metric.limitation,
            )

        def provider_metric_values(
            metric: Any,
            *,
            basis: str,
            limitation: str,
        ) -> tuple[object, ...]:
            value, availability, _, confidence, existing_limitation = metric_values(metric)
            return (
                value,
                availability,
                basis,
                confidence,
                existing_limitation or limitation,
            )

        def absent_metric(
            signal: str,
            *,
            basis: str,
            limitation: str,
        ) -> tuple[object, ...]:
            coverage = coverage_by_signal.get(signal)
            availability = (
                coverage.availability
                if coverage is not None and coverage.availability != "available"
                else "not_observed"
            )
            reason = coverage.reason if coverage is not None else None
            return (
                None,
                availability,
                basis,
                "unavailable",
                reason or limitation,
            )

        def delta_metric(
            start: Any,
            end: Any,
            *,
            basis: str,
            evidence: tuple[Any, ...],
            limitation: str | None = None,
        ) -> tuple[object, ...]:
            delta = comparable_delta(start, end, aligner)
            if delta.availability != "available" or delta.nanoseconds is None:
                return (
                    None,
                    delta.availability,
                    basis,
                    "unavailable",
                    delta.limitation or limitation,
                )
            confidence = delta.confidence
            evidence_confidences = {item.confidence for item in evidence if item is not None}
            if "inferred" in evidence_confidences:
                confidence = "inferred"
            elif "estimated" in evidence_confidences:
                confidence = "estimated"
            return (
                delta.nanoseconds / 1_000_000,
                "available",
                basis,
                confidence,
                limitation,
            )

        def event_delta_metric(
            events: list[Any],
            start_name: str,
            end_name: str,
            *,
            basis: str,
        ) -> tuple[object, ...] | None:
            starts = [item for item in events if item.event_name == start_name]
            ends = [item for item in events if item.event_name == end_name]
            if not starts or not ends:
                return None
            if len(starts) != 1 or len(ends) != 1:
                return (
                    None,
                    "unavailable",
                    basis,
                    "unavailable",
                    "ambiguous_event_boundaries",
                )
            start = starts[0]
            end = ends[0]
            return delta_metric(
                start.time,
                end.time,
                basis=basis,
                evidence=(start.evidence, end.evidence),
            )

        rows: list[tuple[object, ...]] = []
        for turn_index, turn in enumerate(analysis.projections.turns):
            operations = [
                operation_by_id[operation_id]
                for operation_id in turn.operation_ids
                if operation_id in operation_by_id
            ]
            events = [
                event_by_id[event_id] for event_id in turn.event_ids if event_id in event_by_id
            ]
            wall_times = [
                value
                for value in (
                    *(operation.started_at.source_time_unix_nano for operation in operations),
                    *(event.time.source_time_unix_nano for event in events),
                )
                if value is not None
            ]
            started_at = min(wall_times, key=int) if wall_times else None
            # Fleet dimensions describe the response-generating model stage. A
            # transcriber, endpointing model, or synthesizer may use a different
            # provider, so accepting the first gen_ai.* attribute would silently
            # mislabel multi-provider turns. Prefer the governed LLM operation;
            # native speech-to-speech runtimes can identify their unified agent
            # stage instead. Other stage-specific providers remain available in
            # the canonical operation graph and provider measurements.
            dimension_operations = [
                operation for operation in operations if operation.operation_name == "llm"
            ]
            if not dimension_operations:
                dimension_operations = [
                    operation
                    for operation in operations
                    if operation.operation_name in {"agent", "agent_response"}
                ]
            dimension_pairs: set[tuple[str | None, str | None]] = set()
            for operation in dimension_operations:
                provider_value = operation.attributes.get(
                    "gen_ai.provider.name",
                    operation.resource.get("gen_ai.provider.name"),
                )
                model_value = operation.attributes.get(
                    "gen_ai.request.model",
                    operation.resource.get("gen_ai.request.model"),
                )
                provider_value = provider_value if isinstance(provider_value, str) else None
                model_value = model_value if isinstance(model_value, str) else None
                if provider_value is not None or model_value is not None:
                    dimension_pairs.add((provider_value, model_value))
            if len(dimension_pairs) == 1:
                provider, model = next(iter(dimension_pairs))
            else:
                provider = None
                model = None
            language_values = {
                value
                for operation in operations
                if operation.operation_name == "stt"
                for value in (
                    operation.attributes.get(
                        "earshot.language.code",
                        operation.resource.get("earshot.language.code"),
                    ),
                )
                if isinstance(value, str)
            }
            language = next(iter(language_values)) if len(language_values) == 1 else None
            status = (
                "failed"
                if any(
                    operation.status in {"error", "failed", "timeout"} for operation in operations
                )
                else "completed"
            )
            metrics = turn.metrics
            provider_measurements = json.dumps(
                {
                    name: metric.model_dump(mode="json", exclude_none=True)
                    for name, metric in sorted(metrics.provider_measurements.items())
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            stt_finalization = event_delta_metric(
                events,
                "earshot.speech.ended",
                "earshot.transcript.final",
                basis="speech_end_to_transcript_final",
            )
            if stt_finalization is None:
                provider_stt = metrics.provider_measurements.get("earshot.stt.finalization_latency")
                provider_basis = "provider_finalization_latency"
                provider_limitation = "final_transcript_boundary_not_observed"
                if provider_stt is None:
                    provider_stt = metrics.provider_measurements.get("lk.eou.transcription_delay")
                    provider_basis = "provider_transcription_delay"
                    provider_limitation = "provider_defined_transcription_boundary"
                stt_finalization = (
                    provider_metric_values(
                        provider_stt,
                        basis=provider_basis,
                        limitation=provider_limitation,
                    )
                    if provider_stt is not None and provider_stt.availability == "available"
                    else absent_metric(
                        "stt.final",
                        basis="speech_end_to_transcript_final",
                        limitation="final_transcript_boundary_not_observed",
                    )
                )

            eou = event_delta_metric(
                events,
                "earshot.speech.ended",
                "earshot.turn.committed",
                basis="speech_end_to_turn_commit",
            )
            if eou is None:
                provider_eou = metrics.provider_measurements.get("lk.eou.endpointing_delay")
                eou = (
                    provider_metric_values(
                        provider_eou,
                        basis="provider_endpointing_delay",
                        limitation="provider_defined_endpointing_boundary",
                    )
                    if provider_eou is not None and provider_eou.availability == "available"
                    else absent_metric(
                        "turn.end",
                        basis="speech_end_to_turn_commit",
                        limitation="end_of_turn_boundary_not_observed",
                    )
                )

            lifecycle_operations = [
                operation
                for operation in operations
                if operation.operation_name == "framework_operation"
                and operation.attributes.get("earshot.framework.operation.name") == "turn"
            ]
            if len(lifecycle_operations) == 1 and lifecycle_operations[0].ended_at is not None:
                lifecycle = lifecycle_operations[0]
                turn_duration = delta_metric(
                    lifecycle.started_at,
                    lifecycle.ended_at,
                    basis="native_turn_lifecycle",
                    evidence=(lifecycle.evidence,),
                )
            else:
                turn_duration = absent_metric(
                    "turn.lifecycle",
                    basis="native_turn_lifecycle",
                    limitation=(
                        "multiple_turn_lifecycle_intervals"
                        if len(lifecycle_operations) > 1
                        else "turn_lifecycle_interval_not_observed"
                    ),
                )

            interruption_events = [
                event for event in events if event.event_name.startswith("earshot.interruption.")
            ]
            accepted_interruptions = [
                event
                for event in interruption_events
                if event.event_name == "earshot.interruption.accepted"
            ]
            ignored_interruptions = [
                event
                for event in interruption_events
                if event.event_name == "earshot.interruption.ignored"
            ]
            terminal_interruptions = accepted_interruptions + ignored_interruptions
            if terminal_interruptions:
                interruption_evidence = {
                    event.evidence.confidence
                    for event in terminal_interruptions
                    if event.evidence is not None
                }
                interruption_confidence = (
                    "inferred"
                    if "inferred" in interruption_evidence
                    else "estimated"
                    if "estimated" in interruption_evidence
                    else "measured"
                )
                interruption = (
                    len(accepted_interruptions),
                    "available",
                    "accepted_interruption_events",
                    interruption_confidence,
                    None,
                )
            elif interruption_events:
                interruption = (
                    None,
                    "unavailable",
                    "accepted_interruption_events",
                    "unavailable",
                    "interruption_outcome_not_observed",
                )
            else:
                interruption_coverage = coverage_by_signal.get(
                    "interruption.per_turn"
                ) or coverage_by_signal.get("interruption")
                unassigned_interruption = any(
                    event.event_name.startswith("earshot.interruption.") and event.turn_id is None
                    for event in bundle.profile.events
                )
                if (
                    interruption_coverage is not None
                    and interruption_coverage.availability == "available"
                ):
                    interruption = (
                        0,
                        "available",
                        "accepted_interruption_events",
                        "measured",
                        None,
                    )
                elif interruption_coverage is not None:
                    interruption = (
                        None,
                        interruption_coverage.availability,
                        "accepted_interruption_events",
                        "unavailable",
                        interruption_coverage.reason or "interruption_signal_unavailable",
                    )
                elif unassigned_interruption:
                    interruption = (
                        None,
                        "unavailable",
                        "accepted_interruption_events",
                        "unavailable",
                        "turn_correlation_not_observed",
                    )
                else:
                    interruption = (
                        None,
                        "not_observed",
                        "accepted_interruption_events",
                        "unavailable",
                        "interruption_signal_not_observed",
                    )
            rows.append(
                (
                    project_id,
                    bundle_id,
                    bundle.profile.session.session_id,
                    turn.turn_id,
                    turn_index,
                    started_at,
                    framework,
                    provider,
                    model,
                    language,
                    status,
                    *stt_finalization,
                    *eou,
                    *metric_values(metrics.first_token_latency),
                    *metric_values(metrics.generated_response_latency),
                    *metric_values(metrics.sent_response_latency),
                    *metric_values(metrics.received_response_latency),
                    *metric_values(metrics.render_start_response_latency),
                    *metric_values(metrics.response_latency),
                    *turn_duration,
                    metrics.tools.operation_count,
                    metrics.tools.total_work_ms,
                    *interruption,
                    provider_measurements,
                    TURN_FACT_PROJECTION_VERSION,
                    bundle.profile.manifest.schema_version,
                )
            )

        columns = (
            "project_id",
            "bundle_id",
            "session_id",
            "turn_id",
            "turn_index",
            "started_at_unix_nano",
            "framework",
            "provider",
            "model",
            "language",
            "status",
            "stt_finalization_ms",
            "stt_finalization_availability",
            "stt_finalization_basis",
            "stt_finalization_confidence",
            "stt_finalization_limitation",
            "eou_ms",
            "eou_availability",
            "eou_basis",
            "eou_confidence",
            "eou_limitation",
            "first_token_ms",
            "first_token_availability",
            "first_token_basis",
            "first_token_confidence",
            "first_token_limitation",
            "generated_response_ms",
            "generated_response_availability",
            "generated_response_basis",
            "generated_response_confidence",
            "generated_response_limitation",
            "sent_response_ms",
            "sent_response_availability",
            "sent_response_basis",
            "sent_response_confidence",
            "sent_response_limitation",
            "received_response_ms",
            "received_response_availability",
            "received_response_basis",
            "received_response_confidence",
            "received_response_limitation",
            "render_start_response_ms",
            "render_start_response_availability",
            "render_start_response_basis",
            "render_start_response_confidence",
            "render_start_response_limitation",
            "response_ms",
            "response_availability",
            "response_basis",
            "response_confidence",
            "response_limitation",
            "turn_duration_ms",
            "turn_duration_availability",
            "turn_duration_basis",
            "turn_duration_confidence",
            "turn_duration_limitation",
            "tool_operation_count",
            "tool_total_work_ms",
            "interruption_count",
            "interruption_availability",
            "interruption_basis",
            "interruption_confidence",
            "interruption_limitation",
            "provider_measurements_json",
            "projection_version",
            "contract_version",
        )
        placeholders = ", ".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO turn_metrics ({', '.join(columns)}) VALUES ({placeholders})",
            rows,
        )

    def ingest(
        self,
        bundle: IncidentBundle,
        canonical_payload: bytes | None = None,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
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
                    if (
                        connection.execute(
                            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                        ).fetchone()
                        is None
                    ):
                        raise ValueError("project does not exist")
                    tombstone = connection.execute(
                        "SELECT project_id FROM tombstones WHERE bundle_id_sha256 = ?",
                        (_tombstone_key(manifest.bundle_id),),
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
                        "SELECT * FROM incidents WHERE bundle_id = ?", (manifest.bundle_id,)
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
                            bundle_id, project_id, session_id, schema_version, object_digest,
                            size_bytes, status, finality, completeness, framework,
                            created_at_unix_nano, ingested_at_unix_nano,
                            expires_at_unix_nano, export_allowed_local_api,
                            export_allowed_local_cli
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            manifest.bundle_id,
                            project_id,
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

    def get_record(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> IncidentRecord:
        with self._mutation():
            with self._connect() as connection:
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
            if row is not None:
                expiry = row["expires_at_unix_nano"]
                if expiry is not None and _decimal_lte(expiry, str(time.time_ns())):
                    self._purge_existing_locked((bundle_id,), time.time_ns())
                    raise IncidentPurgedError("incident was purged")
                return _record(row)
            if tombstoned:
                raise IncidentPurgedError("incident was purged")
            raise IncidentNotFoundError("incident not found")

    def get_artifact(
        self,
        bundle_id: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> tuple[IncidentRecord, bytes]:
        # Keep the index lookup and object read coherent with concurrent purge.
        with self._mutation():
            record = self.get_record(bundle_id, project_id=project_id)
            return record, self.objects.get(record.digest)

    def list_incidents(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        destination: str | None = None,
    ) -> IncidentPage:
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

    def list_turn_facts(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        session_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TurnFact, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        query = "SELECT * FROM turn_metrics WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        query += " ORDER BY started_at_unix_nano, bundle_id, turn_index LIMIT ?"
        parameters.append(limit)
        with self._mutation():
            self._purge_all_expired_locked(str(time.time_ns()))
            with self._connect() as connection:
                rows = connection.execute(query, parameters).fetchall()
        return tuple(_turn_fact(row) for row in rows)

    def summarize_turn_metric(
        self,
        metric: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        group_by: str = "framework",
    ) -> tuple[TurnMetricSummary, ...]:
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
        query = f"""
            WITH totals AS (
                SELECT COALESCE({group_column}, 'unknown') AS group_value,
                       {availability_column} AS availability_value,
                       {basis_column} AS basis_value,
                       {confidence_column} AS confidence_value,
                       COALESCE({limitation_column}, '') AS limitation_value,
                       COUNT(*) AS turn_count
                FROM turn_metrics
                WHERE project_id = ?
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
                FROM turn_metrics
                WHERE project_id = ? AND {metric_column} IS NOT NULL
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
        with self._mutation():
            self._purge_all_expired_locked(str(time.time_ns()))
            with self._connect() as connection:
                rows = connection.execute(query, (project_id, project_id)).fetchall()
        return tuple(
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
        )

    def get_analysis(
        self,
        bundle_id: str,
        analyzer_version: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> StoredAnalysis | None:
        with self._mutation():
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
        with self._mutation():
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
            stored = self.get_analysis(bundle_id, analyzer_version, project_id=project_id)
            assert stored is not None
            return stored

    def purge(self, bundle_id: str, *, project_id: str = DEFAULT_PROJECT_ID) -> None:
        """Best-effort physically remove evidence and leave a minimal tombstone."""

        with self._mutation():
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM incidents WHERE bundle_id = ? AND project_id = ?",
                    (bundle_id, project_id),
                ).fetchone()
                if row is None:
                    if connection.execute(
                        """
                        SELECT 1 FROM tombstones
                        WHERE bundle_id_sha256 = ? AND project_id = ?
                        """,
                        (_tombstone_key(bundle_id), project_id),
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
                        "SELECT bundle_id, project_id, object_digest FROM incidents "
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
                INSERT INTO tombstones(
                    bundle_id_sha256, project_id, purged_at_unix_nano
                )
                VALUES (?, ?, ?)
                ON CONFLICT(bundle_id_sha256) DO NOTHING
                """,
                (
                    (
                        _tombstone_key(row["bundle_id"]),
                        row["project_id"],
                        purged_at_unix_nano,
                    )
                    for row in rows
                ),
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
