"""Real 0.1.x catalogs upgrade to head, and old releases refuse them after.

The fixtures under ``fixtures/upgrade`` were written by each release's own code,
extracted from git and run in a subprocess (``scripts/make_upgrade_fixtures.py``):
v0.1.0 (schema v10), 553cecf (v17), 86c4c9c (v18) and 3c2f0b3 (v19). Each one
ingested that release's canonical and fault fixtures, cached their analyses,
created a project, issued an API key and revoked a second one, created a
connector with an applied delivery and an external identity, linked an external
reference where the release had them, and purged one incident. The manifest
records what the release wrote. Every test copies a fixture before opening it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient

from catalog_snapshot import sqlite_catalog_snapshot
from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.api import ApiConfig, create_app
from earshot.codec import decode_incident_protobuf
from earshot.contract import DerivedAnalysis
from earshot.query import EvidenceQuery
from earshot.storage import ApiPrincipal, IncidentPurgedError, IncidentStore
from earshot.storage_database import SQLiteDatabase
from earshot.storage_migrations import head_revision
from upgrade_fixtures import copy_upgrade_fixture, upgrade_fixture, upgrade_fixture_names

pytestmark = [pytest.mark.integration, pytest.mark.sqlite_only]

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "scripts" / "make_upgrade_fixtures.py"
HEAD = head_revision()
ALEMBIC_SENTINEL = 1000
FIXTURES = upgrade_fixture_names()
OLDEST, NEWEST = FIXTURES[0], FIXTURES[-1]
# A table the upgrade creates is empty afterwards, except these (and
# incident_ingest_order, see below).
ROWS_OF_ADDED_TABLES = {
    "alembic_version": 1,
    "builtin_price_version": 1,
    "earshot_meta": 1,
    "storage_maintenance": 1,
}


def _row_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in tables
        }


@pytest.fixture(scope="module")
def fresh_catalog(tmp_path_factory) -> Path:
    data_dir = tmp_path_factory.mktemp("fresh")
    IncidentStore(data_dir).close()
    return data_dir / "earshot.sqlite3"


def _expected_rows_after_upgrade(name: str, fresh_catalog: Path) -> dict[str, int]:
    """The fixture's rows, plus what every table the upgrade adds must hold."""

    fixture = upgrade_fixture(name)
    expected = dict(fixture["table_rows"])
    for table in _row_counts(fresh_catalog).keys() - expected.keys():
        if table == "incident_ingest_order":  # the v16 step backfills one row per incident
            expected[table] = len(fixture["incidents"])
        else:
            expected[table] = ROWS_OF_ADDED_TABLES.get(table, 0)
    return expected


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stored_objects(data_dir: Path) -> dict[str, int]:
    """The CAS objects on disk, by name; each file's content must hash to its name."""

    objects = {}
    for path in sorted((data_dir / "objects" / "sha256").glob("*/*")):
        digest = path.parent.name + path.name
        assert _sha256_file(path) == digest, path
        objects[digest] = path.stat().st_size
    return objects


def _foreign_key_violations(database: Path) -> list[tuple]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def _alembic_state(database: Path) -> tuple[int, list[str] | None, list[str]]:
    with closing(sqlite3.connect(database)) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = {
            row[0]: row[1] for row in connection.execute("SELECT name, type FROM sqlite_master")
        }
        revisions = (
            [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
            if "alembic_version" in names
            else None
        )
    return user_version, revisions, sorted(n for n, kind in names.items() if kind == "trigger")


@pytest.mark.parametrize("name", FIXTURES)
def test_each_fixture_is_the_catalog_its_release_wrote(tmp_path, name) -> None:
    fixture = upgrade_fixture(name)
    data_dir = copy_upgrade_fixture(name, tmp_path / name)
    database = data_dir / "earshot.sqlite3"

    assert {file_name: _sha256_file(data_dir / file_name) for file_name in fixture["files"]} == {
        file_name: entry["sha256"] for file_name, entry in fixture["files"].items()
    }
    user_version, revisions, triggers = _alembic_state(database)
    assert (user_version, revisions) == (fixture["schema_version"], None)
    # Schema v16 added the arrival-order trigger that revision 0001 replaces.
    assert triggers == (["incidents_track_ingest_order"] if fixture["schema_version"] >= 16 else [])
    assert _row_counts(database) == fixture["table_rows"]
    assert _stored_objects(data_dir) == fixture["objects"]
    assert {incident["digest"] for incident in fixture["incidents"]} <= fixture["objects"].keys()
    assert _foreign_key_violations(database) == []


@pytest.mark.parametrize("name", FIXTURES)
def test_a_legacy_catalog_upgrades_to_head_keeping_every_row_and_artifact(
    tmp_path, fresh_catalog, name
) -> None:
    fixture = upgrade_fixture(name)
    data_dir = copy_upgrade_fixture(name, tmp_path / name)
    database = data_dir / "earshot.sqlite3"

    IncidentStore(data_dir).close()

    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])
    assert _row_counts(database) == _expected_rows_after_upgrade(name, fresh_catalog)
    assert _foreign_key_violations(database) == []
    assert _stored_objects(data_dir) == fixture["objects"]
    assert sqlite_catalog_snapshot(database) == sqlite_catalog_snapshot(fresh_catalog)


