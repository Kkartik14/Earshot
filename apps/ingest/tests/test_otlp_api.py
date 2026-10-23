"""``POST /v1/traces``, ``GET /v1/otlp/stats`` and ``POST /v1/calls:resolve`` over real HTTP.

Every test runs the real app on a real uvicorn server (``otlp_support.serve``).
The exporter tests build spans with the real OpenTelemetry SDK and send them with
the real ``OTLPSpanExporter``, configured only by ``OTEL_EXPORTER_OTLP_*``. The
call tests replay synthetic Pipecat-shaped OTLP requests. Both dialects (``--db``).
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans

from synthetic_otlp import (
    SYNTHETIC_SESSION_KEY,
    SYNTHETIC_USER_ID,
    synthetic_requests,
    with_identity,
)
from earshot.api import ApiConfig, create_app
from earshot.otlp_receiver import (
    MAX_ATTRIBUTE_KEY_BYTES,
    MAX_REQUEST_SCOPES,
    MAX_REQUEST_SHARED_BYTES,
    MAX_RESOURCE_BYTES,
    MAX_SCOPE_NAME_BYTES,
)
from earshot.storage_calls import OTLP_COUNTERS
from earshot.storage_schema import CALLS_TABLES
from otlp_support import (
    JSON,
    PROTOBUF,
    SECOND,
    START,
    attribute_of_size,
    export_response,
    keyed_request,
    make_request,
    make_span,
    otlp_json,
    post_otlp,
    request,
    rpc_status,
    serve,
    string_attribute,
    with_links,
)
from store_support import catalog, catalog_rows, make_store, stored_needles

pytestmark = pytest.mark.integration

STORED_SPANS = (
    "SELECT project_id, trace_id, span_id, parent_span_id, name, scope_name, start_unix_nano, "
    "end_unix_nano, end_marker, resource_digest, payload, digest FROM spans "
    "ORDER BY project_id, trace_id, span_id"
)


def _span_count(path: Path, project_id: str = "default") -> int:
    return catalog_rows(path, "SELECT count(*) FROM spans WHERE project_id = ?", (project_id,))[0][
        0
    ]


def _stats(port: int, credential: str | None = None) -> dict[str, int]:
    headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    reply = request(port, "GET", "/v1/otlp/stats", headers=headers)
    assert reply.status == 200, reply.body
    return reply.json()


def _replay(port: int, requests, *, media_type: str = PROTOBUF, **post) -> list:
    replies = []
    for item in requests:
        body = item.SerializeToString() if media_type == PROTOBUF else otlp_json(item)
        reply = post_otlp(port, body, media_type=media_type, **post)
        assert reply.status == 200, (reply.status, reply.body)
        replies.append(reply)
    return replies


# --- the real exporter, at the root path -----------------------------------------------------


@pytest.mark.parametrize("compression", ["none", "gzip", "deflate"])
def test_the_real_exporter_reaches_the_receiver_from_the_endpoint_variable_alone(
    tmp_path, monkeypatch, compression
) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")
    credential = store.issue_api_key("support", label="exporter").credential
    with serve(create_app(store=store)) as port:
        # The SDK appends /v1/traces to the endpoint itself; nothing else is set.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Bearer%20{credential}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", compression)
        provider = TracerProvider(resource=Resource.create({"service.name": "otlp-e2e"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
        tracer = provider.get_tracer("pipecat.turn")
        with tracer.start_as_current_span(
            "conversation", attributes={"earshot.session.id": "call-exporter-1"}
        ):
            for name in ("stt", "llm", "tts"):
                with tracer.start_as_current_span(name):
                    pass
        provider.shutdown()
        stats = _stats(port, credential)

    assert _span_count(tmp_path, "support") == 4
    assert _span_count(tmp_path, "default") == 0
    calls = catalog_rows(tmp_path, "SELECT project_id, key_source, span_count FROM calls")
    assert calls == [("support", "earshot.session.id", 4)]
    assert stats == {**dict.fromkeys(OTLP_COUNTERS, 0), "parked_traces": 0, "open_calls": 1}


# --- encodings, decoding and limits ------------------------------------------------------------


def test_protobuf_and_upper_case_hex_json_store_byte_identical_spans_and_resources(
    tmp_path,
) -> None:
    replies = {}
    for media_type, path in ((PROTOBUF, tmp_path / "protobuf"), (JSON, tmp_path / "json")):
        with serve(create_app(store=make_store(path))) as port:
            if media_type == PROTOBUF:
                replies[media_type] = _replay(port, with_links(synthetic_requests()))
            else:
                for item in with_links(synthetic_requests()):
                    reply = post_otlp(port, otlp_json(item, upper=True), media_type=JSON)
                    assert reply.status == 200, reply.body
                    replies.setdefault(media_type, []).append(reply)

    assert {(r.media_type, r.body) for r in replies[PROTOBUF]} == {(PROTOBUF, b"")}
    assert {(r.media_type, r.body) for r in replies[JSON]} == {(JSON, b"{}")}
    stored = catalog_rows(tmp_path / "protobuf", STORED_SPANS)
    assert len(stored) == 17
    assert catalog_rows(tmp_path / "json", STORED_SPANS) == stored
    resources = "SELECT resource_digest, payload FROM otlp_resources ORDER BY resource_digest"
    assert catalog_rows(tmp_path / "json", resources) == catalog_rows(
        tmp_path / "protobuf", resources
    )


def test_a_media_type_with_parameters_is_accepted(tmp_path) -> None:
    body = otlp_json(make_request([make_span(1, 1)]))
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type="Application/JSON; charset=utf-8")

    assert (reply.status, reply.media_type, reply.body) == (200, JSON, b"{}")
    assert _span_count(tmp_path) == 1


@pytest.mark.parametrize(
    ("media_type", "encoding", "status_type"),
    [
        ("text/plain", None, JSON),  # no request encoding: the Collector's JSON fallback
        ("application/protobuf", None, JSON),
        (PROTOBUF, "br", PROTOBUF),
        (JSON, "zstd", JSON),
        (PROTOBUF, "gzip, br", PROTOBUF),
    ],
)
def test_an_unknown_media_type_or_encoding_is_415_with_a_status_body(
    tmp_path, media_type, encoding, status_type
) -> None:
    body = make_request([make_span(1, 1)]).SerializeToString()
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type=media_type, encoding=encoding)

    assert (reply.status, reply.media_type) == (415, status_type)
    code, message = rpc_status(reply)
    assert code == 2 and message
    assert _span_count(tmp_path) == 0


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress), ("deflate", _raw_deflate)],
    ids=["gzip", "deflate-zlib", "deflate-raw"],
)
@pytest.mark.parametrize("media_type", [PROTOBUF, JSON])
def test_gzip_zlib_deflate_and_raw_deflate_bodies_are_decoded(
    tmp_path, encoding, compress, media_type
) -> None:
    payload = make_request([make_span(1, 1), make_span(1, 2)])
    body = payload.SerializeToString() if media_type == PROTOBUF else otlp_json(payload)
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, compress(body), media_type=media_type, encoding=encoding)

    assert reply.status == 200, reply.body
    assert _span_count(tmp_path) == 2


LIMITS = ApiConfig(max_body_bytes=4096, otlp_max_protobuf_bytes=8192, otlp_max_json_bytes=2048)


def _padded(size: int, media_type: str) -> bytes:
    """A valid request of about ``size`` bytes (one long attribute)."""

    span = make_span(1, 1, attributes=[string_attribute("pad", "x" * size)])
    request_ = make_request([span])
    return request_.SerializeToString() if media_type == PROTOBUF else otlp_json(request_)


@pytest.mark.parametrize(
    ("media_type", "body", "encoding"),
    [
        # Over the compressed (wire) bound, the existing request limit.
        (PROTOBUF, _padded(5000, PROTOBUF), None),
        # A small gzip body that inflates past the protobuf bound (a bomb).
        (PROTOBUF, gzip.compress(_padded(9000, PROTOBUF)), "gzip"),
        (PROTOBUF, zlib.compress(_padded(9000, PROTOBUF)), "deflate"),
        # Under the wire bound but over the JSON bound, as sent and after inflating.
        (JSON, _padded(3000, JSON), None),
        (JSON, gzip.compress(_padded(3000, JSON)), "gzip"),
    ],
    ids=["wire", "gzip-bomb", "deflate-bomb", "json-identity", "json-gzip"],
)
def test_bodies_over_the_wire_or_decoded_bounds_are_413(tmp_path, media_type, body, encoding):
    with serve(create_app(store=make_store(tmp_path), config=LIMITS)) as port:
        reply = post_otlp(port, body, media_type=media_type, encoding=encoding)
        # Just under the bounds is accepted.
        fits = post_otlp(port, _padded(1500, media_type), media_type=media_type)

    assert (reply.status, reply.media_type) == (413, media_type)
    assert rpc_status(reply)[0] == 2
    assert fits.status == 200, fits.body
    assert _span_count(tmp_path) == 1


@pytest.mark.parametrize(
    ("media_type", "body", "encoding"),
    [
        (PROTOBUF, b"\xff\xff\xff\xff", None),
        (JSON, b'{"resourceSpans": [{"resource": "\xff"}]}', None),
        (JSON, b"[]", None),
        (PROTOBUF, b"\x1f\x8b\x08\x00 not gzip", "gzip"),
        (PROTOBUF, b"neither zlib nor raw deflate", "deflate"),
    ],
    ids=["protobuf", "json-invalid-utf8", "json-array", "bad-gzip", "bad-deflate"],
)
def test_an_undecodable_body_is_400_with_a_status_in_the_request_encoding(
    tmp_path, media_type, body, encoding
) -> None:
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type=media_type, encoding=encoding)

    assert (reply.status, reply.media_type) == (400, media_type)
    assert rpc_status(reply)[0] == 3


@pytest.mark.parametrize(
    ("media_type", "body", "expected"),
    [
        (PROTOBUF, b"", b""),
        (JSON, b"", b"{}"),
        (JSON, b"{}", b"{}"),
        (PROTOBUF, ExportTraceServiceRequest().SerializeToString(), b""),
        (JSON, b'{"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}', b"{}"),
    ],
)
def test_an_empty_body_or_request_is_200(tmp_path, media_type, body, expected) -> None:
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type=media_type)

    assert (reply.status, reply.media_type, reply.body) == (200, media_type, expected)


def _raw_deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(payload) + compressor.flush()


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress), ("deflate", _raw_deflate)],
    ids=["gzip", "deflate-zlib", "deflate-raw"],
)
@pytest.mark.parametrize(("media_type", "expected"), [(PROTOBUF, b""), (JSON, b"{}")])
def test_an_empty_compressed_body_is_200(tmp_path, encoding, compress, media_type, expected):
    """AC5.5 holds under compression too (tribunal round 1, L-4)."""

    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, compress(b""), media_type=media_type, encoding=encoding)

    assert (reply.status, reply.media_type, reply.body) == (200, media_type, expected)


# --- per-span rejection (AC5.3, AC5.4) --------------------------------------------------------


@pytest.mark.parametrize("media_type", [PROTOBUF, JSON])
def test_spans_outside_the_time_domain_are_rejected_alone_and_the_rest_stored(
    tmp_path, media_type
) -> None:
    spans = [
        make_span(7, 1, start=START),
        make_span(7, 2, start=2**63, end=2**63),  # past the signed 64-bit columns
        make_span(7, 3, start=0, end=START),
        make_span(7, 4, start=START, end=START - SECOND),  # ends before it starts
        make_span(7, 5, start=START + SECOND),
        make_span(7, 6, attributes=[string_attribute("earshot.session.id", "call-7")]),
    ]
    payload = make_request(spans)
    body = payload.SerializeToString() if media_type == PROTOBUF else otlp_json(payload)
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type=media_type)
        stats = _stats(port)

    assert reply.media_type == media_type
    partial = export_response(reply).partial_success
    assert partial.rejected_spans == 3
    assert partial.error_message == (
        "3 spans rejected: 2 with a time outside [1, 2^63-1]; 1 ending before it starts"
    )
    stored = catalog_rows(tmp_path, "SELECT span_id FROM spans ORDER BY span_id")
    assert [bytes(row[0])[-1] for row in stored] == [1, 5, 6]
    assert stats["rejected_spans"] == 3
    assert catalog_rows(tmp_path, "SELECT span_count FROM calls") == [(3,)]


@pytest.mark.parametrize("media_type", [PROTOBUF, JSON])
def test_a_span_with_an_attribute_key_past_its_bound_is_rejected_alone(
    tmp_path, media_type
) -> None:
    # Tribunal round 2, Linus N-2: a sender-chosen key of megabytes is refused, counted.
    long_key = "k" * (MAX_ATTRIBUTE_KEY_BYTES + 1)
    spans = [make_span(9, 1), make_span(9, 2, attributes=[string_attribute(long_key, "v")])]
    payload = make_request(spans)
    body = payload.SerializeToString() if media_type == PROTOBUF else otlp_json(payload)
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body, media_type=media_type)
        stats = _stats(port)

    partial = export_response(reply).partial_success
    assert (reply.status, partial.rejected_spans) == (200, 1)
    assert partial.error_message == (
        f"1 span rejected: 1 with an attribute key longer than {MAX_ATTRIBUTE_KEY_BYTES} bytes"
    )
    assert _span_count(tmp_path) == 1
    assert stats["rejected_spans"] == 1


def test_a_span_name_holding_nul_is_rejected_on_both_dialects(tmp_path) -> None:
    payload = make_request([make_span(8, 1, name="llm\x00x"), make_span(8, 2)])
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, payload.SerializeToString())

    assert export_response(reply).partial_success.rejected_spans == 1
    assert _span_count(tmp_path) == 1


# --- what one request can store (tribunal round 3b) ----------------------------------------


# What a request stores of its spans' own bytes, at most, per decompressed byte: the
# smallest spans there are (IDs and times only), each a trace of its own.
SPAN_BYTES_STORED_PER_BYTE = 8


def _within_storage_bound(stored: int, body: bytes) -> bool:
    """``docs/production.md``: a request stores its spans, and at most
    ``MAX_REQUEST_SHARED_BYTES`` more than once."""

    return stored <= MAX_REQUEST_SHARED_BYTES + SPAN_BYTES_STORED_PER_BYTE * len(body)


def _stored_bytes(path: Path) -> int:
    """What the call tables hold: every stored value's length (bytes, text as UTF-8,
    and a number as 8 bytes)."""

    def size(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        if isinstance(value, bytes | memoryview):
            return len(value)
        return 8

    return sum(
        size(value)
        for table in CALLS_TABLES
        for row in catalog_rows(path, f"SELECT * FROM {table}")
        for value in row
    )


def _one_span_per_scope(
    names, *, resource=(), first_trace: int = 1, **span: object
) -> ResourceSpans:
    """A resource with one ``ScopeSpans`` per scope name, each holding one span (a
    trace of its own, made by ``make_span(…, **span)``)."""

    return ResourceSpans(
        resource=Resource(attributes=list(resource)),
        scope_spans=[
            ScopeSpans(
                scope=InstrumentationScope(name=name),
                spans=[make_span(first_trace + index, 1, **span)],
            )
            for index, name in enumerate(names)
        ],
    )


def test_a_scope_name_past_its_bound_is_rejected_not_stored_once_per_span(tmp_path) -> None:
    """Tribunal round 3, N3-1b: a span's row holds its scope name, so one 100 KB name
    over 2,000 tiny spans (a 0.26 MB request) stored 405 MB. Those spans are
    rejected; the request's other span is stored."""

    key = [string_attribute("earshot.session.id", "call-scope-name")]
    payload = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="s" * 100_000),
                        spans=[make_span(trace, 1, attributes=key) for trace in range(1, 2001)],
                    ),
                    ScopeSpans(
                        scope=InstrumentationScope(name="pipecat"),
                        spans=[make_span(2001, 1, attributes=key)],
                    ),
                ]
            )
        ]
    )
    body = payload.SerializeToString()
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body)
        stats = _stats(port)

    stored = _stored_bytes(tmp_path)
    assert _within_storage_bound(stored, body), (stored, len(body))
    partial = export_response(reply).partial_success
    assert (partial.rejected_spans, partial.error_message) == (
        2000,
        "2000 spans rejected: 2000 with an instrumentation scope name longer than "
        f"{MAX_SCOPE_NAME_BYTES} bytes",
    )
    assert (_span_count(tmp_path), stats["rejected_spans"]) == (1, 2000)


