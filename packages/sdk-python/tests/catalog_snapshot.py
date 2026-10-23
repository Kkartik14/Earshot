"""One dialect-aware description of a catalog schema (AC1.1), for every schema test.

``sqlite_catalog_snapshot(path)`` and ``postgres_catalog_snapshot(url)`` read a
live catalog and return a ``CatalogSnapshot`` with two parts:

``portable``
    What must be equal on every dialect: fresh SQLite, legacy-upgraded SQLite and
    fresh Postgres. Compare it across dialects.
``native``
    Detail only one dialect has (declared type spelling, SQLite's raw NOT NULL
    flag, index key collations, Postgres collations). Comparing two whole
    snapshots of the **same** dialect (``a == b``) compares both parts.

Normalization rules. Each one names what it folds, and nothing else is folded:

1. **Tables.** Every user table, ``alembic_version`` included. SQLite internal
   objects (``sqlite_%``) are left out. Column order is layout, not schema: an
   upgraded SQLite file appends columns that ``ALTER TABLE`` added.
2. **Column type class.** ``INTEGER``/``BIGINT`` → ``integer``;
   ``TEXT``/``VARCHAR(n)``/``character varying(n)`` → ``text``;
   ``REAL``/``double precision`` → ``float``; ``BLOB``/``bytea`` → ``bytes``. The
   declared spelling is native. An unknown declared type raises, so a new type
   needs a rule here.
3. **Nullability.** A primary-key column counts as NOT NULL. Postgres forces it;
   SQLite does not for a non-INTEGER key, and 0.1.x declared its single-column
   TEXT keys without NOT NULL, which the baseline keeps on SQLite. The raw SQLite
   flag stays in ``native``, so two SQLite catalogs must still agree on it.
4. **Defaults.** Text is lowercased; double quotes, whitespace and parentheses
   are removed; Postgres casts (``'x'::text``, ``(0)::double precision``) are
   dropped. A Postgres sequence default (``nextval(…)``) and a SQLite
   ``AUTOINCREMENT`` primary key both read ``autoincrement``.
5. **Primary key.** The ordered key columns. Constraint names are ignored.
6. **Foreign keys.** ``(columns, referenced table, referenced columns, ON UPDATE,
   ON DELETE)``, sorted; constraint names are ignored. Postgres action codes
   (``a r c n d``) become SQL words, as SQLite reports them.
7. **UNIQUE constraints.** The column tuple of each constraint (SQLite automatic
   indexes of origin ``u``; Postgres ``contype = 'u'``), sorted; names ignored.
8. **Indexes.** Every explicitly created index (SQLite origin ``c``; Postgres
   indexes that back no PRIMARY KEY or UNIQUE constraint), as its normalized
   ``CREATE INDEX`` text: rule 4's folding, plus ``public.``, ``USING btree`` and
   ``IF NOT EXISTS`` removed and ``= ANY (ARRAY[…])`` read as ``IN (…)``. The text
   keeps the index name, UNIQUE, the table, every key expression with
   DESC/COLLATE, and the partial ``WHERE`` predicate. SQLite's per-key
   ``index_xinfo`` (sort order and collation) is also kept in ``native``.
9. **CHECK constraints.** Each CHECK's text, normalized as in rule 8, sorted;
   names ignored. SQLite's come from the ``CREATE TABLE`` text (column and table
   level: a CHECK on a column added by ``ALTER TABLE`` lands there too);
   Postgres's from ``pg_get_constraintdef``.
10. **Triggers and views.** Their names. Postgres's internal triggers (the ones
    that enforce foreign keys) are not user triggers. Both lists must be empty.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TYPE_CLASSES = {
    "integer": "integer",
    "bigint": "integer",
    "text": "text",
    "real": "float",
    "double precision": "float",
    "blob": "bytes",
    "bytea": "bytes",
}
_POSTGRES_ACTIONS = {
    "a": "no action",
    "r": "restrict",
    "c": "cascade",
    "n": "set null",
    "d": "set default",
}
_AUTOINCREMENT = "autoincrement"


@dataclass(frozen=True)
class CatalogSnapshot:
    portable: dict[str, Any]
    native: dict[str, Any]


def type_class(declared: str) -> str:
    """Rule 2: the portable class of a declared column type."""

    declared = declared.lower()
    if declared.startswith(("varchar", "character varying")):
        return "text"
    return _TYPE_CLASSES[declared]


def normalized_text(text: str | None) -> str | None:
    """Rules 4, 8 and 9: fold the spelling differences of the dialects' catalog text."""

    if text is None:
        return None
    text = text.lower().replace('"', "")
    text = re.sub(r"::(?:double precision|character varying|[a-z_]+)", "", text)
    text = re.sub(r"=\s*any\s*\(\s*array\[(.*?)\]\s*\)", r" in (\1)", text)
    text = text.replace("public.", "").replace("using btree ", "").replace("if not exists ", "")
    return re.sub(r"[\s()]", "", text)


def _sqlite_checks(table_sql: str) -> list[str]:
    checks = []
    for match in re.finditer(r"\bCHECK\s*\(", table_sql, flags=re.IGNORECASE):
        depth, start = 0, match.end() - 1
        for position in range(start, len(table_sql)):
            depth += {"(": 1, ")": -1}.get(table_sql[position], 0)
            if depth == 0:
                checks.append(normalized_text("check" + table_sql[start : position + 1]))
                break
    return sorted(checks)


