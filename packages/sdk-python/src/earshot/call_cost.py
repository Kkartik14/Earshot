"""A call's cost: its usage, priced (pure).

**Usage** (``call_usage``) is what a projection keeps (``call_projections.usage_json``)
so a price change reprices a call without decoding anything: one entry per billed
request meter, from ``call_span_facts.usage_items`` (the usage-to-meter table):

- ``reported``: the requests' known quantities, summed per (operation, provider,
  model, unit); STT requests with the same provider and model are summed, and
  ``span`` names the request when there is one;
- ``not_reported``: one entry per request that left a meter unknown (a cancelled LLM
  request reports nothing; LiveKit writes 0 for what it did not get; a host STT
  request missing usage remains unknown if another request for that provider/model
  reported seconds);
- ``estimate``: STT audio seconds, which Pipecat and LiveKit do not report on a span:
  once per call and (provider, model), the user audio's duration (the recording's,
  else the call's), as a streaming STT bills connected time. A host provider/model
  gets this estimate only when none of its requests reported seconds. With no
  duration it is ``not_reported``.

Most usage quantities are integers in thousandths of the unit (``quantity_milli``).
Reported host STT seconds retain their exact shortest-decimal float spellings in
``quantity_decimal``; they are summed without rounding so sub-millisecond reports
remain priceable. Estimated STT seconds still come from integer milliseconds.

**Pricing** (``price_usage``). The price of (provider, model, unit) for a call is the
project's own row with the latest ``effective_from_unix_nano`` at or before the call's
first evidence, else the built-in row chosen the same way. An item costs
``quantity * price_nano_usd / per_quantity``, rounded half up to a whole nano-USD.
Statuses: ``priced`` and ``estimated`` (a price was found), ``no_price`` (usage, no
price row), ``not_reported`` (no usage). The lower bound sums the priced items,
the estimate the estimated ones, and ``unknown_items`` counts the rest; a call
with no item at all has neither total (NULL, never 0).

**Bounds.** The catalog stores costs in signed 64-bit columns. An item whose cost
passes ``2^63 - 1`` has no price (``no_price``); a call whose lower bound or estimate
passes it has neither total (NULL), so no cost filter matches it and it is never
complete. Nothing is clamped.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .call_span_facts import AUDIO_SECOND, UsageItem

REPORTED = "reported"
NOT_REPORTED = "not_reported"
ESTIMATE = "estimate"
_INT64_MAX = 2**63 - 1
_MAX_SAFE_API_INTEGER = 2**53 - 1
_MAX_FINITE_API_QUANTITY = Decimal.from_float(sys.float_info.max)
# Each request's meters that are always reported: one missing makes the request
# ``not_reported`` (the other meters are emitted only when reported).
_PRIMARY_METERS = {"llm": ("input_token", "output_token"), "tts": ("character",)}


@dataclass(frozen=True, slots=True)
class PriceRow:
    """One ``prices`` row, as pricing reads it (``project_id`` ``None``: built-in)."""

    id: int
    project_id: str | None
    provider: str
    model: str
    unit: str
    price_nano_usd: int
    per_quantity: int
    effective_from_unix_nano: int


def _span(item: UsageItem) -> dict[str, str]:
    return {"trace_id": item.trace_id, "span_id": item.span_id}


def call_usage(items: Iterable[UsageItem], *, user_audio_ms: int | None) -> list[dict[str, Any]]:
    """The call's usage entries (module docstring), in a stable order."""

    reported: dict[tuple, dict[str, Any]] = {}
    unreported: list[dict[str, Any]] = []
    stt: dict[tuple, list[UsageItem]] = {}
    by_request: dict[tuple, list[UsageItem]] = {}
    for item in items:
        if item.operation == "stt":
            stt.setdefault((item.provider, item.model), []).append(item)
            continue
        by_request.setdefault((item.trace_id, item.span_id, item.operation), []).append(item)
    for (_, _, operation), request in by_request.items():
        missing = [
            item
            for item in request
            if item.quantity is None and item.meter in _PRIMARY_METERS.get(operation, ())
        ]
        for item in request:
            if item.quantity is None:
                continue
            key = (item.operation, item.provider, item.model, item.meter)
            entry = reported.setdefault(
                key,
                {
                    "kind": REPORTED,
                    "operation": item.operation,
                    "provider": item.provider,
                    "model": item.model,
                    "unit": item.meter,
                    "quantity_milli": 0,
                    "spans": [],
                },
            )
            entry["quantity_milli"] += item.quantity * 1000
            entry["spans"].append(_span(item))
        if missing:
            first = request[0]
            unreported.append(
                {
                    "kind": NOT_REPORTED,
                    "operation": first.operation,
                    "provider": first.provider,
                    "model": first.model,
                    "unit": missing[0].meter if len(missing) == 1 else None,
                    "quantity_milli": None,
                    "span": _span(first),
                }
            )
    usage = []
    for entry in reported.values():
        spans = entry.pop("spans")
        entry["span"] = spans[0] if len(spans) == 1 else None
        usage.append(entry)
    for (provider, model), requests in stt.items():
        reported = [item for item in requests if item.quantity is not None]
        if reported:
            quantity_decimal = _audio_seconds_decimal(
                [Decimal(str(item.quantity)) for item in reported]
            )
            spans = [_span(item) for item in reported]
            usage.append(
                {
                    "kind": REPORTED,
                    "operation": "stt",
                    "provider": provider,
                    "model": model,
                    "unit": AUDIO_SECOND,
                    "quantity_decimal": str(quantity_decimal),
                    "span": spans[0] if len(spans) == 1 else None,
                }
            )
            for item in requests:
                if item.quantity is not None:
                    continue
                unreported.append(
                    {
                        "kind": NOT_REPORTED,
                        "operation": "stt",
                        "provider": item.provider,
                        "model": item.model,
                        "unit": AUDIO_SECOND,
                        "quantity_milli": None,
                        "span": _span(item),
                    }
                )
            continue
        usage.append(
            {
                "kind": NOT_REPORTED if user_audio_ms is None else ESTIMATE,
                "operation": "stt",
                "provider": provider,
                "model": model,
                "unit": AUDIO_SECOND,
                "quantity_milli": user_audio_ms,
                "span": None,
            }
        )
    usage.extend(unreported)
    order = {"llm": 0, "tts": 1, "stt": 2, "realtime": 3}
    return sorted(
        usage,
        key=lambda entry: (
            entry["kind"] == NOT_REPORTED,
            order.get(entry["operation"], 9),
            entry["provider"] or "",
            entry["model"] or "",
            entry["unit"] or "",
            (entry["span"] or {}).get("trace_id", ""),
            (entry["span"] or {}).get("span_id", ""),
        ),
    )


