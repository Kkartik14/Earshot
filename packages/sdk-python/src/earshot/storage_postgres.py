"""The Postgres runtime behind the ``Database`` shim (``postgresql://`` catalogs).

Storage SQL is hand-written with qmark binds and runs unchanged on both dialects.
On Postgres it runs on raw psycopg 3 connections that a SQLAlchemy pool hands out:

- every statement is translated from ``?`` to ``%s`` (literal ``%`` escaped) and
  executed with binary results, so ``bytea`` travels as bytes rather than hex;
- rows support index and key access, like ``sqlite3.Row``;
- the connection is the shim's own object. As a context manager it commits on a
  clean exit, rolls back on an exception, and then returns the connection to the
  pool. The pool's proxy connection is never handed out;
- psycopg errors become ``DatabaseError`` (a ``StorageError``), and connection,
  pool, lock and serialization failures become ``DatabaseUnavailableError``, so
  the API answers them with its storage-unavailable 503.

Two session advisory locks replace SQLite's lock files:

- ``K_write`` is held by the outermost ``IncidentStore._mutation()`` of any process
  (server or CLI) for the whole mutation, as the per-mutation ``flock`` is on
  SQLite. It lives on a dedicated connection per store, outside the pool, so the
  pool only serves statements;
- ``K_server`` is held by ``serve`` for its lifetime, on its own keepalive
  connection (``PostgresServerLock``). A second server is refused.

Both lock sessions ask Postgres for short server-side TCP keepalives, so a
holder that vanishes without closing its connection (a lost node, a cut
network) releases its lock in seconds rather than after the OS default of two
hours idle.

Importing this module imports SQLAlchemy and psycopg (the ``server`` and
``postgres`` extras); storage imports it only for a ``postgresql://`` URL.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import logging
import os
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg
import sqlalchemy as sa
from psycopg import pq

from .server_config import (
    DB_POOL_SIZE_ENVIRONMENT,
    DEFAULT_DB_POOL_SIZE,
    MINIMUM_DB_POOL_SIZE,
    ConfigurationError,
    parse_pool_size,
)
from .storage import (
    LOCK_POLL_SECONDS,
    DatabaseError,
    DatabaseInUseError,
    DatabaseLockLostError,
    DatabaseUnavailableError,
    StorageBusyError,
    StorageError,
    StorageOpenError,
)
from .storage_database import POSTGRESQL, qmark_to_pyformat

_LOGGER = logging.getLogger(__name__)

POOL_SIZE_ENVIRONMENT = DB_POOL_SIZE_ENVIRONMENT
DEFAULT_POOL_SIZE = DEFAULT_DB_POOL_SIZE
MINIMUM_POOL_SIZE = MINIMUM_DB_POOL_SIZE
POOL_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 10
_ACCEPTED_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+psycopg"})


def _advisory_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key; locks are scoped to one database."""

    return int.from_bytes(hashlib.sha256(name.encode("ascii")).digest()[:8], "big", signed=True)


K_WRITE = _advisory_key("earshot.storage.k_write.v1")
K_SERVER = _advisory_key("earshot.storage.k_server.v1")
K_MIGRATE = _advisory_key("earshot.storage.k_migrate.v1")

# The server lock's connection notices a dead peer by itself within about
# idle + interval * count seconds, even when nothing is sent on it.
_SERVER_LOCK_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 5,
    "keepalives_interval": 1,
    "keepalives_count": 3,
    "tcp_user_timeout": 10_000,
}

# The same bounds on the Postgres side of both lock sessions. Postgres keeps a
# session advisory lock until it notices that the client is gone, and a client
# that vanished sends nothing: only the server's keepalives notice it. With these
# the server drops a silent lock session within about idle + interval * count
# seconds (8 s), or once data stays unacknowledged for tcp_user_timeout, so a
# replacement server can take the lock. Any user may set them. They are SET after
# connecting, not sent as startup options, because a session-mode PgBouncer
# refuses startup parameters it does not track.
_LOCK_SESSION_SETTINGS = (
    ("tcp_keepalives_idle", "5"),
    ("tcp_keepalives_interval", "1"),
    ("tcp_keepalives_count", "3"),
    ("tcp_user_timeout", "10000"),
)
_SET_LOCK_SESSION_SETTINGS = "SELECT " + ", ".join(
    ["set_config(%s, %s, false)"] * len(_LOCK_SESSION_SETTINGS)
)


