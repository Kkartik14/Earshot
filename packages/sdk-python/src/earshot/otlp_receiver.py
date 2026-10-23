"""The OTLP/HTTP trace receiver's protocol half (FR-5): no HTTP server, no storage.

``api.py`` mounts ``POST /v1/traces`` and calls into this module; the spans it
accepts go to ``storage_calls.CallStore.record_spans``.

**Negotiation.** The decoder is chosen by the parsed media type,
``application/x-protobuf`` or ``application/json`` (parameters such as
``charset`` are ignored). ``Content-Encoding`` is ``identity``, ``gzip`` or
``deflate``. Anything else is a 415.

**Decoding.** Protobuf is ``ExportTraceServiceRequest.FromString``. OTLP/JSON
writes trace and span IDs as hex, which ``json_format`` would silently read as
base64 (a 32-character trace ID becomes 24 wrong bytes), so every ID of a span
and of its links is converted first: upper- or lower-case hex of exactly the
ID's size, or base64 (padded or not) decoding to exactly that size. Any other ID
value becomes one byte, which the span check below rejects for that span alone.
Then ``ParseDict(…, ignore_unknown_fields=True)``. A body that is not a request
at all (invalid UTF-8 included) is ``OtlpDecodeError``, a 400. An empty body is
an empty request.

**Span checks** (``span_rejection``), each rejecting only its span: a 16-byte,
non-zero trace ID; an 8-byte, non-zero span ID; a parent ID that is empty or 8
bytes; link IDs of 16 and 8 bytes; start, end and every event time in
[1, 2^63-1] (the catalog's signed 64-bit columns) with end ≥ start; no
U+0000 in the span name, scope name or session key, which Postgres ``text``
cannot store; no attribute key (span, event, link, resource or scope) over
``MAX_ATTRIBUTE_KEY_BYTES`` of UTF-8, since senders choose keys and every stored
key is classified; and a scope name, scope attributes and resource within
``MAX_SCOPE_NAME_BYTES``, ``MAX_SCOPE_ATTRIBUTES_BYTES`` and ``MAX_RESOURCE_BYTES``.
A rejected span would otherwise fail the whole request's transaction, and the
exporter would retry the same bytes until it gives up. ``validate_spans`` also keeps
only the spans of a request's first ``MAX_REQUEST_SCOPES`` distinct scopes, and
rejects the spans that would take what the request stores more than once (a span's
row holds its scope name; ``otlp_resources`` holds a resource and scope once per
distinct pair) past ``MAX_REQUEST_SHARED_BYTES`` (``docs/production.md``). The
resource and scope checks run once per resource and scope of a request, not once per
span (``RequestMemo``).

**Replies.** Success is an ``ExportTraceServiceResponse`` in the request's
encoding: empty (``b""`` or ``{}``), or with ``partial_success`` when spans were
rejected. Errors the receiver raises are ``google.rpc.Status`` bodies, encoded by
hand (``code`` and ``message``; ``details`` stays empty), with the HTTP-to-gRPC
code mapping of the OpenTelemetry Collector's receiver.

This module needs ``opentelemetry-proto`` (the ``server`` extra).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from .call_capture import OtlpSpan, RequestMemo, resolve_session_key

PROTOBUF_MEDIA_TYPE = "application/x-protobuf"
JSON_MEDIA_TYPE = "application/json"
MEDIA_TYPES = (PROTOBUF_MEDIA_TYPE, JSON_MEDIA_TYPE)
CONTENT_ENCODINGS = ("identity", "gzip", "deflate")
# The catalog stores nanosecond times in signed 64-bit columns.
MAX_TIME_UNIX_NANO = 2**63 - 1
# The longest attribute key a span may carry, in UTF-8 bytes (span, events, links,
# resource, scope).
MAX_ATTRIBUTE_KEY_BYTES = 1024
# What a request's spans share is stored more than once: a span's row holds its scope
# name, and ``otlp_resources`` holds a resource and scope once per distinct pair
# (tribunal round 3, N3-1b and N3-1c). These bound each resource and scope, and
# ``MAX_REQUEST_SHARED_BYTES`` what one request stores more than once
# (docs/production.md).
MAX_SCOPE_NAME_BYTES = 256
# The scope's attributes as serialized (its ``attributes`` field).
MAX_SCOPE_ATTRIBUTES_BYTES = 16 * 1024
# The resource as serialized, attributes included, plus its ``ResourceSpans``' schema
# URL: both are stored again with every scope under the resource.
MAX_RESOURCE_BYTES = 64 * 1024
# Distinct instrumentation scopes (name, version, attributes and schema URL) among one
# request's accepted spans.
MAX_REQUEST_SCOPES = 256
# What one request's accepted spans store more than once: each span's scope name,
# and each distinct resource and scope pair's resource, scope and schema URLs.
# Ordinary exports use kilobytes of it.
MAX_REQUEST_SHARED_BYTES = 8 * 1024 * 1024
TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
# The most one request waits, in total, for every lock its transaction needs.
LOCK_BUDGET_SECONDS = 2.0
# Every 503 says when to retry (as every other Earshot 503 does); never 429,
# which the Python exporter drops instead of retrying.
RETRY_AFTER_SECONDS = 3

# Why a span was rejected, in the order they are checked and reported.
REJECTION_REASONS = {
    "trace_id": "with a trace ID that is not 16 non-zero bytes",
    "span_id": "with a span ID that is not 8 non-zero bytes",
    "parent_span_id": "with a parent span ID that is neither empty nor 8 bytes",
    "link_id": "with a link ID of the wrong length",
    "time_range": "with a time outside [1, 2^63-1]",
    "end_before_start": "ending before it starts",
    "nul_text": "with U+0000 in its name, scope name or session key",
    "attribute_key": f"with an attribute key longer than {MAX_ATTRIBUTE_KEY_BYTES} bytes",
    "scope_name": f"with an instrumentation scope name longer than {MAX_SCOPE_NAME_BYTES} bytes",
    "scope_attributes": (
        f"with instrumentation scope attributes over {MAX_SCOPE_ATTRIBUTES_BYTES} bytes"
    ),
    "resource": f"with a resource over {MAX_RESOURCE_BYTES} bytes",
    # Checked by ``validate_spans``, over the request.
    "scope_limit": f"under an instrumentation scope past the request's first {MAX_REQUEST_SCOPES}",
    "shared_bytes": (
        f"past the request's {MAX_REQUEST_SHARED_BYTES} bytes of repeated resource and scope"
    ),
}

# The OpenTelemetry Collector's mapping of an HTTP status onto a gRPC code
# (``internal/statusutil``, after grpc's http-grpc-status-mapping).
_GRPC_CODES = {400: 3, 401: 16, 403: 7, 404: 12, 429: 8, 502: 14, 503: 14, 504: 14}
_GRPC_UNKNOWN = 2

_HEX_ID = {size: re.compile(rf"[0-9A-Fa-f]{{{2 * size}}}") for size in (8, 16)}
# Decodes to one byte: wrong for every ID, so the span check rejects the span.
_UNREADABLE_ID = base64.b64encode(b"\x00").decode("ascii")


class OtlpDecodeError(ValueError):
    """The body is not an ``ExportTraceServiceRequest`` in its declared encoding."""


def request_media_type(content_type: str | None) -> str | None:
    """The OTLP encoding a ``Content-Type`` header names, or ``None`` (415)."""

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type if media_type in MEDIA_TYPES else None


def request_content_encoding(content_encoding: str | None) -> str | None:
    """The ``Content-Encoding`` as ``identity``, ``gzip`` or ``deflate``, or ``None`` (415)."""

    encoding = (content_encoding or "").strip().lower() or "identity"
    return encoding if encoding in CONTENT_ENCODINGS else None


# --- decoding -------------------------------------------------------------------------------


def decode_export_request(body: bytes, media_type: str) -> ExportTraceServiceRequest:
    """Decode a (decompressed) body; raise ``OtlpDecodeError`` if it is not a request."""

    if not body:
        return ExportTraceServiceRequest()
    if media_type == PROTOBUF_MEDIA_TYPE:
        try:
            return ExportTraceServiceRequest.FromString(body)
        except (DecodeError, RecursionError, ValueError) as error:
            raise OtlpDecodeError("the body is not a protobuf ExportTraceServiceRequest") from error
    if media_type != JSON_MEDIA_TYPE:
        raise ValueError(f"unsupported OTLP media type: {media_type!r}")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise OtlpDecodeError("the body is not UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise OtlpDecodeError("an OTLP/JSON request is a JSON object")
    _convert_json_ids(document)
    try:
        return json_format.ParseDict(
            document, ExportTraceServiceRequest(), ignore_unknown_fields=True
        )
    except (json_format.ParseError, RecursionError, ValueError, TypeError) as error:
        raise OtlpDecodeError("the body is not an OTLP/JSON ExportTraceServiceRequest") from error


def _items(container: Any, *names: str) -> Iterator[dict[str, Any]]:
    """The object items of a list field, under either JSON name (lowerCamel or snake)."""

    if not isinstance(container, dict):
        return
    for name in names:
        values = container.get(name)
        if isinstance(values, list):
            yield from (value for value in values if isinstance(value, dict))


def _convert_json_ids(document: dict[str, Any]) -> None:
    """Rewrite every span and link ID of an OTLP/JSON document as base64, in place."""

    for resource_spans in _items(document, "resourceSpans", "resource_spans"):
        for scope_spans in _items(resource_spans, "scopeSpans", "scope_spans"):
            for span in _items(scope_spans, "spans"):
                _convert_id(span, ("traceId", "trace_id"), TRACE_ID_BYTES)
                _convert_id(span, ("spanId", "span_id"), SPAN_ID_BYTES)
                _convert_id(span, ("parentSpanId", "parent_span_id"), SPAN_ID_BYTES)
                for link in _items(span, "links"):
                    _convert_id(link, ("traceId", "trace_id"), TRACE_ID_BYTES)
                    _convert_id(link, ("spanId", "span_id"), SPAN_ID_BYTES)


def _convert_id(container: dict[str, Any], names: tuple[str, ...], size: int) -> None:
    for name in names:
        if name in container and container[name] is not None:
            container[name] = _id_as_base64(container[name], size)


def _id_as_base64(value: object, size: int) -> str:
    if not isinstance(value, str):
        return _UNREADABLE_ID
    if not value:
        return ""
    if _HEX_ID[size].fullmatch(value):
        return base64.b64encode(bytes.fromhex(value)).decode("ascii")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError):
        return _UNREADABLE_ID
    return base64.b64encode(raw).decode("ascii") if len(raw) == size else _UNREADABLE_ID


# --- span checks ------------------------------------------------------------------------------


def _valid_id(value: bytes, size: int) -> bool:
    return len(value) == size and any(value)


def _in_time_domain(value: int) -> bool:
    return 1 <= value <= MAX_TIME_UNIX_NANO


def span_rejection(span: OtlpSpan, *, memo: RequestMemo | None = None) -> str | None:
    """Why ``span`` cannot be stored (a ``REJECTION_REASONS`` key), or ``None``.
    ``validate_spans`` passes one ``memo`` per request, so the resource and scope
    are checked once each, not once per span under them."""

    memo = RequestMemo() if memo is None else memo
    raw = span.span
    if not _valid_id(raw.trace_id, TRACE_ID_BYTES):
        return "trace_id"
    if not _valid_id(raw.span_id, SPAN_ID_BYTES):
        return "span_id"
    if raw.parent_span_id and len(raw.parent_span_id) != SPAN_ID_BYTES:
        return "parent_span_id"
    if any(
        len(link.trace_id) != TRACE_ID_BYTES or len(link.span_id) != SPAN_ID_BYTES
        for link in raw.links
    ):
        return "link_id"
    times = (raw.start_time_unix_nano, raw.end_time_unix_nano)
    if not all(map(_in_time_domain, times)) or not all(
        _in_time_domain(event.time_unix_nano) for event in raw.events
    ):
        return "time_range"
    if raw.end_time_unix_nano < raw.start_time_unix_nano:
        return "end_before_start"
    key = resolve_session_key(span, memo=memo)
    scope = memo.get(_scope_check, span.scope)
    resource = memo.get(_resource_check, span.resource, extra=(span.resource_schema_url,))
    if "\x00" in raw.name or scope.nul_in_name or (key and "\x00" in key.value):
        return "nul_text"
    if not (
        resource.keys_bounded
        and scope.keys_bounded
        and all(
            map(
                _attribute_keys_bounded,
                (
                    raw.attributes,
                    *(event.attributes for event in raw.events),
                    *(link.attributes for link in raw.links),
                ),
            )
        )
    ):
        return "attribute_key"
    return scope.rejection or resource.rejection


@dataclass(frozen=True, slots=True)
class _ContextCheck:
    """What ``span_rejection`` needs of a resource or a scope, found once per request:
    reading a scope's name copies it, and sizing a message serializes it."""

    keys_bounded: bool
    rejection: str | None
    # Serialized bytes, schema URL included for a resource; stored once per pair.
    size: int = 0
    # A scope's name in UTF-8 bytes, stored once per span (when within its bound).
    name_bytes: int = 0
    nul_in_name: bool = False


