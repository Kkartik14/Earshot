"""Prices: append-only, effective-dated, project-scoped rows in ``prices``.

A price is ``price_nano_usd`` per ``per_quantity`` units of one meter (``unit``)
of one ``(provider, model)``, both lower-cased. Money is an integer number of
nano-USD (1e-9 USD), so both dialects store and sum it exactly, and a
``per_quantity`` keeps a fraction of a nano-USD per unit exact ($0.0375 per 1M
tokens is 37,500 per 1,000,000).

**Scope.** A row with ``project_id`` NULL is a built-in price, written only by the
operator (the migration seed, ``earshot prices add --builtin``); a project writes
rows for itself only. A project's own row wins over a built-in one, and the row
in effect for a call is the latest whose ``effective_from_unix_nano`` is at or
before the call's first evidence.

**Versions.** Every insert bumps the version its scope keeps, in the same
transaction: ``projects.price_version`` for a project's row, the
``builtin_price_version`` singleton for a built-in one. A projection records both,
so a price change marks the projections it may change as stale.

Rows are never updated or deleted, except with their project (project deletion).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from .storage import InvalidCursorError, decode_page_cursor, encode_page_cursor

if TYPE_CHECKING:
    from .call_cost import PriceRow
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection

# The meters a price can be per. Usage is extracted into these units; a new meter
# is added here (the column has no CHECK, so no migration is needed).
PriceUnit = Literal[
    "input_token", "output_token", "cached_input_token", "character", "audio_second"
]
PRICE_UNITS: tuple[str, ...] = get_args(PriceUnit)
PROVIDER_MAX_LENGTH = 128
MODEL_MAX_LENGTH = 256
MAX_PAGE_SIZE = 100
_INT64_MAX = 2**63 - 1
# ``encode_page_cursor``'s identifier part for a price page (its key is the row ID).
_CURSOR_TAG = "prices"
_COLUMNS = (
    "id, project_id, provider, model, unit, price_nano_usd, per_quantity, "
    "effective_from_unix_nano, source_url, note, created_unix_nano"
)


@dataclass(frozen=True, slots=True)
class NewPrice:
    """A price to add; ``provider`` and ``model`` are stored lower-cased."""

    provider: str
    model: str
    unit: str
    price_nano_usd: int
    per_quantity: int
    effective_from_unix_nano: int
    source_url: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class Price:
    """One ``prices`` row; ``project_id`` is ``None`` for a built-in price."""

    id: int
    project_id: str | None
    provider: str
    model: str
    unit: str
    price_nano_usd: int
    per_quantity: int
    effective_from_unix_nano: int
    source_url: str | None
    note: str | None
    created_unix_nano: int

    def document(self) -> dict[str, Any]:
        """The row as the API and the CLI show it (``PriceResponse``)."""

        return {
            "id": self.id,
            "scope": "builtin" if self.project_id is None else "project",
            "provider": self.provider,
            "model": self.model,
            "unit": self.unit,
            "price_nano_usd": self.price_nano_usd,
            "per_quantity": self.per_quantity,
            "effective_from_unix_nano": str(self.effective_from_unix_nano),
            "source_url": self.source_url,
            "note": self.note,
            "created_unix_nano": str(self.created_unix_nano),
        }


@dataclass(frozen=True, slots=True)
class PricePage:
    items: tuple[Price, ...]
    next_cursor: str | None


def _price(row: Any) -> Price:
    return Price(
        id=int(row["id"]),
        project_id=row["project_id"],
        provider=row["provider"],
        model=row["model"],
        unit=row["unit"],
        price_nano_usd=int(row["price_nano_usd"]),
        per_quantity=int(row["per_quantity"]),
        effective_from_unix_nano=int(row["effective_from_unix_nano"]),
        source_url=row["source_url"],
        note=row["note"],
        created_unix_nano=int(row["created_unix_nano"]),
    )


def _validated(price: NewPrice) -> NewPrice:
    provider, model = price.provider.lower(), price.model.lower()
    if not 1 <= len(provider) <= PROVIDER_MAX_LENGTH:
        raise ValueError(f"provider must be 1 to {PROVIDER_MAX_LENGTH} characters")
    if not 1 <= len(model) <= MODEL_MAX_LENGTH:
        raise ValueError(f"model must be 1 to {MODEL_MAX_LENGTH} characters")
    if price.unit not in PRICE_UNITS:
        raise ValueError(f"unit must be one of {', '.join(PRICE_UNITS)}")
    if not 0 <= price.price_nano_usd <= _INT64_MAX:
        raise ValueError("price_nano_usd must be in [0, 2^63 - 1]")
    if not 1 <= price.per_quantity <= _INT64_MAX:
        raise ValueError("per_quantity must be in [1, 2^63 - 1]")
    if not 0 <= price.effective_from_unix_nano <= _INT64_MAX:
        raise ValueError("effective_from_unix_nano must be in [0, 2^63 - 1]")
    return NewPrice(
        provider=provider,
        model=model,
        unit=price.unit,
        price_nano_usd=price.price_nano_usd,
        per_quantity=price.per_quantity,
        effective_from_unix_nano=price.effective_from_unix_nano,
        source_url=price.source_url,
        note=price.note,
    )


class PriceStore:
    """Price operations on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store

    def add(
        self, project_id: str | None, price: NewPrice, *, budget_s: float | None = None
    ) -> Price:
        """Append ``price`` for ``project_id`` (``None``: a built-in price) and bump
        that scope's price version, in one transaction."""

        price = _validated(price)
        with (
            self.store._mutation("add_price", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            if project_id is None:
                connection.execute(
                    "UPDATE builtin_price_version SET version = version + 1 WHERE singleton = 1"
                )
            else:
                self.store._require_active_project(connection, project_id)
                connection.execute(
                    "UPDATE projects SET price_version = price_version + 1 WHERE project_id = ?",
                    (project_id,),
                )
            added = _insert(connection, project_id, price, now=self.store._clock.unix_nano())
            connection.commit()
        return added

    def seed_builtins(self, prices: Sequence[NewPrice], *, budget_s: float | None = None) -> int:
        """``seed_builtin_prices`` in one mutation; how many rows it added."""

        with (
            self.store._mutation("seed_builtin_prices", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            added = seed_builtin_prices(connection, prices, now=self.store._clock.unix_nano())
            connection.commit()
        return added

    def list_prices(
        self, project_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> PricePage:
        """One page of the built-in prices and the project's own, oldest first.

        ``cursor`` is the previous page's ``next_cursor``; a malformed one raises
        ``InvalidCursorError``.
        """

        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        after = 0
        if cursor is not None:
            after, tag = decode_page_cursor(cursor)
            if tag != _CURSOR_TAG:
                raise InvalidCursorError("invalid price pagination cursor")
        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM prices "
                "WHERE (project_id IS NULL OR project_id = ?) AND id > ? ORDER BY id LIMIT ?",
                (project_id, after, limit + 1),
            ).fetchall()
        items = tuple(_price(row) for row in rows[:limit])
        return PricePage(
            items=items,
            next_cursor=(
                encode_page_cursor(items[-1].id, _CURSOR_TAG) if len(rows) > limit else None
            ),
        )


def price_rows(connection: DatabaseConnection, project_id: str) -> list[PriceRow]:
    """Every price a project's calls may use: the built-ins and its own rows."""

    from .call_cost import PriceRow

    return [
        PriceRow(
            id=int(row["id"]),
            project_id=row["project_id"],
            provider=row["provider"],
            model=row["model"],
            unit=row["unit"],
            price_nano_usd=int(row["price_nano_usd"]),
            per_quantity=int(row["per_quantity"]),
            effective_from_unix_nano=int(row["effective_from_unix_nano"]),
        )
        for row in connection.execute(
            "SELECT id, project_id, provider, model, unit, price_nano_usd, per_quantity, "
            "effective_from_unix_nano FROM prices WHERE project_id IS NULL OR project_id = ? "
            "ORDER BY id",
            (project_id,),
        ).fetchall()
    ]


def _insert(
    connection: DatabaseConnection, project_id: str | None, price: NewPrice, *, now: int
) -> Price:
    row = connection.execute(
        "INSERT INTO prices(project_id, provider, model, unit, price_nano_usd, "
        "per_quantity, effective_from_unix_nano, source_url, note, created_unix_nano) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {_COLUMNS}",
        (
            project_id,
            price.provider,
            price.model,
            price.unit,
            price.price_nano_usd,
            price.per_quantity,
            price.effective_from_unix_nano,
            price.source_url,
            price.note,
            now,
        ),
    ).fetchone()
    return _price(row)


def seed_builtin_prices(
    connection: DatabaseConnection, prices: Sequence[NewPrice], *, now: int
) -> int:
    """Add each of ``prices`` as a built-in row unless an equal built-in row (same
    provider, model, unit, price, quantity and effective time) exists, and bump the
    built-in version once if any was added. Returns how many were added. Runs in the
    caller's write transaction.

    Idempotent: a server runs it at every start, so a release's new defaults land
    once and an operator's own built-in rows are never touched."""

    wanted = [_validated(price) for price in prices]
    present = {
        (
            row["provider"],
            row["model"],
            row["unit"],
            int(row["price_nano_usd"]),
            int(row["per_quantity"]),
            int(row["effective_from_unix_nano"]),
        )
        for row in connection.execute(
            "SELECT provider, model, unit, price_nano_usd, per_quantity, "
            "effective_from_unix_nano FROM prices WHERE project_id IS NULL"
        ).fetchall()
    }
    added = 0
    for price in wanted:
        key = (
            price.provider,
            price.model,
            price.unit,
            price.price_nano_usd,
            price.per_quantity,
            price.effective_from_unix_nano,
        )
        if key in present:
            continue
        present.add(key)
        _insert(connection, None, price, now=now)
        added += 1
    if added:
        connection.execute(
            "UPDATE builtin_price_version SET version = version + 1 WHERE singleton = 1"
        )
    return added
