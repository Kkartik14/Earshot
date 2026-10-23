"""A small, short-lived memory of API-key credentials that passed their scrypt check.

Verifying an API key's secret is memory-hard on purpose (scrypt, about 16 MiB and
100 ms), so a client that sends the same key on every request would pay that on
every request. The store remembers only the *positive verdict*, for
``CREDENTIAL_VERDICT_TTL_SECONDS``: which key ID a credential proved. Entries are
keyed by the SHA-256 of the credential, never by the credential itself, and a
failed check is never remembered. The cache is bounded (least recently used
entries go first).

A remembered verdict does not authorize anything by itself: every request still
reads the key's row, so a revoked key or an inactive project is refused at once,
whichever process revoked it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .clock import Clock

CREDENTIAL_VERDICT_TTL_SECONDS = 60.0
CREDENTIAL_VERDICT_CAPACITY = 1024


class CredentialVerdictCache:
    """Positive credential verdicts by credential digest, for a bounded time."""

    def __init__(
        self,
        *,
        clock: Clock,
        capacity: int = CREDENTIAL_VERDICT_CAPACITY,
        ttl_seconds: float = CREDENTIAL_VERDICT_TTL_SECONDS,
    ) -> None:
        if capacity < 1 or ttl_seconds <= 0:
            raise ValueError("the verdict cache needs a positive capacity and lifetime")
        self._clock = clock
        self._capacity = capacity
        self._ttl_nano = int(ttl_seconds * 1_000_000_000)
        self._entries: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._lock = threading.Lock()

    def verdict(self, credential_digest: str) -> str | None:
        """The key ID this credential proved within the lifetime, if any."""

        now = self._clock.monotonic_nano()
        with self._lock:
            entry = self._entries.get(credential_digest)
            if entry is None:
                return None
            key_id, expires_at = entry
            if now >= expires_at:
                del self._entries[credential_digest]
                return None
            self._entries.move_to_end(credential_digest)
            return key_id

    def remember(self, credential_digest: str, key_id: str) -> None:
        """Remember that this credential proved ``key_id``; only call after a pass."""

        expires_at = self._clock.monotonic_nano() + self._ttl_nano
        with self._lock:
            self._entries[credential_digest] = (key_id, expires_at)
            self._entries.move_to_end(credential_digest)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def forget(self, credential_digest: str) -> None:
        with self._lock:
            self._entries.pop(credential_digest, None)
