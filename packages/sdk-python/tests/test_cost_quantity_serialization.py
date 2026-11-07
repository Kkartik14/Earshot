from __future__ import annotations

import json

from earshot.api import CostItemResponse


def _cost_item(quantity: int | float) -> CostItemResponse:
    return CostItemResponse(
        status="priced",
        operation="tts",
        provider="cartesia",
        model="sonic-3",
        unit="character",
        quantity=quantity,
        price_id=1,
        price_nano_usd=1,
        per_quantity=1,
        cost_nano_usd=1,
        span=None,
    )


def test_int64_usage_is_an_exact_integer_json_number_and_fractional_usage_stays_float() -> None:
    maximum_int64 = 2**63 - 1
    integral = _cost_item(maximum_int64)
    integral_json = integral.model_dump_json()
    integral_wire = json.loads(integral_json)

    assert isinstance(integral.quantity, int)
    assert integral_wire["quantity"] == maximum_int64
    assert '"quantity":9223372036854775807' in integral_json

    fractional = _cost_item(0.375)
    fractional_wire = json.loads(fractional.model_dump_json())

    assert isinstance(fractional.quantity, float)
    assert fractional_wire["quantity"] == 0.375
