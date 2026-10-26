"""The OTLP/HTTP receiver's pure half: negotiation, decoding, per-span checks, replies.

Driven by deterministic synthetic Pipecat-shaped OTLP requests and spans built
with the generated OTLP messages. Error bodies are decoded with the
reference ``google.rpc.Status`` message, not with Earshot's own encoder.
"""

from __future__ import annotations

import base64
import json

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from earshot.call_capture import spans_from_request
from earshot.otlp_receiver import (
    JSON_MEDIA_TYPE,
    MAX_ATTRIBUTE_KEY_BYTES,
    MAX_REQUEST_SCOPES,
    MAX_REQUEST_SHARED_BYTES,
    MAX_RESOURCE_BYTES,
    MAX_SCOPE_ATTRIBUTES_BYTES,
    MAX_SCOPE_NAME_BYTES,
    MAX_TIME_UNIX_NANO,
    PROTOBUF_MEDIA_TYPE,
    AdmissionLimiter,
    OtlpDecodeError,
    decode_export_request,
    export_response_body,
    grpc_code,
    request_content_encoding,
    request_media_type,
    span_rejection,
    status_body,
    validate_spans,
)
from otlp_support import (
    SECOND,
    START,
    attribute_of_size,
    best_cpu_seconds,
    equal_shared_values_request,
    make_request,
    make_span,
    otlp_json,
    rpc_status,
    shared_attributes_request,
    string_attribute,
    with_links,
)
from otlp_support import Reply as _Reply
from synthetic_otlp import synthetic_requests

pytestmark = pytest.mark.unit

MAX = MAX_TIME_UNIX_NANO
TRACE = bytes.fromhex("5b8eff5b8eff5b8eff5b8eff5b8eff01")
SPAN = bytes.fromhex("0102030405060708")


# --- negotiation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("application/x-protobuf", PROTOBUF_MEDIA_TYPE),
        ("Application/X-Protobuf", PROTOBUF_MEDIA_TYPE),
        ("application/json", JSON_MEDIA_TYPE),
        ("application/json; charset=utf-8", JSON_MEDIA_TYPE),
        (" application/json ;charset=UTF-8", JSON_MEDIA_TYPE),
        ("application/protobuf", None),
        ("application/vnd.earshot.incident+json", None),
        ("text/plain", None),
        ("", None),
        (None, None),
    ],
)
def test_the_decoder_is_chosen_by_the_parsed_media_type(header, expected) -> None:
    assert request_media_type(header) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "identity"),
        ("", "identity"),
        ("identity", "identity"),
        ("gzip", "gzip"),
        (" GZIP ", "gzip"),
        ("deflate", "deflate"),
        ("br", None),
        ("zstd", None),
        ("x-gzip", None),
        ("gzip, br", None),
    ],
)
def test_only_identity_gzip_and_deflate_are_accepted_encodings(header, expected) -> None:
    assert request_content_encoding(header) == expected


# --- decoding ---------------------------------------------------------------------------


def _deterministic(request: ExportTraceServiceRequest) -> bytes:
    return request.SerializeToString(deterministic=True)


def _base64_ids(json_body: bytes) -> bytes:
    """The same OTLP/JSON with every ID as base64 (what some real clients send)."""

    document = json.loads(json_body)
    for resource_spans in document["resourceSpans"]:
        for scope_spans in resource_spans["scopeSpans"]:
            for span in scope_spans["spans"]:
                for target in (span, *span.get("links", [])):
                    for field in ("traceId", "spanId", "parentSpanId"):
                        if target.get(field):
                            raw = bytes.fromhex(target[field])
                            target[field] = base64.b64encode(raw).decode().rstrip("=")
    return json.dumps(document).encode()


