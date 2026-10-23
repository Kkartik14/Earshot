"""Check capture classification for normalized framework attribute names.

Framework adapters normalize provider keys before applying the capture policy. These
cases cover Pipecat input fields and LiveKit private-identifier fields so a mismatch
cannot silently make content look like metadata.
"""

from __future__ import annotations

import gc
import hashlib
import tracemalloc

import pytest
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Span

from captured_otlp import CAPTURED_SESSION_KEY, captured_requests
from earshot.call_capture import (
    SESSION_KEY_ATTRIBUTES,
    OtlpSpan,
    SessionKey,
    apply_capture_preset,
    attribute_capture_class,
    attribute_values,
    is_end_marker,
    is_session_span,
    payload_digest,
    resolve_session_key,
    resource_payload,
    span_payload,
    spans_from_request,
    valid_session_key,
)
from earshot.privacy import CaptureClass, classify_attribute

M = CaptureClass.METADATA
T = CaptureClass.TRANSCRIPT
MODEL = CaptureClass.MODEL_PAYLOAD
TOOL = CaptureClass.TOOL_PAYLOAD
ID = CaptureClass.IDENTITY
EXT = CaptureClass.EXTENSION_PAYLOAD
DIAG = CaptureClass.DIAGNOSTIC_PAYLOAD

# (raw key, class before normalization, class after normalization)
CLASSIFICATION = [
    # Pipecat 1.5 (research §1.4; the real capture carries the first four)
    ("text", M, T),
    ("input", M, MODEL),
    ("output", M, MODEL),
    ("tools", M, MODEL),
    ("transcript", T, T),
    ("gen_ai.system_instructions", MODEL, MODEL),
    ("user_id", M, ID),
    # Pipecat realtime services (Gemini Live, OpenAI Realtime)
    ("text_output", M, T),
    ("tools.definitions", M, MODEL),
    ("session.instructions", M, MODEL),
    # identity
    ("enduser.id", M, ID),
    ("user.id", M, ID),
    ("lk.pii.participant_identity", M, ID),
    ("lk.participant_identity", M, ID),
    ("lk.pii.sip.phoneNumber", M, ID),
    # phone and caller identifiers (tribunal round 1, L-6)
    ("phone", M, ID),
    ("user.phone", M, ID),
    ("customer.phone", M, ID),
    ("caller.phone_number", ID, ID),
    ("caller.number", M, ID),
    ("callee.number", M, ID),
    ("sip.from", M, ID),
    ("sip.to", M, ID),
    ("sip.from.uri", M, ID),
    ("enduser.role", M, ID),
    ("enduser.pseudo.id", M, ID),
    # LiveKit >= 1.7: every lk.pii.* key is content
    ("lk.pii.user_transcript", T, T),
    ("lk.pii.response.text", M, MODEL),
    ("lk.pii.input_text", M, T),
    ("lk.pii.chat_ctx", M, MODEL),
    ("lk.pii.user_input", M, T),
    ("lk.pii.instructions", M, MODEL),
    ("lk.pii.function_tool.arguments", TOOL, TOOL),
    ("lk.pii.function_tool.output", M, TOOL),
    ("lk.pii.response.function_calls", M, TOOL),
    ("lk.pii.amd.transcript", T, T),
    ("lk.pii.room_name", M, EXT),
    ("lk.pii.rpc.payload", M, EXT),
    ("lk.pii.some_future_key", M, EXT),
    # LiveKit <= 1.6: the same keys before the lk.pii. rename
    ("lk.user_transcript", T, T),
    ("lk.response.text", M, MODEL),
    ("lk.input_text", M, T),
    ("lk.chat_ctx", M, MODEL),
    ("lk.user_input", M, T),
    ("lk.instructions", M, MODEL),
    ("lk.function_tool.arguments", TOOL, TOOL),
    ("lk.function_tool.output", M, TOOL),
    ("lk.response.function_calls", M, TOOL),
    ("lk.room_name", M, EXT),
    ("lk.function_tools", M, MODEL),
    ("lk.provider_tools", M, MODEL),
    ("lk.tool_sets", M, MODEL),
    # OpenTelemetry GenAI content attributes (opt-in, "likely sensitive")
    ("gen_ai.input.messages", MODEL, MODEL),
    ("gen_ai.output.messages", MODEL, MODEL),
    ("gen_ai.tool.definitions", M, MODEL),
    ("gen_ai.tool.call.arguments", M, TOOL),
    ("gen_ai.tool.call.result", M, TOOL),
    ("gen_ai.prompt.variable.customer_name", MODEL, MODEL),
    # OpenInference
    ("input.value", M, MODEL),
    ("output.value", M, MODEL),
    ("llm.input_messages.0.message.content", MODEL, MODEL),
    ("llm.output_messages.0.message.content", MODEL, MODEL),
    # diagnostics
    ("exception.message", DIAG, DIAG),
    ("exception.stacktrace", DIAG, DIAG),
    # metadata stays metadata (and the session keys are metadata, removed separately)
    ("gen_ai.request.model", M, M),
    ("gen_ai.provider.name", M, M),
    ("gen_ai.usage.input_tokens", M, M),
    ("metrics.ttfb", M, M),
    ("metrics.character_count", M, M),
    ("turn.number", M, M),
    ("is_final", M, M),
    ("voice_id", M, M),
    ("settings.model", M, M),
    ("lk.response.ttft", M, M),
    ("lk.speech_id", M, M),
    ("exception.type", M, M),
    ("phone.carrier", M, M),
    ("sip.status_code", M, M),
    ("sip.method", M, M),
    ("earshot.session.id", M, M),
    ("gen_ai.conversation.id", M, M),
    ("session.id", M, M),
    ("conversation.id", M, M),
]


