"""The catalog schema as SQLAlchemy ``MetaData``: one source for every dialect.

``build_v19_baseline_metadata()`` is the schema Alembic revision ``0001_v19_baseline``
creates. It is today's schema-v19 catalog (the 0.1.x ``_SCHEMA``, frozen in
``storage_legacy``), with two differences:

- the ``incidents_track_ingest_order`` trigger is gone; ``IncidentStore.ingest``
  inserts the arrival-order row itself, in the same transaction;
- ``earshot_meta`` is new: small key/value facts about the catalog itself, such as
  ``legacy_bootstrap`` on a catalog converged from 0.1.x.

Type rules, so one definition renders correctly on SQLite and Postgres:

- integer nanosecond columns are ``BIGINT`` on Postgres and stay ``INTEGER`` on
  SQLite;
- decimal-text nanosecond columns stay ``TEXT``, with ``COLLATE "C"`` on Postgres
  only, so their length-then-text comparisons order like numbers;
- ``REAL`` is double precision (``FLOAT(53)``) on Postgres and stays ``REAL`` on
  SQLite;
- a single-column primary key that 0.1.x declared without ``NOT NULL`` keeps that
  declaration on SQLite (Postgres primary keys are always ``NOT NULL``).

The baseline is frozen. A later revision changes the schema with its own
operations; it never edits this function.

``capture_preset_column()`` and ``add_calls_tables()`` are what revision
``0004_calls`` adds, ``projection_columns()`` and ``add_projection_tables()``
what ``0005_projections`` adds, and ``connector_columns()`` and
``add_connector_tables()`` what ``0006_connectors`` adds, frozen the same way.
"""

from __future__ import annotations

import functools

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.types import TypeEngine

# An INTEGER nanosecond timestamp: int64 on every dialect.
UNIX_NANO = BigInteger().with_variant(Integer(), "sqlite")
# A decimal-text nanosecond value (it may exceed int64), compared length first.
DECIMAL_NANO = Text().with_variant(Text(collation="C"), "postgresql")
# An 8-byte float.
DOUBLE = Float(precision=53).with_variant(sqlite.REAL(), "sqlite")
BLOB = LargeBinary()


def _text_key(name: str) -> Column[str]:
    """A ``TEXT PRIMARY KEY`` column as 0.1.x declared it (nullable on SQLite)."""

    return Column(name, Text, primary_key=True, nullable=True)


def _required(name: str, type_: TypeEngine[object] | type[TypeEngine[object]], *args, **kwargs):
    return Column(name, type_, *args, nullable=False, **kwargs)


def _optional(name: str, type_: TypeEngine[object] | type[TypeEngine[object]], *args, **kwargs):
    return Column(name, type_, *args, nullable=True, **kwargs)


def _turn_metric_columns(metric: str) -> list[Column[object]]:
    return [
        _optional(f"{metric}_ms", DOUBLE),
        _required(f"{metric}_availability", Text),
        _required(f"{metric}_basis", Text),
        _required(f"{metric}_confidence", Text),
        _optional(f"{metric}_limitation", Text),
    ]