def test_protobuf_and_json_decode_the_real_capture_to_identical_bytes_links_included() -> None:
    requests = with_links(synthetic_requests())
    assert sum(len(span.span.links) for r in requests for span in spans_from_request(r)) == 16

    for request in requests:
        wire = request.SerializeToString()
        expected = _deterministic(decode_export_request(wire, PROTOBUF_MEDIA_TYPE))
        assert expected == _deterministic(request)
        lower = otlp_json(request)
        upper = otlp_json(request, upper=True)
        assert lower != upper and lower.lower() == upper.lower()
        for body in (lower, upper, _base64_ids(lower)):
            assert _deterministic(decode_export_request(body, JSON_MEDIA_TYPE)) == expected


def test_json_ids_accept_snake_case_keys_and_padded_base64() -> None:
    body = {
        "resource_spans": [
            {
                "scope_spans": [
                    {
                        "spans": [
                            {
                                "trace_id": TRACE.hex(),
                                "span_id": base64.b64encode(SPAN).decode(),
                                "parent_span_id": "",
                                "links": [{"trace_id": TRACE.hex().upper(), "span_id": SPAN.hex()}],
                                "start_time_unix_nano": "5",
                                "end_time_unix_nano": 6,
                                "kind": "SPAN_KIND_SERVER",
                                "unknownField": {"ignored": True},
                            }
                        ]
                    }
                ]
            }
        ]
    }

    decoded = decode_export_request(json.dumps(body).encode(), JSON_MEDIA_TYPE)

    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    assert (span.trace_id, span.span_id, span.parent_span_id) == (TRACE, SPAN, b"")
    assert (span.links[0].trace_id, span.links[0].span_id) == (TRACE, SPAN)
    assert (span.start_time_unix_nano, span.end_time_unix_nano, span.kind) == (5, 6, 2)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("traceId", TRACE.hex()[:30], "trace_id"),  # hex of 15 bytes
        ("traceId", TRACE.hex() + "00", "trace_id"),  # hex of 17 bytes
        ("traceId", "g" * 32, "trace_id"),  # neither hex nor base64
        ("traceId", base64.b64encode(TRACE[:12]).decode(), "trace_id"),  # base64 of 12 bytes
        ("traceId", "", "trace_id"),
        ("traceId", "0" * 32, "trace_id"),  # all zero
        ("spanId", SPAN.hex()[:14], "span_id"),
        ("spanId", "00 01 02 03 04 05 06 07", "span_id"),  # spaces are not hex
        ("spanId", "-_-_-_-_-_-_", "span_id"),  # URL-safe base64 is not accepted
        ("spanId", 7, "span_id"),
        ("parentSpanId", SPAN.hex()[:15], "parent_span_id"),
        ("parentSpanId", base64.b64encode(TRACE).decode(), "parent_span_id"),
        ("linkTraceId", SPAN.hex(), "link_id"),
        ("linkSpanId", TRACE.hex(), "link_id"),
    ],
)
def test_a_json_id_that_is_not_hex_or_base64_of_its_size_rejects_only_its_span(
    field, value, reason
) -> None:
    span = {
        "traceId": TRACE.hex(),
        "spanId": SPAN.hex(),
        "startTimeUnixNano": "1",
        "endTimeUnixNano": "2",
        "links": [{"traceId": TRACE.hex(), "spanId": SPAN.hex()}],
    }
    if field.startswith("link"):
        span["links"][0][field[4].lower() + field[5:]] = value
    else:
        span[field] = value
    good = {**span, "traceId": TRACE.hex(), "spanId": "0a" * 8, "links": []}
    if field == "parentSpanId":
        good.pop("parentSpanId")
    body = {"resourceSpans": [{"scopeSpans": [{"spans": [span, good]}]}]}

    decoded = decode_export_request(json.dumps(body).encode(), JSON_MEDIA_TYPE)
    spans = spans_from_request(decoded)

    assert [span_rejection(item) for item in spans] == [reason, None]


def _one_json_span(fields: str) -> bytes:
    return ('{"resourceSpans": [{"scopeSpans": [{"spans": [{' + fields + "}]}]}]}").encode()