@pytest.mark.parametrize(
    ("resource", "rejected", "reason"),
    [
        # The reported request: a resource of 10,000 attributes, about 130 KB.
        (
            [string_attribute(f"a{index}", "v") for index in range(10_000)],
            2000,
            f"with a resource over {MAX_RESOURCE_BYTES} bytes",
        ),
        # A resource of 16 KiB: stored with the request's first 256 scopes only (the
        # 4 MiB this stores more than once is within the request's budget).
        (
            [attribute_of_size(Resource, 16 * 1024)],
            2000 - MAX_REQUEST_SCOPES,
            f"under an instrumentation scope past the request's first {MAX_REQUEST_SCOPES}",
        ),
        # A resource at its bound: stored with as many scopes as the budget holds.
        (
            [attribute_of_size(Resource, MAX_RESOURCE_BYTES)],
            2000 - (MAX_REQUEST_SHARED_BYTES // MAX_RESOURCE_BYTES - 1),
            f"past the request's {MAX_REQUEST_SHARED_BYTES} bytes of repeated resource and scope",
        ),
    ],
    ids=["as-reported", "resource-of-16-KiB", "resource-at-bound"],
)
def test_a_resource_is_stored_with_at_most_256_distinct_scopes_of_a_request(
    tmp_path, resource, rejected, reason
) -> None:
    """Tribunal round 3, N3-1c: ``otlp_resources`` holds a resource once per distinct
    scope under it, so a resource of 10,000 attributes over 2,000 distinct scopes of
    one span each (a 0.32 MB request) stored 562 MB."""

    names = [f"s{index}" for index in range(2000)]
    key = [string_attribute("earshot.session.id", "call-scopes")]
    payload = ExportTraceServiceRequest(
        resource_spans=[_one_span_per_scope(names, resource=resource, attributes=key)]
    )
    body = payload.SerializeToString()
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body)
        stats = _stats(port)

    stored = _stored_bytes(tmp_path)
    assert _within_storage_bound(stored, body), (stored, len(body))
    partial = export_response(reply).partial_success
    assert (partial.rejected_spans, partial.error_message) == (
        rejected,
        f"{rejected} spans rejected: {rejected} {reason}",
    )
    assert (_span_count(tmp_path), stats["rejected_spans"]) == (2000 - rejected, rejected)