@pytest.mark.parametrize("name", FIXTURES)
def test_every_upgraded_incident_reads_and_analyzes(tmp_path, name) -> None:
    fixture = upgrade_fixture(name)
    store = IncidentStore(copy_upgrade_fixture(name, tmp_path / name))
    try:
        convergence = store.converge_projections()
        assert (convergence.failed, convergence.pending) == ((), 0)
        assert convergence.reprojected == len(fixture["incidents"])
        for incident in fixture["incidents"]:
            bundle_id, project_id = incident["bundle_id"], incident["project_id"]
            record, payload = store.get_artifact(bundle_id, project_id=project_id)
            assert (record.digest, hashlib.sha256(payload).hexdigest()) == (
                incident["digest"],
                incident["digest"],
            )
            bundle = decode_incident_protobuf(payload)
            assert bundle.profile.manifest.bundle_id == bundle_id
            analysis = analyze_incident(
                bundle, input_sha256=record.digest, generated_at_unix_nano=time.time_ns()
            )
            # Where the old release cached an analysis of this analyzer version,
            # that one is served (and re-validated); otherwise the new one is stored.
            stored = store.put_analysis(
                bundle_id, ANALYZER_VERSION, analysis, project_id=project_id
            )
            assert (stored.analyzer_version, stored.input_digest) == (
                ANALYZER_VERSION,
                incident["digest"],
            )
            # The evidence questions the API answers from that stored analysis.
            query = EvidenceQuery(bundle, DerivedAnalysis.model_validate(stored.value))
            assert query.summary().session_id == bundle.profile.manifest.session_id
        purged = fixture["purged"]
        with pytest.raises(IncidentPurgedError):
            store.get_record(purged["bundle_id"], project_id=purged["project_id"])
    finally:
        store.close()


@pytest.mark.parametrize("name", FIXTURES)
def test_upgraded_credentials_connectors_and_links_keep_working(tmp_path, name) -> None:
    fixture = upgrade_fixture(name)
    project_id = fixture["project"]["project_id"]
    store = IncidentStore(copy_upgrade_fixture(name, tmp_path / name))
    try:
        active, revoked = fixture["api_keys"]["active"], fixture["api_keys"]["revoked"]
        assert store.authenticate_api_key(active["credential"]) == ApiPrincipal(
            project_id, active["key_id"]
        )
        assert store.authenticate_api_key(revoked["credential"]) is None

        connector = store.get_connector(fixture["connector"]["endpoint_id"])
        assert connector is not None
        assert (connector.project_id, connector.provider, connector.enabled) == (
            project_id,
            fixture["connector"]["provider"],
            True,
        )
        # The correlation key carried over: the same input gives the old fingerprint.
        identity = fixture["external_identity"]
        assert store.fingerprint(identity["namespace"], identity["value"]) == identity["value_hmac"]

        reference = fixture["external_reference"]
        if reference is not None:
            assert [
                (item.namespace, item.record_type, item.external_id)
                for item in store.get_external_references(
                    reference["bundle_id"], project_id=project_id
                )
            ] == [(reference["namespace"], reference["record_type"], reference["external_id"])]
    finally:
        store.close()


@pytest.mark.parametrize("name", FIXTURES)
def test_the_api_serves_every_upgraded_incident_to_the_old_release_key(tmp_path, name) -> None:
    fixture = upgrade_fixture(name)
    project_id = fixture["project"]["project_id"]
    expected = sorted(
        incident["bundle_id"]
        for incident in fixture["incidents"]
        if incident["project_id"] == project_id
    )
    store = IncidentStore(copy_upgrade_fixture(name, tmp_path / name))
    try:
        # Configured as ``earshot serve`` configures it (server_config.open_server).
        app = create_app(
            store=store,
            config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
            analyzer=analyze_incident,
        )
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {fixture['api_keys']['active']['credential']}"}

        listed = client.get("/v1/incidents", params={"limit": 100}, headers=headers)

        assert listed.status_code == 200
        assert sorted(item["bundle_id"] for item in listed.json()["items"]) == expected
        for bundle_id in expected:
            for read in ("", "/analysis", "/evidence/summary", "/contradictions"):
                response = client.get(f"/v1/incidents/{bundle_id}{read}", headers=headers)
                assert response.status_code == 200, (bundle_id, read, response.text[:200])
    finally:
        store.close()