@pytest.mark.parametrize(
    ("media_type", "body"),
    [
        (PROTOBUF_MEDIA_TYPE, b"\xff\xff\xff\xff"),
        (PROTOBUF_MEDIA_TYPE, b"\x0a\x05\x12\x03\x0a"),  # truncated nested message
        (JSON_MEDIA_TYPE, b'{"resourceSpans": [}'),
        (JSON_MEDIA_TYPE, '{"resourceSpans": []}'.encode("utf-16")),
        (JSON_MEDIA_TYPE, b'{"resourceSpans": [{"resource": {"attributes": "\xff"}}]}'),
        (JSON_MEDIA_TYPE, b"[]"),
        (JSON_MEDIA_TYPE, b'"traces"'),
        (JSON_MEDIA_TYPE, b'{"resourceSpans": {"scopeSpans": []}}'),
        (JSON_MEDIA_TYPE, _one_json_span('"startTimeUnixNano": "x"')),
        (JSON_MEDIA_TYPE, _one_json_span('"startTimeUnixNano": "18446744073709551616"')),
    ],
)
def test_an_undecodable_body_is_refused_as_a_whole(media_type, body) -> None:
    with pytest.raises(OtlpDecodeError):
        decode_export_request(body, media_type)


def test_invalid_utf8_inside_a_protobuf_string_refuses_the_whole_message() -> None:
    wire = make_request([make_span(1, 1, name="placeholder")]).SerializeToString()
    corrupted = wire.replace(b"placeholder", b"placeh\xffder")

    with pytest.raises(OtlpDecodeError):
        decode_export_request(corrupted, PROTOBUF_MEDIA_TYPE)


@pytest.mark.parametrize(
    ("media_type", "body"),
    [
        (PROTOBUF_MEDIA_TYPE, b""),
        (JSON_MEDIA_TYPE, b""),
        (JSON_MEDIA_TYPE, b"{}"),
        (JSON_MEDIA_TYPE, b'{"resourceSpans": []}'),
    ],
)
def test_an_empty_request_decodes_to_no_spans(media_type, body) -> None:
    assert spans_from_request(decode_export_request(body, media_type)) == []


# --- Per-span checks -------------------------------------------------------


def _one(**changes) -> object:
    span = make_span(1, 1, start=START, end=START + SECOND)
    events = changes.pop("event_times", ())
    scope = changes.pop("scope", "pipecat")
    for name, value in changes.items():
        setattr(span, name, value)
    for value in events:
        span.events.add(name="e", time_unix_nano=value)
    return spans_from_request(make_request([span], scope=scope))[0]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({}, None),
        ({"start_time_unix_nano": 1, "end_time_unix_nano": 1}, None),  # end == start
        ({"start_time_unix_nano": MAX, "end_time_unix_nano": MAX}, None),
        ({"parent_span_id": SPAN}, None),
        ({"trace_id": b""}, "trace_id"),
        ({"trace_id": TRACE[:15]}, "trace_id"),
        ({"trace_id": bytes(16)}, "trace_id"),
        ({"span_id": TRACE}, "span_id"),
        ({"span_id": bytes(8)}, "span_id"),
        ({"parent_span_id": SPAN[:7]}, "parent_span_id"),
        ({"start_time_unix_nano": 0}, "time_range"),
        ({"start_time_unix_nano": 2**63, "end_time_unix_nano": 2**63}, "time_range"),
        ({"end_time_unix_nano": 2**64 - 1}, "time_range"),
        ({"end_time_unix_nano": 0}, "time_range"),
        ({"end_time_unix_nano": START - 1}, "end_before_start"),
        ({"event_times": (START, 0)}, "time_range"),
        ({"event_times": (2**63,)}, "time_range"),
        ({"name": "a\x00b"}, "nul_text"),
        ({"scope": "pipe\x00cat"}, "nul_text"),
    ],
)
def test_each_span_is_checked_for_ids_times_and_storable_text(changes, reason) -> None:
    assert span_rejection(_one(**changes)) == reason