def test_the_worst_requests_within_the_bounds_store_at_most_their_budget_more(
    tmp_path,
) -> None:
    """The bound ``docs/production.md`` states. A resource is stored once per distinct
    scope under it, so the most a request byte could make the catalog store is a
    resource at its bound under many scopes, each holding the smallest span there is
    (IDs and times only): two such resources under 256 scopes (a 0.15 MB request)
    asked for 32 MiB. They store their budget, and their other spans are rejected."""

    names = [str(index) for index in range(MAX_REQUEST_SCOPES)]
    payload = ExportTraceServiceRequest(
        resource_spans=[
            _one_span_per_scope(
                names,
                resource=[attribute_of_size(Resource, MAX_RESOURCE_BYTES, fill=fill)],
                first_trace=first_trace,
                name="",
            )
            for first_trace, fill in ((1, "v"), (1 + MAX_REQUEST_SCOPES, "w"))
        ]
    )
    body = payload.SerializeToString()
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body)

    kept = MAX_REQUEST_SHARED_BYTES // MAX_RESOURCE_BYTES - 1
    partial = export_response(reply).partial_success
    assert partial.rejected_spans == 2 * MAX_REQUEST_SCOPES - kept
    assert catalog_rows(tmp_path, "SELECT count(*) FROM otlp_resources") == [(kept,)]
    stored = _stored_bytes(tmp_path)
    # At most the bound, and near it: the budget is what such a request stores.
    assert stored > 0.9 * MAX_REQUEST_SHARED_BYTES, stored
    assert _within_storage_bound(stored, body), (stored, len(body))


