"""Alembic owns the catalog schema; this module configures and runs it in code.

There is no ``alembic.ini``, no ``env.py`` and no ``.mako`` template: revisions are
plain ``.py`` files under ``versions/`` and are run here on a connection storage
provides.

Opening a SQLite catalog (``prepare_sqlite_catalog``) follows one table of states:

=========================================  ==========================================
catalog                                    action
=========================================  ==========================================
``alembic_version`` this binary knows      upgrade to head if behind
``alembic_version`` it does not know       refuse: ``EARSHOT_SCHEMA_TOO_NEW``
no schema objects, ``user_version`` 0      ``upgrade head`` (a new catalog)
no ``alembic_version``, version 0 to 19    0.1.x catalog: run the frozen converge, then
                                           stamp ``0001_v19_baseline``, mark
                                           ``legacy_bootstrap`` and upgrade to head
``user_version`` 20 to 999                 refuse: a newer 0.1.x-style schema
anything else                              refuse: the catalog is inconsistent
=========================================  ==========================================

Every catalog Alembic manages has ``PRAGMA user_version = 1000``. The sentinel is
written in the same transaction as the revision, so any 0.1.x binary refuses the
file with its existing "newer than this binary" error.

Migrations run on their own connection with foreign keys OFF, so a table rebuild
cannot cascade into child rows, inside one ``BEGIN IMMEDIATE`` transaction that
commits only if ``PRAGMA foreign_key_check`` finds nothing. The frozen converge is
the exception: it keeps the connection 0.1.x always gave it (foreign keys ON). It
never drops or renames a parent table, and its result passes the same check before
the stamp commits.

A Postgres catalog (``prepare_postgres_catalog``) has no ``user_version`` and no
0.1.x past: ``alembic_version`` is its only guard. An empty database is created
by ``upgrade head``; a known revision is upgraded to head; an unknown revision is
refused with ``EARSHOT_SCHEMA_TOO_NEW``; tables without ``alembic_version`` are
refused as a database Earshot did not create. Every upgrade is one transaction
(Postgres DDL is transactional) under a transaction-level advisory lock, and the
store calls it under ``K_write`` as well.

Importing this module imports SQLAlchemy and Alembic (the ``server`` extra).
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from ..crash_points import crash_point
from ..storage import (
    CatalogIntegrityError,
    SchemaBehindError,
    SchemaTooNewError,
    StorageError,
)
from ..storage_legacy import converge_legacy_catalog
from ..storage_schema import v19_baseline_metadata

if TYPE_CHECKING:
    from ..storage_database import SQLiteDatabase
    from ..storage_postgres import PostgresDatabase

ALEMBIC_SENTINEL_USER_VERSION = 1000
# Columns ``0004_calls`` gained when it was amended in place, before 0.2.0 shipped. A
# catalog an unreleased build stamped at ``0004_calls`` lacks them, and Alembic sees
# nothing to do; without this check every write would fail on a missing column.
AMENDED_0004_COLUMNS = (("spans", "text_json"), ("calls", "revision_cap_counted"))
UNRELEASED_0004_MESSAGE = "catalog created by an unreleased 0.2.0 build; recreate it"
LEGACY_MAX_USER_VERSION = 19
BASELINE_REVISION = "0001_v19_baseline"
LEGACY_BOOTSTRAP_KEY = "legacy_bootstrap"
_LEGACY_TRIGGER = "incidents_track_ingest_order"


@dataclass(frozen=True, slots=True)
class CatalogState:
    """What a catalog says about its own schema, read without changing it."""

    user_version: int
    has_schema: bool
    revisions: tuple[str, ...] | None  # ``None`` when there is no alembic_version table

    def describe(self, head: str) -> str:
        if self.revisions is not None:
            return "current" if self.revisions == (head,) else "behind"
        if self.user_version == 0 and not self.has_schema:
            return "empty"
        if self.user_version <= LEGACY_MAX_USER_VERSION:
            return "legacy"
        return "unsupported"


@functools.cache
def _script_directory() -> ScriptDirectory:
    return ScriptDirectory(str(Path(__file__).resolve().parent))


def head_revision() -> str:
    head = _script_directory().get_current_head()
    if head is None:  # pragma: no cover - the package always ships its baseline
        raise StorageError("the storage migration package has no revisions")
    return head


def _known_revisions() -> frozenset[str]:
    return frozenset(script.revision for script in _script_directory().walk_revisions())


def read_catalog_state(connection: sqlite3.Connection) -> CatalogState:
    """Read the version markers of an open SQLite catalog (no writes)."""

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    revisions = None
    if "alembic_version" in names:
        revisions = tuple(
            sorted(
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version").fetchall()
            )
        )
    return CatalogState(user_version=user_version, has_schema=bool(names), revisions=revisions)


def inspect_sqlite_catalog(database: SQLiteDatabase, *, read_only: bool) -> CatalogState:
    """Read a catalog's state without creating the file or changing it.

    ``read_only`` opens a file catalog with ``mode=ro`` (``earshot db current``);
    otherwise the reading connection is an ordinary catalog connection, the one
    the store is about to migrate with.
    """

    if not database.uri:
        path = Path(database.path)
        if not path.is_file() or path.stat().st_size == 0:
            return CatalogState(user_version=0, has_schema=False, revisions=None)
    if read_only and not database.uri:
        connection = sqlite3.connect(
            Path(database.path).as_uri() + "?mode=ro", timeout=10.0, uri=True
        )
    else:
        connection = database.connect()
    try:
        return read_catalog_state(connection)
    finally:
        connection.close()


def prepare_sqlite_catalog(database: SQLiteDatabase, *, auto_migrate: bool) -> None:
    """Bring a SQLite catalog to the head revision, or refuse it.

    The caller holds the store lock, which serializes this against every other
    store on the same data directory.
    """

    state = inspect_sqlite_catalog(database, read_only=False)
    head = head_revision()
    if state.revisions is not None:
        unknown = sorted(set(state.revisions) - _known_revisions())
        if unknown:
            raise SchemaTooNewError(
                f"catalog revision {', '.join(unknown)} is not known to this Earshot "
                "binary; run a release that includes it"
            )
        if state.user_version != ALEMBIC_SENTINEL_USER_VERSION:
            raise StorageError(
                "incident catalog is inconsistent: it has an Alembic revision but "
                f"user_version {state.user_version}; it was modified outside Earshot"
            )
        if state.revisions == (head,):
            return
        _require_auto_migrate(auto_migrate, state.describe(head))
        _migrate(database, legacy_bootstrap=False)
        return
    if LEGACY_MAX_USER_VERSION < state.user_version < ALEMBIC_SENTINEL_USER_VERSION:
        raise StorageError("incident database schema is newer than this binary")
    if state.user_version >= ALEMBIC_SENTINEL_USER_VERSION:
        raise StorageError(
            "incident catalog is inconsistent: it claims Alembic management but has "
            "no alembic_version table"
        )
    _require_auto_migrate(auto_migrate, state.describe(head))
    if state.has_schema:
        connection = database.connect()
        try:
            converge_legacy_catalog(connection)
        finally:
            connection.close()
        crash_point("after_legacy_bootstrap")
    _migrate(database, legacy_bootstrap=state.has_schema)


def _require_amended_0004(columns: Callable[[str], set[str]]) -> None:
    missing = [
        f"{table}.{column}"
        for table, column in AMENDED_0004_COLUMNS
        if column not in columns(table)
    ]
    if missing:
        raise CatalogIntegrityError(f"{UNRELEASED_0004_MESSAGE} (it has no {', '.join(missing)})")


def require_sqlite_head_shape(database: SQLiteDatabase) -> None:
    """Refuse a migrated SQLite catalog that lacks a column of the amended 0004."""

    connection = database.connect()
    try:
        _require_amended_0004(
            lambda table: {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
        )
    finally:
        connection.close()


def require_postgres_head_shape(database: PostgresDatabase) -> None:
    """Refuse a migrated Postgres catalog that lacks a column of the amended 0004."""

    with database.engine.connect() as connection:
        try:
            _require_amended_0004(
                lambda table: {
                    str(row[0])
                    for row in connection.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND table_name = :table"
                        ),
                        {"table": table},
                    )
                }
            )
        finally:
            connection.rollback()


def _require_auto_migrate(auto_migrate: bool, described_state: str) -> None:
    if not auto_migrate:
        raise SchemaBehindError(
            f"the catalog schema is {described_state}, behind this binary; run "
            "`earshot db upgrade`, or start without --no-auto-migrate"
        )


def _migrate(database: SQLiteDatabase, *, legacy_bootstrap: bool) -> None:
    """Run every outstanding step in one write transaction, or none of them."""

    raw = database.connect_for_migration()
    try:
        # The journal mode is persistent and cannot change inside a transaction.
        raw.execute("PRAGMA journal_mode = WAL")
        engine = sa.create_engine("sqlite://", creator=lambda: raw, poolclass=sa.pool.StaticPool)
        sa.event.listen(
            engine,
            "begin",
            lambda connection: connection.exec_driver_sql("BEGIN IMMEDIATE"),
        )
        try:
            with engine.connect() as connection, connection.begin():
                if legacy_bootstrap:
                    _finish_legacy_bootstrap(connection)
                _upgrade_to_head(connection)
                connection.exec_driver_sql(f"PRAGMA user_version = {ALEMBIC_SENTINEL_USER_VERSION}")
                _require_foreign_key_integrity(connection)
        finally:
            engine.dispose()
    finally:
        raw.close()


def _finish_legacy_bootstrap(connection: sa.Connection) -> None:
    """Turn a converged schema-v19 catalog into the ``0001_v19_baseline`` schema."""

    # 0001 replaced the trigger with an explicit insert in ``ingest``.
    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_LEGACY_TRIGGER}")
    v19_baseline_metadata().tables["earshot_meta"].create(connection, checkfirst=True)
    context = MigrationContext.configure(connection)
    # A concurrent opener may already have stamped (and upgraded) this catalog in
    # the transaction this one waited for; never move its revision backwards.
    if not context.get_current_heads():
        context.stamp(_script_directory(), BASELINE_REVISION)
    connection.exec_driver_sql(
        "INSERT INTO earshot_meta(key, value) VALUES (?, '1') ON CONFLICT(key) DO NOTHING",
        (LEGACY_BOOTSTRAP_KEY,),
    )


def _upgrade_to_head(connection: sa.Connection) -> None:
    script = _script_directory()
    context = MigrationContext.configure(
        connection,
        opts={"fn": lambda heads, _context: script._upgrade_revs("head", heads)},
    )
    with Operations.context(context):
        context.run_migrations()


def _require_foreign_key_integrity(connection: sa.Connection) -> None:
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({str(row[0]) for row in violations})
        raise CatalogIntegrityError(
            f"catalog migration found {len(violations)} foreign key violation(s) in "
            f"{', '.join(tables)}; nothing was committed"
        )


def current_sqlite_revision(database: SQLiteDatabase) -> dict[str, object]:
    """Describe a catalog's revision for ``earshot db current`` without changing it."""

    state = inspect_sqlite_catalog(database, read_only=True)
    head = head_revision()
    revision = None
    if state.revisions:
        revision = state.revisions[0] if len(state.revisions) == 1 else list(state.revisions)
    return {
        "head": head,
        "revision": revision,
        "state": state.describe(head),
        "user_version": state.user_version,
    }