def _open_with_release(name: str, data_dir: Path) -> dict[str, object]:
    """Open ``data_dir`` with the release ``name`` was generated by, extracted from git."""

    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--open-with", name, "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode == 4:
        pytest.skip(f"needs the git history of {name}: {completed.stderr.strip()}")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("name", FIXTURES)
def test_every_legacy_release_refuses_a_catalog_this_release_upgraded(tmp_path, name) -> None:
    data_dir = copy_upgrade_fixture(name, tmp_path / name)
    IncidentStore(data_dir).close()
    database = data_dir / "earshot.sqlite3"
    before = (_sha256_file(database), _row_counts(database))

    outcome = _open_with_release(name, data_dir)

    assert outcome == {
        "schema_version": upgrade_fixture(name)["schema_version"],
        "refused": "incident database schema is newer than this binary",
    }
    assert (_sha256_file(database), _row_counts(database)) == before
    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])


# A reflected batch rebuild drops unnamed CHECKs and expression indexes (the
# catalog snapshot tests catch that in a real revision); here only rows matter.
@pytest.mark.filterwarnings("ignore:Unnamed CHECK constraint on reflected table")
@pytest.mark.filterwarnings("ignore:Skipped unsupported reflection of expression-based index")
def test_rebuilding_parent_tables_on_the_migration_connection_keeps_every_child_row(
    tmp_path,
) -> None:
    data_dir = copy_upgrade_fixture(NEWEST, tmp_path / NEWEST)
    IncidentStore(data_dir).close()
    database = data_dir / "earshot.sqlite3"
    before = _row_counts(database)
    children = {
        table
        for table, description in sqlite_catalog_snapshot(database).portable["tables"].items()
        if description["foreign_keys"]
    }
    # The fixture populates the children of all three parents rebuilt below.
    assert {table for table in children if before[table]} >= {
        "analyses",
        "api_keys",
        "causal_links",
        "connectors",
        "delivery_receipts",
        "events",
        "external_identities",
        "incident_ingest_order",
        "incidents",
        "operations",
        "turn_metrics",
    }

    # What a later revision's ``batch_alter_table`` does to a table on SQLite:
    # create a copy, move the rows, drop the original and rename the copy.
    raw = SQLiteDatabase(str(database)).connect_for_migration()
    try:
        engine = sa.create_engine("sqlite://", creator=lambda: raw, poolclass=sa.pool.StaticPool)
        sa.event.listen(
            engine, "begin", lambda connection: connection.exec_driver_sql("BEGIN IMMEDIATE")
        )
        with engine.connect() as connection, connection.begin():
            operations = Operations(MigrationContext.configure(connection))
            for parent in ("projects", "connectors", "incidents"):
                with operations.batch_alter_table(parent, recreate="always"):
                    pass
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        engine.dispose()
    finally:
        raw.close()

    assert violations == []
    assert _row_counts(database) == before


def _open_in_subprocess(data_dir: Path, **environment: str) -> int:
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"EARSHOT_TEST_MODE", "EARSHOT_TEST_CRASH_AT"}
    }
    child_environment.update(environment)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nfrom earshot.storage import IncidentStore\n"
            "IncidentStore(sys.argv[1]).close()\n",
            str(data_dir),
        ],
        env=child_environment,
        capture_output=True,
        timeout=60,
        check=False,
    ).returncode


def test_a_crash_after_bootstrapping_a_populated_legacy_catalog_loses_no_row(
    tmp_path, fresh_catalog
) -> None:
    fixture = upgrade_fixture(OLDEST)
    data_dir = copy_upgrade_fixture(OLDEST, tmp_path / OLDEST)
    database = data_dir / "earshot.sqlite3"

    returncode = _open_in_subprocess(
        data_dir, EARSHOT_TEST_MODE="1", EARSHOT_TEST_CRASH_AT="after_legacy_bootstrap"
    )

    assert returncode == 137
    # The frozen converge (v10 to v19) committed; the Alembic stamp did not.
    assert _alembic_state(database) == (19, None, ["incidents_track_ingest_order"])
    converged = _row_counts(database)
    assert {table: converged[table] for table in fixture["table_rows"]} == fixture["table_rows"]
    assert _foreign_key_violations(database) == []

    IncidentStore(data_dir).close()

    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])
    assert _row_counts(database) == _expected_rows_after_upgrade(OLDEST, fresh_catalog)
    assert _foreign_key_violations(database) == []
    assert _stored_objects(data_dir) == fixture["objects"]
    # The bootstrap the next open completed also kept the 0.1.x privacy posture.
    with closing(sqlite3.connect(database)) as connection:
        presets = connection.execute("SELECT DISTINCT capture_preset FROM projects").fetchall()
    assert presets == [("metadata_only",)]