def _set_lock_session_keepalives(connection: psycopg.Connection[Any]) -> None:
    parameters = [item for setting in _LOCK_SESSION_SETTINGS for item in setting]
    connection.execute(_SET_LOCK_SESSION_SETTINGS, parameters)


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).split(":", 1)[0].lower() in _ACCEPTED_SCHEMES


def _parse_url(url: str) -> sa.engine.URL:
    """Parse a database URL without ever echoing it (it may carry a password)."""

    if not is_postgres_url(url):
        raise StorageOpenError("the database URL must start with postgresql://")
    try:
        parsed = sa.engine.make_url(url)
    except Exception:
        raise StorageOpenError("the database URL could not be parsed") from None
    if not parsed.database:
        raise StorageOpenError("the database URL must name a database")
    return parsed.set(drivername="postgresql+psycopg")


def redact_url(url: str) -> str:
    """The URL for display: the password, if any, is replaced by ``***``."""

    return _parse_url(url).set(drivername="postgresql").render_as_string(hide_password=True)


def _libpq_uri(parsed: sa.engine.URL) -> str:
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def pool_size_from_environment() -> int:
    """``EARSHOT_DB_POOL_SIZE``, parsed by the server configuration's own rule."""

    value = os.environ.get(POOL_SIZE_ENVIRONMENT)
    if value is None:
        return DEFAULT_POOL_SIZE
    try:
        return parse_pool_size(POOL_SIZE_ENVIRONMENT, value)
    except ConfigurationError as error:
        raise StorageOpenError(str(error)) from None


def translate_error(error: BaseException) -> DatabaseError:
    """Map a psycopg or pool failure onto storage's error contract (never the SQL)."""

    sqlstate = getattr(error, "sqlstate", None)
    detail = f" (SQLSTATE {sqlstate})" if sqlstate else ""
    if isinstance(
        error,
        psycopg.OperationalError | psycopg.InterfaceError | sa.exc.TimeoutError,
    ) or isinstance(getattr(error, "orig", None), psycopg.OperationalError):
        return DatabaseUnavailableError(f"the database is unavailable; retry{detail}")
    return DatabaseError(f"a database statement failed{detail}")


@functools.lru_cache(maxsize=2048)
def _pyformat(sql: str) -> str:
    return qmark_to_pyformat(sql)


class PostgresRow:
    """A result row readable by position and by column name, like ``sqlite3.Row``."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: Sequence[Any]) -> None:
        self._index = index
        self._values = values

    def __getitem__(self, key: int | str | slice) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._index)

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PostgresRow):
            return self._index == other._index and tuple(self._values) == tuple(other._values)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(self._values))

    def __repr__(self) -> str:
        return f"PostgresRow({dict(zip(self._index, self._values, strict=False))!r})"


def _row_factory(cursor: Any) -> Any:
    description = cursor.description
    if description is None:
        return tuple
    index: dict[str, int] = {}
    for position, column in enumerate(description):
        # sqlite3.Row resolves a repeated name to its first column; so does this.
        index.setdefault(column.name, position)

    def make_row(values: Sequence[Any]) -> PostgresRow:
        return PostgresRow(index, values)

    return make_row


class PostgresConnection:
    """One pooled Postgres connection behind the shim's dialect-neutral interface."""

    dialect = POSTGRESQL

    def __init__(self, database: PostgresDatabase, pooled: Any) -> None:
        self._database = database
        self._pooled: Any | None = pooled
        self._raw: psycopg.Connection[Any] | None = pooled.dbapi_connection

    def _connection(self) -> psycopg.Connection[Any]:
        if self._raw is None:
            raise StorageError("a database connection was used after it was released")
        return self._raw

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> Any:
        cursor = self._connection().cursor(row_factory=_row_factory)
        try:
            cursor.execute(_pyformat(sql), tuple(parameters), binary=True)
        except psycopg.Error as error:
            raise translate_error(error) from error
        return cursor

    def execute_unprepared(self, sql: str, parameters: Sequence[Any] = (), /) -> Any:
        """``execute`` without psycopg's automatic prepare (``prepare_threshold``: a
        text's fifth run). A prepared ``IN``-list read keeps the generic plan Postgres
        made while the table had no statistics, a scan of the project filtering every
        listed value per row, until a new connection: a 1024-trace request took 0.2 s,
        then 1.5 s, until autoanalyze (tribunal round 2, Carmack CR2-4)."""

        cursor = self._connection().cursor(row_factory=_row_factory)
        try:
            cursor.execute(_pyformat(sql), tuple(parameters), binary=True, prepare=False)
        except psycopg.Error as error:
            raise translate_error(error) from error
        return cursor

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        cursor = self._connection().cursor(row_factory=_row_factory)
        try:
            cursor.executemany(_pyformat(sql), [tuple(row) for row in parameters])
        except psycopg.Error as error:
            raise translate_error(error) from error
        return cursor

    def begin_write(self) -> None:
        """Start a write transaction, which on Postgres needs no statement.

        SQLite takes its single write lock at ``BEGIN IMMEDIATE``. On Postgres the
        equivalent is ``K_write``, which the store's outermost ``_mutation()``
        already holds; psycopg opens the transaction at the next statement. A
        write transaction outside that lock is a storage bug, so it is refused.
        """

        if not self._database.holds_write_lock():
            raise StorageError("a write transaction was started outside the store write lock")

    def begin_read(self) -> None:
        """Read the following statements from one snapshot, as SQLite ``BEGIN`` does."""

        if self.in_transaction:
            raise StorageError("a read snapshot must start a transaction")
        self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

    def commit(self) -> None:
        try:
            self._connection().commit()
        except psycopg.Error as error:
            raise translate_error(error) from error

    def rollback(self) -> None:
        try:
            self._connection().rollback()
        except psycopg.Error as error:
            raise translate_error(error) from error

    @property
    def in_transaction(self) -> bool:
        return self._connection().info.transaction_status != pq.TransactionStatus.IDLE

    def close(self) -> None:
        """Return the connection to the pool (which rolls back any open transaction)."""

        pooled, raw = self._pooled, self._raw
        self._pooled = None
        self._raw = None
        if pooled is None:
            return
        if raw is not None and (raw.closed or raw.broken):
            # A dead connection must not go back to the pool.
            with contextlib.suppress(Exception):
                pooled.invalidate()
        with contextlib.suppress(Exception):
            pooled.close()

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                try:
                    self.commit()
                except BaseException:
                    with contextlib.suppress(Exception):
                        self._connection().rollback()
                    raise
            else:
                # The original error wins; a failed rollback only invalidates.
                with contextlib.suppress(Exception):
                    self._connection().rollback()
        finally:
            self.close()