def _scope_check(scope: Any) -> _ContextCheck:
    name = scope.name
    rejection = None
    if not _utf8_at_most(name, MAX_SCOPE_NAME_BYTES):
        rejection = "scope_name"
    elif _field_size(scope.attributes) > MAX_SCOPE_ATTRIBUTES_BYTES:
        rejection = "scope_attributes"
    return _ContextCheck(
        keys_bounded=_attribute_keys_bounded(scope.attributes),
        rejection=rejection,
        size=scope.ByteSize() if rejection is None else 0,
        name_bytes=len(name.encode("utf-8", "surrogatepass")) if rejection is None else 0,
        nul_in_name="\x00" in name,
    )


def _resource_check(resource: Any, schema_url: str) -> _ContextCheck:
    size = resource.ByteSize() + len(schema_url.encode("utf-8", "surrogatepass"))
    return _ContextCheck(
        keys_bounded=_attribute_keys_bounded(resource.attributes),
        rejection="resource" if size > MAX_RESOURCE_BYTES else None,
        size=size,
    )


def _field_size(messages: Iterable[Any]) -> int:
    """The serialized size of a repeated message field: each entry's tag, length and bytes."""

    size = 0
    for message in messages:
        length = message.ByteSize()
        size += 1 + len(_varint(length)) + length
    return size