@pytest.mark.parametrize(
    ("key", "before", "after"), CLASSIFICATION, ids=[r[0] for r in CLASSIFICATION]
)
def test_keys_are_classified_after_normalization(key, before, after) -> None:
    assert classify_attribute(key) is before
    assert attribute_capture_class(key) is after


def _fixture_spans() -> list[OtlpSpan]:
    return [span for request in captured_requests() for span in spans_from_request(request)]


def _attributes(message) -> dict[str, object]:
    return {item.key: item.value.WhichOneof("value") for item in message.attributes}


def _string(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


# --- The real capture ------------------------------------------------------------------


def test_the_real_capture_flattens_to_its_seventeen_spans_with_their_scopes() -> None:
    spans = _fixture_spans()

    assert len(spans) == 17
    assert {(span.scope.name, span.span.name) for span in spans} == {
        ("pipecat.turn", "conversation"),
        ("pipecat.turn", "turn"),
        ("pipecat", "stt"),
        ("pipecat", "llm"),
        ("pipecat", "tts"),
    }
    assert {span.resource.attributes[0].key for span in spans} == {"telemetry.sdk.language"}


def test_every_content_key_of_the_real_capture_is_content_after_normalization() -> None:
    classes = {
        (span.span.name, key): attribute_capture_class(key)
        for span in _fixture_spans()
        for key in _attributes(span.span)
    }

    content = {name_key for name_key, capture_class in classes.items() if capture_class is not M}
    assert content == {
        ("stt", "transcript"),
        ("llm", "input"),
        ("llm", "output"),
        ("llm", "gen_ai.system_instructions"),
        ("tts", "text"),
        # A setting, but the shared classifier reads "completion" as model payload;
        # dropping it under metadata_only errs on the safe side.
        ("llm", "param.user_turn_completion_config"),
    }


def test_not_given_is_absent_from_the_normalized_attributes() -> None:
    llm = next(span for span in _fixture_spans() if span.span.name == "llm")

    raw = {item.key: item.value.string_value for item in llm.span.attributes}
    values = attribute_values(llm.span.attributes)

    assert raw["gen_ai.request.top_k"] == raw["param.user_turn_completion_config"] == "NOT_GIVEN"
    assert "gen_ai.request.top_k" not in values
    assert "param.user_turn_completion_config" not in values
    assert values["stream"] is True
    assert isinstance(values["metrics.ttfb"], float)
    assert values["gen_ai.request.model"] == "openai/gpt-oss-20b"


# --- Presets ----------------------------------------------------------------------------


def test_full_keeps_every_span_and_resource_byte_for_byte() -> None:
    for span in _fixture_spans():
        kept = apply_capture_preset("full", span)

        assert span_payload(kept.span) == span_payload(span.span)
        assert resource_payload(kept) == resource_payload(span)


def test_metadata_only_drops_content_identity_and_session_keys_from_the_real_capture() -> None:
    kept = {}
    for span in _fixture_spans():
        filtered = apply_capture_preset("metadata_only", span)
        kept.setdefault(span.span.name, set()).update(_attributes(filtered.span))
        payload = span_payload(filtered.span)
        assert CAPTURED_SESSION_KEY.encode() not in payload
        for key in _attributes(filtered.span):
            assert attribute_capture_class(key) is M, key
            assert key not in SESSION_KEY_ATTRIBUTES, key

    assert kept["conversation"] == {"conversation.type"}
    assert kept["turn"] == {
        "turn.number",
        "turn.type",
        "turn.user_bot_latency_seconds",
        "turn.duration_seconds",
        "turn.was_interrupted",
    }
    assert {"transcript"}.isdisjoint(kept["stt"]) and "is_final" in kept["stt"]
    assert {"input", "output", "gen_ai.system_instructions"}.isdisjoint(kept["llm"])
    assert {"gen_ai.request.model", "gen_ai.usage.output_tokens"} <= kept["llm"]
    assert "text" not in kept["tts"] and "metrics.character_count" in kept["tts"]


def test_metadata_only_also_filters_resource_scope_events_links_and_status() -> None:
    span = Span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        name="llm_request",
        attributes=[_string("user_id", "caller-7731"), _string("gen_ai.request.model", "m")],
    )
    span.status.message = "provider said: caller-7731 asked for a refund"
    choice = span.events.add(name="gen_ai.choice", time_unix_nano=5)
    choice.attributes.append(_string("message", "the reply"))
    user_message = span.events.add(name="gen_ai.user.message", time_unix_nano=4)
    user_message.attributes.append(_string("content", "my card number is 4242"))
    exception = span.events.add(name="exception", time_unix_nano=6)
    exception.attributes.extend(
        [_string("exception.type", "ValueError"), _string("exception.message", "caller-7731")]
    )
    link = span.links.add(trace_id=b"\x03" * 16, span_id=b"\x04" * 8)
    link.attributes.extend([_string("session.id", "sess-9"), _string("link.kind", "follows")])
    resource = Resource(
        attributes=[
            _string("service.name", "agent"),
            _string("earshot.session.id", "sess-9"),
            _string("enduser.id", "caller-7731"),
        ]
    )
    scope = InstrumentationScope(
        name="livekit-agents", attributes=[_string("lk.pii.room_name", "room-of-caller-7731")]
    )
    original = OtlpSpan(resource=resource, scope=scope, span=span)

    filtered = apply_capture_preset("metadata_only", original)

    assert _attributes(filtered.span) == {"gen_ai.request.model": "string_value"}
    assert filtered.span.status.message == ""
    assert [event.name for event in filtered.span.events] == ["exception"]
    assert _attributes(filtered.span.events[0]) == {"exception.type": "string_value"}
    assert _attributes(filtered.span.links[0]) == {"link.kind": "string_value"}
    assert _attributes(filtered.resource) == {"service.name": "string_value"}
    assert _attributes(filtered.scope) == {}
    for payload in (span_payload(filtered.span), resource_payload(filtered)):
        for secret in (b"caller-7731", b"sess-9", b"4242", b"the reply"):
            assert secret not in payload
    # The input is not modified.
    assert len(original.span.attributes) == 2 and len(original.resource.attributes) == 3


