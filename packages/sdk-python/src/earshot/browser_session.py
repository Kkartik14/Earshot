"""Bounded, in-memory authentication sessions for the same-origin viewer."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BrowserSession:
    project_id: str
    key_id: str | None
    csrf_token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class IssuedBrowserSession:
    token: str
    session: BrowserSession


class BrowserSessionStore:
    """Process-local revocable sessions keyed only by a digest of the cookie."""

    def __init__(self, *, capacity: int, ttl_seconds: int) -> None:
        if capacity < 1:
            raise ValueError("browser session capacity must be positive")
        if ttl_seconds < 1:
            raise ValueError("browser session TTL must be positive")
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[bytes, BrowserSession] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _purge_expired(self, now: float) -> None:
        expired = [digest for digest, item in self._sessions.items() if item.expires_at <= now]
        for digest in expired:
            self._sessions.pop(digest, None)

    def issue(self, *, project_id: str, key_id: str | None) -> IssuedBrowserSession:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        session = BrowserSession(
            project_id=project_id,
            key_id=key_id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            while len(self._sessions) >= self.capacity:
                self._sessions.popitem(last=False)
            self._sessions[self._digest(token)] = session
        return IssuedBrowserSession(token=token, session=session)

    def authenticate(self, token: str) -> BrowserSession | None:
        if not token or len(token) > 256:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            return self._sessions.get(self._digest(token))

    def revoke(self, token: str) -> bool:
        if not token or len(token) > 256:
            return False
        with self._lock:
            return self._sessions.pop(self._digest(token), None) is not None

    @staticmethod
    def csrf_matches(session: BrowserSession, supplied: str) -> bool:
        return (
            bool(supplied)
            and len(supplied) <= 256
            and hmac.compare_digest(
                supplied,
                session.csrf_token,
            )
        )