def _attribute_keys_bounded(attributes: Iterable[Any]) -> bool:
    """Whether every key is at most ``MAX_ATTRIBUTE_KEY_BYTES`` of UTF-8."""

    for item in attributes:
        key = item.key
        # Inline, the test every short key passes (spans carry many attributes).
        if len(key) * 4 > MAX_ATTRIBUTE_KEY_BYTES and not _utf8_at_most(
            key, MAX_ATTRIBUTE_KEY_BYTES
        ):
            return False
    return True


def _utf8_at_most(text: str, limit: int) -> bool:
    """Whether ``text`` is at most ``limit`` bytes of UTF-8. Only a text whose length
    in characters straddles the bound is encoded."""

    if len(text) * 4 <= limit:
        return True
    return len(text) <= limit and len(text.encode("utf-8", "surrogatepass")) <= limit


@dataclass(frozen=True, slots=True)
class SpanValidation:
    """The spans of one request that can be stored, and why the others cannot."""

    accepted: list[OtlpSpan]
    rejected: Mapping[str, int] = field(default_factory=dict)

    @property
    def rejected_spans(self) -> int:
        return sum(self.rejected.values())

    @property
    def error_message(self) -> str:
        """``partial_success.error_message``: counts per reason, never span values."""

        total = self.rejected_spans
        if not total:
            return ""
        parts = [
            f"{self.rejected[reason]} {text}"
            for reason, text in REJECTION_REASONS.items()
            if self.rejected.get(reason)
        ]
        return f"{total} span{'s' if total != 1 else ''} rejected: {'; '.join(parts)}"