def _with_unknown_field(message, secret: bytes):
    """``message`` parsed back with field 999 (unknown to the schema) carrying ``secret``,
    as a newer OTLP exporter's message reaches the receiver."""

    tag = (999 << 3) | 2  # field 999, length-delimited
    encoded = bytearray()
    for value in (tag, len(secret)):
        while value > 0x7F:
            encoded.append((value & 0x7F) | 0x80)
            value >>= 7
        encoded.append(value)
    return type(message).FromString(message.SerializeToString() + bytes(encoded) + secret)


def test_metadata_only_stores_no_unknown_protobuf_fields() -> None:
    """Tribunal round 1, L-3: fields outside the known schema are not metadata."""

    span = Span(trace_id=b"\x01" * 16, span_id=b"\x02" * 8, name="llm")
    span.events.add(name="exception", time_unix_nano=5)
    span.events[0].CopyFrom(_with_unknown_field(span.events[0], b"SECRET-EVENT"))
    original = OtlpSpan(
        resource=_with_unknown_field(
            Resource(attributes=[_string("service.name", "agent")]), b"SECRET-RESOURCE"
        ),
        scope=_with_unknown_field(InstrumentationScope(name="pipecat.turn"), b"SECRET-SCOPE"),
        span=_with_unknown_field(span, b"SECRET-SPAN"),
    )
    secrets = (b"SECRET-SPAN", b"SECRET-EVENT", b"SECRET-RESOURCE", b"SECRET-SCOPE")

    full = apply_capture_preset("full", original)
    filtered = apply_capture_preset("metadata_only", original)

    stored_full = span_payload(full.span) + resource_payload(full)
    stored = span_payload(filtered.span) + resource_payload(filtered)
    assert all(secret in stored_full for secret in secrets)  # full keeps bytes as sent
    assert [secret for secret in secrets if secret in stored] == []
    assert filtered.resource.attributes[0].key == "service.name"