class PostgresDatabase:
    """A Postgres catalog: a statement pool plus the store's ``K_write`` connection."""

    dialect = POSTGRESQL

    def __init__(self, url: str, *, pool_size: int | None = None) -> None:
        parsed = _parse_url(url)
        self.display_url = parsed.set(drivername="postgresql").render_as_string(hide_password=True)
        self._libpq_uri = _libpq_uri(parsed)
        size = pool_size_from_environment() if pool_size is None else pool_size
        if size < MINIMUM_POOL_SIZE:
            raise StorageOpenError(f"the database pool size must be at least {MINIMUM_POOL_SIZE}")
        self.pool_size = size
        self.engine = sa.create_engine(
            parsed,
            pool_size=size,
            max_overflow=0,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                "application_name": "earshot",
            },
        )
        self._lock_connection: psycopg.Connection[Any] | None = None
        self._write_lock_owner: int | None = None
        self._owner_pid = os.getpid()

    def connect(self) -> PostgresConnection:
        try:
            pooled = self.engine.raw_connection()
        except (sa.exc.TimeoutError, sa.exc.DBAPIError, psycopg.Error) as error:
            raise translate_error(error) from error
        return PostgresConnection(self, pooled)

    def holds_write_lock(self) -> bool:
        return self._write_lock_owner == threading.get_ident()

    @contextlib.contextmanager
    def write_lock(self, *, deadline: float | None = None) -> Iterator[None]:
        """Hold ``K_write`` for one outermost mutation, across all its transactions.

        The caller serializes this per store (the mutation ``RLock``), so the lock
        connection is only ever used by the thread that holds the mutation. With a
        ``deadline`` (a ``time.perf_counter()`` value) the lock is polled with
        ``pg_try_advisory_lock`` and ``StorageBusyError`` is raised once it passes.
        """

        connection = self._acquire_write_lock(deadline)
        self._write_lock_owner = threading.get_ident()
        try:
            yield
        finally:
            self._write_lock_owner = None
            try:
                connection.execute("SELECT pg_advisory_unlock(%s)", (K_WRITE,))
            except psycopg.Error:
                # The session is gone and took the lock with it. The mutation's
                # own transactions have already committed or failed on their own.
                _LOGGER.warning("EARSHOT_WRITE_LOCK_LOST: the write-lock session ended early")
                self._discard_lock_connection()

    def _acquire_write_lock(self, deadline: float | None) -> psycopg.Connection[Any]:
        for attempt in (1, 2):
            connection = self._open_lock_connection()
            try:
                if deadline is None:
                    connection.execute("SELECT pg_advisory_lock(%s)", (K_WRITE,))
                    return connection
                while True:
                    row = connection.execute(
                        "SELECT pg_try_advisory_lock(%s)", (K_WRITE,)
                    ).fetchone()
                    if row is not None and row[0]:
                        return connection
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise StorageBusyError("another session holds the store write lock")
                    time.sleep(min(LOCK_POLL_SECONDS, remaining))
            except psycopg.OperationalError as error:
                self._discard_lock_connection()
                if attempt == 2:
                    raise translate_error(error) from error
            except psycopg.Error as error:
                raise translate_error(error) from error
        raise AssertionError("unreachable")  # pragma: no cover

    def _open_lock_connection(self) -> psycopg.Connection[Any]:
        connection = self._lock_connection
        if connection is not None and not connection.closed and not connection.broken:
            return connection
        try:
            connection = psycopg.connect(
                self._libpq_uri,
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                application_name="earshot-write-lock",
            )
        except psycopg.Error as error:
            raise translate_error(error) from error
        try:
            _set_lock_session_keepalives(connection)
        except psycopg.Error as error:
            connection.close()
            raise translate_error(error) from error
        self._lock_connection = connection
        return connection

    def _discard_lock_connection(self) -> None:
        connection, self._lock_connection = self._lock_connection, None
        if connection is not None and os.getpid() == self._owner_pid:
            with contextlib.suppress(Exception):
                connection.close()

    def reset_after_fork(self) -> None:
        """Give a forked child its own connections without touching the parent's.

        The child drops the inherited pool and lock connection without closing
        them: closing would end the parent's sessions on the shared sockets.
        psycopg only finalizes a connection in the process that opened it.
        """

        self.engine.dispose(close=False)
        self._lock_connection = None
        self._write_lock_owner = None
        self._owner_pid = os.getpid()

    def close(self) -> None:
        self._discard_lock_connection()
        self.engine.dispose()