def validate_spans(spans: Sequence[OtlpSpan]) -> SpanValidation:
    """``span_rejection`` of each span, then the request's bounds, in request order:
    the spans of its first ``MAX_REQUEST_SCOPES`` distinct scopes are kept, and those
    of any later one rejected (``scope_limit``); and a span is rejected
    (``shared_bytes``) when what it would store more than once (its scope name, and
    its resource and scope when their pair is new) would take the request past
    ``MAX_REQUEST_SHARED_BYTES``. A scope or pair counts once one of its spans is
    kept; equal scopes (and schema URLs) count once, under whichever resource, and
    equal resources (and schema URLs) once, as ``otlp_resources`` stores them."""

    accepted: list[OtlpSpan] = []
    rejected: Counter[str] = Counter()
    memo = RequestMemo()
    # Each object a span shares (a scope or resource and its schema URL) as a small
    # integer, found once per object: one for every equal scope (or resource) and
    # URL in the request, with the bytes a pair stores of it. Per-span keys are then
    # integers, whatever the size of what they stand for.
    scope_numbers: dict[tuple[int, str], int] = {}
    resource_numbers: dict[tuple[int, str], int] = {}
    shared_objects: dict[tuple[int, int], tuple[int, int]] = {}
    scopes: set[int] = set()
    pairs: set[tuple[int, int]] = set()
    shared = 0
    for span in spans:
        reason = span_rejection(span, memo=memo)
        if reason is None:
            scope, scope_bytes = _number(
                shared_objects,
                scope_numbers,
                memo,
                span.scope,
                span.scope_schema_url,
                memo.get(_scope_check, span.scope).size,
            )
            if scope not in scopes and len(scopes) >= MAX_REQUEST_SCOPES:
                reason = "scope_limit"
        if reason is None:
            resource, resource_bytes = _number(
                shared_objects,
                resource_numbers,
                memo,
                span.resource,
                span.resource_schema_url,
                memo.get(_resource_check, span.resource, extra=(span.resource_schema_url,)).size,
                url_counted=True,
            )
            pair = (resource, scope)
            cost = memo.get(_scope_check, span.scope).name_bytes
            if pair not in pairs:
                cost += resource_bytes + scope_bytes
            if shared + cost > MAX_REQUEST_SHARED_BYTES:
                reason = "shared_bytes"
            else:
                shared += cost
                scopes.add(scope)
                pairs.add(pair)
        if reason is None:
            accepted.append(span)
        else:
            rejected[reason] += 1
    return SpanValidation(accepted=accepted, rejected=dict(rejected))


