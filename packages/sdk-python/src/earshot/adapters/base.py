from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_ADAPTER_TRACKING_ENTRIES = 4_096
MAX_ADAPTER_TRACKING_ENTRIES = 65_536


@dataclass(frozen=True, slots=True)
class AdapterTrackingStatus:
    """Content-free sizes for an adapter's bounded identity ledgers."""

    limit_per_ledger: int
    entries: tuple[tuple[str, int], ...]
    saturated_ledgers: tuple[str, ...] = ()


def validate_tracking_limit(limit: int) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_ADAPTER_TRACKING_ENTRIES
    ):
        raise ValueError(
            f"max_tracking_entries must be an integer between 1 and {MAX_ADAPTER_TRACKING_ENTRIES}"
        )
    return limit


class AdapterDependencyError(RuntimeError):
    pass


def value(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


def seconds_to_nano(value_in_seconds: object) -> int | None:
    if isinstance(value_in_seconds, (int, float)) and value_in_seconds >= 0:
        return round(float(value_in_seconds) * 1_000_000_000)
    return None
