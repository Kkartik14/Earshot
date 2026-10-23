"""The Postgres runtime: shim, pool, write lock, correlation key, artifacts, migrations.

Every test runs against the real test Postgres server (``--db postgresql`` or
``--db all``); under the default ``--db sqlite`` they are skipped.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from earshot.api import create_app
from earshot.cli import main
from earshot.codec import encode_incident_protobuf
from earshot.contract import RetentionPolicy
from earshot.maintenance import database_lock_watchdog
from earshot.storage import (
    ArtifactCorruptionError,
    CorrelationKeyMismatchError,
    CorrelationKeyRequiredError,
    DatabaseError,
    DatabaseInUseError,
    DatabaseUnavailableError,
    IncidentPurgedError,
    IncidentStore,
    SchemaTooNewError,
    StorageError,
    StorageOpenError,
    acquire_server_lock,
)
from earshot.storage_postgres import PostgresConnection, PostgresDatabase
from incident_factory import make_valid_bundle
from store_support import (
    TEST_CORRELATION_KEY,
    TEST_CORRELATION_KEY_BASE64,
    catalog,
    catalog_rows,
    make_store,
    postgres_cluster,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres_only]

HEAD = "0004_calls"


@pytest.fixture
def database(postgres_url: str):
    database = PostgresDatabase(postgres_url, pool_size=2)
    try:
        yield database
    finally:
        database.close()


def _with_expired_metadata(bundle):
    policies = tuple(
        policy.model_copy(update={"retention": RetentionPolicy(expires_at_unix_nano="0")})
        if policy.capture_class == "metadata"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def _without_correlation_key_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARSHOT_CORRELATION_KEY", raising=False)
    monkeypatch.delenv("EARSHOT_CORRELATION_KEY_FILE", raising=False)


# --- The shim (AC1.9) -----------------------------------------------------------------


def test_connection_context_commits_on_clean_exit_and_rolls_back_on_error(database) -> None:
    with database.write_lock():
        with database.connect() as connection:
            assert type(connection) is PostgresConnection
            connection.execute("CREATE TABLE notes (value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE deferred_unique (value INTEGER UNIQUE DEFERRABLE INITIALLY DEFERRED)"
            )
        with database.connect() as connection:
            connection.execute("INSERT INTO notes(value) VALUES (?)", ("implicit",))
        with database.connect() as connection:
            connection.begin_write()
            connection.execute("INSERT INTO notes(value) VALUES (?)", ("explicit",))
        with pytest.raises(RuntimeError, match="abort"), database.connect() as connection:
            connection.begin_write()
            connection.execute("INSERT INTO notes(value) VALUES (?)", ("rolled-back",))
            raise RuntimeError("abort")
        # A commit that fails (here a deferred constraint) rolls back and raises.
        with pytest.raises(DatabaseError), database.connect() as connection:
            connection.begin_write()
            connection.execute("INSERT INTO notes(value) VALUES (?)", ("failed-commit",))
            connection.execute("INSERT INTO deferred_unique(value) VALUES (1), (1)")

    with database.connect() as reader:
        rows = reader.execute("SELECT value FROM notes ORDER BY value").fetchall()
    assert [(row[0], row["value"]) for row in rows] == [
        ("explicit", "explicit"),
        ("implicit", "implicit"),
    ]
    # Every connection went back to the pool, and a released one cannot be reused.
    assert database.engine.pool.checkedout() == 0
    with pytest.raises(StorageError, match="after it was released"):
        reader.execute("SELECT 1")


def test_write_transaction_outside_the_store_write_lock_is_refused(database) -> None:
    with (
        pytest.raises(StorageError, match="outside the store write lock"),
        database.connect() as connection,
    ):
        connection.begin_write()


def test_qmark_statements_keep_literal_percent_and_bytea_travels_exactly(database) -> None:
    payload = bytes(range(256)) * 64
    with database.write_lock(), database.connect() as connection:
        connection.execute("CREATE TABLE blobs (name TEXT PRIMARY KEY, payload BYTEA NOT NULL)")
        connection.execute("INSERT INTO blobs(name, payload) VALUES (?, ?)", ("100%", payload))

    with database.connect() as connection:
        row = connection.execute(
            "SELECT name, payload, '50%' AS literal FROM blobs WHERE name LIKE '100%'"
        ).fetchone()

    assert (row["name"], row["literal"], row[1]) == ("100%", "50%", payload)
    assert type(row["payload"]) is bytes


def test_pool_timeout_is_reported_as_storage_unavailable(database) -> None:
    first = database.connect()
    second = database.connect()
    try:
        started = time.monotonic()
        with pytest.raises(DatabaseUnavailableError) as unavailable:
            database.connect()
        waited = time.monotonic() - started
    finally:
        first.close()
        second.close()

    assert isinstance(unavailable.value, StorageError)
    assert 4.5 <= waited < 8.0
    with database.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_pool_size_comes_from_the_environment(postgres_url, monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_DB_POOL_SIZE", "3")
    sized = PostgresDatabase(postgres_url)
    try:
        assert (sized.pool_size, sized.engine.pool.size()) == (3, 3)
    finally:
        sized.close()

    monkeypatch.delenv("EARSHOT_DB_POOL_SIZE")
    default = PostgresDatabase(postgres_url)
    try:
        assert default.pool_size == 8
    finally:
        default.close()

    for invalid in ("1", "eight"):
        monkeypatch.setenv("EARSHOT_DB_POOL_SIZE", invalid)
        with pytest.raises(StorageOpenError, match="EARSHOT_DB_POOL_SIZE"):
            PostgresDatabase(postgres_url)


def test_database_outage_answers_503_and_recovers(tmp_path, postgres_admin) -> None:
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))
    name = postgres_cluster().database_name(tmp_path)
    assert client.get("/v1/incidents").status_code == 200

    postgres_admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
    try:
        postgres_admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        listing = client.get("/v1/incidents")
        readiness = client.get("/readyz")
    finally:
        postgres_admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')

    assert listing.status_code == 503
    assert listing.json()["error"]["code"] == "EARSHOT_STORAGE_UNAVAILABLE"
    assert readiness.status_code == 503
    assert client.get("/v1/incidents").status_code == 200
    assert client.get("/readyz").status_code == 200


# --- The correlation key (AC1.7) ------------------------------------------------------


def test_postgres_store_requires_a_configured_correlation_key(postgres_url, monkeypatch) -> None:
    _without_correlation_key_environment(monkeypatch)

    with pytest.raises(CorrelationKeyRequiredError) as refused:
        IncidentStore(database_url=postgres_url)

    assert refused.value.code == "EARSHOT_CORRELATION_KEY_REQUIRED"
    assert "EARSHOT_CORRELATION_KEY" in str(refused.value)
    assert "EARSHOT_CORRELATION_KEY_FILE" in str(refused.value)


def test_only_a_key_id_is_stored_and_another_key_is_refused(tmp_path, monkeypatch) -> None:
    url = postgres_cluster().database_for(tmp_path)
    _without_correlation_key_environment(monkeypatch)
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
    IncidentStore(database_url=url).close()

    rows = catalog_rows(tmp_path, "SELECT key, value FROM earshot_meta")
    assert [key for key, _ in rows] == ["correlation_key_id"]
    key_id = rows[0][1]
    assert len(key_id) == 64
    assert TEST_CORRELATION_KEY.hex() not in key_id
    assert TEST_CORRELATION_KEY_BASE64 not in key_id

    other_key = base64.b64encode(hashlib.sha256(b"another key").digest()).decode("ascii")
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", other_key)
    with pytest.raises(CorrelationKeyMismatchError) as refused:
        IncidentStore(database_url=url)
    assert refused.value.code == "EARSHOT_CORRELATION_KEY_MISMATCH"

    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
    IncidentStore(database_url=url).close()
    assert catalog_rows(tmp_path, "SELECT value FROM earshot_meta") == [(key_id,)]


def test_a_sqlite_key_file_carries_its_identities_to_postgres(
    tmp_path, postgres_url, monkeypatch
) -> None:
    sqlite_store = IncidentStore(tmp_path / "sqlite")
    expected = (
        sqlite_store.fingerprint("delivery:test", "caller-1"),
        sqlite_store.hosted_bundle_id("tenant", "submission-1"),
    )
    sqlite_store.close()
    _without_correlation_key_environment(monkeypatch)
    monkeypatch.setenv(
        "EARSHOT_CORRELATION_KEY_FILE", str(tmp_path / "sqlite" / "instance-correlation.key")
    )

    postgres_store = IncidentStore(database_url=postgres_url)
    try:
        assert (
            postgres_store.fingerprint("delivery:test", "caller-1"),
            postgres_store.hosted_bundle_id("tenant", "submission-1"),
        ) == expected
    finally:
        postgres_store.close()


def test_a_key_file_readable_by_others_is_refused(tmp_path, postgres_url, monkeypatch) -> None:
    key_file = tmp_path / "correlation.key"
    key_file.write_bytes(TEST_CORRELATION_KEY)
    key_file.chmod(0o644)
    _without_correlation_key_environment(monkeypatch)
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY_FILE", str(key_file))

    with pytest.raises(CorrelationKeyRequiredError, match="chmod 600"):
        IncidentStore(database_url=postgres_url)


def test_correlated_catalog_without_a_key_id_is_refused(tmp_path) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="correlated"))
    store.close()
    with catalog(tmp_path) as connection:
        connection.execute("DELETE FROM earshot_meta WHERE key = 'correlation_key_id'")

    with pytest.raises(CorrelationKeyMismatchError, match="no correlation key ID"):
        make_store(tmp_path)


# --- Artifacts are catalog rows (AC1.8) -----------------------------------------------


def test_artifacts_are_catalog_rows_and_no_file_is_written(tmp_path) -> None:
    data_dir = tmp_path / "data"
    store = make_store(data_dir)
    bundle = make_valid_bundle(bundle_id="row-artifact")
    canonical = encode_incident_protobuf(bundle)

    record = store.ingest(bundle, canonical).record

    assert catalog_rows(data_dir, "SELECT digest, payload FROM artifacts") == [
        (record.digest, canonical)
    ]
    assert record.digest == hashlib.sha256(canonical).hexdigest()
    assert store.get_artifact("row-artifact")[1] == canonical
    assert not data_dir.exists()
    assert catalog_rows(data_dir, "SELECT COUNT(*) FROM pending_ingests") == [(0,)]


def test_purge_deletes_the_artifact_row_and_records_no_scrub(tmp_path) -> None:
    store = make_store(tmp_path)
    kept = store.ingest(make_valid_bundle(bundle_id="kept")).record
    store.ingest(make_valid_bundle(bundle_id="purged"))

    store.purge("purged")

    assert catalog_rows(tmp_path, "SELECT digest FROM artifacts") == [(kept.digest,)]
    assert catalog_rows(tmp_path, "SELECT COUNT(*) FROM tombstones") == [(1,)]
    # Postgres cannot scrub pages, so no scrub generation is ever claimed.
    assert catalog_rows(
        tmp_path, "SELECT purge_generation, scrubbed_generation FROM storage_maintenance"
    ) == [(0, 0)]
    with pytest.raises(IncidentPurgedError):
        store.get_artifact("purged")


def test_retention_and_project_deletion_release_artifact_rows(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("tenant-a", display_name="Tenant A")
    store.create_project("tenant-b", display_name="Tenant B")
    store.ingest(make_valid_bundle(bundle_id="a-evidence"), project_id="tenant-a")
    kept = store.ingest(make_valid_bundle(bundle_id="b-evidence"), project_id="tenant-b").record
    store.ingest(_with_expired_metadata(make_valid_bundle(bundle_id="expired")))

    assert store.purge_expired() == 1
    assert store.delete_project_data("tenant-a") is True

    assert catalog_rows(tmp_path, "SELECT digest FROM artifacts") == [(kept.digest,)]


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("UPDATE artifacts SET payload = ?", "digest mismatch"),
        ("DELETE FROM artifacts WHERE length(?) > 0", "missing"),
    ],
    ids=("rewritten", "deleted"),
)
def test_damaged_artifact_rows_are_integrity_errors(tmp_path, damage: str, message: str) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="damaged"))
    with catalog(tmp_path) as connection:
        connection.execute(damage, (b"not the canonical bytes",))

    with pytest.raises(ArtifactCorruptionError, match=message):
        store.get_artifact("damaged")


def test_explicit_cleanup_sweeps_unreferenced_artifact_rows(tmp_path) -> None:
    store = make_store(tmp_path)
    live = store.ingest(make_valid_bundle(bundle_id="live")).record
    orphan = b"bytes no incident references"
    with catalog(tmp_path) as connection:
        connection.execute(
            "INSERT INTO artifacts(digest, payload) VALUES (?, ?)",
            (hashlib.sha256(orphan).hexdigest(), orphan),
        )

    assert store.cleanup_unreferenced_objects() == 1
    assert catalog_rows(tmp_path, "SELECT digest FROM artifacts") == [(live.digest,)]


# --- The write lock K_write (AC1.6) ---------------------------------------------------


def _cli_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment["EARSHOT_CORRELATION_KEY"] = TEST_CORRELATION_KEY_BASE64
    return environment


def _waiting_advisory_locks(postgres_admin, database_name: str) -> int:
    return postgres_admin.execute(
        "SELECT COUNT(*) FROM pg_locks AS lock JOIN pg_database AS db ON db.oid = lock.database "
        "WHERE lock.locktype = 'advisory' AND NOT lock.granted AND db.datname = %s",
        (database_name,),
    ).fetchone()[0]


def test_a_cli_writer_waits_until_another_process_finishes_its_mutation(
    tmp_path, postgres_admin
) -> None:
    store = make_store(tmp_path)
    url = postgres_cluster().database_for(tmp_path)
    name = postgres_cluster().database_name(tmp_path)

    with store.project_access_scope("default"):
        writer = subprocess.Popen(
            [
                *(sys.executable, "-m", "earshot.cli", "project", "create", "late"),
                *("--display-name", "Late", "--database-url", url),
            ],
            env=_cli_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30
        while _waiting_advisory_locks(postgres_admin, name) == 0:
            assert writer.poll() is None, writer.communicate()
            assert time.monotonic() < deadline, "the CLI never queued for K_write"
            time.sleep(0.02)
        # A whole transaction commits inside this mutation; K_write stays held.
        store.create_project("inner", display_name="Inner")
        assert _waiting_advisory_locks(postgres_admin, name) == 1
        assert writer.poll() is None
        assert store.project_exists("late") is False

    stdout, stderr = writer.communicate(timeout=30)
    assert writer.returncode == 0, stderr
    assert json.loads(stdout)["project_id"] == "late"
    assert store.project_exists("late") is True


def test_the_write_lock_is_released_when_a_mutation_fails(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="project does not exist"):
        store.issue_api_key("missing", label="never")

    # A second store has its own K_write session: it would wait forever on a leak.
    other = make_store(tmp_path)
    finished = threading.Event()

    def create_project() -> None:
        other.create_project("after", display_name="After")
        finished.set()

    worker = threading.Thread(target=create_project, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert finished.is_set()


# --- Fork safety (the after-fork hook disposes the pool) ------------------------------


def _earshot_sessions(postgres_admin, database_name: str) -> set[tuple[str, int]]:
    return {
        (row[0], row[1])
        for row in postgres_admin.execute(
            "SELECT application_name, pid FROM pg_stat_activity WHERE datname = %s "
            "AND application_name LIKE 'earshot%%'",
            (database_name,),
        )
    }


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_never_ends_the_parents_database_sessions(tmp_path, postgres_admin) -> None:
    store = make_store(tmp_path)
    store.create_project("before", display_name="Before")
    name = postgres_cluster().database_name(tmp_path)
    parent_sessions = _earshot_sessions(postgres_admin, name)
    assert {application for application, _ in parent_sessions} == {
        "earshot",
        "earshot-write-lock",
    }

    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the child
        status = 1
        try:
            store.create_project("child", display_name="Child")
            status = 0 if store.project_exists("child") else 3
            store.close()
            del store
            import gc

            gc.collect()
        except BaseException:
            status = 2
        os._exit(status)
    _, wait_status = os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(wait_status) == 0
    # The child used sessions of its own and ended them; the parent's survive.
    assert _earshot_sessions(postgres_admin, name) == parent_sessions
    store.create_project("after", display_name="After")
    assert store.project_exists("child") is True


# --- Migrations: alembic_version is the only guard ------------------------------------


def test_new_postgres_catalog_is_created_at_head(empty_postgres_url, capsys, monkeypatch) -> None:
    _without_correlation_key_environment(monkeypatch)
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)

    assert main(["db", "current", "--database-url", empty_postgres_url]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": None,
        "state": "empty",
    }
    assert main(["db", "upgrade", "--database-url", empty_postgres_url]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": HEAD,
        "revision_before": None,
    }
    assert main(["db", "current", "--database-url", empty_postgres_url]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "current"


def test_unknown_postgres_revision_is_refused_with_a_stable_code(tmp_path) -> None:
    make_store(tmp_path).close()
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")

    with pytest.raises(SchemaTooNewError) as refused:
        make_store(tmp_path)

    assert refused.value.code == "EARSHOT_SCHEMA_TOO_NEW"
    assert catalog_rows(tmp_path, "SELECT version_num FROM alembic_version") == [
        ("9999_from_the_future",)
    ]


def test_postgres_database_with_foreign_tables_is_refused(empty_postgres_url) -> None:
    database = PostgresDatabase(empty_postgres_url, pool_size=2)
    try:
        with database.write_lock(), database.connect() as connection:
            connection.execute("CREATE TABLE someone_elses (id INTEGER)")
    finally:
        database.close()

    with pytest.raises(StorageError, match="no alembic_version"):
        IncidentStore(database_url=empty_postgres_url, correlation_key=TEST_CORRELATION_KEY)


def test_postgres_store_creates_nothing_in_its_data_dir(tmp_path) -> None:
    data_dir: Path = tmp_path / "never-created"
    make_store(data_dir).close()
    assert not data_dir.exists()


# --- K_server and its watchdog (AC1.6) ------------------------------------------------


def test_only_one_server_lock_per_database(postgres_url) -> None:
    held = acquire_server_lock(postgres_url)
    try:
        with pytest.raises(DatabaseInUseError) as refused:
            acquire_server_lock(postgres_url)
        assert refused.value.code == "EARSHOT_DATABASE_IN_USE"
    finally:
        held.release()
    acquire_server_lock(postgres_url).release()


def test_watchdog_reports_a_terminated_lock_session(postgres_url, postgres_admin) -> None:
    lock = acquire_server_lock(postgres_url)
    lost: list[str] = []
    watchdog = database_lock_watchdog(lock, on_lost=lost.append)
    try:
        asyncio.run(watchdog(0))
        assert lost == []

        postgres_admin.execute("SELECT pg_terminate_backend(%s)", (lock.backend_pid,))
        asyncio.run(watchdog(0))
    finally:
        lock.release()

    assert len(lost) == 1
    assert "SQLSTATE 57P01" in lost[0]
    acquire_server_lock(postgres_url).release()


def test_watchdog_treats_an_unanswered_probe_as_a_lost_lock(postgres_url) -> None:
    lock = acquire_server_lock(postgres_url)
    lost: list[str] = []
    try:
        asyncio.run(database_lock_watchdog(lock, on_lost=lost.append, probe_timeout_seconds=0)(0))
    finally:
        lock.release()

    assert lost == ["the server lock connection did not answer"]
