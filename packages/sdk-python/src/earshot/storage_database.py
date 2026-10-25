"""The dialect-neutral connection shim that storage SQL runs through.

Storage keeps hand-written SQL with qmark (``?``) binds. A ``Database`` hands out
connections whose interface is the same on every dialect:

- ``execute(sql, parameters)`` and ``executemany(sql, rows)`` take qmark SQL;
- rows support both index and key access (``row[0]`` and ``row["name"]``);
- ``begin_write()`` opens a transaction that takes the database write lock up
  front, and ``begin_read()`` opens a read snapshot;
- ``commit()``, ``rollback()``, ``close()`` and ``in_transaction``;
- as a context manager the connection commits on a clean exit and rolls back on an
  exception (also after a failed commit). It never closes the connection, and no
  dialect may hand out a pool's proxy connection in its place.

SQLite connections are the stdlib ``sqlite3`` connections storage has always used,
created with the same pragmas and timeouts. Postgres connections come from
``storage_postgres``. SQL that one dialect spells differently goes through
``connection.dialect.sql(...)`` so each dialect gets its own text; a hook without
a variant for the running dialect fails closed rather than running SQLite text.

This module imports only the standard library: the base install can load it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol


class Row(Protocol):
    """A result row readable by column position and by column name."""

    def __getitem__(self, key: int | str) -> Any: ...

    def keys(self) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class Dialect:
    """Chooses the statement text a dialect runs for one SQLite-specific construct."""

    name: str

    def sql(self, hook: str, *, sqlite: str, postgresql: str | None = None) -> str:
        if self.name == "sqlite":
            return sqlite
        if postgresql is None:
            self._not_implemented(hook)
        return postgresql

    def only_sqlite(self, hook: str) -> None:
        """Guard a code path that exists only for SQLite catalogs (files, pragmas)."""

        if self.name != "sqlite":
            self._not_implemented(hook)

    def _not_implemented(self, hook: str) -> None:
        raise NotImplementedError(
            f"the {self.name} variant of storage hook {hook!r} is not implemented"
        )


SQLITE = Dialect("sqlite")
POSTGRESQL = Dialect("postgresql")


class DatabaseConnection(Protocol):
    """The connection interface storage code relies on, on every dialect."""

    dialect: Dialect

    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...

    def execute_unprepared(self, sql: str, parameters: Any = ..., /) -> Any:
        """``execute`` for a statement whose text varies with its data (an ``IN`` list
        of a batch's values): never prepared on the server. Postgres would otherwise
        keep the generic plan it made for such a text, which on a fresh table (no
        statistics yet) is a scan of the project."""
        ...

    def executemany(self, sql: str, parameters: Any, /) -> Any: ...

    def begin_write(self) -> None: ...

    def begin_read(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...

    @property
    def in_transaction(self) -> bool: ...

    def __enter__(self) -> DatabaseConnection: ...

    def __exit__(self, *exc_info: object) -> bool | None: ...


class SQLiteConnection(sqlite3.Connection):
    """A stdlib SQLite connection that also speaks the shim's dialect-neutral verbs.

    Everything else, including the context manager (commit on a clean exit,
    rollback on an exception), is ``sqlite3.Connection`` behaviour, unchanged.
    """

    dialect = SQLITE

    def execute_unprepared(self, sql: str, parameters: Any = (), /) -> Any:
        # SQLite plans each statement with its values bound; nothing is reused.
        return self.execute(sql, parameters)

    def begin_write(self) -> None:
        # Take SQLite's single write lock at BEGIN, not at the first write, so a
        # read-then-write transaction cannot be overtaken by another writer.
        self.execute("BEGIN IMMEDIATE")

    def begin_read(self) -> None:
        self.execute("BEGIN")


def configure_sqlite_connection(connection: sqlite3.Connection) -> None:
    """Apply the pragmas every catalog connection runs with."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA wal_autocheckpoint = 1000")
    connection.execute("PRAGMA journal_size_limit = 0")


class SQLiteDatabase:
    """One SQLite catalog: a file path, or a shared-memory URI when ``uri`` is true.

    There is no pool: each operation opens its own connection, as storage always has.
    """

    dialect = SQLITE

    def __init__(self, path: str, *, uri: bool = False) -> None:
        self.path = path
        self.uri = uri

    def connect(self) -> SQLiteConnection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            uri=self.uri,
            factory=SQLiteConnection,
        )
        configure_sqlite_connection(connection)
        return connection

    def connect_for_migration(self) -> sqlite3.Connection:
        """Open the connection schema migrations run on.

        It has the catalog pragmas except that foreign keys are OFF, so a table
        rebuild cannot cascade into child rows. Transactions are explicit
        (``isolation_level=None``): the migration runner emits ``BEGIN IMMEDIATE``
        and must run ``PRAGMA foreign_key_check`` before it commits.
        """

        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            uri=self.uri,
            isolation_level=None,
        )
        configure_sqlite_connection(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        # SQLAlchemy drives this connection and expects plain DB-API rows.
        connection.row_factory = None
        return connection


def qmark_to_pyformat(sql: str) -> str:
    """Rewrite qmark binds as psycopg ``%s`` binds and escape literal ``%``.

    A ``?`` inside a string literal, a quoted identifier or a comment is text, not a
    bind. psycopg scans the whole statement for ``%`` placeholders, quotes and
    comments included, so every literal ``%`` is doubled wherever it appears.
    """

    output: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in {"'", '"'}:
            end = index + 1
            while True:
                end = sql.find(character, end)
                if end == -1:
                    raise ValueError("unterminated quoted text in SQL statement")
                if end + 1 < length and sql[end + 1] == character:
                    end += 2
                    continue
                break
            output.append(sql[index : end + 1].replace("%", "%%"))
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            output.append(sql[index:end].replace("%", "%%"))
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated comment in SQL statement")
            output.append(sql[index : end + 2].replace("%", "%%"))
            index = end + 2
        elif character == "?":
            output.append("%s")
            index += 1
        elif character == "%":
            output.append("%%")
            index += 1
        else:
            output.append(character)
            index += 1
    return "".join(output)