def test_a_session_key_holding_nul_rejects_its_span() -> None:
    span = make_span(1, 1, attributes=[string_attribute("earshot.session.id", "call\x001")])
    assert span_rejection(spans_from_request(make_request([span]))[0]) == "nul_text"


LONG_KEY = "k" * (MAX_ATTRIBUTE_KEY_BYTES + 1)


@pytest.mark.parametrize(
    ("where", "key", "reason"),
    [
        ("span", "k" * MAX_ATTRIBUTE_KEY_BYTES, None),
        ("span", "é" * (MAX_ATTRIBUTE_KEY_BYTES // 2), None),  # 2 UTF-8 bytes each
        ("span", "é" * (MAX_ATTRIBUTE_KEY_BYTES // 2 + 1), "attribute_key"),
        ("span", LONG_KEY, "attribute_key"),
        ("event", LONG_KEY, "attribute_key"),
        ("link", LONG_KEY, "attribute_key"),
        ("resource", LONG_KEY, "attribute_key"),
        ("scope", LONG_KEY, "attribute_key"),
    ],
    ids=[
        "at-bound",
        "multibyte-at-bound",
        "multibyte-over",
        "span",
        "event",
        "link",
        "resource",
        "scope",
    ],
)
def test_an_attribute_key_longer_than_its_bound_rejects_its_span(where, key, reason) -> None:
    """Attribute keys are chosen by senders and must have a size limit."""

    span = make_span(1, 1)
    request = make_request([span])
    resource_spans = request.resource_spans[0]
    target = {
        "span": lambda: resource_spans.scope_spans[0].spans[0].attributes,
        "event": lambda: resource_spans.scope_spans[0].spans[0].events.add(name="e").attributes,
        "link": lambda: (
            resource_spans.scope_spans[0]
            .spans[0]
            .links.add(trace_id=TRACE, span_id=SPAN)
            .attributes
        ),
        "resource": lambda: resource_spans.resource.attributes,
        "scope": lambda: resource_spans.scope_spans[0].scope.attributes,
    }[where]()
    if where == "event":
        resource_spans.scope_spans[0].spans[0].events[0].time_unix_nano = START
    target.append(string_attribute(key, "v"))

    assert span_rejection(spans_from_request(request)[0]) == reason


def _under(
    *,
    scope_name: str = "pipecat",
    scope_attributes: tuple[KeyValue, ...] = (),
    resource: tuple[KeyValue, ...] = (),
    resource_schema_url: str = "",
) -> object:
    """One span under a resource and a scope made of these."""

    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=resource),
                schema_url=resource_schema_url,
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name=scope_name, attributes=scope_attributes),
                        spans=[make_span(1, 1)],
                    )
                ],
            )
        ]
    )
    return spans_from_request(request)[0]


SCHEMA_URL = "https://opentelemetry.io/schemas/1.27.0"


def _scope_attributes(size: int) -> dict[str, object]:
    return {"scope_attributes": (attribute_of_size(InstrumentationScope, size),)}


