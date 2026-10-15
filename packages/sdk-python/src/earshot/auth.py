"""Short-lived, project-scoped service JWT verification for hosted API mode."""

from __future__ import annotations

import re
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from jwt import PyJWK, PyJWKClient, PyJWKClientConnectionError, PyJWKClientError

_ALLOWED_ALGORITHMS = ("RS256", "ES256")
_JWT_MAX_BYTES = 8 * 1024
_MAX_TOKEN_LIFETIME_SECONDS = 5 * 60
_CLOCK_SKEW_SECONDS = 30
_JWKS_CACHE_SECONDS = 300
_UNKNOWN_KID_REFRESH_SECONDS = 30
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCOPE = re.compile(r"^[a-zA-Z0-9:._-]+(?: [a-zA-Z0-9:._-]+)*$")


class InvalidHostedToken(ValueError):
    """The credential is not a valid Earshot-scoped hosted token."""


class HostedJwksUnavailable(RuntimeError):
    """The configured key service could not be reached to verify a token."""


@dataclass(frozen=True, slots=True)
class HostedPrincipal:
    subject: str
    project_id: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    token_id: str


class HostedJwtVerifier:
    """Verify RS256/ES256 tokens against one configured issuer and JWKS URI.

    The JWKS URI and trust configuration are operator supplied. Keys are cached
    for five minutes. Unknown key IDs can trigger at most one refresh per
    cooldown window; those refreshes do not block verification with a cached key.
    Signature verification is synchronous, so callers should run ``verify`` in
    an ASGI worker thread rather than on the event loop.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        jwks_ca_file: str | Path | None = None,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        ssl_context = (
            ssl.create_default_context(cafile=str(jwks_ca_file))
            if jwks_ca_file is not None
            else ssl.create_default_context()
        )
        self._client = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=300,
            timeout=3,
            ssl_context=ssl_context,
        )
        self._keys_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._keys_expires_at = 0.0
        self._next_refresh_at = 0.0

    def verify(self, token: str) -> HostedPrincipal:
        if not token or len(token) > _JWT_MAX_BYTES:
            raise InvalidHostedToken("bearer token is missing or too large")

        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if algorithm not in _ALLOWED_ALGORITHMS:
                raise InvalidHostedToken("unsupported signature algorithm")
            if not isinstance(key_id, str) or not key_id or len(key_id) > 256:
                raise InvalidHostedToken("a bounded key ID is required")

            signing_key = self._signing_key(key_id)
            if signing_key.algorithm_name != algorithm:
                raise InvalidHostedToken("token algorithm does not match its signing key")

            claims = jwt.decode(
                token,
                signing_key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "project_id",
                        "scope",
                        "iat",
                        "exp",
                        "jti",
                    ],
                    "strict_aud": True,
                },
            )
        except PyJWKClientConnectionError as error:
            raise HostedJwksUnavailable("configured JWKS is unavailable") from error
        except (PyJWKClientError, jwt.InvalidTokenError, TypeError, ValueError) as error:
            if isinstance(error, InvalidHostedToken):
                raise
            raise InvalidHostedToken("bearer token is invalid") from error

        subject = claims.get("sub")
        project_id = claims.get("project_id")
        scope = claims.get("scope")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        token_id = claims.get("jti")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 255
            or any(ord(character) < 0x20 for character in subject)
        ):
            raise InvalidHostedToken("subject is invalid")
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise InvalidHostedToken("project claim is invalid")
        if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
            raise InvalidHostedToken("scope claim is invalid")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= issued_at
            or expires_at - issued_at > _MAX_TOKEN_LIFETIME_SECONDS
        ):
            raise InvalidHostedToken("token lifetime is invalid")
        if not isinstance(token_id, str) or not token_id or len(token_id) > 128:
            raise InvalidHostedToken("token ID is invalid")

        return HostedPrincipal(
            subject=subject,
            project_id=project_id,
            scopes=frozenset(scope.split(" ")),
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )

    def _signing_key(self, key_id: str) -> PyJWK:
        now = time.monotonic()
        with self._keys_lock:
            keys_are_fresh = now < self._keys_expires_at
            signing_key = self._keys.get(key_id)
            if keys_are_fresh and signing_key is not None:
                return signing_key
            if now < self._next_refresh_at:
                if not keys_are_fresh:
                    raise HostedJwksUnavailable("JWKS refresh is inside its retry cooldown")
                raise PyJWKClientError("unknown key ID is inside the refresh cooldown")

        # An unknown key must never hold a lock needed by valid cached keys. If
        # another request is already refreshing, reject this miss instead of
        # queueing untrusted kids behind the issuer's network timeout.
        if not self._refresh_lock.acquire(blocking=False):
            if keys_are_fresh:
                raise PyJWKClientError("a JWKS refresh is already in progress")
            raise HostedJwksUnavailable("a JWKS refresh is already in progress")
        try:
            now = time.monotonic()
            with self._keys_lock:
                keys_are_fresh = now < self._keys_expires_at
                signing_key = self._keys.get(key_id)
                if keys_are_fresh and signing_key is not None:
                    return signing_key
                if now < self._next_refresh_at:
                    if not keys_are_fresh:
                        raise HostedJwksUnavailable("JWKS refresh is inside its retry cooldown")
                    raise PyJWKClientError("unknown key ID is inside the refresh cooldown")
                self._next_refresh_at = now + _UNKNOWN_KID_REFRESH_SECONDS

            refreshed_keys = self._client.get_signing_keys(refresh=True)
            now = time.monotonic()
            refreshed = {key.key_id: key for key in refreshed_keys if key.key_id is not None}
            with self._keys_lock:
                self._keys = refreshed
                self._keys_expires_at = now + _JWKS_CACHE_SECONDS
                self._next_refresh_at = now + _UNKNOWN_KID_REFRESH_SECONDS
                signing_key = self._keys.get(key_id)
            if signing_key is None:
                raise PyJWKClientError("JWKS contains no matching key ID")
            return signing_key
        finally:
            self._refresh_lock.release()


def validate_jwks_url(value: str) -> None:
    """Reject non-HTTPS and credential-bearing JWKS URLs at configuration time."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("jwks_url must be an HTTPS URL without embedded credentials")


__all__ = [
    "HostedJwksUnavailable",
    "HostedJwtVerifier",
    "HostedPrincipal",
    "InvalidHostedToken",
    "validate_jwks_url",
]
