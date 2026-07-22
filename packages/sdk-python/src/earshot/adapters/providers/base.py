"""Shared, provider-SDK-free event adapter primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ...pipeline import TurnRecorder

_ApplyUpdate = Callable[[TurnRecorder], None]

# Backstop cap on per-adapter replay/dedupe state. A single voice session stays
# far below this; the cap only bounds a long-lived adapter reused across many
# sessions without ``close()``. Reuse across sessions should call ``close()``.
_MAX_REMEMBERED_UPDATES = 65_536


@dataclass(slots=True)
class AdapterUpdate:
    """A fully validated provider update that can be applied exactly once.

    Provider payloads are parsed before this object is created. The retained
    action contains only governed facts, never transcript or audio content.
    """

    provider: str
    event_type: str
    update_id: str
    correlation_id: str
    _apply_update: _ApplyUpdate = field(repr=False, compare=False)
    terminal: bool = False
    turn_commit: bool = False
    _applied: bool = field(default=False, init=False, repr=False, compare=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )
    _state_lock: threading.RLock | None = field(default=None, init=False, repr=False, compare=False)

    def apply(self, turn: TurnRecorder) -> bool:
        """Apply this update once; return ``False`` for an exact replay."""

        if not isinstance(turn, TurnRecorder):
            raise TypeError("turn must be an earshot TurnRecorder")
        with self._lock:
            if self._applied:
                return False
            state_lock = self._state_lock
            if state_lock is None:
                self._apply_update(turn)
            else:
                with state_lock:
                    self._apply_update(turn)
            self._applied = True
            return True


class ProviderAdapter:
    """In-process replay and opaque-correlation support for stream adapters."""

    def __init__(self, provider: str, *, identity_key: bytes | None = None) -> None:
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        if identity_key is not None and not isinstance(identity_key, bytes):
            raise TypeError("identity_key must be bytes")
        if identity_key is not None and len(identity_key) < 16:
            raise ValueError("identity_key must contain at least 16 bytes")
        self.provider = provider
        self._identity_key = identity_key or secrets.token_bytes(32)
        self._updates: OrderedDict[bytes, AdapterUpdate] = OrderedDict()
        self._native_updates: OrderedDict[bytes, bytes] = OrderedDict()
        self._lock = threading.RLock()

    def close(self) -> None:
        """Release replay/dedupe state so a reused adapter does not leak it."""

        with self._lock:
            self._updates.clear()
            self._native_updates.clear()

    def _remember(
        self,
        payload: Mapping[str, object],
        factory: Callable[[str], AdapterUpdate],
        *,
        observed_at_ms: float | None = None,
        native_update_id: str | None = None,
        fingerprint_context: Mapping[str, object] | None = None,
    ) -> AdapterUpdate:
        fingerprint = self._fingerprint(
            payload,
            observed_at_ms=observed_at_ms,
            fingerprint_context=fingerprint_context,
        )
        with self._lock:
            native_key: bytes | None = None
            if native_update_id is not None:
                native_key = hmac.new(
                    self._identity_key,
                    f"{self.provider}\x1fupdate\x1f{native_update_id}".encode(),
                    hashlib.sha256,
                ).digest()
                prior_fingerprint = self._native_updates.get(native_key)
                if prior_fingerprint is not None and prior_fingerprint != fingerprint:
                    raise ValueError("conflicting provider update identity")
            existing = self._updates.get(fingerprint)
            if existing is not None:
                self._updates.move_to_end(fingerprint)
                return existing
            update_id = f"{self.provider}-update-{fingerprint.hex()[:24]}"
            update = factory(update_id)
            update._state_lock = self._lock
            self._updates[fingerprint] = update
            if native_key is not None:
                self._native_updates[native_key] = fingerprint
                self._native_updates.move_to_end(native_key)
            self._evict_over_cap()
            return update

    def _evict_over_cap(self) -> None:
        while len(self._updates) > _MAX_REMEMBERED_UPDATES:
            self._updates.popitem(last=False)
        while len(self._native_updates) > _MAX_REMEMBERED_UPDATES:
            self._native_updates.popitem(last=False)

    def _opaque_id(self, kind: str, native_id: str) -> str:
        digest = hmac.new(
            self._identity_key,
            f"{self.provider}\x1f{kind}\x1f{native_id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        return f"{self.provider}-{kind}-{digest}"

    def _fingerprint(
        self,
        payload: Mapping[str, object],
        *,
        observed_at_ms: float | None,
        fingerprint_context: Mapping[str, object] | None,
    ) -> bytes:
        identity: object = payload
        if observed_at_ms is not None or fingerprint_context is not None:
            identity = {
                "context": fingerprint_context,
                "observed_at_ms": observed_at_ms,
                "payload": payload,
            }
        try:
            canonical = json.dumps(
                identity,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("provider payload must contain finite JSON values") from error
        return hmac.new(self._identity_key, canonical, hashlib.sha256).digest()


def require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def require_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return require_string(value, field_name)


def require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def require_nonnegative_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return normalized


def require_nonnegative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def optional_probability(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    normalized = require_nonnegative_number(value, field_name)
    if normalized > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return normalized


def safe_attributes(correlation_id: str, event_type: str) -> dict[str, Any]:
    """Return metadata-only attributes; native identifiers never leave the adapter."""

    return {
        "earshot.request.id": correlation_id,
        "earshot.correlation": "provider_request",
        "earshot.source.event.name": event_type,
    }