def _resource(size: int, schema_url: str = "") -> dict[str, object]:
    resource = attribute_of_size(Resource, size - len(SCHEMA_URL) if schema_url else size)
    return {"resource": (resource,), "resource_schema_url": schema_url}


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        ({"scope_name": "s" * MAX_SCOPE_NAME_BYTES}, None),
        ({"scope_name": "é" * (MAX_SCOPE_NAME_BYTES // 2)}, None),  # 2 UTF-8 bytes each
        ({"scope_name": "é" * (MAX_SCOPE_NAME_BYTES // 2 + 1)}, "scope_name"),
        ({"scope_name": "s" * 100_000}, "scope_name"),
        (_scope_attributes(MAX_SCOPE_ATTRIBUTES_BYTES), None),
        (_scope_attributes(MAX_SCOPE_ATTRIBUTES_BYTES + 1), "scope_attributes"),
        (_resource(MAX_RESOURCE_BYTES), None),
        (_resource(MAX_RESOURCE_BYTES + 1), "resource"),
        (_resource(MAX_RESOURCE_BYTES, SCHEMA_URL), None),
        (_resource(MAX_RESOURCE_BYTES + 1, SCHEMA_URL), "resource"),  # the URL counts
    ],
    ids=[
        "scope-name-at-bound",
        "multibyte-scope-name-at-bound",
        "multibyte-scope-name-over",
        "scope-name-100kb",
        "scope-attributes-at-bound",
        "scope-attributes-over",
        "resource-at-bound",
        "resource-over",
        "resource-and-schema-url-at-bound",
        "resource-and-schema-url-over",
    ],
)
def test_a_scope_or_resource_past_its_size_bound_rejects_its_spans(context, reason) -> None:
    """A span's row stores its scope name, and the
    resource is stored again with every scope under it, so each is bounded."""

    assert span_rejection(_under(**context)) == reason


def test_a_request_keeps_the_spans_of_its_first_256_distinct_scopes() -> None:
    """A scope takes one of the request's slots when one of
    its spans is accepted; equal scopes take one slot wherever they are, under any
    resource; a schema URL makes another scope."""

    def scope_spans(name: str, *spans: Span, schema_url: str = "") -> ScopeSpans:
        scope = InstrumentationScope(name=name)
        return ScopeSpans(scope=scope, schema_url=schema_url, spans=spans)

    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                scope_spans=[
                    scope_spans("bad", make_span(0, 1)),  # rejected for its trace ID
                    *(
                        scope_spans(f"s{index}", make_span(index + 1, 1))
                        for index in range(MAX_REQUEST_SCOPES)
                    ),
                    scope_spans("late", make_span(900, 1), make_span(900, 2)),
                    scope_spans("s0", make_span(901, 1)),
                    scope_spans("s1", make_span(902, 1), schema_url=SCHEMA_URL),
                ]
            ),
            ResourceSpans(
                resource=Resource(attributes=[string_attribute("service.name", "other")]),
                scope_spans=[scope_spans("s2", make_span(903, 1))],
            ),
        ]
    )

    result = validate_spans(spans_from_request(request))

    assert [int.from_bytes(span.span.trace_id, "big") for span in result.accepted] == [
        *range(1, MAX_REQUEST_SCOPES + 1),
        901,
        903,
    ]
    assert result.rejected == {"trace_id": 1, "scope_limit": 3}
    assert result.error_message == (
        "4 spans rejected: 1 with a trace ID that is not 16 non-zero bytes; 3 under an "
        f"instrumentation scope past the request's first {MAX_REQUEST_SCOPES}"
    )


def test_a_request_stores_at_most_its_budget_of_repeated_resource_and_scope_bytes() -> None:
    """Within the per-resource and per-scope bounds, a resource
    is still stored once per distinct scope under it, so a 32 MiB request could store
    6.6 GiB. A request's accepted spans now store at most ``MAX_REQUEST_SHARED_BYTES``
    more than once; a span past it is rejected, and a later span whose resource and
    scope are already stored still fits."""

    resource = Resource(attributes=[attribute_of_size(Resource, MAX_RESOURCE_BYTES)])
    scopes = [
        ScopeSpans(scope=InstrumentationScope(name=f"s{index}"), spans=[make_span(index + 1, 1)])
        for index in range(200)
    ]
    scopes.append(ScopeSpans(scope=InstrumentationScope(name="s0"), spans=[make_span(900, 1)]))
    request = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(resource=resource, scope_spans=scopes)]
    )

    result = validate_spans(spans_from_request(request))

    # Each pair stores the resource and a little more, so one fewer than fit exactly.
    pairs = MAX_REQUEST_SHARED_BYTES // MAX_RESOURCE_BYTES - 1
    assert [int.from_bytes(span.span.trace_id, "big") for span in result.accepted] == [
        *range(1, pairs + 1),
        900,
    ]
    assert result.rejected == {"shared_bytes": 200 - pairs}
    assert result.error_message == (
        f"{200 - pairs} spans rejected: {200 - pairs} past the request's "
        f"{MAX_REQUEST_SHARED_BYTES} bytes of repeated resource and scope"
    )