def test_the_smallest_spans_store_at_most_their_bound_per_byte(tmp_path) -> None:
    """``SPAN_BYTES_STORED_PER_BYTE``: spans of IDs and times only, each a trace of
    its own under one short scope name, store the most rows per request byte."""

    payload = make_request([make_span(trace, 1, name="") for trace in range(1, 2001)], scope="s")
    body = payload.SerializeToString()
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = post_otlp(port, body)

    assert (reply.status, reply.body) == (200, b"")
    stored = _stored_bytes(tmp_path)
    assert stored <= SPAN_BYTES_STORED_PER_BYTE * len(body), (stored, len(body), stored / len(body))


# --- idempotency (AC5.6) -------------------------------------------------------------------


CALL_STATE = (
    "SELECT call_id, state, dirty, span_count, first_evidence_unix_nano, last_evidence_unix_nano, "
    "end_signal_unix_nano, close_due_unix_nano, pending_revision, latest_revision FROM calls"
)
TRACE_STATE = (
    "SELECT trace_id, call_id, has_session_span, first_span_received_unix_nano, "
    "last_span_received_unix_nano FROM call_traces"
)


def test_resending_synthetic_requests_stores_nothing_new_and_touches_no_call(tmp_path) -> None:
    with serve(create_app(store=make_store(tmp_path))) as port:
        _replay(port, synthetic_requests())
        before = (
            catalog_rows(tmp_path, STORED_SPANS),
            catalog_rows(tmp_path, CALL_STATE),
            catalog_rows(tmp_path, TRACE_STATE),
        )
        replies = _replay(port, synthetic_requests())
        after = (
            catalog_rows(tmp_path, STORED_SPANS),
            catalog_rows(tmp_path, CALL_STATE),
            catalog_rows(tmp_path, TRACE_STATE),
        )
        stats = _stats(port)

    assert len(before[0]) == 17
    assert after == before
    assert {reply.body for reply in replies} == {b""}
    assert stats == {**dict.fromkeys(OTLP_COUNTERS, 0), "parked_traces": 0, "open_calls": 1}
    assert catalog_rows(tmp_path, "SELECT count(*) FROM otlp_counters") == [(0,)]


