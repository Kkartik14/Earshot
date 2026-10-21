"""FROZEN: the 0.1.x schema converge routine (catalog schema v1 to v19).

Do not edit this module. It is the code every 0.1.x release ran to create or
upgrade a catalog, moved here verbatim from ``storage.py`` (main 3c2f0b3,
``IncidentStore._initialize`` and the module helpers it used). Opening a catalog
that has no ``alembic_version`` and a ``user_version`` from 1 to 19 runs it once;
``storage_migrations`` then stamps the Alembic baseline ``0001_v19_baseline``.

Schema changes after v19 are Alembic revisions in ``storage_migrations``, never
edits here. The only changes made while moving the code: the routine is a function
of the connection it receives (instead of a method that opened one under the store
lock), and ``_mark_scrub_required`` is a module function.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .storage import StorageError

_SCHEMA_VERSION = 19

_SCHEMA = """

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at_unix_nano INTEGER NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'deleting', 'deleted')),
    delete_started_at_unix_nano INTEGER,
    deleted_at_unix_nano INTEGER
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

CREATE TABLE IF NOT EXISTS incident_ingest_order (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id TEXT NOT NULL UNIQUE REFERENCES incidents(bundle_id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS incidents_track_ingest_order
AFTER INSERT ON incidents
WHEN NOT EXISTS (
    SELECT 1 FROM incident_ingest_order WHERE bundle_id = NEW.bundle_id
)
BEGIN
    INSERT INTO incident_ingest_order(bundle_id) VALUES (NEW.bundle_id);
END;

CREATE UNIQUE INDEX IF NOT EXISTS incidents_project_bundle_idx
    ON incidents(project_id, bundle_id);

-- Mutable cross-product relationships are catalog metadata, not part of the
-- immutable incident artifact or its digest-bound analysis.
CREATE TABLE IF NOT EXISTS incident_external_references (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    bundle_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    record_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    linked_at_unix_nano INTEGER NOT NULL,
    PRIMARY KEY (project_id, bundle_id, namespace, record_type),
    FOREIGN KEY (project_id, bundle_id)
        REFERENCES incidents(project_id, bundle_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS incident_external_reference_lookup_idx
    ON incident_external_references(project_id, namespace, record_type, external_id);

CREATE INDEX IF NOT EXISTS incidents_session_idx
    ON incidents(project_id, session_id, ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_project_idx
    ON incidents(project_id, ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_digest_idx ON incidents(object_digest);
CREATE INDEX IF NOT EXISTS incidents_expiry_idx
    ON incidents(length(expires_at_unix_nano), expires_at_unix_nano)
    WHERE expires_at_unix_nano IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_export_local_api_idx
    ON incidents(project_id, export_allowed_local_api,
                 ingested_at_unix_nano DESC, bundle_id DESC);
CREATE INDEX IF NOT EXISTS incidents_export_local_cli_idx
    ON incidents(project_id, export_allowed_local_cli,
                 ingested_at_unix_nano DESC, bundle_id DESC);

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

CREATE TABLE IF NOT EXISTS pending_object_deletions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    object_digest TEXT NOT NULL CHECK (length(object_digest) = 64),
    queued_at_unix_nano INTEGER NOT NULL,
    PRIMARY KEY (project_id, object_digest)
);
CREATE INDEX IF NOT EXISTS pending_object_deletions_project_idx
    ON pending_object_deletions(project_id, queued_at_unix_nano, object_digest);

CREATE TABLE IF NOT EXISTS pending_ingests (
    intent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    object_digest TEXT NOT NULL CHECK (length(object_digest) = 64),
    started_at_unix_nano INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_ingests_project_idx
    ON pending_ingests(project_id, started_at_unix_nano, intent_id);
CREATE INDEX IF NOT EXISTS pending_ingests_digest_idx
    ON pending_ingests(object_digest);

CREATE TABLE IF NOT EXISTS storage_maintenance (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    purge_generation INTEGER NOT NULL DEFAULT 0 CHECK (purge_generation >= 0),
    scrubbed_generation INTEGER NOT NULL DEFAULT 0 CHECK (scrubbed_generation >= 0)
);
INSERT OR IGNORE INTO storage_maintenance(singleton) VALUES (1);

CREATE TABLE IF NOT EXISTS legacy_deletion_reviews (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    detected_at_unix_nano INTEGER NOT NULL
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


def _tombstone_key(bundle_id: str) -> str:
    return hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()


def _mark_scrub_required(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE storage_maintenance SET purge_generation = purge_generation + 1 WHERE singleton = 1"
    )


def converge_legacy_catalog(connection: sqlite3.Connection) -> None:
    """Converge a catalog to schema v19 and commit; refuse a newer schema.

    ``connection`` is a catalog connection (``sqlite3.Row`` rows, foreign keys ON,
    the catalog pragmas). The caller holds the store lock.
    """

    with connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise StorageError("incident database schema is newer than this binary")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        if version < 19:
            # Listing scans in result order and filters expiry as a residual
            # predicate. The expiry-first indexes forced a sort across live
            # rows and added write work without a remaining query owner.
            connection.execute("DROP INDEX IF EXISTS incidents_project_expiry_idx")
            connection.execute("DROP INDEX IF EXISTS incidents_project_no_expiry_idx")
        # Project ownership is needed by later migrations (notably scoped
        # tombstones), so establish the authorization root before inspecting
        # or rebuilding any legacy table that now references it.
        connection.execute(
            """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at_unix_nano INTEGER NOT NULL,
                    lifecycle_state TEXT NOT NULL DEFAULT 'active'
                        CHECK (lifecycle_state IN ('active', 'deleting', 'deleted')),
                    delete_started_at_unix_nano INTEGER,
                    deleted_at_unix_nano INTEGER
                )
                """
        )
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
        for column, declaration in {
            "lifecycle_state": (
                "TEXT NOT NULL DEFAULT 'active' "
                "CHECK (lifecycle_state IN ('active', 'deleting', 'deleted'))"
            ),
            "delete_started_at_unix_nano": "INTEGER",
            "deleted_at_unix_nano": "INTEGER",
        }.items():
            if column not in project_columns:
                connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {declaration}")
        legacy_deleting_projects = (
            connection.execute(
                "SELECT project_id FROM projects WHERE lifecycle_state = 'deleting'"
            ).fetchall()
            if version < 13
            else ()
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
                    connection.execute(f"ALTER TABLE incidents ADD COLUMN {column} {declaration}")
            session_index = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'incidents_session_idx'"
            ).fetchone()
            if session_index is not None and "project_id" not in str(session_index["sql"]):
                connection.execute("DROP INDEX incidents_session_idx")
            for index_name in (
                "incidents_export_local_api_idx",
                "incidents_export_local_cli_idx",
            ):
                destination_index = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (index_name,),
                ).fetchone()
                if destination_index is not None and "project_id" not in str(
                    destination_index["sql"]
                ):
                    connection.execute(f"DROP INDEX {index_name}")
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
        if version < 15 and connection.execute("SELECT 1 FROM tombstones LIMIT 1").fetchone():
            # Catalogs created before the scrub generation cannot distinguish
            # completed purges from interrupted physical cleanup.
            _mark_scrub_required(connection)
        if legacy_deleting_projects:
            connection.executemany(
                """
                    INSERT INTO legacy_deletion_reviews(project_id, detected_at_unix_nano)
                    VALUES (?, ?)
                    ON CONFLICT(project_id) DO NOTHING
                    """,
                ((row["project_id"], time.time_ns()) for row in legacy_deleting_projects),
            )
        if version < 17:
            # Backfill the arrival sequence once so startup sweeps can exclude
            # concurrent rows without counting or materializing the backlog.
            connection.execute(
                """
                    INSERT INTO incident_ingest_order(bundle_id)
                    SELECT incidents.bundle_id
                    FROM incidents
                    LEFT JOIN incident_ingest_order AS ingest_order
                        ON ingest_order.bundle_id = incidents.bundle_id
                    WHERE ingest_order.bundle_id IS NULL
                    ORDER BY incidents.rowid
                    """
            )
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()