def test_a_scope_name_counts_against_the_budget_once_per_span() -> None:
    """A span's row holds its scope name, so the smallest spans under a scope name at
    its bound store about six times their size; the budget bounds that too."""

    spans = MAX_REQUEST_SHARED_BYTES // MAX_SCOPE_NAME_BYTES + 10
    request = make_request(
        [make_span(1, index + 1) for index in range(spans)], scope="s" * MAX_SCOPE_NAME_BYTES
    )

    result = validate_spans(spans_from_request(request))

    accepted = len(result.accepted)
    assert spans - 10 - 8 <= accepted < spans - 10, accepted
    assert result.rejected == {"shared_bytes": spans - accepted}


@pytest.mark.parametrize(
    ("scope_per_span", "reason"),
    [(False, "scope_attributes"), (True, "resource")],
    ids=["one-scope", "equal-scopes"],
)
def test_spans_sharing_a_large_resource_and_scope_are_checked_in_time_linear_in_the_request(
    scope_per_span, reason
) -> None:
    """The resource and scope were re-checked for
    every span under them, so a request of thousands of spans under a resource of
    tens of thousands of attributes held a worker for seconds to hours. Such a
    resource and scope are now past their bounds (round 3b): every check still runs
    once per resource and scope, the size checks that reject them included."""

    def check(spans: int, attributes: int) -> float:
        body = shared_attributes_request(
            spans, attributes, scope_per_span=scope_per_span
        ).SerializeToString()

        def receive() -> None:
            request = decode_export_request(body, PROTOBUF_MEDIA_TYPE)
            assert validate_spans(spans_from_request(request)).rejected == {reason: spans}

        return best_cpu_seconds(receive)

    small, large = check(2_000, 10_000), check(4_000, 20_000)

    # Twice the request, about twice the time (spans x attributes would give 4x).
    assert large < 3 * small, (small, large)
    assert large < 1.0, large


def test_equal_large_scopes_and_schema_urls_cost_nothing_per_span_to_check() -> None:
    """Equal scopes and schema URLs in separate ``ScopeSpans`` are separate objects:
    a 4 MB schema URL and a 2 MB scope version, compared once per span, made each
    span cost their size (an 8 MB URL over 20,000 spans took 12 s)."""

    def check(url_bytes: int, version_bytes: int) -> float:
        request = equal_shared_values_request(
            8_000, url_bytes=url_bytes, version_bytes=version_bytes
        )

        def receive() -> None:
            assert len(validate_spans(spans_from_request(request)).accepted) == 8_001

        return best_cpu_seconds(receive, repeat=2)

    plain, shared = check(0, 0), check(4_000_000, 2_000_000)

    assert shared < 2 * plain + 0.1, (plain, shared)


def test_spans_under_a_long_scope_name_are_checked_in_time_linear_in_the_request() -> None:
    """Every span read its scope's name to look for U+0000, and
    reading a name copies it, so spans x name length: tens of thousands of spans
    under a megabyte name (a 2 MB request, far less gzipped) took seconds."""

    def check(spans: int, name_bytes: int) -> tuple[float, dict[str, int]]:
        scope = "s" * name_bytes
        body = make_request([make_span(i + 1, 1) for i in range(spans)], scope=scope)
        body = body.SerializeToString()
        results = []

        def receive() -> None:
            request = decode_export_request(body, PROTOBUF_MEDIA_TYPE)
            results.append(validate_spans(spans_from_request(request)).rejected)

        return best_cpu_seconds(receive), results[-1]

    (small, _), (large, rejected) = check(10_000, 500_000), check(20_000, 1_000_000)

    # Twice the spans and twice the name: about twice the time, not 4x.
    assert large < 3 * small, (small, large)
    assert large < 1.0, large
    assert rejected == {"scope_name": 20_000}


