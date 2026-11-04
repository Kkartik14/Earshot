"""Built-in provider prices use exact model IDs and preserve their source dates."""

from earshot.call_cost import PriceRow, price_usage
from earshot.pricing_defaults import BUILTIN_PRICES


def test_sonic_36_builtin_price_is_exact_and_reaches_tts_cost() -> None:
    price = next(
        item
        for item in BUILTIN_PRICES
        if (item.provider, item.model, item.unit) == ("cartesia", "sonic-3.6", "character")
    )
    assert (price.price_nano_usd, price.per_quantity, price.effective_from_unix_nano) == (
        38_000,
        1,
        0,
    )
    assert price.source_url == "https://www.cartesia.ai/pricing"
    assert price.note.startswith("regular list price as of 2026-11-03;")

    cost = price_usage(
        [
            {
                "kind": "reported",
                "operation": "tts",
                "provider": "cartesia",
                "model": "sonic-3.6",
                "unit": "character",
                "quantity_milli": 1000,
                "span": None,
            }
        ],
        [
            PriceRow(
                id=1,
                project_id=None,
                provider=price.provider,
                model=price.model,
                unit=price.unit,
                price_nano_usd=price.price_nano_usd,
                per_quantity=price.per_quantity,
                effective_from_unix_nano=price.effective_from_unix_nano,
            )
        ],
        project_id="default",
        at_unix_nano=1,
    )

    assert cost.items[0]["status"] == "priced"
    assert cost.lower_bound_nano_usd == 38_000
    assert cost.unknown_items == 0
