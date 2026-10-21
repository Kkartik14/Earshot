"""AC3.2, AC3.3, FR-4's pure half and AC8.1: what is kept of a span, and which session it names.

Classification runs on the *normalized* key: frameworks put content under keys
Earshot's classifier reads as metadata (Pipecat's raw ``input``; LiveKit 1.7's
``lk.pii.*``). Every row below states the class before normalization (the
classifier alone) and after it, so a key that silently leaks shows up as a row
whose "after" is metadata. The keys are the ones the Month 1 research found
(`local/research/month-1/voice-frameworks-telemetry.md` §1, §4, §5) and the
ones the real Pipecat capture carries.

LiveKit keys have their own table (``LIVEKIT_KEYS``): every attribute key of
LiveKit Agents 1.6.10, 1.7.1 and 1.8.8 (``telemetry/trace_types.py`` of each
release) and of the real 1.8 capture, with the semantic key the bundle records it
under and its capture class. An ``lk.*`` key outside the table is never classified
by its name.
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

from captured_livekit_otlp import livekit_spans
from captured_otlp import CAPTURED_SESSION_KEY, captured_requests
from earshot.call_capture import (
    ATTRIBUTE_KEYS,
    SESSION_KEY_ATTRIBUTES,
    OtlpSpan,
    SessionKey,
    apply_capture_preset,
    attribute_capture_class,
    attribute_values,
    canonical_attribute_key,
    is_end_marker,
    is_session_span,
    payload_digest,
    recorder_agrees,
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
    # any other key with a dot-delimited ``pii`` segment is content, as LiveKit defines it
    ("vendor.pii.note", M, EXT),
    # OpenTelemetry GenAI content attributes (opt-in, "likely sensitive")
    ("gen_ai.input.messages", MODEL, MODEL),
    ("gen_ai.output.messages", MODEL, MODEL),
    ("gen_ai.tool.definitions", M, MODEL),
    ("gen_ai.tool.description", M, MODEL),
    ("gen_ai.tool.call.arguments", M, TOOL),
    ("gen_ai.tool.call.result", M, TOOL),
    ("gen_ai.retrieval.query.text", M, MODEL),
    ("gen_ai.retrieval.documents", M, MODEL),
    ("gen_ai.memory.query.text", M, MODEL),
    ("gen_ai.memory.records", M, MODEL),
    ("gen_ai.evaluation.explanation", M, MODEL),
    ("gen_ai.prompt.variable.customer_name", MODEL, MODEL),
    ("gen_ai.tool.name", M, M),
    ("gen_ai.tool.call.id", M, M),
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


# LiveKit keys whose value is plain metadata (ids, labels, enums, counts, durations,
# metric JSON): the union over 1.6.10, 1.7.1 and 1.8.8, recorded under their own name.
LIVEKIT_METADATA_KEYS = (
    # identifiers and labels
    "lk.agent_label",
    "lk.agent_name",
    "lk.callback.name",
    "lk.cloud_agent_id",
    "lk.deployment_id",
    "lk.dispatch_id",
    "lk.fallback.index",
    "lk.fallback.label",
    "lk.fallback.recovery",
    "lk.function_tool.id",
    "lk.function_tool.is_error",
    "lk.function_tool.name",
    "lk.generation_count",
    "lk.generation_id",
    "lk.input.base_span_id",
    "lk.input.delta",
    "lk.input.dropped_from_base",
    "lk.job.agent_id",
    "lk.job.worker_id",
    "lk.job_id",
    "lk.parent_generation_id",
    "lk.participant_id",
    "lk.participant_kind",
    "lk.previous_agent_label",
    "lk.provider_request_ids",
    "lk.room_sid",
    "lk.speech_id",
    "lk.track_sid",
    "lk.track_source",
    "lk.tts.label",
    # states, reasons and flags (enumerations, not free text)
    "lk.amd.category",
    "lk.amd.reason",
    "lk.close.drain",
    "lk.close_reason",
    "lk.connection_state",
    "lk.disconnect_reason",
    "lk.eou.from_cache",
    "lk.eou.language",
    "lk.eou.outcome",
    "lk.eou.source",
    "lk.interrupted",
    "lk.interruption.source",
    "lk.is_interruption",
    "lk.new_state",
    "lk.old_state",
    "lk.room.auto_subscribe",
    "lk.room.e2ee",
    "lk.room_io.participant_filter",
    "lk.rpc.error_code",
    "lk.rpc.handler_registered",
    "lk.rpc.request_id",
    "lk.shutdown.user_initiated",
    "lk.tts.streaming",
    # counts, sizes, times and durations
    "lk.amd.delay",
    "lk.amd.speech_duration",
    "lk.blocking.count",
    "lk.blocking.cpu_time",
    "lk.blocking.duration",
    "lk.blocking.gc_time",
    "lk.blocking.import",
    "lk.blocking.max_duration",
    "lk.blocking.severity",
    "lk.blocking.suppressed",
    "lk.blocking.task",
    "lk.blocking.threshold",
    "lk.blocking.total_duration",
    "lk.e2e_latency",
    "lk.end_of_turn_delay",
    "lk.end_time",
    "lk.eou.detection_delay",
    "lk.eou.endpointing_delay",
    "lk.eou.not_committed_count",
    "lk.eou.probability",
    "lk.eou.rearm_count",
    "lk.eou.resume_count",
    "lk.eou.transcription_delay",
    "lk.eou.unlikely_threshold",
    "lk.eou.wait_duration",
    "lk.first_frame_delay",
    "lk.interruption.detection_delay",
    "lk.interruption.prediction_duration",
    "lk.interruption.probability",
    "lk.interruption.total_duration",
    "lk.job.accept_latency",
    "lk.job.assignment_latency",
    "lk.job.dispatch_latency",
    "lk.job.entrypoint_latency",
    "lk.job.launch_latency",
    "lk.keyterms.added",
    "lk.keyterms.count",
    "lk.keyterms.removed",
    "lk.on_user_turn_completed_delay",
    "lk.playout.position",
    "lk.response.ttfb",
    "lk.response.ttft",
    "lk.retry_count",
    "lk.room.remote_participant_count",
    "lk.rpc.payload_size",
    "lk.rpc.response_size",
    "lk.rpc.response_timeout",
    "lk.speech.queue_wait",
    "lk.start_time",
    "lk.transcript_confidence",
    "lk.transcription_delay",
    # usage and timing JSON (labels, request ids, counts; never text) and the 1.8.4
    # transcript event timeline (arrival times and lengths only)
    "lk.llm_metrics",
    "lk.realtime_model_metrics",
    "lk.stt.events",
    "lk.tts_metrics",
)

# (raw key, semantic key, capture class). A 1.6 key and its 1.7 ``lk.pii.`` name share
# one semantic key; one span's content keys share one class, because a bundle record
# holds at most one sensitive class (``agent_turn`` carries the user input, the
# instructions and the reply; ``llm_node`` the context, the reply and its tool calls).
LIVEKIT_KEYS = [
    ("lk.user_transcript", "speech.text", T),
    ("lk.pii.user_transcript", "speech.text", T),
    ("lk.input_text", "speech.text", T),
    ("lk.pii.input_text", "speech.text", T),
    ("lk.amd.transcript", "lk.pii.amd.transcript", T),
    ("lk.pii.amd.transcript", "lk.pii.amd.transcript", T),
    ("lk.user_input", "gen_ai.input.user_message", MODEL),
    ("lk.pii.user_input", "gen_ai.input.user_message", MODEL),
    ("lk.instructions", "gen_ai.input.system_instruction", MODEL),
    ("lk.pii.instructions", "gen_ai.input.system_instruction", MODEL),
    ("lk.chat_ctx", "gen_ai.input", MODEL),
    ("lk.pii.chat_ctx", "gen_ai.input", MODEL),
    ("lk.response.text", "gen_ai.output.messages", MODEL),
    ("lk.pii.response.text", "gen_ai.output.messages", MODEL),
    ("lk.response.function_calls", "gen_ai.output.messages.function_calls", MODEL),
    ("lk.pii.response.function_calls", "gen_ai.output.messages.function_calls", MODEL),
    ("lk.function_tools", "gen_ai.input.function_tools", MODEL),
    ("lk.provider_tools", "gen_ai.input.provider_tools", MODEL),
    ("lk.tool_sets", "gen_ai.input.tool_sets", MODEL),
    ("lk.function_tool.arguments", "tool.arguments", TOOL),
    ("lk.pii.function_tool.arguments", "tool.arguments", TOOL),
    ("lk.function_tool.output", "tool.result", TOOL),
    ("lk.pii.function_tool.output", "tool.result", TOOL),
    ("lk.participant_identity", "participant.name", ID),
    ("lk.pii.participant_identity", "participant.name", ID),
    ("lk.pii.sip.phoneNumber", "sip.phone_number", ID),
    ("lk.rpc.caller_identity", "rpc.caller.participant.name", ID),
    ("lk.rpc.destination_identity", "rpc.destination.participant.name", ID),
    ("lk.room_name", "lk.pii.room_name", EXT),
    ("lk.pii.room_name", "lk.pii.room_name", EXT),
    ("lk.pii.rpc.payload", "lk.pii.rpc.payload", EXT),
    ("lk.pii.rpc.response", "lk.pii.rpc.response", EXT),
    ("lk.session_options", "lk.session_options", EXT),
    ("lk.shutdown.reason", "lk.shutdown.reason", EXT),  # developer-authored free text
    ("lk.blocking.stack", "lk.blocking.exception.stacktrace", DIAG),
    ("lk.pre_connect_audio.duration", "lk.pre_connect.buffer_duration", M),
    *((key, key, M) for key in LIVEKIT_METADATA_KEYS),
]


@pytest.mark.parametrize(
    ("raw", "semantic", "capture_class"), LIVEKIT_KEYS, ids=[r[0] for r in LIVEKIT_KEYS]
)
def test_every_livekit_key_has_an_explicit_semantic_key_and_class(
    raw, semantic, capture_class
) -> None:
    assert ATTRIBUTE_KEYS[raw].semantic_key == semantic
    assert ATTRIBUTE_KEYS[raw].capture_class is capture_class
    assert canonical_attribute_key(raw) == semantic
    assert attribute_capture_class(raw) is capture_class


def test_the_livekit_rows_of_the_table_are_exactly_the_reviewed_keys() -> None:
    table = {key for key in ATTRIBUTE_KEYS if key.startswith("lk.")}

    assert table == {row[0] for row in LIVEKIT_KEYS}


@pytest.mark.parametrize(
    ("key", "named_as"),
    [
        ("lk.future.transcript_text", T),  # the name says transcript; it is not trusted
        ("lk.pii.some_future_key", M),
        ("lk.future_metric", M),
        ("lk.sip.trunkPhoneNumber", M),  # LiveKit copies SIP attributes under lk.sip.
        ("lk.audio_stream", CaptureClass.AUDIO),
    ],
)
def test_an_unreviewed_livekit_key_is_extension_payload_whatever_its_name_says(
    key, named_as
) -> None:
    assert key not in ATTRIBUTE_KEYS
    assert classify_attribute(key) is named_as
    assert attribute_capture_class(key) is EXT
    assert canonical_attribute_key(key) == key


def test_every_livekit_key_of_the_real_1_8_capture_is_in_the_table() -> None:
    keys = set()
    for span in livekit_spans():
        for message in (span.span, span.resource, span.scope, *span.span.events):
            keys.update(item.key for item in message.attributes)

    livekit = {key for key in keys if key.startswith("lk.")}
    assert len(livekit) > 40
    assert livekit <= set(ATTRIBUTE_KEYS)
    assert {"lk.pii.user_transcript", "lk.pii.response.text", "lk.stt.events"} <= livekit


# The keys whose class the OTLP path (this table) and the SDK classifier
# (``privacy.classify_attribute``, which the recorder, validation and checkpoint
# governance use) disagree on: the SDK files their semantic name under another
# class. Making ``classify_attribute`` agree would re-class attributes of bundles
# that 0.2.x SDKs already produced (their validation would fail) and would make
# records mixing these with transcript or model content refused, so the table is
# authoritative for OTLP and these stay listed here (the WS4 log carries them).
SDK_CLASSIFIER_DIFFERENCES = {
    "user_id": (ID, M),
    "user.id": (ID, M),
    "gen_ai.tool.definitions": (MODEL, M),
    "gen_ai.tool.description": (MODEL, M),
    "gen_ai.tool.call.arguments": (TOOL, M),
    "gen_ai.tool.call.result": (TOOL, M),
    "gen_ai.retrieval.query.text": (MODEL, M),
    "gen_ai.retrieval.documents": (MODEL, M),
    "gen_ai.memory.query.text": (MODEL, M),
    "gen_ai.memory.records": (MODEL, M),
    "gen_ai.evaluation.explanation": (MODEL, M),
}
# Keys outside the table that the OTLP path classifies by shape (identity, ``pii``).
SDK_CLASSIFIER_SHAPE_DIFFERENCES = {
    "enduser.id": (ID, M),
    "caller.number": (ID, M),
    "callee.number": (ID, M),
    "user.phone": (ID, M),
    "sip.from": (ID, M),
    "sip.to": (ID, M),
    "vendor.pii.note": (EXT, M),
}


def test_the_recorder_files_every_table_key_under_its_class_except_the_listed_ones() -> None:
    differences = {
        raw: (rule.capture_class, classify_attribute(rule.semantic_key))
        for raw, rule in ATTRIBUTE_KEYS.items()
        if not recorder_agrees(rule.semantic_key, rule.capture_class)
    }

    assert differences == SDK_CLASSIFIER_DIFFERENCES


@pytest.mark.parametrize("key", sorted(SDK_CLASSIFIER_SHAPE_DIFFERENCES))
def test_the_sdk_classifier_differs_from_the_otlp_one_only_on_the_listed_shapes(key) -> None:
    otlp, sdk = SDK_CLASSIFIER_SHAPE_DIFFERENCES[key]

    assert key not in ATTRIBUTE_KEYS
    assert (attribute_capture_class(key), classify_attribute(key)) == (otlp, sdk)


@pytest.mark.parametrize(
    ("key", "capture_class", "agrees"),
    [
        ("lk.e2e_latency", M, True),  # safe metadata
        ("lk.eou.probability", M, True),  # metadata the bundle keeps on the extension grant
        ("lk.pii.room_name", EXT, True),  # the same gate, for content
        ("lk.e2e_latency", EXT, False),  # safe metadata would leak extension payload
        ("speech.text", T, True),
        ("speech.text", MODEL, False),
        ("gen_ai.tool.definitions", MODEL, False),
    ],
)
def test_the_recorder_agrees_with_a_class_when_it_files_the_key_under_it(
    key, capture_class, agrees
) -> None:
    assert recorder_agrees(key, capture_class) is agrees


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
