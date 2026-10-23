"""What Earshot keeps of an OTLP span, and which call it belongs to (FR-3, FR-4).

One pure module, shared by the OTLP receiver and the call materializer. It knows
nothing about storage.

**Normalization before classification.** Frameworks put content under keys that
``privacy.classify_attribute`` reads as metadata: Pipecat's raw ``text``,
``input``, ``output`` and ``tools``, LiveKit 1.7's ``lk.pii.*`` keys (and their
1.6 names), ``user_id``. ``canonical_attribute_key`` maps each such key to the
name that says what it holds, and ``attribute_capture_class`` classifies that
name: from this module's table where the shared classifier does not know it,
from ``classify_attribute`` otherwise. Phone and caller identifiers
(``phone``, ``*.phone``, ``*.phone_number``, ``caller.number``, ``sip.from``,
``sip.to``, ``enduser.*``) are identity, and any key with a dot-delimited ``pii``
segment is content, as LiveKit defines it. The string ``"NOT_GIVEN"`` (Pipecat's
leaked sentinel) is an absent value. This is a classifier of key names, not an
allowlist: a key it does not recognize is metadata (``docs/privacy.md``).

**Presets.** ``full`` keeps every span, resource and scope byte for byte.
``metadata_only`` keeps only metadata-class attributes, everywhere a span carries
them (span, events, links, resource, scope), drops the GenAI content events whole,
clears the status message, discards protobuf fields outside the known schema, and
also removes the four session-key attributes, so the raw key is never stored.

**Session keys** (``resolve_session_key``): the span's own ``earshot.session.id``,
``gen_ai.conversation.id``, ``session.id`` or ``conversation.id``, in that order,
then the resource's ``earshot.session.id`` (and no other resource attribute: a
resource is a process, which exports many calls). A key is a string of 1 to 256
UTF-8 bytes after trimming; anything else counts as no key.

**End markers and session spans** are matched by instrumentation scope and span
name, never by root status; any span with ``earshot.call.ended = true`` is an end
marker too.

Only the functions that build new messages import ``opentelemetry-proto`` (the
``server`` extra), so the constants here are importable on a base install.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from .privacy import CaptureClass, classify_attribute, memoize_short_keys

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable

    from google.protobuf.message import Message
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import (
        AnyValue,
        InstrumentationScope,
        KeyValue,
    )
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

_T = TypeVar("_T")
_M = TypeVar("_M", bound="Message")

CAPTURE_PRESET_FULL = "full"
CAPTURE_PRESET_METADATA_ONLY = "metadata_only"
# The presets a project can have in this release, and those it cannot have yet.
CAPTURE_PRESETS = (CAPTURE_PRESET_FULL, CAPTURE_PRESET_METADATA_ONLY)
UNAVAILABLE_CAPTURE_PRESETS = ("redacted",)

EARSHOT_SESSION_ID = "earshot.session.id"
EARSHOT_CALL_ENDED = "earshot.call.ended"
# Span attributes that name a call's session key, highest priority first.
SESSION_KEY_ATTRIBUTES = (
    EARSHOT_SESSION_ID,
    "gen_ai.conversation.id",
    "session.id",
    "conversation.id",
)
# Attributes the OTLP receiver interprets; ``scripts/check_semconv.py`` requires
# the Earshot ones to be registered in ``semconv/earshot.yaml``.
RECEIVER_ATTRIBUTES = (*SESSION_KEY_ATTRIBUTES, EARSHOT_CALL_ENDED)
SESSION_KEY_MAX_BYTES = 256
NOT_GIVEN = "NOT_GIVEN"

# (instrumentation scope, span name) of the spans that end a call, and of the spans
# that show a trace is one framework session. Scopes as the frameworks emit them:
# Pipecat ``trace.get_tracer("pipecat.turn")`` (the real 1.5 capture), LiveKit
# ``_DynamicTracer("livekit-agents")`` (1.6 and 1.8). LiveKit's ``agent_session``
# ends at session close (<= 1.7); 1.8 adds a ``session_close`` span.
END_MARKER_SPANS = frozenset(
    {
        ("pipecat.turn", "conversation"),
        ("livekit-agents", "agent_session"),
        ("livekit-agents", "session_close"),
    }
)
SESSION_SPANS = frozenset(
    {
        ("pipecat.turn", "conversation"),
        ("livekit-agents", "agent_session"),
    }
)

# A raw key, and the canonical key that says what it holds.
_CANONICAL_KEYS = {
    # Pipecat 1.5 (``utils/tracing/service_decorators.py``, ``service_attributes.py``)
    "text": "speech.text",
    "input": "gen_ai.input.messages",
    "output": "gen_ai.output.messages",
    "tools": "gen_ai.tool.definitions",
    "user_id": "enduser.id",
    # Pipecat realtime services (Gemini Live, OpenAI Realtime)
    "text_output": "speech.text",
    "tools.definitions": "gen_ai.tool.definitions",
    "session.instructions": "gen_ai.system_instructions",
    # LiveKit <= 1.6; 1.7 renamed them under the ``lk.pii.`` marker segment
    "lk.chat_ctx": "lk.pii.chat_ctx",
    "lk.user_transcript": "lk.pii.user_transcript",
    "lk.response.text": "lk.pii.response.text",
    "lk.response.function_calls": "lk.pii.response.function_calls",
    "lk.input_text": "lk.pii.input_text",
    "lk.user_input": "lk.pii.user_input",
    "lk.instructions": "lk.pii.instructions",
    "lk.function_tool.arguments": "lk.pii.function_tool.arguments",
    "lk.function_tool.output": "lk.pii.function_tool.output",
    "lk.participant_identity": "lk.pii.participant_identity",
    "lk.room_name": "lk.pii.room_name",
    "lk.function_tools": "gen_ai.tool.definitions",
    "lk.provider_tools": "gen_ai.tool.definitions",
    "lk.tool_sets": "gen_ai.tool.definitions",
    # OpenInference
    "input.value": "gen_ai.input.messages",
    "output.value": "gen_ai.output.messages",
    "user.id": "enduser.id",
}
# Canonical keys whose class ``classify_attribute`` does not know.
_CLASSES = {
    "enduser.id": CaptureClass.IDENTITY,
    "lk.pii.participant_identity": CaptureClass.IDENTITY,
    "lk.pii.sip.phoneNumber": CaptureClass.IDENTITY,
    "lk.pii.chat_ctx": CaptureClass.MODEL_PAYLOAD,
    "lk.pii.instructions": CaptureClass.MODEL_PAYLOAD,
    "lk.pii.response.text": CaptureClass.MODEL_PAYLOAD,
    "lk.pii.input_text": CaptureClass.TRANSCRIPT,
    "lk.pii.user_input": CaptureClass.TRANSCRIPT,
    "lk.pii.function_tool.output": CaptureClass.TOOL_PAYLOAD,
    "lk.pii.response.function_calls": CaptureClass.TOOL_PAYLOAD,
    # OpenTelemetry GenAI attributes the semantic conventions flag as sensitive
    "gen_ai.tool.definitions": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.tool.description": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.tool.call.arguments": CaptureClass.TOOL_PAYLOAD,
    "gen_ai.tool.call.result": CaptureClass.TOOL_PAYLOAD,
    "gen_ai.retrieval.query.text": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.retrieval.documents": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.memory.query.text": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.memory.records": CaptureClass.MODEL_PAYLOAD,
    "gen_ai.evaluation.explanation": CaptureClass.MODEL_PAYLOAD,
}
# Identity keys by shape (``_identity_key``): last segments, and whole keys that may
# carry a further suffix (``sip.from.uri``).
_IDENTITY_LAST_SEGMENTS = frozenset({"phone", "phone_number", "phonenumber", "msisdn"})
_IDENTITY_KEYS = ("caller.number", "callee.number", "sip.from", "sip.to")
# Span events that carry message bodies in generic attributes (``content``); under
# metadata_only they are dropped whole. LiveKit 1.6 emits the first five.
_CONTENT_EVENTS = frozenset(
    {
        "gen_ai.system.message",
        "gen_ai.user.message",
        "gen_ai.assistant.message",
        "gen_ai.tool.message",
        "gen_ai.choice",
        "gen_ai.client.inference.operation.details",
    }
)


def canonical_attribute_key(key: str) -> str:
    """The name that says what a raw attribute key holds (itself if it already does)."""

    return _CANONICAL_KEYS.get(key, key)


def _identity_key(canonical: str) -> bool:
    """Phone and caller identifiers ``classify_attribute`` reads as metadata: a key
    whose last segment is a phone number (``phone``, ``user.phone``), a caller or SIP
    endpoint (``caller.number``, ``sip.from``, ``sip.to``) and OpenTelemetry's
    ``enduser.*``."""

    lowered = canonical.lower()
    return (
        lowered.rsplit(".", 1)[-1] in _IDENTITY_LAST_SEGMENTS
        or lowered.startswith("enduser.")
        or any(lowered == key or lowered.startswith(f"{key}.") for key in _IDENTITY_KEYS)
    )


@memoize_short_keys
def attribute_capture_class(key: str) -> CaptureClass:
    """Classify an attribute key after normalizing it (memoized for short keys only:
    senders choose them)."""

    canonical = canonical_attribute_key(key)
    known = _CLASSES.get(canonical)
    if known is not None:
        return known
    capture_class = classify_attribute(canonical)
    if capture_class is CaptureClass.METADATA and _identity_key(canonical):
        return CaptureClass.IDENTITY
    if capture_class is CaptureClass.METADATA and "pii" in canonical.split("."):
        return CaptureClass.EXTENSION_PAYLOAD
    return capture_class


def _kept_by_metadata_only(key: str) -> bool:
    return key not in SESSION_KEY_ATTRIBUTES and attribute_capture_class(key) is (
        CaptureClass.METADATA
    )


def any_value(value: AnyValue) -> Any:
    """An OTLP ``AnyValue`` as a Python value (``None`` when unset)."""

    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return attribute_values(value.kvlist_value.values)
    return getattr(value, kind)


def attribute_values(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    """Attributes as a dict; ``NOT_GIVEN`` and unset values are absent, first key wins."""

    values: dict[str, Any] = {}
    for item in attributes:
        value = any_value(item.value)
        if value is None or value == NOT_GIVEN:
            continue
        values.setdefault(item.key, value)
    return values


@dataclass(frozen=True, slots=True)
class OtlpSpan:
    """One span with the resource and instrumentation scope it was exported under."""

    resource: Resource
    scope: InstrumentationScope
    span: Span
    resource_schema_url: str = ""
    scope_schema_url: str = ""


def spans_from_request(request: ExportTraceServiceRequest) -> list[OtlpSpan]:
    """Flatten an export request into its spans, in request order. The spans of one
    ``ResourceSpans`` share one resource object and schema URL, and those of one
    ``ScopeSpans`` one scope object and schema URL (what ``RequestMemo`` relies on).
    Equal schema URLs anywhere in the request are one object, so comparing them, as
    every per-span key does, takes no time whatever their length."""

    spans: list[OtlpSpan] = []
    urls: dict[str, str] = {}
    for resource_spans in request.resource_spans:
        resource = resource_spans.resource
        resource_schema_url = urls.setdefault(resource_spans.schema_url, resource_spans.schema_url)
        for scope_spans in resource_spans.scope_spans:
            scope = scope_spans.scope
            scope_schema_url = urls.setdefault(scope_spans.schema_url, scope_spans.schema_url)
            spans.extend(
                OtlpSpan(
                    resource=resource,
                    scope=scope,
                    span=span,
                    resource_schema_url=resource_schema_url,
                    scope_schema_url=scope_schema_url,
                )
                for span in scope_spans.spans
            )
    return spans


class RequestMemo:
    """Work that depends only on what a request's spans share, done once per request.

    Every span of one ``ResourceSpans`` shares its resource and every span of one
    ``ScopeSpans`` its scope (``spans_from_request``), so a value computed from those
    alone (an attribute check, the preset's copy, the stored bytes and their digest)
    is computed once, not once per span: a request of thousands of spans under a
    resource of thousands of attributes costs its size, not their product (tribunal
    round 3, N3-1). Keys are object identities (``first_equal`` maps equal scopes to
    one); each entry holds its objects, so no identity is reused while the memo
    lives. One memo serves one request.
    """

    __slots__ = ("_by_content", "_entries")

    def __init__(self) -> None:
        self._entries: dict[tuple[Any, ...], tuple[tuple[object, ...], Any]] = {}
        self._by_content: dict[tuple[type, bytes], Any] = {}

    def get(
        self, compute: Callable[..., _T], *shared: object, extra: tuple[Hashable, ...] = ()
    ) -> _T:
        """``compute(*shared, *extra)``, once per identity of ``shared`` and value of
        ``extra`` (``compute`` must be a module-level function)."""

        key = (compute, *map(id, shared), *extra)
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = (shared, compute(*shared, *extra))
        result: _T = entry[1]
        return result

    def first_equal(self, message: _M) -> _M:
        """The request's first message with ``message``'s deterministic bytes, so
        equal scopes in separate ``ScopeSpans`` share their entries. Found once per
        message object: comparing equal bytes again for each span would cost spans x
        their size."""

        key = (_FIRST_EQUAL, id(message))
        entry = self._entries.get(key)
        if entry is None:
            content = self.get(_deterministic_bytes, message)
            first = self._by_content.setdefault((type(message), content), message)
            entry = self._entries[key] = ((message,), first)
        result: _M = entry[1]
        return result


# ``RequestMemo.first_equal``'s entries, beside ``get``'s (keyed by a function).
_FIRST_EQUAL = object()


def _deterministic_bytes(message: Message) -> bytes:
    return message.SerializeToString(deterministic=True)


def _copy(message: Message) -> Any:
    copied = type(message)()
    copied.CopyFrom(message)
    return copied


def _keep_attributes(attributes: Any) -> None:
    for index in reversed(range(len(attributes))):
        if not _kept_by_metadata_only(attributes[index].key):
            del attributes[index]


def _kept_context(message: Message, preset: str) -> Any:
    """What ``preset`` keeps of a resource or an instrumentation scope (a copy)."""

    kept = _copy(message)
    if preset == CAPTURE_PRESET_METADATA_ONLY:
        # Fields outside the known OTLP schema (a newer exporter's) are not metadata;
        # this also makes the protobuf path store what the JSON path decodes.
        kept.DiscardUnknownFields()
        _keep_attributes(kept.attributes)
    return kept


def apply_capture_preset(
    preset: str, span: OtlpSpan, *, memo: RequestMemo | None = None
) -> OtlpSpan:
    """Return what ``preset`` keeps of ``span`` (copies; the input is not modified).

    With a ``memo``, the kept resource and scope are made once per resource and
    scope, and shared by the spans under them: read them, never change them.
    """

    if preset not in CAPTURE_PRESETS:
        raise ValueError(f"unknown capture preset; expected one of {', '.join(CAPTURE_PRESETS)}")
    memo = RequestMemo() if memo is None else memo
    resource = memo.get(_kept_context, span.resource, extra=(preset,))
    scope = memo.get(_kept_context, span.scope, extra=(preset,))
    kept = _copy(span.span)
    if preset == CAPTURE_PRESET_METADATA_ONLY:
        kept.DiscardUnknownFields()
        _keep_attributes(kept.attributes)
        for index in reversed(range(len(kept.events))):
            if kept.events[index].name in _CONTENT_EVENTS:
                del kept.events[index]
            else:
                _keep_attributes(kept.events[index].attributes)
        for link in kept.links:
            _keep_attributes(link.attributes)
        # Free text (exception and provider error messages can quote the caller).
        kept.status.message = ""
    return OtlpSpan(
        resource=resource,
        scope=scope,
        span=kept,
        resource_schema_url=span.resource_schema_url,
        scope_schema_url=span.scope_schema_url,
    )


def span_payload(span: Span) -> bytes:
    """The span's deterministic protobuf bytes (what ``spans.payload`` stores)."""

    return span.SerializeToString(deterministic=True)


def resource_payload(span: OtlpSpan, *, memo: RequestMemo | None = None) -> bytes:
    """The deterministic bytes of the span's resource and scope (``otlp_resources``):
    a ``ResourceSpans`` holding the resource and one ``ScopeSpans`` with no spans.
    With a ``memo``, made once per resource and scope a request's spans share."""

    extra = (span.resource_schema_url, span.scope_schema_url)
    if memo is None:
        return _resource_payload(span.resource, span.scope, *extra)
    return memo.get(_resource_payload, span.resource, memo.first_equal(span.scope), extra=extra)


def _resource_payload(
    resource: Resource, scope: InstrumentationScope, resource_schema_url: str, scope_schema_url: str
) -> bytes:
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans

    message = ResourceSpans(
        resource=resource,
        schema_url=resource_schema_url,
        scope_spans=[ScopeSpans(scope=scope, schema_url=scope_schema_url)],
    )
    return message.SerializeToString(deterministic=True)


def stored_span(span_bytes: bytes, resource_bytes: bytes) -> OtlpSpan:
    """A span read back from ``spans.payload`` and its ``otlp_resources.payload``."""

    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Span

    resource_spans = ResourceSpans.FromString(resource_bytes)
    [scope_spans] = resource_spans.scope_spans
    return OtlpSpan(
        resource=resource_spans.resource,
        scope=scope_spans.scope,
        span=Span.FromString(span_bytes),
        resource_schema_url=resource_spans.schema_url,
        scope_schema_url=scope_spans.schema_url,
    )


def payload_digest(payload: bytes) -> bytes:
    """The 32-byte SHA-256 of a stored payload."""

    return hashlib.sha256(payload).digest()


@dataclass(frozen=True, slots=True)
class SessionKey:
    """A valid session key and the attribute it came from (``calls.key_source``)."""

    value: str
    source: str


def valid_session_key(value: object) -> str | None:
    """The trimmed key, or ``None`` if ``value`` is not a UTF-8 string of 1 to 256 bytes."""

    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if trimmed == NOT_GIVEN:
        return None
    try:
        size = len(trimmed.encode("utf-8"))
    except UnicodeEncodeError:  # lone surrogates are not UTF-8
        return None
    return trimmed if 1 <= size <= SESSION_KEY_MAX_BYTES else None


def _first_valid(attributes: Iterable[KeyValue], keys: tuple[str, ...]) -> SessionKey | None:
    found: dict[str, str] = {}
    for item in attributes:
        if item.key in keys and item.key not in found:
            key = valid_session_key(any_value(item.value))
            if key is not None:
                found[item.key] = key
    for source in keys:
        if source in found:
            return SessionKey(found[source], source)
    return None


def resolve_session_key(span: OtlpSpan, *, memo: RequestMemo | None = None) -> SessionKey | None:
    """The session key a span names, or ``None`` (FR-4 resolution order). With a
    ``memo``, the resource is searched once per resource, not once per span."""

    own = _first_valid(span.span.attributes, SESSION_KEY_ATTRIBUTES)
    if own is not None:
        return own
    if memo is None:
        return _resource_session_key(span.resource)
    return memo.get(_resource_session_key, span.resource)


def _resource_session_key(resource: Resource) -> SessionKey | None:
    return _first_valid(resource.attributes, (EARSHOT_SESSION_ID,))


def is_end_marker(span: OtlpSpan) -> bool:
    """Whether the span signals that its call ended (FR-6 end markers)."""

    if (span.scope.name, span.span.name) in END_MARKER_SPANS:
        return True
    return any(
        item.key == EARSHOT_CALL_ENDED
        and item.value.WhichOneof("value") == "bool_value"
        and item.value.bool_value
        for item in span.span.attributes
    )


def is_session_span(span: OtlpSpan) -> bool:
    """Whether the span is a framework's session span (its trace is one call)."""

    return (span.scope.name, span.span.name) in SESSION_SPANS
