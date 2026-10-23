"""Test support for the OTLP/HTTP receiver: real servers, raw HTTP, OTLP/JSON, spans.

Nothing here simulates Earshot. ``serve`` runs the real app on a real uvicorn
server in a thread; ``post`` speaks HTTP/1.1 over a real socket (so a test can
set any ``Host`` header); ``otlp_json`` writes the OTLP/JSON encoding a
spec-following client sends (hex IDs, integer enums, decimal-string int64s).
Importing this module needs ``opentelemetry-proto`` (the ``server`` extra).
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def serve(app: Any) -> Iterator[int]:
    """Run ``app`` on a real uvicorn server on 127.0.0.1; yield its port."""

    import uvicorn

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise AssertionError("the OTLP test server did not start")
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def media_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip()

    def json(self) -> Any:
        return json.loads(self.body)

    def problem_code(self) -> str:
        return str(self.json()["error"]["code"])


def request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
    host: str | None = None,
    timeout: float = 30.0,
) -> Reply:
    """One HTTP/1.1 request on its own connection; ``host`` overrides the Host header."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host or f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return Reply(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def post_otlp(
    port: int,
    body: bytes,
    *,
    media_type: str = PROTOBUF,
    encoding: str | None = None,
    credential: str | None = None,
    host: str | None = None,
    timeout: float = 30.0,
) -> Reply:
    headers = {"Content-Type": media_type}
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return request(port, "POST", "/v1/traces", body, headers=headers, host=host, timeout=timeout)


def export_response(reply: Reply) -> ExportTraceServiceResponse:
    """The 200 body, decoded from the reply's own encoding."""

    assert reply.status == 200, (reply.status, reply.body[:200])
    if reply.media_type == JSON:
        return json_format.Parse(reply.body, ExportTraceServiceResponse())
    assert reply.media_type == PROTOBUF, reply.headers
    return ExportTraceServiceResponse.FromString(reply.body)


def rpc_status(reply: Reply) -> tuple[int, str]:
    """``(code, message)`` of a ``google.rpc.Status`` error body, decoded by the
    reference ``googleapis-common-protos`` message (not Earshot's encoder)."""

    from google.rpc.status_pb2 import Status

    if reply.media_type == JSON:
        status = json_format.Parse(reply.body, Status())
    else:
        assert reply.media_type == PROTOBUF, reply.headers
        status = Status.FromString(reply.body)
    assert not status.details
    return status.code, status.message


# --- spans ------------------------------------------------------------------------------


def string_attribute(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def attribute_of_size(
    message_type: type[Resource | InstrumentationScope], size: int, *, fill: str = "v"
) -> KeyValue:
    """One attribute that, alone in a ``message_type``, serializes to exactly ``size``
    bytes (measured by protobuf itself)."""

    for length in range(size - 16, size):
        attribute = string_attribute("k", fill * length)
        if message_type(attributes=[attribute]).ByteSize() == size:
            return attribute
    raise ValueError(f"no single attribute serializes to {size} bytes")


def make_span(
    trace: int,
    span: int,
    *,
    name: str = "llm",
    parent: int | None = None,
    start: int = START,
    end: int | None = None,
    attributes: Sequence[KeyValue] = (),
) -> Span:
    """A span whose IDs are the big-endian bytes of ``trace`` and ``span``."""

    return Span(
        trace_id=trace.to_bytes(16, "big"),
        span_id=span.to_bytes(8, "big"),
        parent_span_id=b"" if parent is None else parent.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=start + SECOND if end is None else end,
        attributes=list(attributes),
    )


def make_request(
    spans: Sequence[Span],
    *,
    scope: str = "pipecat",
    resource: Sequence[KeyValue] = (string_attribute("service.name", "otlp-test"),),
) -> ExportTraceServiceRequest:
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=list(resource)),
                scope_spans=[ScopeSpans(scope=InstrumentationScope(name=scope), spans=spans)],
            )
        ]
    )


