"""Store construction for tests, shared by both test trees.

Tests that exercise storage behaviour build their store with ``make_store(path)``
instead of ``IncidentStore(path)``, so one pytest option, ``--db``, chooses the
backend. The repository-root ``conftest.py`` loads this module as a plugin, which
registers ``--db`` once whichever test path pytest is given.

``--db sqlite`` (the default)
    ``make_store(path)`` is ``IncidentStore(path, **kwargs)``. Tests marked
    ``postgres_only`` are skipped.
``--db postgresql``
    ``make_store(path)`` opens a Postgres store in the database
    ``t_<sha1(path)[:16]>``, cloned on first use from a template database this
    session migrated once. The same ``path`` reopens the same database, so restart
    tests work unchanged. Tests marked ``sqlite_only`` are skipped. Every database
    this session created is dropped ``WITH (FORCE)`` when it ends.
``--db all``
    Every test in a module that uses ``make_store`` (and every test marked
    ``sqlite_only`` or ``postgres_only``) runs twice, as ``[sqlite]`` and
    ``[postgresql]``, with the same skips. Other tests run once.

The Postgres server is ``EARSHOT_TEST_POSTGRES_URL`` (an admin URL that may
``CREATE DATABASE``), by default the ``earshot-dev-postgres`` container. URLs are
never printed: they carry a password.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from earshot.storage import IncidentStore

DB_CHOICES = ("sqlite", "postgresql", "all")
DIALECTS = ("sqlite", "postgresql")
POSTGRES_URL_ENVIRONMENT = "EARSHOT_TEST_POSTGRES_URL"
# The local development container (``earshot-dev-postgres``); not a deployment secret.
DEFAULT_POSTGRES_URL = "postgresql://earshot:earshot@127.0.0.1:55460/earshot"
# Every Postgres test store uses this key, so reopening a path keeps its identity.
TEST_CORRELATION_KEY = hashlib.sha256(b"earshot test correlation key").digest()
TEST_CORRELATION_KEY_BASE64 = base64.b64encode(TEST_CORRELATION_KEY).decode("ascii")

_current_dialect = "sqlite"
_open_postgres_stores: list[IncidentStore] = []
_open_postgres_stores_lock = threading.Lock()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--db",
        choices=DB_CHOICES,
        default="sqlite",
        help=(
            "storage backend that make_store() builds: sqlite (default), postgresql, "
            "or all (storage modules run once per backend)"
        ),
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if metafunc.config.getoption("db") != "all":
        return
    if "_earshot_db" not in metafunc.fixturenames:
        return
    dialect_marked = any(
        metafunc.definition.get_closest_marker(name) for name in ("sqlite_only", "postgres_only")
    )
    if dialect_marked or getattr(metafunc.module, "make_store", None) is make_store:
        metafunc.parametrize("_earshot_db", DIALECTS, indirect=True, ids=DIALECTS)


@pytest.fixture(autouse=True)
def _earshot_db(request: pytest.FixtureRequest) -> Iterator[str]:
    """Select this test's backend for ``make_store``, apply the dialect skips,
    and close the Postgres stores the test left open (their pools hold sessions)."""

    global _current_dialect
    selected = request.config.getoption("db")
    dialect = getattr(request, "param", "sqlite" if selected == "all" else selected)
    if dialect == "postgresql" and request.node.get_closest_marker("sqlite_only"):
        pytest.skip("sqlite_only: reaches into SQLite or file-CAS internals")
    if dialect == "sqlite" and request.node.get_closest_marker("postgres_only"):
        pytest.skip("postgres_only: run with --db postgresql or --db all")
    previous, _current_dialect = _current_dialect, dialect
    try:
        yield dialect
    finally:
        _current_dialect = previous
        _close_open_postgres_stores()


def make_store(path: str | Path, **kwargs: Any) -> IncidentStore:
    """Build the store a test runs against: one store per ``path``.

    Reopening the same ``path`` reopens the same store, as ``IncidentStore`` does.
    """

    if _current_dialect == "sqlite":
        return IncidentStore(path, **kwargs)
    unsupported = sorted(set(kwargs) - {"auto_migrate", "clock"})
    if unsupported:
        raise TypeError(
            f"make_store under Postgres takes no {', '.join(unsupported)}; "
            "a test that needs it is sqlite_only"
        )
    store = IncidentStore(
        path,
        database_url=postgres_cluster().database_for(path),
        correlation_key=TEST_CORRELATION_KEY,
        **kwargs,
    )
    with _open_postgres_stores_lock:
        _open_postgres_stores.append(store)
    return store


def current_dialect() -> str:
    """The backend ``make_store`` builds for the running test."""

    return _current_dialect


@contextlib.contextmanager
def catalog(path: str | Path) -> Iterator[Any]:
    """A raw connection to the catalog of the store at ``path``, on its dialect.

    For tests that read or rewrite catalog rows directly. SQL uses qmark binds on
    both dialects. The connection commits on a clean exit and rolls back on an
    exception; on Postgres it also holds ``K_write``, so it never interleaves with
    a store mutation.
    """

    if _current_dialect == "sqlite":
        from earshot.storage_database import SQLiteDatabase

        connection = SQLiteDatabase(str(Path(path) / "earshot.sqlite3")).connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
        return
    from earshot.storage_postgres import PostgresDatabase

    database = PostgresDatabase(postgres_cluster().database_for(path), pool_size=2)
    try:
        with database.write_lock(), database.connect() as connection:
            connection.begin_write()
            yield connection
    finally:
        database.close()


def catalog_rows(path: str | Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple]:
    """Run one query on the catalog of the store at ``path``; rows as plain tuples."""

    with catalog(path) as connection:
        return [tuple(row) for row in connection.execute(sql, parameters).fetchall()]


def stored_needles(path: str | Path, needles: list[bytes]) -> set[bytes]:
    """The needles present anywhere in the stored catalog of the store at ``path``.

    On SQLite: every byte of the database file, its WAL and its shared memory
    (close the store first, so nothing is only in memory). On Postgres: every
    ``text`` and ``bytea`` column of the call and projection tables, ``incidents``
    and ``artifacts``.
    """

    path = Path(path)
    if _current_dialect == "sqlite":
        data = b"".join(
            candidate.read_bytes()
            for candidate in (
                path / "earshot.sqlite3",
                path / "earshot.sqlite3-wal",
                path / "earshot.sqlite3-shm",
            )
            if candidate.exists()
        )
        return {needle for needle in needles if needle in data}
    import psycopg

    from earshot.storage_schema import CALLS_TABLES, PROJECTION_TABLES

    found = set()
    with psycopg.connect(postgres_cluster().database_for(path)) as connection:
        columns = connection.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY(%s) "
            "AND data_type IN ('text', 'bytea')",
            ([*CALLS_TABLES, *PROJECTION_TABLES, "incidents", "artifacts"],),
        ).fetchall()
        assert len(columns) > 20
        for table, column, data_type in columns:
            for needle in needles:
                value = needle if data_type == "bytea" else needle.decode()
                test = "position(%s IN {c}) > 0" if data_type == "bytea" else "strpos({c}, %s) > 0"
                hit = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {test.format(c=column)} LIMIT 1", (value,)
                ).fetchone()
                if hit:
                    found.add(needle)
    return found


def drop_post_v19_objects(connection: Any) -> None:
    """Strip a current SQLite catalog of everything a 0.1.x (schema <= v19) one lacks.

    Tests that turn a current catalog back into a legacy one call this: the
    Alembic state (``alembic_version``, ``earshot_meta``) and what later
    revisions added. A new revision that touches a v19 table extends it here.
    """

    connection.execute("DROP TABLE alembic_version")
    connection.execute("DROP TABLE earshot_meta")
    connection.execute("DROP TABLE artifacts")  # 0002_artifacts
    columns = {row[1] for row in connection.execute("PRAGMA table_info(incidents)")}
    if "projection_version" in columns:  # 0003_projection_version
        connection.execute("DROP INDEX incidents_projection_version_idx")
        connection.execute("ALTER TABLE incidents DROP COLUMN projection_version")
    # 0004_calls (some legacy simulations rebuild or drop ``projects`` themselves)
    project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    drop_calls_objects(connection, preset_column="capture_preset" in project_columns)


def drop_calls_objects(connection: Any, *, preset_column: bool = True) -> None:
    """Undo revision ``0004_calls`` on a catalog of either dialect (children first),
    and before it every later revision built on its tables."""

    from earshot.storage_schema import CALLS_TABLES

    drop_projection_objects(connection)
    for table in reversed(CALLS_TABLES):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    if preset_column:
        connection.execute("ALTER TABLE projects DROP COLUMN capture_preset")


def _table_columns(connection: Any, table: str) -> set[str]:
    if isinstance(connection, sqlite3.Connection):
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    return {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
    }


def drop_projection_objects(connection: Any) -> None:
    """Undo revision ``0005_projections`` on a catalog of either dialect (children
    first), keeping every row of the tables it added a column to. A catalog without
    some of its objects (one a legacy simulation rebuilt) is fine."""

    from earshot.storage_schema import PROJECTION_COLUMNS, PROJECTION_TABLES

    drop_connector_objects(connection)
    for table in reversed(PROJECTION_TABLES):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in PROJECTION_COLUMNS:
        if column in _table_columns(connection, table):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def drop_connector_objects(connection: Any) -> None:
    """Undo revision ``0006_connectors`` on a catalog of either dialect, keeping every
    row of the tables it added a column to. A catalog without some of its objects
    (one a legacy simulation rebuilt) is fine."""

    from earshot.storage_schema import CONNECTOR_COLUMNS, CONNECTOR_TABLES

    for table in reversed(CONNECTOR_TABLES):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in CONNECTOR_COLUMNS:
        if column in _table_columns(connection, table):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _close_open_postgres_stores() -> None:
    with _open_postgres_stores_lock:
        stores = list(_open_postgres_stores)
        _open_postgres_stores.clear()
    for store in stores:
        with contextlib.suppress(Exception):
            store.close()


@pytest.fixture
def postgres_url(tmp_path: Path) -> str:
    """A migrated Postgres database of its own for this test (Postgres tests only)."""

    return postgres_cluster().database_for(tmp_path / "postgres-url")


@pytest.fixture
def empty_postgres_url(tmp_path: Path) -> str:
    """An empty Postgres database (no schema yet) of its own for this test."""

    return postgres_cluster().database_for(tmp_path / "empty-postgres-url", empty=True)


@pytest.fixture
def postgres_admin() -> Iterator[Any]:
    """An autocommit admin connection to the test server (``pg_terminate_backend`` etc.)."""

    import psycopg

    with psycopg.connect(postgres_cluster().admin_url, autocommit=True) as connection:
        yield connection


@dataclass
class _PostgresCluster:
    """The test Postgres server: one migrated template, one database per store path."""

    admin_url: str
    template: str = field(
        default_factory=lambda: f"earshot_tpl_{os.getpid()}_{secrets.token_hex(3)}"
    )
    databases: dict[str, str] = field(default_factory=dict)
    _template_ready: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def url_for(self, database: str) -> str:
        from sqlalchemy.engine import make_url

        return make_url(self.admin_url).set(database=database).render_as_string(hide_password=False)

    def database_for(self, path: str | Path, *, empty: bool = False) -> str:
        """The URL of the database for ``path``, created on first use.

        It is a clone of the migrated template, or an empty database with ``empty``.
        """

        key = str(Path(path).expanduser().resolve())
        with self._lock:
            name = self.databases.get(key)
            if name is None:
                name = f"t_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
                if empty:
                    self._admin(f'CREATE DATABASE "{name}"')
                else:
                    self._ensure_template()
                    self._admin(f'CREATE DATABASE "{name}" TEMPLATE "{self.template}"')
                self.databases[key] = name
        return self.url_for(name)

    def database_name(self, path: str | Path) -> str:
        return self.databases[str(Path(path).expanduser().resolve())]

    def _ensure_template(self) -> None:
        if self._template_ready:
            return
        from earshot.storage_migrations import prepare_postgres_catalog
        from earshot.storage_postgres import PostgresDatabase

        self._admin(f'CREATE DATABASE "{self.template}"')
        database = PostgresDatabase(self.url_for(self.template), pool_size=2)
        try:
            prepare_postgres_catalog(database, auto_migrate=True)
        finally:
            database.close()
        self._template_ready = True

    def _admin(self, statement: str) -> None:
        import psycopg

        try:
            with psycopg.connect(self.admin_url, autocommit=True, connect_timeout=10) as connection:
                connection.execute(statement)
        except psycopg.OperationalError as error:
            # The message names host and port, never the password.
            raise pytest.UsageError(
                f"the test Postgres server is unavailable ({POSTGRES_URL_ENVIRONMENT}): "
                f"{error.__class__.__name__}"
            ) from None

    def drop_all(self) -> None:
        names = [*self.databases.values(), *([self.template] if self._template_ready else [])]
        self.databases.clear()
        for name in names:
            with contextlib.suppress(Exception):
                self._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


_cluster: _PostgresCluster | None = None


def postgres_cluster() -> _PostgresCluster:
    global _cluster
    if _cluster is None:
        _cluster = _PostgresCluster(
            os.environ.get(POSTGRES_URL_ENVIRONMENT) or DEFAULT_POSTGRES_URL
        )
    return _cluster


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _close_open_postgres_stores()
    if _cluster is not None:
        _cluster.drop_all()
