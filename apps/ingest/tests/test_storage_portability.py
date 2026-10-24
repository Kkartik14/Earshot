"""Storage behaviour that must be identical on SQLite and Postgres (``--db``).

Value widths, the shim's transaction contract and the "behind"
migration path, each through the public store API and the dialect's own catalog.
"""

from __future__ import annotations

import pytest

from earshot.storage import SchemaBehindError
from incident_factory import make_valid_bundle
from store_support import catalog, catalog_rows, drop_calls_objects, make_store

pytestmark = pytest.mark.integration

UINT64_MAX = "18446744073709551615"
WIDE_INTEGER_NANO = 2**62
PRECISE_MILLISECONDS = 0.1234567890123


def test_nanosecond_and_real_columns_keep_their_full_width(tmp_path) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="wide"))
    store.ingest(make_valid_bundle(bundle_id="short-lived"))
    with catalog(tmp_path) as connection:
        connection.execute(
            "UPDATE incidents SET created_at_unix_nano = ?, expires_at_unix_nano = ?, "
            "ingested_at_unix_nano = ? WHERE bundle_id = ?",
            (UINT64_MAX, UINT64_MAX, WIDE_INTEGER_NANO, "wide"),
        )
        # A three-digit deadline is long past; the twenty-digit one is far ahead.
        connection.execute(
            "UPDATE incidents SET expires_at_unix_nano = ? WHERE bundle_id = ?",
            ("999", "short-lived"),
        )
        connection.execute(
            "UPDATE turn_metrics SET first_token_ms = ? WHERE bundle_id = ?",
            (PRECISE_MILLISECONDS, "wide"),
        )

    record = store.get_record("wide")
    assert record.created_at_unix_nano == UINT64_MAX
    assert record.ingested_at_unix_nano == str(WIDE_INTEGER_NANO)
    assert catalog_rows(
        tmp_path,
        "SELECT ingested_at_unix_nano, expires_at_unix_nano FROM incidents WHERE bundle_id = ?",
        ("wide",),
    ) == [(WIDE_INTEGER_NANO, UINT64_MAX)]
    assert [fact.first_token_ms for fact in store.list_turn_facts()] == [PRECISE_MILLISECONDS]
    # The length-then-text comparison orders decimal deadlines like numbers.
    assert [item.bundle_id for item in store.list_incidents().items] == ["wide"]
    assert store.purge_expired() == 1
    assert catalog_rows(tmp_path, "SELECT bundle_id FROM incidents") == [("wide",)]


def test_catalog_connection_commits_on_clean_exit_and_rolls_back_on_error(tmp_path) -> None:
    make_store(tmp_path).close()
    keys = "SELECT key FROM earshot_meta WHERE key LIKE 'probe-%' ORDER BY key"

    with catalog(tmp_path) as connection:
        connection.execute("INSERT INTO earshot_meta(key, value) VALUES (?, ?)", ("probe-1", "1"))
    with pytest.raises(RuntimeError, match="abort"), catalog(tmp_path) as connection:
        connection.execute("INSERT INTO earshot_meta(key, value) VALUES (?, ?)", ("probe-2", "2"))
        raise RuntimeError("abort")

    assert catalog_rows(tmp_path, keys) == [("probe-1",)]


def test_a_catalog_one_revision_behind_is_upgraded_unless_auto_migrate_is_off(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="before-upgrade"))
    store.close()
    with catalog(tmp_path) as connection:  # back to 0002_artifacts
        drop_calls_objects(connection)
        connection.execute("DROP INDEX incidents_projection_version_idx")
        connection.execute("ALTER TABLE incidents DROP COLUMN projection_version")
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0002_artifacts",))

    with pytest.raises(SchemaBehindError, match="behind"):
        make_store(tmp_path, auto_migrate=False)
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="after-upgrade"))

    assert catalog_rows(tmp_path, "SELECT version_num FROM alembic_version") == [
        ("0006_connectors",)
    ]
    # A catalog Earshot created upgrades with its projects capturing in full.
    assert catalog_rows(tmp_path, "SELECT capture_preset FROM projects") == [("full",)]
    # An incident from before the revision is stale until it is re-projected.
    assert catalog_rows(
        tmp_path, "SELECT bundle_id, projection_version FROM incidents ORDER BY bundle_id"
    ) == [("after-upgrade", 1), ("before-upgrade", None)]
    assert store.projections_pending() == 1