def test_a_changed_payload_for_a_stored_span_keeps_the_first_write_and_counts_a_conflict(
    tmp_path,
) -> None:
    first = synthetic_requests()[0]
    changed = synthetic_requests()[0]
    changed.resource_spans[0].scope_spans[0].spans[0].name = "stt-renamed"
    with serve(create_app(store=make_store(tmp_path))) as port:
        _replay(port, [first])
        stored = catalog_rows(tmp_path, STORED_SPANS)
        reply = post_otlp(port, changed.SerializeToString())
        stats = _stats(port)

    assert (reply.status, reply.body) == (200, b"")
    assert catalog_rows(tmp_path, STORED_SPANS) == stored
    assert stats["span_conflicts"] == 1


# --- stats (AC5.12) and resolve (AC4.6) ---------------------------------------------------------


def test_stats_are_scoped_to_the_authenticated_project(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("other", display_name="Other")
    other = store.issue_api_key("other", label="other").credential
    rejected = make_request([make_span(9, 1, start=0), make_span(10, 1)])
    with serve(create_app(store=store)) as port:
        post_otlp(port, rejected.SerializeToString())  # default: 1 rejected, 1 parked
        _replay(port, synthetic_requests()[:3], credential=other)  # other: 1 open call
        default_stats, other_stats = _stats(port), _stats(port, other)

    assert default_stats == {
        **dict.fromkeys(OTLP_COUNTERS, 0),
        "rejected_spans": 1,
        "parked_traces": 1,
        "open_calls": 0,
    }
    assert other_stats == {**dict.fromkeys(OTLP_COUNTERS, 0), "parked_traces": 0, "open_calls": 1}


def _resolve(port: int, body: object, credential: str | None = None):
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return request(port, "POST", "/v1/calls:resolve", json.dumps(body).encode(), headers=headers)


def test_resolve_finds_the_call_of_a_session_key_in_the_current_generation_only(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.create_project("other", display_name="Other")
    other = store.issue_api_key("other", label="other").credential
    with serve(create_app(store=store)) as port:
        _replay(port, synthetic_requests())
        found = _resolve(port, {"session_key": f"  {SYNTHETIC_SESSION_KEY}\n"})
        foreign = _resolve(port, {"session_key": SYNTHETIC_SESSION_KEY}, other)
        unknown = _resolve(port, {"session_key": "call-never-seen"})
        invalid = _resolve(port, {"session_key": "x" * 300})
        malformed = _resolve(port, {"key": SYNTHETIC_SESSION_KEY})
        # A call purge (WS8) bumps the key's generation; resolve follows it.
        with catalog(tmp_path) as connection:
            fingerprint = connection.execute("SELECT key_fingerprint FROM calls").fetchone()[0]
            connection.execute(
                "INSERT INTO call_key_generations(project_id, key_fingerprint, generation) "
                "VALUES ('default', ?, 1)",
                (fingerprint,),
            )
            connection.commit()
        bumped = _resolve(port, {"session_key": SYNTHETIC_SESSION_KEY})
        # New evidence for the key (a new trace) starts the generation-1 call.
        assert post_otlp(port, keyed_request(SYNTHETIC_SESSION_KEY, 99, 1)).status == 200
        renewed = _resolve(port, {"session_key": SYNTHETIC_SESSION_KEY})

    call_ids = [row[0] for row in catalog_rows(tmp_path, "SELECT call_id FROM calls")]
    first_call = catalog_rows(tmp_path, "SELECT call_id FROM calls WHERE generation = 0")[0][0]
    second_call = catalog_rows(tmp_path, "SELECT call_id FROM calls WHERE generation = 1")[0][0]
    assert (found.status, found.json()) == (200, {"call_id": first_call})
    for reply in (foreign, unknown, invalid, bumped):
        assert (reply.status, reply.problem_code()) == (404, "EARSHOT_CALL_NOT_FOUND")
        assert SYNTHETIC_SESSION_KEY.encode() not in reply.body
        assert b"call-never-seen" not in reply.body
    assert malformed.status == 422
    assert (renewed.status, renewed.json()) == (200, {"call_id": second_call})
    assert sorted(call_ids) == sorted([first_call, second_call])


# --- the capture preset over HTTP (FR-3, AC3.2) ------------------------------------------------


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_metadata_only_stores_no_raw_key_and_no_user_id_through_the_receiver(
    tmp_path, preset
) -> None:
    store = make_store(tmp_path)
    store.set_capture_preset("default", preset)
    needles = [
        SYNTHETIC_SESSION_KEY.encode(),
        SYNTHETIC_USER_ID.encode(),
        b"Seven thirty works for us.",  # an stt transcript
    ]
    with serve(create_app(store=store)) as port:
        _replay(port, with_identity(synthetic_requests()))
        resolved = _resolve(port, {"session_key": SYNTHETIC_SESSION_KEY})
    store.close()  # the search reads the database files, WAL included

    assert resolved.status == 200
    assert SYNTHETIC_SESSION_KEY.encode() not in resolved.body
    if preset == "metadata_only":
        assert catalog_rows(tmp_path, "SELECT session_key, span_count FROM calls") == [(None, 17)]
        assert stored_needles(tmp_path, needles) == set()
    else:  # the control: the same search finds every needle when it is stored
        assert stored_needles(tmp_path, needles) == set(needles)