class PostgresServerLock:
    """``K_server``: the one-server-per-database lock, held for the server's lifetime.

    It sits on a dedicated connection outside the pool, with TCP keepalives, so a
    dead peer is noticed even while the connection is idle. ``verify`` is the
    watchdog's probe: it fails once the session (and with it the lock) is gone.
    """

    def __init__(self, connection: psycopg.Connection[Any], display_url: str) -> None:
        self._connection = connection
        self._pid = os.getpid()
        self.display_url = display_url

    @classmethod
    def acquire(cls, url: str) -> PostgresServerLock:
        parsed = _parse_url(url)
        display_url = parsed.set(drivername="postgresql").render_as_string(hide_password=True)
        try:
            connection = psycopg.connect(
                _libpq_uri(parsed),
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                application_name="earshot-server-lock",
                **_SERVER_LOCK_KEEPALIVES,
            )
        except psycopg.Error as error:
            raise StorageOpenError(
                f"could not connect to the database to take the server lock{_sqlstate(error)}"
            ) from error
        try:
            _set_lock_session_keepalives(connection)
            acquired = connection.execute("SELECT pg_try_advisory_lock(%s)", (K_SERVER,)).fetchone()
        except psycopg.Error as error:
            connection.close()
            raise StorageOpenError(f"could not take the server lock{_sqlstate(error)}") from error
        if acquired is None or not acquired[0]:
            connection.close()
            raise DatabaseInUseError(
                "another Earshot server is running on this database; run one server "
                "(and one worker) per database"
            )
        return cls(connection, display_url)

    @property
    def backend_pid(self) -> int:
        """The Postgres backend that holds the lock (for operators and tests)."""

        return int(self._connection.info.backend_pid)

    def verify(self) -> None:
        """Raise ``DatabaseLockLostError`` unless this process still holds the lock."""

        if os.getpid() != self._pid:
            raise DatabaseLockLostError("the server lock belongs to the parent process")
        try:
            self._connection.execute("SELECT 1").fetchone()
        except psycopg.Error as error:
            raise DatabaseLockLostError(
                f"the server lock connection was lost{_sqlstate(error)}"
            ) from error

    def release(self) -> None:
        if os.getpid() == self._pid:
            with contextlib.suppress(Exception):
                self._connection.close()


def _sqlstate(error: BaseException) -> str:
    sqlstate = getattr(error, "sqlstate", None)
    return f" (SQLSTATE {sqlstate})" if sqlstate else ""
