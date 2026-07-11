from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


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