def _audio_seconds_decimal(seconds: Sequence[Decimal]) -> Decimal:
    """Sum reported seconds exactly without rounding to an artificial unit."""

    if not seconds:
        return Decimal(0)
    decimal_places = max(0, -min(value.as_tuple().exponent for value in seconds))
    whole_digits = max(1, max(value.adjusted() + 1 for value in seconds))
    precision = whole_digits + decimal_places + len(str(len(seconds))) + 4
    with localcontext() as context:
        context.prec = max(context.prec, precision)
        return sum(seconds, Decimal(0))


def match_price(
    prices: Sequence[PriceRow],
    project_id: str,
    provider: str | None,
    model: str | None,
    unit: str | None,
    at_unix_nano: int,
) -> PriceRow | None:
    """The price in effect (module docstring), or ``None``."""

    if provider is None or model is None or unit is None:
        return None
    provider, model = provider.lower(), model.lower()
    for scope in (project_id, None):
        rows = [
            row
            for row in prices
            if row.project_id == scope
            and (row.provider, row.model, row.unit) == (provider, model, unit)
            and row.effective_from_unix_nano <= at_unix_nano
        ]
        if rows:
            return max(rows, key=lambda row: (row.effective_from_unix_nano, row.id))
    return None


def _cost(quantity_milli: int, price: PriceRow) -> int:
    denominator = price.per_quantity * 1000
    return (2 * quantity_milli * price.price_nano_usd + denominator) // (2 * denominator)


def _cost_decimal(quantity: Decimal, price: PriceRow) -> int | None:
    """Price an exact decimal quantity with rational integer arithmetic."""

    if not quantity.is_finite() or quantity < 0:
        return None
    sign, digits, exponent = quantity.as_tuple()
    if sign:
        return None
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    if exponent >= 0:
        numerator = coefficient * 10**exponent * price.price_nano_usd
        denominator = price.per_quantity
    else:
        numerator = coefficient * price.price_nano_usd
        denominator = price.per_quantity * 10 ** (-exponent)
    return (2 * numerator + denominator) // (2 * denominator)


