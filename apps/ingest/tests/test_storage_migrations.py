"""Opening a SQLite catalog: Alembic for new files, the frozen converge for 0.1.x files.

Every catalog this binary opens ends up managed by Alembic (an ``alembic_version``
row) with ``PRAGMA user_version = 1000``, the sentinel that makes any 0.1.x binary
refuse it with its existing newer-schema error.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from catalog_snapshot import sqlite_catalog_snapshot
from earshot.cli import main
from earshot.storage import CatalogIntegrityError, SchemaTooNewError, StorageError
from earshot.storage_database import SQLiteDatabase
from earshot.storage_legacy import converge_legacy_catalog
from store_support import make_store

pytestmark = [pytest.mark.integration, pytest.mark.sqlite_only]

HEAD = "0004_calls"
ALEMBIC_SENTINEL = 1000


def _query(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _alembic_state(path: Path) -> tuple[int, list[str] | None, list[str]]:
    """Return ``(user_version, alembic revisions or None, trigger names)``."""

    with closing(sqlite3.connect(path)) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        revisions = (
            [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
            if has_version_table
            else None
        )
        triggers = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ]
    return user_version, revisions, triggers


def _write_legacy_v19_catalog(path: Path) -> None:
    """Write a genuine schema-v19 catalog with the frozen 0.1.x converge routine."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = SQLiteDatabase(str(path)).connect()
    try:
        converge_legacy_catalog(connection)
    finally:
        connection.close()


def test_new_catalog_is_created_by_alembic_and_carries_the_sentinel(tmp_path) -> None:
    make_store(tmp_path).close()

    database = tmp_path / "earshot.sqlite3"
    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])
    assert _query(database, "SELECT key, value FROM earshot_meta") == []
    assert _query(database, "SELECT project_id, display_name FROM projects") == [
        ("default", "Default")
    ]
    assert _query(database, "SELECT singleton, purge_generation FROM storage_maintenance") == [
        (1, 0)
    ]


def test_metadata_schema_matches_the_v19_schema_a_legacy_binary_wrote(tmp_path) -> None:
    make_store(tmp_path / "fresh").close()
    legacy = tmp_path / "legacy"
    _write_legacy_v19_catalog(legacy / "earshot.sqlite3")
    assert _alembic_state(legacy / "earshot.sqlite3") == (
        19,
        None,
        ["incidents_track_ingest_order"],
    )

    make_store(legacy).close()

    assert _alembic_state(legacy / "earshot.sqlite3") == (ALEMBIC_SENTINEL, [HEAD], [])
    assert _query(legacy / "earshot.sqlite3", "SELECT key, value FROM earshot_meta") == [
        ("legacy_bootstrap", "1")
    ]
    fresh_snapshot = sqlite_catalog_snapshot(tmp_path / "fresh" / "earshot.sqlite3")
    assert fresh_snapshot == sqlite_catalog_snapshot(legacy / "earshot.sqlite3")
    assert fresh_snapshot.portable["triggers"] == []


def test_last_version_below_the_alembic_sentinel_is_refused_untouched(tmp_path) -> None:
    # test_storage.py covers user_version 20, the first value above v19.
    database = tmp_path / "earshot.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(StorageError, match="schema is newer than this binary"):
        make_store(tmp_path)

    assert _alembic_state(database) == (999, None, [])


def test_unknown_alembic_revision_is_refused_with_a_stable_code(tmp_path) -> None:
    make_store(tmp_path).close()
    database = tmp_path / "earshot.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")

    with pytest.raises(SchemaTooNewError, match="EARSHOT_SCHEMA_TOO_NEW") as refused:
        make_store(tmp_path)

    assert refused.value.code == "EARSHOT_SCHEMA_TOO_NEW"
    assert _alembic_state(database) == (ALEMBIC_SENTINEL, ["9999_from_the_future"], [])


def test_alembic_catalog_with_a_legacy_user_version_is_refused_as_inconsistent(
    tmp_path,
) -> None:
    make_store(tmp_path).close()
    database = tmp_path / "earshot.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 19")

    with pytest.raises(StorageError, match="inconsistent"):
        make_store(tmp_path)

    assert _alembic_state(database) == (19, [HEAD], [])


def test_sentinel_without_an_alembic_revision_is_refused_as_inconsistent(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE incidents (bundle_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {ALEMBIC_SENTINEL}")

    with pytest.raises(StorageError, match="inconsistent"):
        make_store(tmp_path)

    assert _alembic_state(database) == (ALEMBIC_SENTINEL, None, [])


def test_legacy_binary_refuses_a_catalog_managed_by_alembic(tmp_path) -> None:
    make_store(tmp_path).close()
    database = tmp_path / "earshot.sqlite3"
    before = sqlite_catalog_snapshot(database)

    # storage_legacy holds the 0.1.x converge verbatim, including its version guard.
    connection = SQLiteDatabase(str(database)).connect()
    try:
        with pytest.raises(StorageError, match="schema is newer than this binary"):
            converge_legacy_catalog(connection)
    finally:
        connection.close()

    assert sqlite_catalog_snapshot(database) == before
    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])


