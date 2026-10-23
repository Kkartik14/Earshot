"""Read access to the real Pipecat 1.5 OTLP capture (``fixtures/captured/pipecat-otlp-1.5``).

Every request is returned as a fresh ``ExportTraceServiceRequest`` (tests may edit
it), in the order the real exporter sent them. Importing this module needs
``opentelemetry-proto`` (the ``server`` extra).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "captured" / "pipecat-otlp-1.5"


@functools.cache
def captured_manifest() -> dict[str, Any]:
    return json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))


@functools.cache
def _request_bytes() -> tuple[bytes, ...]:
    entries = captured_manifest()["requests"]
    return tuple((FIXTURE / entry["file"]).read_bytes() for entry in entries)


def captured_requests() -> list[ExportTraceServiceRequest]:
    """The seven requests of the real call, decoded, in arrival order."""

    return [ExportTraceServiceRequest.FromString(payload) for payload in _request_bytes()]


# The session key the example set as both ``conversation.id`` and ``earshot.session.id``.
CAPTURED_SESSION_KEY: str = captured_manifest()["provenance"]["conversation_id"]
# A caller identity, added by ``with_identity`` as Pipecat's documented ``user_id``.
CAPTURED_USER_ID = "caller-user-7731"


def with_identity(
    requests: list[ExportTraceServiceRequest], user_id: str = CAPTURED_USER_ID
) -> list[ExportTraceServiceRequest]:
    """The requests, with Pipecat's documented ``user_id`` attribute on every stt span."""

    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    if span.name == "stt":
                        span.attributes.append(
                            KeyValue(key="user_id", value=AnyValue(string_value=user_id))
                        )
    return requests