def test_metadata_only_drops_phone_and_caller_identifiers() -> None:
    """Tribunal round 1, L-6: these keys are identity, so ``metadata_only`` drops them."""

    identifiers = {
        "phone": "+14155550101",
        "user.phone": "+14155550102",
        "caller.number": "+14155550103",
        "sip.from": "sip:+14155550104@carrier.example",
        "sip.to": "sip:+14155550105@carrier.example",
        "enduser.id": "caller-7731",
        "customer.phone_number": "+14155550106",
    }
    span = Span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        name="call",
        attributes=[_string(key, value) for key, value in identifiers.items()]
        + [_string("sip.method", "INVITE")],
    )
    resource = Resource(attributes=[_string("user.phone", "+14155550107")])

    filtered = apply_capture_preset(
        "metadata_only", OtlpSpan(resource=resource, scope=InstrumentationScope(), span=span)
    )

    stored = span_payload(filtered.span) + resource_payload(filtered)
    assert _attributes(filtered.span) == {"sip.method": "string_value"}
    assert [value for value in identifiers.values() if value.encode() in stored] == []
    assert b"+14155550107" not in stored


def test_classifying_sender_chosen_long_keys_retains_none_of_them() -> None:
    """Tribunal round 2, Linus N-2: the classification memo pinned every distinct key
    it saw, so one 4 MiB key per request grew the server by 4 MiB for good."""

    def one_long_key(index: int) -> None:
        key = f"k{index}." + "x" * (1 << 20)  # a distinct 1 MiB key
        span = Span(trace_id=b"\x01" * 16, span_id=b"\x02" * 8, name="x")
        span.attributes.append(_string(key, "v"))
        apply_capture_preset(
            "metadata_only", OtlpSpan(resource=Resource(), scope=InstrumentationScope(), span=span)
        )
        classify_attribute(key)  # the recorder's and the validator's classifier

    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for index in range(32):
            one_long_key(index)
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()

    assert retained < 2 * 1024 * 1024, f"{retained / 2**20:.1f} MiB retained"


def test_presets_outside_this_release_are_refused() -> None:
    span = _fixture_spans()[0]

    for preset in ("redacted", "everything", ""):
        with pytest.raises(ValueError, match="capture preset"):
            apply_capture_preset(preset, span)


# --- Deterministic payloads ---------------------------------------------------------------


def test_payloads_are_deterministic_and_their_digest_is_sha256() -> None:
    span = next(span for span in _fixture_spans() if span.span.name == "llm")

    payload = span_payload(span.span)
    reordered = Span()
    reordered.CopyFrom(span.span)

    assert payload == span_payload(reordered) == span.span.SerializeToString(deterministic=True)
    assert payload_digest(payload) == hashlib.sha256(payload).digest()
    assert len(payload_digest(payload)) == 32
    assert Span.FromString(payload) == span.span

    stored = ResourceSpans.FromString(resource_payload(span))
    assert stored.resource == span.resource
    assert [scope_spans.scope.name for scope_spans in stored.scope_spans] == ["pipecat"]
    assert not stored.scope_spans[0].spans
    other_scope = OtlpSpan(
        resource=span.resource, scope=InstrumentationScope(name="pipecat.turn"), span=span.span
    )
    assert resource_payload(other_scope) != resource_payload(span)


# --- Session keys ----------------------------------------------------------------------------


def _keyed(span_attributes=(), resource_attributes=()) -> OtlpSpan:
    return OtlpSpan(
        resource=Resource(attributes=list(resource_attributes)),
        scope=InstrumentationScope(name="any"),
        span=Span(trace_id=b"\x01" * 16, span_id=b"\x02" * 8, attributes=list(span_attributes)),
    )