def sqlite_catalog_snapshot(path: str | Path) -> CatalogSnapshot:
    """Snapshot the SQLite catalog file at ``path`` (read-only)."""

    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        portable_tables: dict[str, Any] = {}
        native_tables: dict[str, Any] = {}
        for _, table, table_sql in sorted(row for row in objects if row[0] == "table"):
            autoincrement = "autoincrement" in table_sql.lower()
            columns, declared, primary_key = {}, {}, {}
            for _, name, declared_type, notnull, default, pk, _ in connection.execute(
                f"PRAGMA table_xinfo({table})"
            ):
                columns[name] = (
                    type_class(declared_type),
                    bool(notnull or pk),
                    _AUTOINCREMENT if autoincrement and pk else normalized_text(default),
                )
                declared[name] = (declared_type.upper(), notnull)
                if pk:
                    primary_key[pk] = name
            foreign_keys: dict[int, list[tuple]] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
                foreign_keys.setdefault(row[0], []).append(row)
            uniques, indexes, index_keys = [], [], {}
            for _, index, _, origin, _ in connection.execute(f"PRAGMA index_list({table})"):
                keys = tuple(
                    (name, desc, collation)
                    for _, _, name, desc, collation, key in connection.execute(
                        f"PRAGMA index_xinfo({index})"
                    )
                    if key
                )
                if origin == "u":
                    uniques.append(tuple(name for name, _, _ in keys))
                    index_keys[f"unique{uniques[-1]}"] = keys
                elif origin == "c":
                    index_sql = next(row[2] for row in objects if row[1] == index)
                    indexes.append(normalized_text(index_sql))
                    index_keys[index] = keys
            portable_tables[table] = {
                "columns": columns,
                "primary_key": [primary_key[position] for position in sorted(primary_key)],
                "foreign_keys": sorted(
                    (
                        tuple(row[3] for row in rows),
                        rows[0][2],
                        tuple(row[4] for row in rows),
                        rows[0][5].lower(),
                        rows[0][6].lower(),
                    )
                    for rows in foreign_keys.values()
                ),
                "uniques": sorted(uniques),
                "indexes": sorted(indexes),
                "checks": _sqlite_checks(table_sql),
            }
            native_tables[table] = {"columns": declared, "index_keys": index_keys}
        return CatalogSnapshot(
            portable={
                "tables": portable_tables,
                "triggers": sorted(row[1] for row in objects if row[0] == "trigger"),
                "views": sorted(row[1] for row in objects if row[0] == "view"),
            },
            native={"dialect": "sqlite", "tables": native_tables},
        )


def postgres_catalog_snapshot(url: str) -> CatalogSnapshot:
    """Snapshot the ``current_schema()`` of the Postgres database at ``url``."""

    import psycopg

    with psycopg.connect(url) as connection:
        portable_tables: dict[str, Any] = {}
        native_tables: dict[str, Any] = {}
        relations = connection.execute(
            "SELECT c.oid, c.relname FROM pg_class AS c "
            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r' ORDER BY c.relname"
        ).fetchall()
        for oid, table in relations:
            names, columns, declared = {}, {}, {}
            for number, name, declared_type, notnull, default, collation in connection.execute(
                "SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), "
                "a.attnotnull, pg_get_expr(d.adbin, d.adrelid), co.collname "
                "FROM pg_attribute AS a "
                "LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "LEFT JOIN pg_collation AS co ON co.oid = a.attcollation "
                "WHERE a.attrelid = %s::oid AND a.attnum > 0 AND NOT a.attisdropped",
                (oid,),
            ):
                names[number] = name
                sequence = default is not None and default.startswith("nextval(")
                columns[name] = (
                    type_class(declared_type),
                    bool(notnull),
                    _AUTOINCREMENT if sequence else normalized_text(default),
                )
                declared[name] = (declared_type, collation)
            constraints = connection.execute(
                "SELECT contype, conkey, confrelid::regclass::text, confkey, confupdtype, "
                "confdeltype, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = %s::oid",
                (oid,),
            ).fetchall()
            indexes = connection.execute(
                "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index AS i "
                "WHERE i.indrelid = %s::oid AND i.indexrelid NOT IN ("
                "SELECT conindid FROM pg_constraint "
                "WHERE conrelid = %s::oid AND contype IN ('p', 'u'))",
                (oid, oid),
            ).fetchall()

            def referenced_columns(target: str, numbers: list[int]) -> tuple[str, ...]:
                return tuple(
                    connection.execute(
                        "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
                        "AND attnum = %s",
                        (target, number),
                    ).fetchone()[0]
                    for number in numbers
                )

            portable_tables[table] = {
                "columns": columns,
                "primary_key": next(
                    [names[number] for number in key]
                    for kind, key, *_ in constraints
                    if kind == "p"
                ),
                "foreign_keys": sorted(
                    (
                        tuple(names[number] for number in key),
                        target,
                        referenced_columns(target, target_key),
                        _POSTGRES_ACTIONS[on_update],
                        _POSTGRES_ACTIONS[on_delete],
                    )
                    for kind, key, target, target_key, on_update, on_delete, _ in constraints
                    if kind == "f"
                ),
                "uniques": sorted(
                    tuple(names[number] for number in key)
                    for kind, key, *_ in constraints
                    if kind == "u"
                ),
                "indexes": sorted(normalized_text(row[0]) for row in indexes),
                "checks": sorted(
                    normalized_text(definition)
                    for kind, *_, definition in constraints
                    if kind == "c"
                ),
            }
            native_tables[table] = {"columns": declared}
        triggers = sorted(
            row[0]
            for row in connection.execute(
                "SELECT t.tgname FROM pg_trigger AS t JOIN pg_class AS c ON c.oid = t.tgrelid "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND NOT t.tgisinternal"
            )
        )
        views = sorted(
            row[0]
            for row in connection.execute(
                "SELECT c.relname FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('v', 'm')"
            )
        )
        return CatalogSnapshot(
            portable={"tables": portable_tables, "triggers": triggers, "views": views},
            native={"dialect": "postgresql", "tables": native_tables},
        )
