"""How providers authenticate their deliveries: a shared secret in a header, or an
HMAC of the body. Both verify a body as it streams in, without keeping it, so an
oversized delivery can be authenticated before it is answered."""

from __future__ import annotations

import hashlib
import hmac

from .types import DeliveryTrustError

Headers = tuple[tuple[bytes, bytes], ...]


def header_values(headers: Headers, name: bytes) -> tuple[bytes, ...]:
    lowered = name.lower()
    return tuple(value for key, value in headers if key.lower() == lowered)


class HeaderSecretVerifier:
    """Exactly one of ``bearer`` (``Authorization: Bearer <s>``) or ``header``
    (``<header>: <s>``) carries one of ``secrets``; the body is not signed. Checked
    when built, before any body byte is read."""

    def __init__(self, headers: Headers, secrets: tuple[str, ...], *, header: bytes) -> None:
        authorization = header_values(headers, b"authorization")
        custom = header_values(headers, header)
        if not secrets or bool(authorization) == bool(custom):
            raise DeliveryTrustError
        values = authorization or custom
        if len(values) != 1 or len(values[0]) > 4096:
            raise DeliveryTrustError
        try:
            supplied = values[0].decode("utf-8")
        except UnicodeDecodeError:
            raise DeliveryTrustError from None
        if authorization:
            scheme, separator, token = supplied.partition(" ")
            if not separator or scheme.lower() != "bearer" or not token:
                raise DeliveryTrustError
            supplied = token
        supplied_bytes = supplied.encode("utf-8")
        valid = False
        for secret in secrets:
            valid = hmac.compare_digest(secret.encode("utf-8"), supplied_bytes) or valid
        if not valid:
            raise DeliveryTrustError

    def update(self, chunk: bytes) -> None:
        del chunk

    def verify(self) -> None:
        return None


class HmacVerifier:
    """HMAC-SHA256 of ``prefix + body + suffix`` under each secret, compared with
    every supplied hex digest in constant time."""

    def __init__(
        self,
        secrets: tuple[str, ...],
        supplied: tuple[str, ...],
        *,
        prefix: bytes = b"",
        suffix: bytes = b"",
    ) -> None:
        if not secrets or not supplied:
            raise DeliveryTrustError
        self._supplied = tuple(value.lower() for value in supplied)
        self._suffix = suffix
        self._macs = [
            hmac.new(secret.encode("utf-8"), prefix, hashlib.sha256) for secret in secrets
        ]

    def update(self, chunk: bytes) -> None:
        for mac in self._macs:
            mac.update(chunk)

    def verify(self) -> None:
        valid = False
        for mac in self._macs:
            final = mac.copy()
            final.update(self._suffix)
            expected = final.hexdigest()
            for supplied in self._supplied:
                valid = hmac.compare_digest(expected, supplied) or valid
        if not valid:
            raise DeliveryTrustError


def within_window(timestamp_seconds: float, now: int, window_seconds: int) -> None:
    """Refuse a signature timestamp more than ``window_seconds`` from ``now``."""

    if abs(now - timestamp_seconds) > window_seconds:
        raise DeliveryTrustError
