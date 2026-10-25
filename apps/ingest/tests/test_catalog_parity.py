"""One catalog schema on every dialect.

Two kinds of evidence:

- a dialect-aware **catalog snapshot** (``catalog_snapshot``, which documents every
  normalization rule). Fresh SQLite, SQLite upgraded from every real 0.1.x
  fixture (``fixtures/upgrade``), and fresh Postgres have one portable snapshot;
  the upgraded SQLite catalogs also match fresh SQLite in their native detail;
- **behaviour probes**, generated from the schema's own ``MetaData``: for every
  CHECK and every foreign key, a row that satisfies it is accepted and a row that
  violates it is refused, on the dialect under test (``--db``).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from catalog_snapshot import postgres_catalog_snapshot, sqlite_catalog_snapshot
from earshot.storage import IncidentStore
from earshot.storage_schema import (
    CALLS_TABLES,
    CONNECTOR_TABLES,
    PROJECTION_TABLES,
    add_calls_tables,
    add_connector_tables,
    add_projection_tables,
    build_v19_baseline_metadata,
    capture_preset_column,
    connector_columns,
    projection_columns,
)
from store_support import current_dialect, make_store, postgres_cluster
from upgrade_fixtures import copy_upgrade_fixture, upgrade_fixture_names

pytestmark = pytest.mark.integration


def _schema_metadata() -> sa.MetaData:
    """The head schema: the v19 baseline, revision 0002's ``artifacts``, 0004's calls,
    0005's projections and 0006's connector columns and fetches."""

    metadata = build_v19_baseline_metadata()
    sa.Table(
        "artifacts",
        metadata,
        sa.Column("digest", sa.Text(), primary_key=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("length(digest) = 64"),
    )
    metadata.tables["projects"].append_column(capture_preset_column())
    add_calls_tables(metadata)
    for table, column in projection_columns():
        metadata.tables[table].append_column(column)
    add_projection_tables(metadata)
    for table, column in connector_columns():
        metadata.tables[table].append_column(column)
    add_connector_tables(metadata)
    return metadata


SCHEMA = _schema_metadata()


# --- Snapshot -------------------------------------------------------------------------


@pytest.mark.postgres_only
def test_fresh_sqlite_legacy_upgraded_sqlite_and_fresh_postgres_have_one_schema(tmp_path) -> None:
    IncidentStore(tmp_path / "sqlite").close()
    make_store(tmp_path / "postgres").close()
    upgraded = {}
    for name in upgrade_fixture_names():
        data_dir = copy_upgrade_fixture(name, tmp_path / "upgraded" / name)
        IncidentStore(data_dir).close()
        upgraded[name] = sqlite_catalog_snapshot(data_dir / "earshot.sqlite3")

    fresh_sqlite = sqlite_catalog_snapshot(tmp_path / "sqlite" / "earshot.sqlite3")
    fresh_postgres = postgres_catalog_snapshot(
        postgres_cluster().database_for(tmp_path / "postgres")
    )

    assert fresh_postgres.portable == fresh_sqlite.portable
    for name, snapshot in upgraded.items():
        # One dialect: the native detail (declared types, raw NOT NULL) must match too.
        assert snapshot == fresh_sqlite, name
    assert (fresh_sqlite.portable["triggers"], fresh_sqlite.portable["views"]) == ([], [])
    assert set(fresh_sqlite.portable["tables"]) == {*SCHEMA.tables, "alembic_version"}
    # Revision 0004 adds its tables and one column without rebuilding a table, so
    # nothing the baseline declared (CHECKs, the expression index) is lost on the
    # way: the equalities above hold for them, and they are really there.
    tables = fresh_sqlite.portable["tables"]
    assert set(CALLS_TABLES) <= set(tables)
    assert "checkcapture_presetin'full','metadata_only'" in tables["projects"]["checks"]
    assert "checklifecycle_statein'active','deleting','deleted'" in tables["projects"]["checks"]
    assert any(
        index.startswith("createindexincidents_expiry_idx")
        for index in tables["incidents"]["indexes"]
    )
    assert any(
        "whereexpires_at_unix_nanoisnotnull" in index for index in tables["incidents"]["indexes"]
    )
    # Revision 0005 adds its tables and three columns the same way: the CHECKs on the
    # added columns are there on every catalog, and so is the timing shape of a
    # transcript line.
    assert set(PROJECTION_TABLES) <= set(tables)
    assert (
        "checkchannel_speakers_jsonin'[user]','[agent]','[user,agent]','[agent,user]'"
        in tables["recordings"]["checks"]
    )
    assert "checkprojection_seq>=0" in tables["calls"]["checks"]
    assert "checkprice_version>=0" in tables["projects"]["checks"]
    assert (
        "checktiming='measured'andstart_msisnotnullandend_msisnotnullor"
        "timing='start_only'andstart_msisnotnullandend_msisnullor"
        "timing='order_only'andstart_msisnullandend_msisnull"
    ) in tables["call_transcripts"]["checks"]
    assert [
        (key, action)
        for key, target, _, _, action in tables["audio_analysis"]["foreign_keys"]
        if target == "recordings"
    ] == [(("project_id", "recording_id"), "cascade")]
    # Revision 0006 adds its table and three columns the same way: a receipt's digest
    # kind is checked on every catalog, and a fetch goes with its recording and with
    # its connector.
    assert set(CONNECTOR_TABLES) <= set(tables)
    assert "checkdigest_kindin'body_v1','evidence_v1'" in tables["delivery_receipts"]["checks"]
    assert sorted(
        (key, target, action)
        for key, target, _, _, action in tables["recording_fetches"]["foreign_keys"]
    ) == [
        (("endpoint_id",), "connectors", "cascade"),
        (("project_id", "recording_id"), "recordings", "cascade"),
    ]


@pytest.mark.postgres_only
def test_postgres_columns_have_the_portable_widths(tmp_path) -> None:
    import psycopg

    make_store(tmp_path).close()
    with psycopg.connect(postgres_cluster().database_for(tmp_path)) as connection:
        declared = {
            (table, column): (kind, collation)
            for table, column, kind, collation in connection.execute(
                "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
                "co.collname FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_collation AS co ON co.oid = a.attcollation "
                "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND a.attnum > 0"
            )
        }
    nano = {key: value for key, value in declared.items() if key[1].endswith("_nano")}
    integer_nano = {key for key, (kind, _) in nano.items() if kind != "text"}
    decimal_nano = {key for key, (kind, _) in nano.items() if kind == "text"}

    # These expected counts cover the baseline plus columns added by revisions 0004 through 0006.
    assert (len(integer_nano), len(decimal_nano)) == (45, 14)
    assert {nano[key][0] for key in integer_nano} == {"bigint"}
    assert {nano[key][1] for key in decimal_nano} == {"C"}
    assert {kind for kind, _ in declared.values() if kind in {"real", "double precision"}} == {
        "double precision"
    }
    assert declared[("incident_ingest_order", "sequence")][0] == "bigint"


# --- Behaviour probes -----------------------------------------------------------------


@dataclass(frozen=True)
class _CheckRule:
    column: str
    valid: Any
    invalid: Any


def _check_rule(sqltext: str) -> _CheckRule:
    """The valid and violating value of one CHECK, read from its own text."""

    if match := re.fullmatch(r"(\w+) IN \((.+)\)", sqltext):
        items = [item.strip() for item in match[2].split(",")]
        if items[0].startswith("'"):
            return _CheckRule(match[1], items[0].strip("'"), "not-a-listed-value")
        return _CheckRule(match[1], int(items[0]), max(int(item) for item in items) + 1)
    if match := re.fullmatch(r"(\w+) >= (\d+)", sqltext):
        return _CheckRule(match[1], int(match[2]), int(match[2]) - 1)
    if match := re.fullmatch(r"length\((\w+)\) = (\d+)", sqltext):
        return _CheckRule(match[1], "a" * int(match[2]), "a" * (int(match[2]) - 1))
    if match := re.fullmatch(r"length\((\w+)\) >= (\d+) AND length\((\w+)\) <= (\d+)", sqltext):
        return _CheckRule(match[1], "a" * int(match[4]), "a" * (int(match[4]) + 1))
    if match := re.fullmatch(r"(\w+) = (\d+)", sqltext):
        return _CheckRule(match[1], int(match[2]), int(match[2]) + 1)
    # A row shape per value of one column, the first shape with every column set (as
    # a probe row is) and the last with some NULL: the last value violates it.
    shapes = r"\((\w+) = '(\w+)' AND .+\) OR .*\((\w+) = '(\w+)' AND .+\)"
    if match := re.fullmatch(shapes, sqltext):
        return _CheckRule(match[1], match[2], match[4])
    raise AssertionError(f"no probe rule for CHECK ({sqltext})")


def _checks(table: sa.Table) -> list[_CheckRule]:
    constraints = [
        constraint for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
    ]
    for column in table.columns:
        constraints.extend(
            constraint
            for constraint in column.constraints
            if isinstance(constraint, sa.CheckConstraint)
        )
    rules = [_check_rule(str(constraint.sqltext)) for constraint in constraints]
    # A length CHECK on a bytes column is probed with bytes of that length.
    return [
        _CheckRule(rule.column, rule.valid.encode(), rule.invalid.encode())
        if table.columns[rule.column].type.python_type is bytes
        else rule
        for rule in rules
    ]


CHECK_PROBES = [
    pytest.param(table.name, rule, id=f"{table.name}.{rule.column}")
    for table in SCHEMA.sorted_tables
    for rule in _checks(table)
]
FOREIGN_KEY_PROBES = [
    pytest.param(
        table.name,
        tuple(element.parent.name for element in constraint.elements),
        id=f"{table.name}({','.join(element.parent.name for element in constraint.elements)})"
        f"->{constraint.referred_table.name}",
    )
    for table in SCHEMA.sorted_tables
    for constraint in sorted(table.foreign_key_constraints, key=lambda item: item.column_keys)
]


# Tables whose one row the catalog is created with (``singleton = 1``).
_SINGLETON_TABLES = frozenset({"storage_maintenance", "builtin_price_version"})


class _RawCatalog:
    """Autocommit statements on a throwaway catalog, with foreign keys switchable."""

    def __init__(self, path: Path) -> None:
        self._sequence = 0
        if current_dialect() == "sqlite":
            self.connection: Any = sqlite3.connect(path / "earshot.sqlite3", isolation_level=None)
            self._bind = "?"
        else:
            import psycopg

            self.connection = psycopg.connect(
                postgres_cluster().database_for(path), autocommit=True
            )
            self._bind = "%s"

    def close(self) -> None:
        self.connection.close()

    def foreign_keys(self, enabled: bool) -> None:
        if current_dialect() == "sqlite":
            self.connection.execute(f"PRAGMA foreign_keys = {'ON' if enabled else 'OFF'}")
        else:
            # Postgres enforces foreign keys with internal triggers; "replica"
            # sessions skip them (the test role is a superuser).
            role = "origin" if enabled else "replica"
            self.connection.execute(f"SET session_replication_role = {role}")

    def insert(self, table: str, row: dict[str, Any]) -> None:
        names = ", ".join(row)
        binds = ", ".join(self._bind for _ in row)
        self.connection.execute(
            f"INSERT INTO {table} ({names}) VALUES ({binds})", tuple(row.values())
        )

    def refused(self, table: str, row: dict[str, Any]) -> str:
        """Insert a row that must be refused; return the constraint kind that refused it."""

        try:
            self.insert(table, row)
        except sqlite3.IntegrityError as error:
            message = str(error)
            return (
                "check"
                if "CHECK" in message
                else "foreign key"
                if "FOREIGN KEY" in message
                else message
            )
        except Exception as error:
            sqlstate = getattr(error, "sqlstate", None)
            return {"23514": "check", "23503": "foreign key"}.get(sqlstate, repr(error))
        return "accepted"

    def valid_row(self, table: sa.Table) -> dict[str, Any]:
        """A row with every column set, unique keys, and every CHECK satisfied."""

        self._sequence += 1
        row: dict[str, Any] = {}
        for column in table.columns:
            python_type = column.type.python_type
            if python_type is int:
                row[column.name] = 1_000 + self._sequence
            elif python_type is float:
                row[column.name] = 1.5
            elif python_type is bytes:
                row[column.name] = b"probe"
            else:
                row[column.name] = f"probe-{self._sequence}-{column.name}"
        for rule in _checks(table):
            row[rule.column] = rule.valid
            if isinstance(rule.valid, str) and set(rule.valid) == {"a"}:
                # A fixed-length value may be (part of) a key: keep it unique.
                row[rule.column] = str(self._sequence).zfill(len(rule.valid))
            elif isinstance(rule.valid, bytes) and set(rule.valid) == {ord("a")}:
                row[rule.column] = str(self._sequence).zfill(len(rule.valid)).encode()
        if table.name in _SINGLETON_TABLES:
            self.connection.execute(f"DELETE FROM {table.name}")
        return row

    def give_parents(self, table: sa.Table, row: dict[str, Any]) -> None:
        """Insert (foreign keys off) a parent row for every foreign key of ``row``."""

        self.foreign_keys(False)
        for constraint in table.foreign_key_constraints:
            parent_table = constraint.referred_table
            parent = self.valid_row(parent_table)
            for element in constraint.elements:
                parent[element.column.name] = row[element.parent.name]
            self.insert(parent_table.name, parent)
        self.foreign_keys(True)


@pytest.fixture(scope="module")
def upgraded_catalog(tmp_path_factory) -> Path:
    """The oldest real 0.1.x catalog (schema v10), upgraded to head once per module."""

    name = upgrade_fixture_names()[0]
    data_dir = copy_upgrade_fixture(name, tmp_path_factory.mktemp("upgraded") / name)
    IncidentStore(data_dir).close()
    return data_dir / "earshot.sqlite3"


@pytest.fixture(params=["fresh", "upgraded"])
def raw_catalog(request, tmp_path):
    """A catalog to probe: a fresh one on the dialect under test, or (SQLite) an
    upgraded legacy one, with its rows."""

    if request.param == "fresh":
        make_store(tmp_path).close()
    elif current_dialect() == "sqlite":
        with (
            closing(sqlite3.connect(request.getfixturevalue("upgraded_catalog"))) as source,
            closing(sqlite3.connect(tmp_path / "earshot.sqlite3")) as copy,
        ):
            source.backup(copy)
    else:
        pytest.skip("a legacy catalog is a SQLite file")
    catalog = _RawCatalog(tmp_path)
    try:
        yield catalog
    finally:
        catalog.close()


@pytest.mark.parametrize(("table_name", "rule"), CHECK_PROBES)
def test_every_check_refuses_a_violating_row(raw_catalog, table_name, rule) -> None:
    table = SCHEMA.tables[table_name]
    raw_catalog.foreign_keys(False)

    accepted = raw_catalog.valid_row(table)
    raw_catalog.insert(table_name, accepted)
    violating = raw_catalog.valid_row(table)
    violating[rule.column] = rule.invalid

    assert raw_catalog.refused(table_name, violating) == "check"


@pytest.mark.parametrize(("table_name", "columns"), FOREIGN_KEY_PROBES)
def test_every_foreign_key_refuses_a_missing_parent(raw_catalog, table_name, columns) -> None:
    table = SCHEMA.tables[table_name]
    accepted = raw_catalog.valid_row(table)
    raw_catalog.give_parents(table, accepted)
    raw_catalog.insert(table_name, accepted)

    orphan = raw_catalog.valid_row(table)
    raw_catalog.give_parents(table, orphan)
    value = orphan[columns[-1]]
    orphan[columns[-1]] = (
        "no-such-parent"
        if isinstance(value, str)
        else b"\xff" * len(value)  # a valid length, no parent
        if isinstance(value, bytes)
        else 999_999
    )

    assert raw_catalog.refused(table_name, orphan) == "foreign key"
