"""Shared numeric domains for governed quality measurements.

The contract intentionally permits provider-specific scalars. Only names or units
with declared duration, latency, counter, or probability meaning are constrained.
Authoring, validation, and deterministic analysis all call this one boundary.
"""

from __future__ import annotations

import math


def measurement_value_limitation(name: str, value: object, unit: str) -> str | None:
    """Return why a governed scalar cannot represent the named measurement."""

    normalized = name.lower()
    probability = normalized.endswith((".probability", "_probability"))
    probability = (
        probability
        or normalized == "packet_loss_ratio"
        or normalized.endswith((".packet_loss_ratio", "_packet_loss_ratio"))
    )
    counter = unit == "count" or normalized.endswith(
        (".count", "_count", ".counter", "_counter", ".tokens", "_tokens")
    )
    duration_or_latency = normalized.startswith("earshot.duration.") or any(
        token in normalized
        for token in (
            "latency",
            "duration",
            "delay",
            "ttfb",
            "ttft",
            "jitter",
            "round_trip",
            "roundtrip",
            ".rtt",
        )
    )

    if isinstance(value, bool):
        return "measurement_not_numeric" if probability or counter or duration_or_latency else None
    if not isinstance(value, (int, float)):
        return "measurement_not_numeric"
    if isinstance(value, float) and not math.isfinite(value):
        return "measurement_not_finite"

    if probability and not 0 <= value <= 1:
        return "probability_outside_unit_interval"
    if counter and (value < 0 or (isinstance(value, float) and not value.is_integer())):
        return "counter_not_nonnegative_integer"
    if duration_or_latency and value < 0:
        return "duration_or_latency_negative"
    return None