def _quantity(quantity_milli: int | None) -> int | float | None:
    if quantity_milli is None:
        return None
    whole, part = divmod(quantity_milli, 1000)
    if part == 0 and whole <= _MAX_SAFE_API_INTEGER:
        return whole
    decimal_quantity = Decimal(quantity_milli)
    with localcontext() as context:
        context.prec = max(context.prec, len(decimal_quantity.as_tuple().digits) + 3)
        seconds = decimal_quantity / Decimal(1000)
    if seconds > _MAX_FINITE_API_QUANTITY:
        return None
    try:
        api_quantity = float(seconds)
    except OverflowError:
        return None
    if not math.isfinite(api_quantity):
        return None
    # Python's shortest JSON float spelling can round large exact integers (for
    # example, 2**53 + 1) to a different quantity. Keep the API truthful by
    # returning null when that wire number would not round-trip through Decimal.
    return api_quantity if Decimal(str(api_quantity)) == seconds else None


def _quantity_decimal(quantity: Decimal | None) -> int | float | None:
    """Expose an exact usage decimal only when the API JSON number round-trips."""

    if quantity is None or not quantity.is_finite() or quantity < 0:
        return None
    try:
        if quantity == quantity.to_integral_value():
            whole = int(quantity)
            if abs(whole) <= _MAX_SAFE_API_INTEGER:
                return whole
        api_quantity = float(quantity)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(api_quantity):
        return None
    return api_quantity if Decimal(str(api_quantity)) == quantity else None


def _stored_quantity_decimal(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        quantity = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return quantity if quantity.is_finite() and quantity >= 0 else None


@dataclass(frozen=True, slots=True)
class CallCost:
    items: tuple[dict[str, Any], ...]
    lower_bound_nano_usd: int | None
    estimated_nano_usd: int | None
    unknown_items: int


def price_usage(
    usage: Sequence[Mapping[str, Any]],
    prices: Sequence[PriceRow],
    *,
    project_id: str,
    at_unix_nano: int,
) -> CallCost:
    """The cost items of ``usage`` at ``prices`` (module docstring)."""

    items = []
    lower = estimated = 0
    any_estimated = False
    unknown = 0
    for entry in usage:
        raw_decimal_quantity = entry.get("quantity_decimal")
        decimal_quantity = (
            _stored_quantity_decimal(raw_decimal_quantity)
            if raw_decimal_quantity is not None
            else None
        )
        quantity_milli = entry.get("quantity_milli")
        api_quantity = (
            _quantity_decimal(decimal_quantity)
            if raw_decimal_quantity is not None
            else _quantity(quantity_milli)
        )
        price = None
        if entry["kind"] != NOT_REPORTED and not (
            raw_decimal_quantity is not None and decimal_quantity is None
        ):
            price = match_price(
                prices, project_id, entry["provider"], entry["model"], entry["unit"], at_unix_nano
            )
        if price is None:
            cost = None
        elif raw_decimal_quantity is not None:
            cost = None if decimal_quantity is None else _cost_decimal(decimal_quantity, price)
        else:
            cost = None if quantity_milli is None else _cost(quantity_milli, price)
        if cost is not None and cost > _INT64_MAX:
            # Beyond what the catalog stores: unknown, never a clamped number.
            price, cost = None, None
        if entry["kind"] == NOT_REPORTED:
            status = "not_reported"
        elif price is None:
            status = "no_price"
        else:
            status = "estimated" if entry["kind"] == ESTIMATE else "priced"
        if status == "priced":
            lower += cost
        elif status == "estimated":
            estimated += cost
            any_estimated = True
        else:
            unknown += 1
        items.append(
            {
                "status": status,
                "operation": entry["operation"],
                "provider": entry["provider"],
                "model": entry["model"],
                "unit": entry["unit"],
                "quantity": api_quantity,
                "price_id": None if price is None else price.id,
                "price_nano_usd": None if price is None else price.price_nano_usd,
                "per_quantity": None if price is None else price.per_quantity,
                "cost_nano_usd": cost,
                "span": entry["span"],
            }
        )
    within = lower <= _INT64_MAX and estimated <= _INT64_MAX
    return CallCost(
        items=tuple(items),
        lower_bound_nano_usd=lower if items and within else None,
        estimated_nano_usd=estimated if any_estimated and within else None,
        unknown_items=unknown,
    )