def test_the_real_capture_names_its_session_on_the_conversation_and_turn_spans() -> None:
    keys = {(span.span.name, resolve_session_key(span)) for span in _fixture_spans()}

    assert keys == {
        ("conversation", SessionKey(CAPTURED_SESSION_KEY, "earshot.session.id")),
        ("turn", SessionKey(CAPTURED_SESSION_KEY, "conversation.id")),
        ("stt", None),
        ("llm", None),
        ("tts", None),
    }


def test_span_keys_resolve_in_priority_order() -> None:
    attributes = [
        _string("conversation.id", "d"),
        _string("session.id", "c"),
        _string("gen_ai.conversation.id", "b"),
        _string("earshot.session.id", "a"),
    ]
    for expected in ("a", "b", "c", "d"):
        key = resolve_session_key(_keyed(attributes))
        assert key == SessionKey(expected, SESSION_KEY_ATTRIBUTES["abcd".index(expected)])
        attributes = [item for item in attributes if item.value.string_value != expected]
    assert resolve_session_key(_keyed(attributes)) is None


def test_on_the_resource_only_the_earshot_session_id_counts() -> None:
    process_wide = [_string(key, "process-key") for key in SESSION_KEY_ATTRIBUTES[1:]]

    assert resolve_session_key(_keyed(resource_attributes=process_wide)) is None
    assert resolve_session_key(
        _keyed(resource_attributes=[*process_wide, _string("earshot.session.id", "r")])
    ) == SessionKey("r", "earshot.session.id")
    # A span's own key wins over the resource's.
    assert resolve_session_key(
        _keyed([_string("conversation.id", "s")], [_string("earshot.session.id", "r")])
    ) == SessionKey("s", "conversation.id")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("call-1", "call-1"),
        ("  call-1\t\n", "call-1"),
        ("é" * 128, "é" * 128),  # 256 UTF-8 bytes
        ("é" * 128 + "x", None),  # 257 bytes
        ("x" * 257, None),
        ("", None),
        ("   ", None),
        ("NOT_GIVEN", None),
        (" NOT_GIVEN ", None),
    ],
)
def test_session_keys_are_one_to_256_utf8_bytes_after_trimming(value, expected) -> None:
    assert valid_session_key(value) == expected


def test_an_invalid_or_non_string_key_counts_as_no_key() -> None:
    integer_key = KeyValue(key="earshot.session.id", value=AnyValue(int_value=7))

    assert resolve_session_key(
        _keyed(
            [integer_key, _string("gen_ai.conversation.id", "x" * 300), _string("session.id", "ok")]
        )
    ) == SessionKey("ok", "session.id")


# --- End markers and session spans -------------------------------------------------------------


def test_only_the_conversation_span_of_the_real_capture_ends_the_call() -> None:
    markers = {
        (span.span.name, is_end_marker(span), is_session_span(span)) for span in _fixture_spans()
    }

    assert markers == {
        ("conversation", True, True),
        ("turn", False, False),
        ("stt", False, False),
        ("llm", False, False),
        ("tts", False, False),
    }


@pytest.mark.parametrize(
    ("scope", "name", "end", "session"),
    [
        ("pipecat.turn", "conversation", True, True),
        ("pipecat", "conversation", False, False),
        ("livekit-agents", "agent_session", True, True),
        ("livekit-agents", "session_close", True, False),
        ("livekit-agents", "agent_turn", False, False),
        ("other", "agent_session", False, False),
    ],
)
def test_end_markers_and_session_spans_match_scope_and_name(scope, name, end, session) -> None:
    span = OtlpSpan(
        resource=Resource(), scope=InstrumentationScope(name=scope), span=Span(name=name)
    )

    assert (is_end_marker(span), is_session_span(span)) == (end, session)


def test_any_span_can_end_its_call_with_earshot_call_ended() -> None:
    def marked(value: AnyValue) -> OtlpSpan:
        return OtlpSpan(
            resource=Resource(),
            scope=InstrumentationScope(name="my-app"),
            span=Span(name="hangup", attributes=[KeyValue(key="earshot.call.ended", value=value)]),
        )

    assert is_end_marker(marked(AnyValue(bool_value=True)))
    assert not is_end_marker(marked(AnyValue(bool_value=False)))
    assert not is_end_marker(marked(AnyValue(string_value="true")))