@dataclass(frozen=True, slots=True)
class PostgresCatalogState:
    """What a Postgres catalog says about its schema, read without changing it."""

    has_schema: bool
    revisions: tuple[str, ...] | None  # ``None`` when there is no alembic_version table

    def describe(self, head: str) -> str:
        if self.revisions is not None:
            return "current" if self.revisions == (head,) else "behind"
        return "unsupported" if self.has_schema else "empty"


def _read_postgres_state(connection: sa.Connection) -> PostgresCatalogState:
    names = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT c.relname FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'S')"
        )
    }
    revisions = None
    if "alembic_version" in names:
        revisions = tuple(
            sorted(
                row[0]
                for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
            )
        )
    return PostgresCatalogState(has_schema=bool(names), revisions=revisions)


def inspect_postgres_catalog(database: PostgresDatabase) -> PostgresCatalogState:
    with database.engine.connect() as connection:
        try:
            return _read_postgres_state(connection)
        finally:
            connection.rollback()


def _check_postgres_state(state: PostgresCatalogState, *, auto_migrate: bool) -> bool:
    """Refuse a catalog this binary must not touch; return whether it needs migrating."""

    head = head_revision()
    if state.revisions is not None:
        unknown = sorted(set(state.revisions) - _known_revisions())
        if unknown:
            raise SchemaTooNewError(
                f"catalog revision {', '.join(unknown)} is not known to this Earshot "
                "binary; run a release that includes it"
            )
        if state.revisions == (head,):
            return False
    elif state.has_schema:
        raise StorageError(
            "the database has tables but no alembic_version; Earshot only opens a "
            "database it created (use an empty database)"
        )
    _require_auto_migrate(auto_migrate, state.describe(head))
    return True


def prepare_postgres_catalog(database: PostgresDatabase, *, auto_migrate: bool) -> None:
    """Bring a Postgres catalog to the head revision, or refuse it.

    The upgrade is one transaction holding a transaction-level advisory lock, and
    it re-reads the catalog under that lock, so concurrent openers apply each
    revision exactly once.
    """

    if not _check_postgres_state(inspect_postgres_catalog(database), auto_migrate=auto_migrate):
        return
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_k_migrate()})")
        if not _check_postgres_state(_read_postgres_state(connection), auto_migrate=auto_migrate):
            return
        _upgrade_to_head(connection)


def _k_migrate() -> int:
    from ..storage_postgres import K_MIGRATE

    return K_MIGRATE


def current_postgres_revision(database: PostgresDatabase) -> dict[str, object]:
    """Describe a Postgres catalog's revision for ``earshot db current``."""

    state = inspect_postgres_catalog(database)
    head = head_revision()
    revision = None
    if state.revisions:
        revision = state.revisions[0] if len(state.revisions) == 1 else list(state.revisions)
    return {"head": head, "revision": revision, "state": state.describe(head)}