def test_migration_refuses_to_commit_foreign_key_violations(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    _write_legacy_v19_catalog(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO analyses VALUES ('missing-incident', 'legacy', 'digest', '1', X'7B7D')"
        )

    with pytest.raises(CatalogIntegrityError, match="analyses"):
        make_store(tmp_path)

    # The converge had committed; the stamp transaction rolled back as a whole.
    assert _alembic_state(database) == (19, None, ["incidents_track_ingest_order"])


def _open_store_in_subprocess(data_dir: Path, **environment: str) -> int:
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"EARSHOT_TEST_MODE", "EARSHOT_TEST_CRASH_AT"}
    }
    child_environment.update(environment)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from earshot.storage import IncidentStore\n"
            "IncidentStore(sys.argv[1]).close()\n",
            str(data_dir),
        ],
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode


def test_crash_after_legacy_bootstrap_is_completed_by_the_next_open(tmp_path) -> None:
    database = tmp_path / "earshot.sqlite3"
    _write_legacy_v19_catalog(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO projects(project_id, display_name, created_at_unix_nano) "
            "VALUES ('legacy-tenant', 'Legacy tenant', 1)"
        )

    returncode = _open_store_in_subprocess(
        tmp_path,
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_CRASH_AT="after_legacy_bootstrap",
    )

    assert returncode == 137
    # The converge committed; nothing after it did.
    assert _alembic_state(database) == (19, None, ["incidents_track_ingest_order"])

    make_store(tmp_path).close()
    make_store(tmp_path).close()

    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])
    assert _query(database, "SELECT key, value FROM earshot_meta") == [("legacy_bootstrap", "1")]
    assert _query(
        database, "SELECT display_name FROM projects WHERE project_id = ?", ("legacy-tenant",)
    ) == [("Legacy tenant",)]


# Only the exact value "1" arms a crash point; "true" must not.
@pytest.mark.parametrize("test_mode", [None, "true"])
def test_crash_points_are_inert_without_test_mode(tmp_path, test_mode: str | None) -> None:
    database = tmp_path / "earshot.sqlite3"
    _write_legacy_v19_catalog(database)
    environment = {"EARSHOT_TEST_CRASH_AT": "after_legacy_bootstrap"}
    if test_mode is not None:
        environment["EARSHOT_TEST_MODE"] = test_mode

    assert _open_store_in_subprocess(tmp_path, **environment) == 0

    assert _alembic_state(database) == (ALEMBIC_SENTINEL, [HEAD], [])


def test_db_commands_report_and_apply_the_alembic_revision(tmp_path, capsys) -> None:
    assert main(["db", "current", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": None,
        "state": "empty",
        "user_version": 0,
    }
    assert not (tmp_path / "earshot.sqlite3").exists()

    assert main(["db", "upgrade", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": HEAD,
        "revision_before": None,
    }

    assert main(["db", "current", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": HEAD,
        "state": "current",
        "user_version": ALEMBIC_SENTINEL,
    }


def test_db_current_reports_a_legacy_catalog_without_touching_it(tmp_path, capsys) -> None:
    _write_legacy_v19_catalog(tmp_path / "earshot.sqlite3")

    assert main(["db", "current", "--data-dir", str(tmp_path)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "head": HEAD,
        "revision": None,
        "state": "legacy",
        "user_version": 19,
    }
    assert _alembic_state(tmp_path / "earshot.sqlite3")[:2] == (19, None)


def test_serve_without_auto_migrate_refuses_a_catalog_that_is_behind(tmp_path, capsys) -> None:
    _write_legacy_v19_catalog(tmp_path / "earshot.sqlite3")

    assert main(["serve", "--no-auto-migrate", "--data-dir", str(tmp_path)]) == 2

    error = capsys.readouterr().err
    assert "EARSHOT_SCHEMA_BEHIND" in error
    assert "earshot db upgrade" in error
    assert _alembic_state(tmp_path / "earshot.sqlite3")[:2] == (19, None)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_serve_without_auto_migrate_starts_once_the_catalog_is_upgraded(tmp_path) -> None:
    cli = [sys.executable, "-m", "earshot.cli"]
    data_dir = str(tmp_path / "data")
    subprocess.run([*cli, "db", "upgrade", "--data-dir", data_dir], check=True, timeout=60)
    port = _free_port()
    server = subprocess.Popen(
        [
            *cli,
            "serve",
            "--no-auto-migrate",
            "--data-dir",
            data_dir,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline and server.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as reply:
                    status = reply.status
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
        assert status == 200, server.stderr.read() if server.poll() is not None else "timeout"
    finally:
        server.terminate()
        server.wait(timeout=10)
