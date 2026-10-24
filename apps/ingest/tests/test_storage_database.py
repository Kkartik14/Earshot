"""Contract of the dialect-neutral ``Database`` shim that storage SQL runs through.

The SQLite connection behaviour pinned here is today's, unchanged; the paramstyle
translation and the dialect hooks are what the Postgres runtime builds on.
"""

from __future__ import annotations

import sqlite3

import pytest

from earshot.storage_database import POSTGRESQL, SQLITE, SQLiteDatabase, qmark_to_pyformat
from incident_factory import make_valid_bundle
from store_support import make_store

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("qmark", "pyformat"),
    [
        ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
        # psycopg scans the whole statement for placeholders, quotes included, so
        # every literal percent sign must be doubled wherever it appears.
        ("SELECT '50%' WHERE x LIKE 'a%' AND y = ?", "SELECT '50%%' WHERE x LIKE 'a%%' AND y = %s"),
        # A question mark inside a string, identifier or comment is not a bind.
        (
            "SELECT 'why?', \"col?\" FROM t WHERE x = ?",
            "SELECT 'why?', \"col?\" FROM t WHERE x = %s",
        ),
        ("SELECT 'it''s ?' , ?", "SELECT 'it''s ?' , %s"),
        ("-- ok?\nSELECT ? /* and ? or %? */", "-- ok?\nSELECT %s /* and ? or %%? */"),
        ("SELECT 1", "SELECT 1"),
    ],
)
def test_qmark_binds_become_pyformat_binds_and_literal_percent_is_escaped(
    qmark: str, pyformat: str
) -> None:
    assert qmark_to_pyformat(qmark) == pyformat


def test_unterminated_literal_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        qmark_to_pyformat("SELECT 'open ? FROM t WHERE x = ?")


def test_dialect_hooks_keep_sqlite_text_and_fail_closed_until_postgres_variant_exists() -> None:
    sqlite_text = "DELETE FROM t WHERE rowid IN (SELECT rowid FROM t LIMIT ?)"

    assert SQLITE.sql("batched-delete", sqlite=sqlite_text) == sqlite_text
    assert POSTGRESQL.sql("batched-delete", sqlite=sqlite_text, postgresql="DELETE x") == (
        "DELETE x"
    )
    with pytest.raises(NotImplementedError, match=r"postgresql variant.*batched-delete"):
        POSTGRESQL.sql("batched-delete", sqlite=sqlite_text)
    SQLITE.only_sqlite("page-scrub")
    with pytest.raises(NotImplementedError, match=r"postgresql variant.*page-scrub"):
        POSTGRESQL.only_sqlite("page-scrub")


@pytest.mark.sqlite_only
def test_sqlite_connections_keep_the_durability_and_erasure_pragmas(tmp_path) -> None:
    database = SQLiteDatabase(str(tmp_path / "catalog.sqlite3"))

    connection = database.connect()
    try:
        pragmas = {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in (
                "foreign_keys",
                "busy_timeout",
                "synchronous",
                "secure_delete",
                "temp_store",
                "wal_autocheckpoint",
                "journal_size_limit",
            )
        }
    finally:
        connection.close()

    assert pragmas == {
        "foreign_keys": 1,
        "busy_timeout": 10000,
        "synchronous": 2,  # FULL
        "secure_delete": 1,
        "temp_store": 2,  # MEMORY
        "wal_autocheckpoint": 1000,
        "journal_size_limit": 0,
    }


@pytest.mark.sqlite_only
def test_connection_context_commits_on_clean_exit_and_rolls_back_on_error(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE notes (value TEXT NOT NULL)")
    database = SQLiteDatabase(str(path))

    with database.connect() as connection:
        connection.execute("INSERT INTO notes(value) VALUES (?)", ("implicit",))
    with database.connect() as connection:
        connection.begin_write()
        connection.execute("INSERT INTO notes(value) VALUES (?)", ("explicit",))
    with pytest.raises(RuntimeError, match="abort"), database.connect() as connection:
        connection.begin_write()
        connection.execute("INSERT INTO notes(value) VALUES (?)", ("rolled-back",))
        raise RuntimeError("abort")

    reader = database.connect()
    try:
        rows = reader.execute("SELECT value FROM notes ORDER BY rowid").fetchall()
    finally:
        reader.close()
    assert [(row[0], row["value"]) for row in rows] == [
        ("implicit", "implicit"),
        ("explicit", "explicit"),
    ]


@pytest.mark.sqlite_only
def test_purge_compaction_durably_records_the_scrubbed_generation(tmp_path) -> None:
    # The scrubbed_generation UPDATE has no explicit commit: it relies on the
    # connection context committing on a clean exit. If that commit were lost,
    # every later maintenance pass would VACUUM the whole catalog again.
    store = make_store(tmp_path)
    bundle = make_valid_bundle(bundle_id="scrubbed-generation")
    store.ingest(bundle)
    store.purge("scrubbed-generation")
    store.close()

    with sqlite3.connect(tmp_path / "earshot.sqlite3") as connection:
        purge_generation, scrubbed_generation = connection.execute(
            "SELECT purge_generation, scrubbed_generation FROM storage_maintenance"
        ).fetchone()

    assert purge_generation >= 1
    assert scrubbed_generation == purge_generation