def _number(
    seen: dict[tuple[int, int], tuple[int, int]],
    numbers: dict[tuple[int, str], int],
    memo: RequestMemo,
    message: Any,
    schema_url: str,
    size: int,
    *,
    url_counted: bool = False,
) -> tuple[int, int]:
    """``validate_spans``' number for a scope or resource object and its schema URL,
    with the bytes a pair stores of them (``size``, plus the URL unless ``size``
    counts it). Keyed by the objects' identities; the content key is made once per
    object."""

    identity = (id(message), id(schema_url))
    known = seen.get(identity)
    if known is None:
        url_bytes = 0 if url_counted else len(schema_url.encode("utf-8", "surrogatepass"))
        number = numbers.setdefault((id(memo.first_equal(message)), schema_url), len(numbers))
        known = seen[identity] = (number, size + url_bytes)
    return known


# --- replies ----------------------------------------------------------------------------------


def export_response_body(rejected_spans: int, error_message: str, media_type: str) -> bytes:
    """The 200 body: an ``ExportTraceServiceResponse`` in the request's encoding."""

    response = ExportTraceServiceResponse()
    if rejected_spans or error_message:
        response.partial_success.CopyFrom(
            ExportTracePartialSuccess(rejected_spans=rejected_spans, error_message=error_message)
        )
    if media_type == JSON_MEDIA_TYPE:
        return json.dumps(
            json_format.MessageToDict(response), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    return response.SerializeToString()


def grpc_code(http_status: int) -> int:
    """The ``google.rpc.Status`` code for an HTTP status (Collector mapping)."""

    return _GRPC_CODES.get(http_status, _GRPC_UNKNOWN)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def status_body(http_status: int, message: str, media_type: str) -> bytes:
    """A ``google.rpc.Status`` (``code`` = 1, ``message`` = 2, ``details`` = 3, left
    empty) in ``media_type``, encoded by hand so the server needs no extra package."""

    code = grpc_code(http_status)
    if media_type == JSON_MEDIA_TYPE:
        return json.dumps(
            {"code": code, "message": message}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    text = message.encode("utf-8")
    body = b""
    if code:
        body += b"\x08" + _varint(code)
    if text:
        body += b"\x12" + _varint(len(text)) + text
    return body


class AdmissionLimiter:
    """At most ``capacity`` requests in flight; a full limiter refuses at once (503)."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self.capacity:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("release without a matching acquire")
            self._in_flight -= 1
