"""Fail-open OTLP-HTTP push client for the projected incident document.

Phoenix and Langfuse both ingest OTLP over HTTP at ``/v1/traces``. This client
POSTs the OTLP/JSON document produced by :func:`earshot.exporters.otlp.to_otlp`
(or :func:`earshot.exporters.openinference.to_openinference`) to such an endpoint,
reusing the hardening from :mod:`earshot.exporter`:

* bounded endpoint validation (absolute HTTP(S), no userinfo/query/fragment, HTTPS
  required off loopback);
* redirect refusal, so credentials are never forwarded to another origin;
* the same retry classification (permanent 4xx vs. retryable 408/429/5xx);
* a bounded request timeout and a maximum body size.

``export`` is **fail-open**: it never raises into a caller's hot path. Every
outcome -- success, permanent rejection, retryable failure, or a transport error --
is reported as an :class:`OtlpExportResult`. Errors are reported as sanitized codes,
never as response bodies or exception detail.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..context import suppress_instrumentation
from ..exporter import _RejectRedirects

_TRACES_PATH = "/v1/traces"
_DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class OtlpExportResult:
    """The outcome of a single OTLP push attempt. Never carries payload detail."""

    ok: bool
    status: int | None = None
    retryable: bool = False
    error: str | None = None
    spans: int = 0


def _validate_endpoint(endpoint: str) -> str:
    if endpoint != endpoint.strip() or any(ord(character) < 33 for character in endpoint):
        raise ValueError("OTLP endpoint must not contain whitespace or control characters")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OTLP endpoint must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("OTLP endpoint must not contain userinfo")
    if parsed.query:
        raise ValueError("OTLP endpoint must not contain a query")
    if parsed.fragment:
        raise ValueError("OTLP endpoint must not contain a fragment")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("OTLP endpoint contains an invalid port") from error
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise ValueError("non-loopback OTLP endpoints require HTTPS")
    normalized = endpoint.rstrip("/")
    if not normalized.endswith(_TRACES_PATH):
        normalized += _TRACES_PATH
    return normalized


def _validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("OTLP headers must be strings")
        if not key or any(ord(character) < 33 or ord(character) == 0x7F for character in key):
            raise ValueError("OTLP header names must not contain whitespace or control characters")
        if any(ord(character) < 32 or ord(character) == 0x7F for character in value):
            raise ValueError("OTLP header values must not contain control characters")
        validated[key] = value
    return validated


def serialize_document(document: Mapping[str, Any]) -> bytes:
    """Serialize an OTLP/JSON document to deterministic, compact UTF-8 bytes."""

    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _count_spans(document: Mapping[str, Any]) -> int:
    total = 0
    for resource_spans in document.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            total += len(scope_spans.get("spans", []))
    return total


class OtlpHttpExporter:
    """POST an OTLP/JSON document to a configured ``/v1/traces`` endpoint."""

    def __init__(
        self,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        *,
        timeout: float = 10.0,
        max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        if timeout <= 0:
            raise ValueError("OTLP push timeout must be positive")
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.endpoint = _validate_endpoint(endpoint)
        self._headers = _validate_headers(headers)
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def export(self, document: Mapping[str, Any]) -> OtlpExportResult:
        """POST ``document`` once. Fail-open: this never raises."""

        spans = _count_spans(document)
        try:
            payload = serialize_document(document)
        except (TypeError, ValueError, RecursionError):
            return OtlpExportResult(ok=False, error="serialize.failed", spans=spans)
        if len(payload) > self.max_body_bytes:
            return OtlpExportResult(ok=False, error="payload.too_large", spans=spans)

        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            **self._headers,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with (
                suppress_instrumentation(),
                self._opener.open(request, timeout=self.timeout) as response,
            ):
                status = int(response.status)
            if 200 <= status < 300:
                return OtlpExportResult(ok=True, status=status, spans=spans)
            # A non-2xx without an HTTPError (rare) is treated conservatively.
            retryable = status in {408, 429} or status >= 500
            return OtlpExportResult(
                ok=False, status=status, retryable=retryable, error="unexpected.status", spans=spans
            )
        except urllib.error.HTTPError as error:
            retryable = error.code in {408, 429} or error.code >= 500
            return OtlpExportResult(
                ok=False,
                status=int(error.code),
                retryable=retryable,
                error="http.retryable" if retryable else "http.rejected",
                spans=spans,
            )
        except Exception:  # transport failures must never escape a fail-open push
            return OtlpExportResult(
                ok=False, retryable=True, error="transport.failure", spans=spans
            )

    def __repr__(self) -> str:
        return f"OtlpHttpExporter(endpoint={self.endpoint!r}, timeout={self.timeout!r})"


def phoenix_exporter(
    endpoint: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> OtlpHttpExporter:
    """Build an exporter for an Arize Phoenix OTLP collector.

    Phoenix ingests OTLP at a bare ``/v1/traces`` endpoint (e.g.
    ``http://localhost:6006``); pass optional ``headers`` for a hosted deployment
    that requires an API key.
    """

    return OtlpHttpExporter(endpoint, headers=headers, timeout=timeout)


def langfuse_exporter(
    endpoint: str,
    public_key: str,
    secret_key: str,
    *,
    timeout: float = 10.0,
) -> OtlpHttpExporter:
    """Build an exporter for Langfuse's OTLP endpoint using Basic auth.

    ``endpoint`` is the Langfuse base URL (e.g. ``https://cloud.langfuse.com``);
    Langfuse exposes OTLP under ``/api/public/otel`` and authenticates with HTTP
    Basic auth over the ``public_key:secret_key`` pair.
    """

    if not public_key or not secret_key:
        raise ValueError("Langfuse export requires a public and secret key")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    base = endpoint.rstrip("/")
    if not base.endswith("/api/public/otel"):
        base += "/api/public/otel"
    return OtlpHttpExporter(
        base,
        headers={"Authorization": f"Basic {token}"},
        timeout=timeout,
    )