def shared_attributes_request(
    spans: int, attributes: int, *, scope_per_span: bool = False
) -> ExportTraceServiceRequest:
    """Many small spans under one resource and one scope that each carry
    ``attributes`` attributes (tribunal round 3, N3-1). The spans name no session
    key, so each takes the resource's, which is its last attribute. With
    ``scope_per_span``, each span is in its own ``ScopeSpans`` of an equal, empty
    scope instead."""

    resource = [string_attribute(f"r{index}", "v") for index in range(attributes - 1)]
    resource.append(string_attribute("earshot.session.id", "shared-call"))
    request = make_request([make_span(index + 1, 1) for index in range(spans)], resource=resource)
    resource_spans = request.resource_spans[0]
    if scope_per_span:
        resource_spans.scope_spans.extend(
            ScopeSpans(scope=InstrumentationScope(name="pipecat"), spans=[span])
            for span in resource_spans.scope_spans.pop().spans
        )
    else:
        resource_spans.scope_spans[0].scope.attributes.extend(
            string_attribute(f"s{index}", "v") for index in range(attributes)
        )
    return request


def equal_shared_values_request(
    spans: int, *, url_bytes: int, version_bytes: int
) -> ExportTraceServiceRequest:
    """One resource with two ``ScopeSpans`` of an equal scope (its version
    ``version_bytes`` long) and an equal schema URL (``url_bytes`` long): one span in
    the first, ``spans`` in the second. Equal values in separate ``ScopeSpans`` are
    separate objects, so code that compares them per span costs spans x their size."""

    key = [string_attribute("earshot.session.id", "shared-call")]
    scope = InstrumentationScope(name="pipecat", version="v" * version_bytes)
    url = "https://example.test/" + "u" * url_bytes
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=key),
                scope_spans=[
                    ScopeSpans(scope=scope, schema_url=url, spans=[make_span(1, 1)]),
                    ScopeSpans(
                        scope=scope,
                        schema_url=url,
                        spans=[make_span(index + 2, 1) for index in range(spans)],
                    ),
                ],
            )
        ]
    )


def best_cpu_seconds(work: Callable[[], object], *, repeat: int = 3) -> float:
    """The least process CPU time ``work`` took over ``repeat`` runs."""

    best = float("inf")
    for _ in range(repeat):
        started = time.process_time()
        work()
        best = min(best, time.process_time() - started)
    return best


def keyed_request(session_key: str, trace: int, spans: int, *, first_span: int = 1) -> bytes:
    """``spans`` spans of one trace, each naming ``session_key``, serialized."""

    return make_request(
        [
            make_span(
                trace,
                first_span + index,
                attributes=[string_attribute("earshot.session.id", session_key)],
            )
            for index in range(spans)
        ]
    ).SerializeToString()


# --- OTLP/JSON --------------------------------------------------------------------------

_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def otlp_json(request: ExportTraceServiceRequest, *, upper: bool = False) -> bytes:
    """``request`` in OTLP/JSON as the specification writes it: lowerCamelCase keys,
    integer enums, and trace/span IDs (links included) as hex, not base64."""

    document = json_format.MessageToDict(request, use_integers_for_enums=True)

    def to_hex(value: str) -> str:
        text = base64.b64decode(value).hex()
        return text.upper() if upper else text

    for resource_spans in document.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                for target in (span, *span.get("links", [])):
                    for field in _ID_FIELDS:
                        if target.get(field):
                            target[field] = to_hex(target[field])
    return json.dumps(document).encode("utf-8")


def with_links(requests: Sequence[ExportTraceServiceRequest]) -> list[ExportTraceServiceRequest]:
    """The requests with links (and a timed event) on every span: each span links to
    the previous span seen, with a trace state and an attribute."""

    previous: Span | None = None
    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    if previous is not None:
                        link = span.links.add()
                        link.trace_id = previous.trace_id
                        link.span_id = previous.span_id
                        link.trace_state = "earshot=1"
                        link.attributes.append(string_attribute("link.kind", "previous"))
                    event = span.events.add()
                    event.name = "probe"
                    event.time_unix_nano = span.start_time_unix_nano
                    previous = span
    return list(requests)