def test_link_ids_must_have_their_lengths() -> None:
    span = make_span(1, 1)
    span.links.add(trace_id=TRACE, span_id=SPAN)
    assert span_rejection(spans_from_request(make_request([span]))[0]) is None
    span.links.add(trace_id=TRACE, span_id=SPAN[:4])
    assert span_rejection(spans_from_request(make_request([span]))[0]) == "link_id"


def test_validation_keeps_the_valid_spans_in_order_and_counts_each_reason() -> None:
    spans = [
        make_span(1, 1),
        make_span(1, 2, start=2**63, end=2**63),
        make_span(1, 3, start=0),
        make_span(1, 4, end=START - SECOND),
        make_span(1, 5),
        make_span(0, 6),
    ]

    result = validate_spans(spans_from_request(make_request(spans)))

    assert [item.span.span_id[-1] for item in result.accepted] == [1, 5]
    assert result.rejected == {"time_range": 2, "end_before_start": 1, "trace_id": 1}
    assert result.rejected_spans == 4
    assert result.error_message == (
        "4 spans rejected: 1 with a trace ID that is not 16 non-zero bytes; 2 with a time "
        "outside [1, 2^63-1]; 1 ending before it starts"
    )


# --- replies ------------------------------------------------------------------------------


def _reply(body: bytes, media_type: str, status: int = 200) -> _Reply:
    return _Reply(status, {"content-type": media_type}, body)


def test_full_success_is_an_empty_response_in_the_request_encoding() -> None:
    assert export_response_body(0, "", PROTOBUF_MEDIA_TYPE) == b""
    assert export_response_body(0, "", JSON_MEDIA_TYPE) == b"{}"


@pytest.mark.parametrize("media_type", [PROTOBUF_MEDIA_TYPE, JSON_MEDIA_TYPE])
def test_partial_success_carries_the_count_and_message(media_type) -> None:
    body = export_response_body(3, "3 spans rejected", media_type)

    if media_type == JSON_MEDIA_TYPE:
        assert json.loads(body) == {
            "partialSuccess": {"rejectedSpans": "3", "errorMessage": "3 spans rejected"}
        }
        decoded = json_format.Parse(body, ExportTraceServiceResponse())
    else:
        decoded = ExportTraceServiceResponse.FromString(body)
    assert decoded.partial_success.rejected_spans == 3
    assert decoded.partial_success.error_message == "3 spans rejected"


@pytest.mark.parametrize(
    ("http_status", "code"),
    [(400, 3), (401, 16), (403, 7), (404, 12), (410, 2), (413, 2), (415, 2), (429, 8), (503, 14)],
)
def test_http_statuses_map_to_grpc_codes_as_the_collector_maps_them(http_status, code) -> None:
    assert grpc_code(http_status) == code


@pytest.mark.parametrize("media_type", [PROTOBUF_MEDIA_TYPE, JSON_MEDIA_TYPE])
@pytest.mark.parametrize(
    ("http_status", "message"),
    [(400, "the body is not a request"), (503, "busy; retry"), (413, "é" * 200)],
)
def test_status_bodies_decode_with_the_reference_message(media_type, http_status, message) -> None:
    body = status_body(http_status, message, media_type)

    assert rpc_status(_reply(body, media_type, http_status)) == (grpc_code(http_status), message)
    if media_type == JSON_MEDIA_TYPE:
        assert json.loads(body) == {"code": grpc_code(http_status), "message": message}


def test_the_protobuf_status_is_hand_encoded_field_by_field() -> None:
    # code = 3 (field 1, varint), message = "bad" (field 2, length-delimited); no details.
    assert status_body(400, "bad", PROTOBUF_MEDIA_TYPE) == b"\x08\x03\x12\x03bad"


def test_the_admission_limiter_admits_up_to_its_capacity_and_frees_slots() -> None:
    limiter = AdmissionLimiter(2)

    assert [limiter.try_acquire() for _ in range(3)] == [True, True, False]
    assert limiter.in_flight == 2
    limiter.release()
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    with pytest.raises(ValueError):
        AdmissionLimiter(0)