def build_v19_baseline_metadata() -> MetaData:
    """Return a new ``MetaData`` holding the frozen ``0001_v19_baseline`` schema."""

    metadata = MetaData()

    Table(
        "projects",
        metadata,
        _text_key("project_id"),
        _required("display_name", Text),
        _required("created_at_unix_nano", UNIX_NANO),
        _required(
            "lifecycle_state",
            Text,
            CheckConstraint("lifecycle_state IN ('active', 'deleting', 'deleted')"),
            server_default=text("'active'"),
        ),
        _optional("delete_started_at_unix_nano", UNIX_NANO),
        _optional("deleted_at_unix_nano", UNIX_NANO),
    )

    api_keys = Table(
        "api_keys",
        metadata,
        _text_key("key_id"),
        _required(
            "project_id",
            Text,
            ForeignKey("projects.project_id", ondelete="CASCADE"),
        ),
        _required("label", Text),
        _required("secret_salt", BLOB),
        _required("secret_hash", BLOB),
        _required("created_at_unix_nano", UNIX_NANO),
        _optional("last_used_at_unix_nano", UNIX_NANO),
        _optional("revoked_at_unix_nano", UNIX_NANO),
    )
    Index(
        "api_keys_project_idx",
        api_keys.c.project_id,
        api_keys.c.revoked_at_unix_nano,
        api_keys.c.created_at_unix_nano.desc(),
    )

    incidents = Table(
        "incidents",
        metadata,
        _text_key("bundle_id"),
        _required(
            "project_id",
            Text,
            ForeignKey("projects.project_id"),
            server_default=text("'default'"),
        ),
        _required("session_id", Text),
        _required("schema_version", Text),
        _required("object_digest", Text),
        _required("size_bytes", Integer, CheckConstraint("size_bytes >= 0")),
        _required("status", Text),
        _required("finality", Text),
        _required("completeness", Text),
        _optional("framework", Text),
        _required("created_at_unix_nano", DECIMAL_NANO),
        _required("ingested_at_unix_nano", UNIX_NANO),
        _optional("expires_at_unix_nano", DECIMAL_NANO),
        _required(
            "export_allowed_local_api",
            Integer,
            CheckConstraint("export_allowed_local_api IN (0, 1)"),
            server_default=text("1"),
        ),
        _required(
            "export_allowed_local_cli",
            Integer,
            CheckConstraint("export_allowed_local_cli IN (0, 1)"),
            server_default=text("1"),
        ),
    )
    Index(
        "incidents_project_bundle_idx", incidents.c.project_id, incidents.c.bundle_id, unique=True
    )
    Index(
        "incidents_session_idx",
        incidents.c.project_id,
        incidents.c.session_id,
        incidents.c.ingested_at_unix_nano.desc(),
        incidents.c.bundle_id.desc(),
    )
    Index(
        "incidents_project_idx",
        incidents.c.project_id,
        incidents.c.ingested_at_unix_nano.desc(),
        incidents.c.bundle_id.desc(),
    )
    Index("incidents_digest_idx", incidents.c.object_digest)
    Index(
        "incidents_expiry_idx",
        func.length(incidents.c.expires_at_unix_nano),
        incidents.c.expires_at_unix_nano,
        sqlite_where=text("expires_at_unix_nano IS NOT NULL"),
        postgresql_where=text("expires_at_unix_nano IS NOT NULL"),
    )
    for destination in ("api", "cli"):
        Index(
            f"incidents_export_local_{destination}_idx",
            incidents.c.project_id,
            incidents.c[f"export_allowed_local_{destination}"],
            incidents.c.ingested_at_unix_nano.desc(),
            incidents.c.bundle_id.desc(),
        )

    Table(
        "incident_ingest_order",
        metadata,
        Column("sequence", UNIX_NANO, primary_key=True, nullable=True, autoincrement=True),
        _required(
            "bundle_id",
            Text,
            ForeignKey("incidents.bundle_id", ondelete="CASCADE"),
            unique=True,
        ),
        sqlite_autoincrement=True,
    )

    # Mutable cross-product relationships are catalog metadata, not part of the
    # immutable incident artifact or its digest-bound analysis.
    external_references = Table(
        "incident_external_references",
        metadata,
        _required("project_id", Text, ForeignKey("projects.project_id", ondelete="CASCADE")),
        _required("bundle_id", Text),
        _required("namespace", Text),
        _required("record_type", Text),
        _required("external_id", Text),
        _required("linked_at_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "bundle_id", "namespace", "record_type"),
        ForeignKeyConstraint(
            ["project_id", "bundle_id"],
            ["incidents.project_id", "incidents.bundle_id"],
            ondelete="CASCADE",
        ),
    )
    Index(
        "incident_external_reference_lookup_idx",
        external_references.c.project_id,
        external_references.c.namespace,
        external_references.c.record_type,
        external_references.c.external_id,
    )

    Table(
        "analyses",
        metadata,
        _required("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="CASCADE")),
        _required("analyzer_version", Text),
        _required("input_digest", Text),
        _required("generated_at_unix_nano", DECIMAL_NANO),
        _required("output_json", BLOB),
        PrimaryKeyConstraint("bundle_id", "analyzer_version", "input_digest"),
    )

    Table(
        "tombstones",
        metadata,
        Column(
            "bundle_id_sha256",
            Text,
            CheckConstraint("length(bundle_id_sha256) = 64"),
            primary_key=True,
            nullable=True,
        ),
        _required("project_id", Text, ForeignKey("projects.project_id")),
        _required("purged_at_unix_nano", UNIX_NANO),
    )

    pending_object_deletions = Table(
        "pending_object_deletions",
        metadata,
        _required("project_id", Text, ForeignKey("projects.project_id")),
        _required("object_digest", Text, CheckConstraint("length(object_digest) = 64")),
        _required("queued_at_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "object_digest"),
    )
    Index(
        "pending_object_deletions_project_idx",
        pending_object_deletions.c.project_id,
        pending_object_deletions.c.queued_at_unix_nano,
        pending_object_deletions.c.object_digest,
    )

    pending_ingests = Table(
        "pending_ingests",
        metadata,
        _text_key("intent_id"),
        _required("project_id", Text, ForeignKey("projects.project_id")),
        _required("object_digest", Text, CheckConstraint("length(object_digest) = 64")),
        _required("started_at_unix_nano", UNIX_NANO),
    )
    Index(
        "pending_ingests_project_idx",
        pending_ingests.c.project_id,
        pending_ingests.c.started_at_unix_nano,
        pending_ingests.c.intent_id,
    )
    Index("pending_ingests_digest_idx", pending_ingests.c.object_digest)

    Table(
        "storage_maintenance",
        metadata,
        Column(
            "singleton",
            Integer,
            CheckConstraint("singleton = 1"),
            primary_key=True,
            nullable=True,
            autoincrement=False,
        ),
        _required(
            "purge_generation",
            Integer,
            CheckConstraint("purge_generation >= 0"),
            server_default=text("0"),
        ),
        _required(
            "scrubbed_generation",
            Integer,
            CheckConstraint("scrubbed_generation >= 0"),
            server_default=text("0"),
        ),
    )

    Table(
        "legacy_deletion_reviews",
        metadata,
        Column(
            "project_id",
            Text,
            ForeignKey("projects.project_id"),
            primary_key=True,
            nullable=True,
        ),
        _required("detected_at_unix_nano", UNIX_NANO),
    )

    operations = Table(
        "operations",
        metadata,
        _required("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="CASCADE")),
        _required("operation_id", Text),
        _required("operation_name", Text),
        _required("status", Text),
        _optional("participant_id", Text),
        _optional("stream_id", Text),
        _optional("turn_id", Text),
        _optional("trace_id", Text),
        _optional("span_id", Text),
        _optional("parent_span_id", Text),
        _required("parent_scope", Text),
        _optional("started_source_time_unix_nano", DECIMAL_NANO),
        _optional("started_observed_time_unix_nano", DECIMAL_NANO),
        _optional("started_monotonic_time_nano", DECIMAL_NANO),
        _optional("started_clock_domain_id", Text),
        _optional("ended_source_time_unix_nano", DECIMAL_NANO),
        _optional("ended_observed_time_unix_nano", DECIMAL_NANO),
        _optional("ended_monotonic_time_nano", DECIMAL_NANO),
        _optional("ended_clock_domain_id", Text),
        _optional("evidence_source", Text),
        _optional("evidence_confidence", Text),
        _optional("evidence_availability", Text),
        _required("capture_class", Text),
        PrimaryKeyConstraint("bundle_id", "operation_id"),
    )
    Index(
        "operations_otel_identity_idx",
        operations.c.bundle_id,
        operations.c.trace_id,
        operations.c.span_id,
        unique=True,
        sqlite_where=text("trace_id IS NOT NULL AND span_id IS NOT NULL"),
        postgresql_where=text("trace_id IS NOT NULL AND span_id IS NOT NULL"),
    )
    Index(
        "operations_turn_idx",
        operations.c.bundle_id,
        operations.c.turn_id,
        operations.c.operation_name,
    )
    Index(
        "operations_participant_stream_idx",
        operations.c.bundle_id,
        operations.c.participant_id,
        operations.c.stream_id,
    )
    Index(
        "operations_parent_idx",
        operations.c.bundle_id,
        operations.c.trace_id,
        operations.c.parent_span_id,
    )

    causal_links = Table(
        "causal_links",
        metadata,
        _required("bundle_id", Text),
        _required("source_operation_id", Text),
        _required("link_index", Integer, CheckConstraint("link_index >= 0")),
        _required("relationship", Text),
        _required("target_scope", Text),
        _optional("target_operation_id", Text),
        _optional("trace_id", Text),
        _optional("span_id", Text),
        PrimaryKeyConstraint("bundle_id", "source_operation_id", "link_index"),
        ForeignKeyConstraint(
            ["bundle_id", "source_operation_id"],
            ["operations.bundle_id", "operations.operation_id"],
            ondelete="CASCADE",
        ),
    )
    Index(
        "causal_links_target_idx",
        causal_links.c.bundle_id,
        causal_links.c.target_operation_id,
    )
    Index(
        "causal_links_otel_target_idx",
        causal_links.c.bundle_id,
        causal_links.c.trace_id,
        causal_links.c.span_id,
    )

    events = Table(
        "events",
        metadata,
        _required("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="CASCADE")),
        _required("event_id", Text),
        _required("event_name", Text),
        _optional("operation_id", Text),
        _optional("participant_id", Text),
        _optional("stream_id", Text),
        _optional("turn_id", Text),
        _optional("trace_id", Text),
        _optional("span_id", Text),
        _optional("source_time_unix_nano", DECIMAL_NANO),
        _optional("observed_time_unix_nano", DECIMAL_NANO),
        _optional("monotonic_time_nano", DECIMAL_NANO),
        _optional("clock_domain_id", Text),
        _optional("evidence_source", Text),
        _optional("evidence_confidence", Text),
        _optional("evidence_availability", Text),
        _required("capture_class", Text),
        PrimaryKeyConstraint("bundle_id", "event_id"),
        ForeignKeyConstraint(
            ["bundle_id", "operation_id"],
            ["operations.bundle_id", "operations.operation_id"],
            ondelete="CASCADE",
        ),
    )
    Index("events_turn_idx", events.c.bundle_id, events.c.turn_id, events.c.event_name)
    Index("events_operation_idx", events.c.bundle_id, events.c.operation_id)
    Index(
        "events_participant_stream_idx",
        events.c.bundle_id,
        events.c.participant_id,
        events.c.stream_id,
    )

    turn_metrics = Table(
        "turn_metrics",
        metadata,
        _required("project_id", Text, ForeignKey("projects.project_id")),
        _required("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="CASCADE")),
        _required("session_id", Text),
        _required("turn_id", Text),
        _required("turn_index", Integer, CheckConstraint("turn_index >= 0")),
        _optional("started_at_unix_nano", DECIMAL_NANO),
        _optional("framework", Text),
        _optional("provider", Text),
        _optional("model", Text),
        _optional("language", Text),
        _required("status", Text),
        *_turn_metric_columns("stt_finalization"),
        *_turn_metric_columns("eou"),
        *_turn_metric_columns("first_token"),
        *_turn_metric_columns("generated_response"),
        *_turn_metric_columns("sent_response"),
        *_turn_metric_columns("received_response"),
        *_turn_metric_columns("render_start_response"),
        *_turn_metric_columns("response"),
        *_turn_metric_columns("turn_duration"),
        _required(
            "tool_operation_count",
            Integer,
            CheckConstraint("tool_operation_count >= 0"),
        ),
        _required("tool_total_work_ms", DOUBLE, CheckConstraint("tool_total_work_ms >= 0")),
        _optional("interruption_count", Integer, CheckConstraint("interruption_count >= 0")),
        _required("interruption_availability", Text),
        _required("interruption_basis", Text),
        _required("interruption_confidence", Text),
        _optional("interruption_limitation", Text),
        _required("provider_measurements_json", BLOB),
        _required("projection_version", Text),
        _required("contract_version", Text),
        PrimaryKeyConstraint("bundle_id", "turn_id"),
    )
    Index(
        "turn_metrics_project_time_idx",
        turn_metrics.c.project_id,
        turn_metrics.c.started_at_unix_nano,
        turn_metrics.c.bundle_id,
        turn_metrics.c.turn_index,
    )
    Index(
        "turn_metrics_project_dimensions_idx",
        turn_metrics.c.project_id,
        turn_metrics.c.framework,
        turn_metrics.c.provider,
        turn_metrics.c.model,
        turn_metrics.c.language,
        turn_metrics.c.status,
    )

    connectors = Table(
        "connectors",
        metadata,
        _text_key("endpoint_id"),
        _required("project_id", Text, ForeignKey("projects.project_id", ondelete="CASCADE")),
        _required("provider", Text),
        _required("secret_ref", Text),
        _required(
            "enabled",
            Integer,
            CheckConstraint("enabled IN (0, 1)"),
            server_default=text("1"),
        ),
        _required("normalizer_version", Text),
        _required("created_at_unix_nano", UNIX_NANO),
    )
    Index(
        "connectors_project_idx",
        connectors.c.project_id,
        connectors.c.provider,
        connectors.c.enabled,
    )

    delivery_receipts = Table(
        "delivery_receipts",
        metadata,
        _text_key("receipt_id"),
        _required("project_id", Text, ForeignKey("projects.project_id", ondelete="CASCADE")),
        _required(
            "endpoint_id",
            Text,
            ForeignKey("connectors.endpoint_id", ondelete="CASCADE"),
        ),
        _required(
            "delivery_key_hmac",
            Text,
            CheckConstraint("length(delivery_key_hmac) = 64"),
        ),
        _required("body_sha256", Text, CheckConstraint("length(body_sha256) = 64")),
        _required("event_type", Text),
        _required("state", Text),
        _required("first_received_at_unix_nano", UNIX_NANO),
        _optional("completed_at_unix_nano", UNIX_NANO),
        _optional("lease_until_unix_nano", UNIX_NANO),
        _required("attempt_count", Integer, CheckConstraint("attempt_count >= 1")),
        _optional("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="SET NULL")),
        _optional("canonical_sha256", Text),
        _optional("failure_code", Text),
        UniqueConstraint("endpoint_id", "delivery_key_hmac"),
    )
    Index(
        "delivery_receipts_project_state_idx",
        delivery_receipts.c.project_id,
        delivery_receipts.c.state,
        delivery_receipts.c.first_received_at_unix_nano.desc(),
    )

    Table(
        "external_identities",
        metadata,
        _required("project_id", Text, ForeignKey("projects.project_id", ondelete="CASCADE")),
        _required(
            "endpoint_id",
            Text,
            ForeignKey("connectors.endpoint_id", ondelete="CASCADE"),
        ),
        _required("provider", Text),
        _required("key_kind", Text),
        _required("value_hmac", Text, CheckConstraint("length(value_hmac) = 64")),
        _required("bundle_id", Text, ForeignKey("incidents.bundle_id", ondelete="CASCADE")),
        _required("sensitivity", Text),
        _required("created_at_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "endpoint_id", "key_kind", "value_hmac", "bundle_id"),
    )

    Table(
        "earshot_meta",
        metadata,
        Column("key", Text, primary_key=True),
        _required("value", Text),
    )

    return metadata


@functools.cache
def v19_baseline_metadata() -> MetaData:
    """The shared, read-only baseline ``MetaData`` (built once per process)."""

    return build_v19_baseline_metadata()


# Rows 0.1.x seeded into every catalog; the baseline inserts them after its DDL.
V19_SEED_STATEMENTS = (
    "INSERT INTO projects(project_id, display_name, created_at_unix_nano) "
    "VALUES ('default', 'Default', 0)",
    "INSERT INTO storage_maintenance(singleton) VALUES (1)",
)


# --- Revision 0004_calls (frozen) ------------------------------------------------------
#
# What ``0004_calls`` adds: ``projects.capture_preset`` and the call tables. Like the
# baseline, these definitions are frozen history: a later revision changes the
# schema with its own operations and never edits them, so a catalog created
# today and one upgraded later end up identical.
#
# Boolean columns are ``INTEGER`` with ``CHECK (x IN (0, 1))``, as the baseline's
# are (``export_allowed_local_api``, ``connectors.enabled``): the hand-written SQL
# binds 0 and 1 on both dialects.

CALLS_TABLES = (
    "calls",
    "call_traces",
    "otlp_resources",
    "spans",
    "call_revisions",
    "recordings",
    "pending_blob_deletions",
    "call_key_generations",
    "call_tombstones",
    "otlp_counters",
)
_KEY_SOURCES = (
    "earshot.session.id",
    "gen_ai.conversation.id",
    "session.id",
    "conversation.id",
    "trace_id",
    "recording",
)
_OTLP_COUNTERS = (
    "rejected_spans",
    "span_conflicts",
    "parked_traces_discarded",
    "key_disagreements",
    "revision_cap_reached",
    "purged_call_evidence_dropped",
)


def _in(column: str, values: tuple[str, ...] | tuple[int, ...]) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" if isinstance(value, str) else str(value) for value in values)
    return CheckConstraint(f"{column} IN ({listed})")


def _flag(name: str, *, default: int | None = None) -> Column[int]:
    kwargs = {} if default is None else {"server_default": text(str(default))}
    return _required(name, Integer, _in(name, (0, 1)), **kwargs)


def _at_least(name: str, minimum: int) -> CheckConstraint:
    return CheckConstraint(f"{name} >= {minimum}")


def _length(name: str, size: int) -> CheckConstraint:
    return CheckConstraint(f"length({name}) = {size}")


def _length_range(name: str, low: int, high: int) -> CheckConstraint:
    # Not BETWEEN: Postgres stores BETWEEN as this conjunction, so writing it out
    # keeps the CHECK text equal on both dialects.
    return CheckConstraint(f"length({name}) >= {low} AND length({name}) <= {high}")


def _project(**kwargs: object) -> Column[str]:
    return _required("project_id", Text, ForeignKey("projects.project_id"), **kwargs)


def capture_preset_column() -> Column[str]:
    """``projects.capture_preset``, which revision 0004 adds (new projects: ``full``)."""

    return _required(
        "capture_preset",
        Text,
        _in("capture_preset", ("full", "metadata_only")),
        server_default=text("'full'"),
    )


def add_calls_tables(metadata: MetaData) -> list[Table]:
    """Add the tables revision ``0004_calls`` creates to ``metadata`` (which holds
    ``projects``) and return them, parents first."""

    calls = Table(
        "calls",
        metadata,
        _project(),
        _required("call_id", Text),
        # The host's session key, trimmed; NULL under metadata_only and for calls
        # keyed by trace or created by a recording without one.
        _optional("session_key", Text, _length_range("session_key", 1, 256)),
        # HMAC of the key (or trace): call purge needs it to bump the generation
        # even when session_key is NULL.
        _required("key_fingerprint", Text, _length("key_fingerprint", 64)),
        _required("key_source", Text, _in("key_source", _KEY_SOURCES)),
        _required("generation", Integer, _at_least("generation", 0)),
        _required("state", Text, _in("state", ("open", "closed"))),
        _required("first_evidence_unix_nano", UNIX_NANO),
        _required("last_evidence_unix_nano", UNIX_NANO),
        _optional("end_signal_unix_nano", UNIX_NANO),
        _optional("close_due_unix_nano", UNIX_NANO),
        _flag("dirty"),
        _optional("pending_revision", Integer, _at_least("pending_revision", 1)),
        _optional("latest_revision", Integer, _at_least("latest_revision", 1)),
        _optional("lease_until_unix_nano", UNIX_NANO),
        _required(
            "lease_token",
            BigInteger,
            _at_least("lease_token", 0),
            server_default=text("0"),
        ),
        _required(
            "materialize_attempts",
            Integer,
            _at_least("materialize_attempts", 0),
            server_default=text("0"),
        ),
        _optional("materialize_error", Text),
        _required("span_count", BigInteger, _at_least("span_count", 0), server_default=text("0")),
        _required(
            "recording_count",
            Integer,
            _at_least("recording_count", 0),
            server_default=text("0"),
        ),
        _flag("evidence_after_latest_revision", default=0),
        # Whether ``revision_cap_reached`` has counted this call (once per call).
        _flag("revision_cap_counted", default=0),
        _required("created_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "call_id"),
        UniqueConstraint("call_id"),
    )
    Index(
        "calls_project_evidence_idx",
        calls.c.project_id,
        calls.c.first_evidence_unix_nano.desc(),
        calls.c.call_id.desc(),
    )
    Index("calls_close_due_idx", calls.c.dirty, calls.c.close_due_unix_nano)
    Index(
        "calls_pending_revision_idx",
        calls.c.pending_revision,
        sqlite_where=text("pending_revision IS NOT NULL"),
        postgresql_where=text("pending_revision IS NOT NULL"),
    )

    call_traces = Table(
        "call_traces",
        metadata,
        _project(),
        _required("trace_id", BLOB, _length("trace_id", 16)),
        _optional("call_id", Text),
        _flag("has_session_span"),
        _required("first_span_received_unix_nano", UNIX_NANO),
        _required("last_span_received_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "trace_id"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["calls.project_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
    )
    Index("call_traces_call_idx", call_traces.c.project_id, call_traces.c.call_id)
    Index(
        "call_traces_parked_idx",
        call_traces.c.last_span_received_unix_nano,
        sqlite_where=text("call_id IS NULL"),
        postgresql_where=text("call_id IS NULL"),
    )

    Table(
        "otlp_resources",
        metadata,
        _project(),
        _required("resource_digest", BLOB, _length("resource_digest", 32)),
        # Deterministic, preset-filtered ResourceSpans holding the Resource and one
        # ScopeSpans with the scope (and no spans).
        _required("payload", BLOB),
        PrimaryKeyConstraint("project_id", "resource_digest"),
    )

    spans = Table(
        "spans",
        metadata,
        _required("project_id", Text),
        _required("trace_id", BLOB, _length("trace_id", 16)),
        _required("span_id", BLOB, _length("span_id", 8)),
        _optional("parent_span_id", BLOB, _length("parent_span_id", 8)),
        _required("name", Text),
        _required("scope_name", Text),
        _required("start_unix_nano", UNIX_NANO),
        _required("end_unix_nano", UNIX_NANO),
        _flag("end_marker"),
        _required("resource_digest", BLOB, _length("resource_digest", 32)),
        _required("digest", BLOB, _length("digest", 32)),
        _required("received_unix_nano", UNIX_NANO),
        # The span's transcript line as JSON (``call_materializer``), extracted from
        # the stored span when it is written, so a call's transcript is read without
        # its payloads; NULL for a span that carries no line (and so for every span
        # stored under metadata_only, whose filter removes the text).
        _optional("text_json", Text),
        # Deterministic, preset-filtered Span. Last: an LLM span's payload repeats
        # the whole conversation, and SQLite reads a column stored after a large
        # value by walking that value's overflow pages.
        _required("payload", BLOB),
        PrimaryKeyConstraint("project_id", "trace_id", "span_id"),
        ForeignKeyConstraint(
            ["project_id", "trace_id"],
            ["call_traces.project_id", "call_traces.trace_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "resource_digest"],
            ["otlp_resources.project_id", "otlp_resources.resource_digest"],
        ),
    )
    Index("spans_trace_start_idx", spans.c.project_id, spans.c.trace_id, spans.c.start_unix_nano)
    # Serves the foreign key from otlp_resources and the unreferenced-resource sweep.
    Index("spans_resource_idx", spans.c.project_id, spans.c.resource_digest)

    Table(
        "call_revisions",
        metadata,
        _required("project_id", Text),
        _required("call_id", Text),
        _required("revision", Integer, _at_least("revision", 1)),
        _required("bundle_id", Text),
        _required(
            "close_reason",
            Text,
            _in("close_reason", ("end_signal", "quiet_period", "late_data")),
        ),
        _required("finality", Text, _in("finality", ("final", "provisional"))),
        _required("span_count", BigInteger, _at_least("span_count", 0)),
        _required("recording_count", Integer, _at_least("recording_count", 0)),
        _required("created_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "call_id", "revision"),
        UniqueConstraint("bundle_id"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["calls.project_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
    )

    recordings = Table(
        "recordings",
        metadata,
        _required("project_id", Text),
        _required("recording_id", Text, _length_range("recording_id", 1, 128)),
        _required("call_id", Text),
        _required("speaker", Text, _in("speaker", ("user", "agent", "mixed"))),
        _required("content_type", Text),
        _optional("label", Text),
        _required("started_at_unix_nano", DECIMAL_NANO),
        _required("state", Text, _in("state", ("pending", "stored", "failed"))),
        _optional("blob_key", Text),
        _optional("size", BigInteger, _at_least("size", 0)),
        _optional("sha256", Text, _length("sha256", 64)),
        _optional("duration_ms", BigInteger, _at_least("duration_ms", 0)),
        _required("created_unix_nano", UNIX_NANO),
        _optional("stored_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "recording_id"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["calls.project_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
    )
    Index("recordings_call_idx", recordings.c.project_id, recordings.c.call_id)
    Index("recordings_state_idx", recordings.c.state, recordings.c.created_unix_nano)

    Table(
        "pending_blob_deletions",
        metadata,
        _project(),
        _required("blob_key", Text),
        _required("queued_at_unix_nano", UNIX_NANO),
        _required("attempts", Integer, _at_least("attempts", 0), server_default=text("0")),
        PrimaryKeyConstraint("project_id", "blob_key"),
    )

    Table(
        "call_key_generations",
        metadata,
        _project(),
        _required("key_fingerprint", Text, _length("key_fingerprint", 64)),
        _required("generation", Integer, _at_least("generation", 0)),
        PrimaryKeyConstraint("project_id", "key_fingerprint"),
    )

    Table(
        "call_tombstones",
        metadata,
        _project(),
        _required("key_fingerprint", Text, _length("key_fingerprint", 64)),
        _required("generation", Integer, _at_least("generation", 0)),
        _required("expires_at_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "key_fingerprint", "generation"),
    )

    Table(
        "otlp_counters",
        metadata,
        _project(),
        _required("name", Text, _in("name", _OTLP_COUNTERS)),
        _required("value", BigInteger, _at_least("value", 0)),
        PrimaryKeyConstraint("project_id", "name"),
    )

    return [metadata.tables[name] for name in CALLS_TABLES]


# --- Revision 0005_projections (frozen) ------------------------------------------------
#
# What ``0005_projections`` adds, frozen like 0004's definitions:
#
# - three columns, each by ``ALTER TABLE … ADD COLUMN`` (a SQLite batch rebuild would
#   drop the table's unnamed CHECKs): a recording's channel map, a call's projection
#   counter and a project's price version (``PROJECTION_COLUMNS``);
# - the tables a call's analyses and projection live in, and the price tables
#   (``PROJECTION_TABLES``).
#
# A projection's unknowns are NULL, never 0: only values that are always known are
# NOT NULL (the stamps, turn counts, the unknown-cost count, the analysis state).
# Open vocabularies (a call's agent, framework and outcome, a price's unit, a
# provider's cost basis) have no CHECK: the code that writes them validates them,
# and SQLite cannot change a CHECK without rebuilding its table.

PROJECTION_TABLES = (
    "builtin_price_version",
    "prices",
    "audio_analysis",
    "call_projections",
    "call_transcripts",
    "call_tags",
)
PROJECTION_COLUMNS = (
    ("recordings", "channel_speakers_json"),
    ("calls", "projection_seq"),
    ("projects", "price_version"),
    ("spans", "first_revision"),
    ("recordings", "first_revision"),
)
# A recording's channels in order, as compact JSON: user and agent, each at most once.
_CHANNEL_SPEAKERS_JSON = ('["user"]', '["agent"]', '["user","agent"]', '["agent","user"]')
_ANALYSIS_STATES = ("pending", "running", "stored", "failed", "unavailable")
# A projection's summary of its revision's analyses: no recording; any analysis not
# yet terminal; all stored; terminal with some stored; terminal with none stored and
# some failed; all unavailable.
_CALL_ANALYSIS_STATES = ("none", "pending", "stored", "partial", "failed", "unavailable")
_TRANSCRIPT_SPEAKERS = ("user", "agent")
_TRANSCRIPT_TIMINGS = ("measured", "start_only", "order_only")
# A transcript line's times follow its timing: both, the start only, or neither (an
# end is never inferred from the next line's start).
_TRANSCRIPT_TIMING_SHAPE = (
    "(timing = 'measured' AND start_ms IS NOT NULL AND end_ms IS NOT NULL) OR "
    "(timing = 'start_only' AND start_ms IS NOT NULL AND end_ms IS NULL) OR "
    "(timing = 'order_only' AND start_ms IS NULL AND end_ms IS NULL)"
)
_ROW_ID = BigInteger().with_variant(Integer(), "sqlite")


def _counter(name: str) -> Column[int]:
    return _required(name, BigInteger, _at_least(name, 0), server_default=text("0"))


def _unknown_or_at_least_zero(name: str, type_: type[TypeEngine[object]] = BigInteger):
    return _optional(name, type_, _at_least(name, 0))


def projection_columns() -> list[tuple[str, Column[object]]]:
    """The columns revision ``0005_projections`` adds, as ``(table, column)``."""

    return [
        (
            "recordings",
            _optional(
                "channel_speakers_json",
                Text,
                _in("channel_speakers_json", _CHANNEL_SPEAKERS_JSON),
            ),
        ),
        # Bumped with every change a call's projection reads (a finished revision, a
        # stored analysis result); a projection records the value it was built from.
        ("calls", _counter("projection_seq")),
        # Bumped by every insert of the project's own prices.
        ("projects", _counter("price_version")),
        # The first revision built from the span or recording (NULL: none yet), set
        # once by the claim that snapshots it: revision r holds exactly the evidence
        # with first_revision <= r.
        ("spans", _optional("first_revision", Integer, _at_least("first_revision", 1))),
        ("recordings", _optional("first_revision", Integer, _at_least("first_revision", 1))),
    ]


def add_projection_tables(metadata: MetaData) -> list[Table]:
    """Add the tables revision ``0005_projections`` creates to ``metadata`` (which
    holds ``projects`` and the call tables) and return them, parents first."""

    # The version of the built-in prices (``prices.project_id IS NULL``): one row.
    Table(
        "builtin_price_version",
        metadata,
        Column(
            "singleton",
            Integer,
            CheckConstraint("singleton = 1"),
            primary_key=True,
            autoincrement=False,
        ),
        _counter("version"),
    )

    # Append-only and effective-dated. ``project_id`` NULL is a built-in price; a
    # project's own row wins over a built-in one. A price is ``price_nano_usd`` per
    # ``per_quantity`` units, so a fraction of a nano-USD per unit stays exact.
    prices = Table(
        "prices",
        metadata,
        Column("id", _ROW_ID, primary_key=True, autoincrement=True),
        _optional("project_id", Text, ForeignKey("projects.project_id")),
        _required("provider", Text, _length_range("provider", 1, 128)),
        _required("model", Text, _length_range("model", 1, 256)),
        _required("unit", Text, _length_range("unit", 1, 64)),
        _required("price_nano_usd", BigInteger, _at_least("price_nano_usd", 0)),
        _required("per_quantity", BigInteger, _at_least("per_quantity", 1)),
        _required("effective_from_unix_nano", UNIX_NANO),
        _optional("source_url", Text),
        _optional("note", Text),
        _required("created_unix_nano", UNIX_NANO),
        sqlite_autoincrement=True,
    )
    Index(
        "prices_match_idx",
        prices.c.provider,
        prices.c.model,
        prices.c.unit,
        prices.c.project_id,
        prices.c.effective_from_unix_nano,
    )
    Index("prices_project_idx", prices.c.project_id)

    # One analysis job per recording. Its result is written only by the lease that
    # computed it (``lease_token``), and goes with its recording.
    analysis = Table(
        "audio_analysis",
        metadata,
        _required("project_id", Text),
        _required("recording_id", Text),
        _required("state", Text, _in("state", _ANALYSIS_STATES)),
        _required("attempts", Integer, _at_least("attempts", 0), server_default=text("0")),
        _optional("lease_until_unix_nano", UNIX_NANO),
        _counter("lease_token"),
        # When a retried job may run again (its backoff); NULL: now.
        _optional("next_attempt_unix_nano", UNIX_NANO),
        _optional("error_code", Text),
        # The analysis algorithm a result was computed with; 0 before any has run.
        _required("algorithm_version", Integer, _at_least("algorithm_version", 0)),
        _optional("peaks_blob_key", Text),
        _optional("rendition_blob_key", Text),
        _required("updated_unix_nano", UNIX_NANO),
        # Last: it can be large, and SQLite reads a column stored after a large value
        # by walking that value's overflow pages.
        _optional("result_json", Text),
        PrimaryKeyConstraint("project_id", "recording_id"),
        ForeignKeyConstraint(
            ["project_id", "recording_id"],
            ["recordings.project_id", "recordings.recording_id"],
            ondelete="CASCADE",
        ),
    )
    Index("audio_analysis_lease_idx", analysis.c.state, analysis.c.lease_until_unix_nano)
    Index("audio_analysis_due_idx", analysis.c.state, analysis.c.next_attempt_unix_nano)

    # A call's projection: what its latest revision, that revision's analyses and the
    # prices say, stamped with the inputs' versions it was computed from.
    projection = Table(
        "call_projections",
        metadata,
        _required("project_id", Text),
        _required("call_id", Text),
        _required("revision", Integer, _at_least("revision", 1)),
        _required("source_seq", BigInteger, _at_least("source_seq", 0)),
        _required("price_version", BigInteger, _at_least("price_version", 0)),
        _required("builtin_price_version", BigInteger, _at_least("builtin_price_version", 0)),
        _required("projection_version", Integer, _at_least("projection_version", 1)),
        # sha256 (hex) of the inputs: an unchanged input is not rewritten.
        _required("input_digest", Text, _length("input_digest", 64)),
        _optional("agent", Text),
        _optional("framework", Text),
        _optional("outcome", Text),
        # The call's own evidence start (not the server's receive time).
        _optional("started_unix_nano", UNIX_NANO),
        _unknown_or_at_least_zero("duration_ms"),
        _required("turn_count", Integer, _at_least("turn_count", 0)),
        _unknown_or_at_least_zero("reported_latency_p50_ms"),
        _unknown_or_at_least_zero("reported_latency_max_ms"),
        _required("measurable_turns", Integer, _at_least("measurable_turns", 0)),
        _unknown_or_at_least_zero("audible_gap_p50_ms"),
        _unknown_or_at_least_zero("audible_gap_max_ms"),
        _unknown_or_at_least_zero("talk_time_user_ms"),
        _unknown_or_at_least_zero("talk_time_agent_ms"),
        _unknown_or_at_least_zero("overlap_ms"),
        _unknown_or_at_least_zero("barge_in_count", Integer),
        _unknown_or_at_least_zero("talk_over_count", Integer),
        _unknown_or_at_least_zero("long_silence_count", Integer),
        _unknown_or_at_least_zero("dead_air_ms"),
        _unknown_or_at_least_zero("cost_lower_bound_nano_usd"),
        _unknown_or_at_least_zero("cost_estimated_nano_usd"),
        _required("cost_unknown_items", Integer, _at_least("cost_unknown_items", 0)),
        # What a hosted provider reported the call cost, never added to Earshot's own.
        _unknown_or_at_least_zero("provider_cost_nano_usd"),
        _optional("provider_cost_basis", Text),
        _required("analysis_state", Text, _in("analysis_state", _CALL_ANALYSIS_STATES)),
        _required("updated_unix_nano", UNIX_NANO),
        # The usage a reprice reads instead of the bundle, then the call view's body.
        _optional("usage_json", Text),
        _required("detail_json", Text),
        PrimaryKeyConstraint("project_id", "call_id"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["calls.project_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "call_id", "revision"],
            ["call_revisions.project_id", "call_revisions.call_id", "call_revisions.revision"],
            ondelete="CASCADE",
        ),
    )
    Index("call_projections_agent_idx", projection.c.project_id, projection.c.agent)
    Index("call_projections_outcome_idx", projection.c.project_id, projection.c.outcome)
    Index(
        "call_projections_reported_latency_idx",
        projection.c.project_id,
        projection.c.reported_latency_p50_ms,
    )
    Index(
        "call_projections_audible_gap_idx",
        projection.c.project_id,
        projection.c.audible_gap_p50_ms,
    )
    Index(
        "call_projections_cost_idx",
        projection.c.project_id,
        projection.c.cost_lower_bound_nano_usd,
    )
    Index(
        "call_projections_started_idx",
        projection.c.project_id,
        projection.c.started_unix_nano,
    )

    # A projection's transcript, one row per line in order (written under the
    # ``full`` preset only); call-relative milliseconds.
    Table(
        "call_transcripts",
        metadata,
        _required("project_id", Text),
        _required("call_id", Text),
        _required("line_no", Integer, _at_least("line_no", 0)),
        _required("speaker", Text, _in("speaker", _TRANSCRIPT_SPEAKERS)),
        _required("timing", Text, _in("timing", _TRANSCRIPT_TIMINGS)),
        _optional("start_ms", BigInteger),
        _optional("end_ms", BigInteger),
        _required("text", Text),
        CheckConstraint(_TRANSCRIPT_TIMING_SHAPE),
        PrimaryKeyConstraint("project_id", "call_id", "line_no"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["call_projections.project_id", "call_projections.call_id"],
            ondelete="CASCADE",
        ),
    )

    # A projection's tags (the union of the call's ``earshot.call.tags``).
    tags = Table(
        "call_tags",
        metadata,
        _required("project_id", Text),
        _required("call_id", Text),
        _required("tag", Text, _length_range("tag", 1, 256)),
        PrimaryKeyConstraint("project_id", "call_id", "tag"),
        ForeignKeyConstraint(
            ["project_id", "call_id"],
            ["call_projections.project_id", "call_projections.call_id"],
            ondelete="CASCADE",
        ),
    )
    Index("call_tags_tag_idx", tags.c.project_id, tags.c.tag)

    return [metadata.tables[name] for name in PROJECTION_TABLES]


# --- Revision 0006_connectors (frozen) -------------------------------------------------
#
# What ``0006_connectors`` adds, frozen like 0005's definitions:
#
# - three columns, each by ``ALTER TABLE … ADD COLUMN`` (``CONNECTOR_COLUMNS``): a
#   connector's configuration, and a delivery receipt's call and digest kind;
# - the table a provider recording's fetch job lives in (``CONNECTOR_TABLES``).

CONNECTOR_TABLES = ("recording_fetches",)
CONNECTOR_COLUMNS = (
    ("connectors", "config_json"),
    ("delivery_receipts", "call_id"),
    ("delivery_receipts", "digest_kind"),
)
# What a receipt's ``body_sha256`` holds: the sha256 of the raw body (every receipt
# 0.3.x wrote), or of the delivery's canonical evidence and recording locators.
_DIGEST_KINDS = ("body_v1", "evidence_v1")
# Where a fetch gets its bytes: a URL the provider put in the payload, or one built
# from the provider call ID on the provider's own API host.
_FETCH_SOURCE_KINDS = ("payload_url", "provider_api")
# ``pending`` and ``running`` as the leased-job helper uses them; the rest terminal.
_FETCH_STATES = ("pending", "running", "stored", "failed", "expired")


def connector_columns() -> list[tuple[str, Column[object]]]:
    """The columns revision ``0006_connectors`` adds, as ``(table, column)``."""

    return [
        # JSON, validated by the code that writes it; NULL: the defaults.
        ("connectors", _optional("config_json", Text)),
        # The call an applied delivery's evidence went to (NULL: none, or a 0.3.x
        # receipt, which bound an incident instead).
        ("delivery_receipts", _optional("call_id", Text)),
        (
            "delivery_receipts",
            _required(
                "digest_kind",
                Text,
                _in("digest_kind", _DIGEST_KINDS),
                server_default=text("'body_v1'"),
            ),
        ),
    ]


def add_connector_tables(metadata: MetaData) -> list[Table]:
    """Add the tables revision ``0006_connectors`` creates to ``metadata`` (which
    holds the baseline's ``connectors`` and the call tables) and return them."""

    # One fetch job per provider recording, with the leased-job helper's columns. A
    # payload URL is a bearer capability: it is kept only while the job can use it.
    fetches = Table(
        "recording_fetches",
        metadata,
        _required("project_id", Text),
        _required("recording_id", Text),
        _required(
            "endpoint_id",
            Text,
            ForeignKey("connectors.endpoint_id", ondelete="CASCADE"),
        ),
        _required("source_kind", Text, _in("source_kind", _FETCH_SOURCE_KINDS)),
        # The locator: the connector's provider, this call ID and this track.
        _required("provider_call_id", Text, _length_range("provider_call_id", 1, 512)),
        _required("track", Text, _length_range("track", 1, 64)),
        _optional("url", Text),
        _optional("url_expires_unix_nano", UNIX_NANO),
        _required("state", Text, _in("state", _FETCH_STATES)),
        _required("attempts", Integer, _at_least("attempts", 0), server_default=text("0")),
        _optional("lease_until_unix_nano", UNIX_NANO),
        _counter("lease_token"),
        _optional("next_attempt_unix_nano", UNIX_NANO),
        _optional("error_code", Text),
        _required("created_unix_nano", UNIX_NANO),
        _required("updated_unix_nano", UNIX_NANO),
        PrimaryKeyConstraint("project_id", "recording_id"),
        ForeignKeyConstraint(
            ["project_id", "recording_id"],
            ["recordings.project_id", "recordings.recording_id"],
            ondelete="CASCADE",
        ),
    )
    Index("recording_fetches_lease_idx", fetches.c.state, fetches.c.lease_until_unix_nano)
    Index("recording_fetches_due_idx", fetches.c.state, fetches.c.next_attempt_unix_nano)
    Index("recording_fetches_endpoint_idx", fetches.c.endpoint_id)

    return [metadata.tables[name] for name in CONNECTOR_TABLES]
