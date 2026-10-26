"""The pure half: a call's stored spans and recordings become a revision bundle.

The materializer is the hosted connectors' server-side recorder pattern: an
``IncidentRecorder`` on a ``ManualClock`` with a fixed clock domain, fed spans in
``(start, trace_id, span_id)`` order through the adapter their instrumentation
scope names (Pipecat). Every test drives it with the real Pipecat 1.5
capture (``fixtures/captured/pipecat-otlp-1.5``) decoded as the receiver decodes
it, and stored as the receiver stores it.
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys

import pytest

from adapter_conformance import assert_capture_conforms
from captured_otlp import FIXTURE, captured_manifest, captured_requests
from earshot.adapters.pipecat import PipecatAdapter
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.privacy import classify_attribute
from earshot.recorder import IncidentRecorder, RecorderConfig
from otlp_support import make_request, make_span, string_attribute, with_links

pytestmark = pytest.mark.unit

MANUAL_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CALL_ID = "call_0123456789abcdef0123456789abcdef"


def _captured_spans(requests=None):
    return [
        span
        for request in (captured_requests() if requests is None else requests)
        for span in spans_from_request(request)
    ]


# --- the recorder takes its manual trace ID ------------------------------------------------


def test_a_recorder_uses_an_injected_manual_trace_id_for_its_manual_operations() -> None:
    def bundle():
        recorder = IncidentRecorder(
            session_id="call-1",
            bundle_id="call-1.r1",
            clock=ManualClock(wall=1_800_000_000_000_000_000, monotonic=0),
            config=RecorderConfig(clock_domain_id="otlp-call-1"),
            manual_trace_id=MANUAL_TRACE_ID,
        )
        with recorder.operation("framework_operation", operation_id="op-1"):
            pass
        return recorder.close()

    first = bundle()

    assert [operation.trace_id for operation in first.profile.operations] == [MANUAL_TRACE_ID]
    assert bundle().profile.operations[0].trace_id == MANUAL_TRACE_ID


@pytest.mark.parametrize("value", ["0" * 32, "ABC", "4BF92F3577B34DA6A3CE929D0E0E4736", ""])
def test_a_recorder_refuses_a_manual_trace_id_that_is_not_an_otlp_trace_id(value) -> None:
    with pytest.raises(ValueError, match="manual_trace_id"):
        IncidentRecorder(session_id="call-1", manual_trace_id=value)


# --- the Pipecat adapter takes hex link IDs -----------------------------------------------


def test_the_pipecat_adapter_consumes_spans_whose_links_carry_hex_ids() -> None:
    recorder = IncidentRecorder(
        session_id="call-1",
        bundle_id="call-1.r1",
        clock=ManualClock(wall=1_800_000_000_000_000_000, monotonic=0),
        config=RecorderConfig(clock_domain_id="otlp-call-1"),
    )
    adapter = PipecatAdapter(recorder)
    target_trace = "0af7651916cd43dd8448eb211c80319c"
    target_span = "b7ad6b7169203331"

    adapter.consume_span(
        {
            "name": "llm",
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "span_id": "00f067aa0ba902b7",
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_001_000_000_000,
            "links": [{"context": {"trace_id": target_trace, "span_id": target_span}}],
        }
    )
    [operation] = recorder.close().profile.operations

    assert [(link.trace_id, link.span_id) for link in operation.links] == [
        (target_trace, target_span)
    ]


# --- transcript lines -----------------------------------------------------------------------

USER_LINES = [
    "Hi. I would like to book a table for two people tomorrow evening.",
    "Seven thirty works for us.",
    "Could we sit somewhere quiet near the window?",
    "Perfect. That is everything I needed.",
    "Thank you very much. Goodbye.",
]
AGENT_LINES = [
    "Sure, what time would you like to come in tomorrow evening?",
    "Sure, we\u2019ve booked a quiet window seat for you at 7:30. We look forward to "
    "welcoming you tomorrow.",
    "You're welcome! See you tomorrow.",
]


def _lines(spans):
    from earshot.call_materializer import transcript_lines

    return [(line.speaker, line.text) for line in transcript_lines(spans)]


def test_the_real_capture_gives_every_stt_transcript_and_every_completed_llm_output() -> None:
    from earshot.call_materializer import transcript_lines

    spans = _captured_spans()
    lines = transcript_lines(reversed(spans))  # arrival order does not matter

    # is_final is false on all five stt spans of the real capture: not a filter.
    assert [line.text for line in lines if line.speaker == "user"] == USER_LINES
    # Two of the five llm spans were cancelled (no output): no line, no error.
    assert [line.text for line in lines if line.speaker == "agent"] == AGENT_LINES
    assert [line.start_unix_nano for line in lines] == sorted(
        line.start_unix_nano for line in lines
    )
    stt = next(span.span for span in spans if span.span.name == "stt")
    first = lines[0]
    assert (first.trace_id, first.span_id) == (stt.trace_id.hex(), stt.span_id.hex())
    assert (first.start_unix_nano, first.end_unix_nano) == (
        stt.start_time_unix_nano,
        stt.end_time_unix_nano,
    )


def _set_attribute(span, key: str, value: str) -> None:
    for item in span.attributes:
        if item.key == key:
            item.value.string_value = value
            return
    item = span.attributes.add()
    item.key = key
    item.value.string_value = value


def test_agent_lines_come_from_llm_output_on_1_5_and_1_6_shaped_spans_alike() -> None:
    # Pipecat 1.5 keeps only the last sentence in tts.text; 1.6 keeps the whole reply.
    shaped_1_6 = _captured_spans()
    outputs = iter(AGENT_LINES)
    for span in sorted(shaped_1_6, key=lambda item: item.span.start_time_unix_nano):
        if span.span.name == "tts":
            _set_attribute(span.span, "text", next(outputs))

    assert _lines(shaped_1_6) == _lines(_captured_spans())
    assert [text for speaker, text in _lines(shaped_1_6) if speaker == "agent"] == AGENT_LINES


@pytest.mark.parametrize("transcript", ["NOT_GIVEN", "", "   "])
def test_an_absent_or_blank_transcript_or_output_gives_no_line(transcript) -> None:
    spans = _captured_spans()
    for span in spans:
        if span.span.name == "stt":
            _set_attribute(span.span, "transcript", transcript)
        if span.span.name == "llm":
            _set_attribute(span.span, "output", transcript)

    assert _lines(spans) == []


def test_only_pipecat_scopes_give_lines() -> None:
    spans = [span for span in _captured_spans() if span.span.name in {"stt", "llm"}]
    for span in spans:
        span.scope.name = "some.other.framework"

    assert _lines(spans) == []


# --- a stored span, as the adapters read it --------------------------------------------------


def _stored(spans):
    """The spans as the receiver stores them and the closer reads them back."""

    from earshot.call_capture import resource_payload, span_payload, stored_span

    return [stored_span(span_payload(span.span), resource_payload(span)) for span in spans]


def test_a_stored_span_reads_back_as_the_span_resource_and_scope_it_was_stored_from() -> None:
    spans = _captured_spans(with_links(captured_requests()))

    assert _stored(spans) == spans


def test_a_stored_span_becomes_the_dict_an_adapter_consumes() -> None:
    from earshot.call_materializer import adapter_span

    spans = _captured_spans(with_links(captured_requests()))
    span = next(item for item in spans[1:] if item.span.name == "llm")
    span.span.status.code = 2  # STATUS_CODE_ERROR
    raw = span.span
    converted = adapter_span(span)

    assert converted["name"] == raw.name
    assert (converted["trace_id"], converted["span_id"]) == (raw.trace_id.hex(), raw.span_id.hex())
    assert converted["parent_span_id"] == raw.parent_span_id.hex()
    assert (converted["start_time"], converted["end_time"]) == (
        raw.start_time_unix_nano,
        raw.end_time_unix_nano,
    )
    assert converted["status"] == "error"
    assert "gen_ai.request.top_k" not in converted["attributes"]  # "NOT_GIVEN" is absent
    assert converted["attributes"]["gen_ai.request.model"] == "openai/gpt-oss-20b"
    [link] = converted["links"]
    assert link["context"] == {
        "trace_id": raw.links[0].trace_id.hex(),
        "span_id": raw.links[0].span_id.hex(),
    }
    assert link["attributes"] == {"link.kind": "previous"}
    [event] = converted["events"]
    assert (event["name"], event["timestamp"]) == (raw.events[0].name, raw.events[0].time_unix_nano)
    assert converted["instrumentation_scope"] == {
        "name": span.scope.name,
        "version": None,
        "attributes": {},
        "schema_url": None,
    }
    assert converted["resource"]["attributes"]["service.name"] == "earshot-pipecat-otlp-example"


@pytest.mark.parametrize(("code", "status"), [(0, "unset"), (1, "ok"), (2, "error")])
def test_span_status_codes_become_their_names(code, status) -> None:
    from earshot.call_materializer import adapter_span

    span = _captured_spans()[0]
    span.span.status.code = code

    assert adapter_span(span)["status"] == status


# --- the revision bundle -----------------------------------------------------------------------


def _recording(speaker: str, **changes):
    from earshot.storage_recordings import Recording

    audio = captured_manifest()["audio"]
    body = (FIXTURE / audio[speaker]["file"]).read_bytes()
    fields = {
        "project_id": "default",
        "recording_id": f"conv-1.{speaker}",
        "call_id": CALL_ID,
        "speaker": speaker,
        "content_type": "audio/wav",
        "label": None,
        "started_at_unix_nano": audio["started_at_unix_nano"],
        "state": "stored",
        "blob_key": f"recordings/default/{speaker}-attempt",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "duration_ms": round(audio[speaker]["duration_s"] * 1000),
        "created_unix_nano": 1,
        "stored_unix_nano": 2,
        **changes,
    }
    return Recording(**fields)


def _snapshot(spans=None, *, final=True, preset="full", revision=1, recordings=()):
    from earshot.call_materializer import CallSnapshot

    return CallSnapshot(
        call_id=CALL_ID,
        revision=revision,
        final=final,
        capture_preset=preset,
        spans=tuple(_stored(_captured_spans() if spans is None else spans)),
        recordings=tuple(recordings),
    )


def _materialize(snapshot):
    from earshot.call_materializer import materialize

    return materialize(snapshot)


def test_a_revision_is_the_calls_bundle_under_identities_derived_from_the_call() -> None:
    spans = _captured_spans()
    bundle = _materialize(_snapshot(spans, revision=3))
    profile = bundle.profile

    assert profile.manifest.bundle_id == f"{CALL_ID}.r3"
    assert profile.manifest.session_id == profile.session.session_id == CALL_ID
    assert [domain.clock_domain_id for domain in profile.clock_domains] == [f"otlp-{CALL_ID}"]
    assert profile.manifest.producer.name == "earshot.closer"
    assert [adapter.name for adapter in profile.manifest.adapters] == ["earshot.pipecat"]
    assert len(profile.operations) == 17
    assert {(op.trace_id, op.span_id) for op in profile.operations} == {
        (span.span.trace_id.hex(), span.span.span_id.hex()) for span in spans
    }
    # Session times are the evidence's, never the server clock's.
    assert profile.session.started_at.source_time_unix_nano == str(
        min(span.span.start_time_unix_nano for span in spans)
    )
    assert profile.session.ended_at.source_time_unix_nano == str(
        max(span.span.end_time_unix_nano for span in spans)
    )
    assert (profile.session.status, profile.manifest.finality) == ("completed", "final")
    assert profile.manifest.completeness == "complete"


def test_materializing_one_snapshot_twice_gives_byte_identical_valid_artifacts() -> None:
    snapshot = _snapshot(recordings=(_recording("user"), _recording("agent")))

    # Valid, canonical round trip, byte-identical twice (a wall-clock leak differs).
    bundle = assert_capture_conforms(lambda: _materialize(snapshot))

    shuffled = list(snapshot.spans)
    random.Random(7).shuffle(shuffled)
    again = _materialize(_snapshot_with(snapshot, spans=tuple(shuffled)))
    from earshot.codec import encode_incident_protobuf

    assert encode_incident_protobuf(again) == encode_incident_protobuf(bundle)


def _snapshot_with(snapshot, **changes):
    import dataclasses

    return dataclasses.replace(snapshot, **changes)


_DIGEST_IN_A_NEW_PROCESS = """
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import test_call_materializer as module
from earshot.codec import encode_incident_protobuf
snapshot = module._snapshot(recordings=(module._recording("user"), module._recording("agent")))
print(hashlib.sha256(encode_incident_protobuf(module._materialize(snapshot))).hexdigest())
"""


@pytest.mark.parametrize("seed", ["0", "4242"])
def test_another_process_with_another_hash_seed_materializes_the_same_bytes(seed) -> None:
    from earshot.codec import encode_incident_protobuf

    here = os.path.dirname(__file__)
    snapshot = _snapshot(recordings=(_recording("user"), _recording("agent")))
    expected = hashlib.sha256(encode_incident_protobuf(_materialize(snapshot))).hexdigest()
    result = subprocess.run(
        [sys.executable, "-c", _DIGEST_IN_A_NEW_PROCESS, here],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == expected


def test_a_quiet_period_close_is_a_provisional_interrupted_partial_observation() -> None:
    bundle = _materialize(_snapshot(final=False))
    manifest = bundle.profile.manifest

    assert (manifest.finality, manifest.completeness) == ("provisional", "incomplete")
    assert bundle.profile.session.status == "interrupted"
    assert bundle.profile.session.ended_at is None
    assert manifest.recovery is not None
    assert (
        manifest.recovery.method,
        manifest.recovery.reason,
        manifest.recovery.close_observed,
    ) == ("otlp_quiet_period", "no_end_signal", False)


def test_spans_of_an_unknown_scope_stay_out_of_the_bundle_and_are_declared() -> None:
    foreign = spans_from_request(
        make_request(
            [make_span(1, 1, name="GET /weather", start=1_792_372_170_000_000_000)],
            scope="opentelemetry.instrumentation.httpx",
        )
    )
    bundle = _materialize(_snapshot([*_captured_spans(), *foreign]))

    assert len(bundle.profile.operations) == 17
    [coverage] = [item for item in bundle.profile.coverage if item.signal == "call.spans"]
    assert (coverage.availability, coverage.reason, coverage.dropped_count) == (
        "partial",
        "unrecognized_instrumentation_scope",
        1,
    )
    field = hashlib.sha256(b"otlp.scope.opentelemetry.instrumentation.httpx").hexdigest()
    assert any(
        omission.attributes.get("field_key_sha256") == field
        for omission in bundle.profile.privacy.omissions
    )


def _omitted_fields(bundle) -> dict[str, str]:
    return {
        omission.attributes["field_key_sha256"]: omission.capture_class
        for omission in bundle.profile.privacy.omissions
        if "field_key_sha256" in omission.attributes
    }


def test_llm_input_history_is_left_out_of_the_bundle_first_and_declared() -> None:
    spans = _captured_spans()
    history = "[" + ", ".join(['{"role": "user", "content": "' + "x" * 200 + '"}'] * 400) + "]"
    assert len(history) > 64 * 1024
    for span in spans:
        if span.span.name == "llm":
            _set_attribute(span.span, "input", history)
    bundle = _materialize(_snapshot(spans))

    llm = [op for op in bundle.profile.operations if op.operation_name == "llm"]
    assert len(llm) == 5
    assert not any(key.startswith("gen_ai.input") for op in llm for key in op.attributes)
    outputs = [
        op.attributes["gen_ai.output.messages"]
        for op in llm
        if op.attributes.get("gen_ai.output.messages")
    ]
    assert outputs == AGENT_LINES
    assert _omitted_fields(bundle)[hashlib.sha256(b"input").hexdigest()] == "model_payload"
    assert not any(item.reason == "max_value_bytes" for item in bundle.profile.privacy.omissions)


# Pipecat keys (raw, as the receiver stores them) on each Pipecat span kind.
_PIPECAT_KEYS = (
    "text",
    "input",
    "output",
    "tools",
    "user_id",
    "transcript",
    "text_output",
    "tools.definitions",
    "session.instructions",
    "gen_ai.system_instructions",
)


@pytest.mark.parametrize("name", ["stt", "llm", "tts", "llm_tool_call", "turn"])
@pytest.mark.parametrize("key", _PIPECAT_KEYS)
def test_a_pipecat_key_reaches_the_bundle_only_under_the_class_the_receiver_gives_it(
    name, key
) -> None:
    from earshot.adapters.pipecat import native_attribute_name
    from earshot.call_capture import attribute_capture_class

    span = spans_from_request(
        make_request([make_span(1, 1, name=name, attributes=[string_attribute(key, "v")])])
    )
    bundle = _materialize(_snapshot(span))
    [operation] = bundle.profile.operations
    expected = attribute_capture_class(key).value
    recorded = native_attribute_name({"name": name, "attributes": {key: "v"}}, key)

    if recorded in operation.attributes:
        assert classify_attribute(recorded).value == expected
    else:
        assert _omitted_fields(bundle)[hashlib.sha256(key.encode()).hexdigest()] == expected


def test_each_stored_recording_is_a_media_reference_aligned_to_the_call_clock() -> None:
    from earshot.validation import validate_incident

    user, agent = _recording("user"), _recording("agent")
    bundle = _materialize(_snapshot(recordings=(agent, user)))
    profile = bundle.profile
    result = validate_incident(bundle)

    assert result.ok, result.issues
    assert not result.warnings, result.warnings  # no EARSHOT_MEDIA_UNALIGNED
    refs = {ref.media_id: ref for ref in profile.media_refs}
    assert sorted(refs) == ["recording-conv-1.agent", "recording-conv-1.user"]
    for recording, stream in ((user, "stream-user"), (agent, "stream-agent")):
        ref = refs[f"recording-{recording.recording_id}"]
        assert (ref.sha256, ref.size_bytes, ref.content_type) == (
            recording.sha256,
            recording.size,
            "audio/wav",
        )
        assert (ref.integrity, ref.custodian, ref.stream_id) == (
            "content_digest",
            "earshot.blob_store",
            stream,
        )
        [relation] = [
            item
            for item in profile.clock_relations
            if item.from_clock_domain_id == ref.clock_domain_id
        ]
        assert relation.to_clock_domain_id == f"otlp-{CALL_ID}"
        assert relation.offset_nano == recording.started_at_unix_nano
        assert ref.time_range.start.source_time_unix_nano == recording.started_at_unix_nano
    streams = {stream.stream_id: stream for stream in profile.audio_streams}
    participants = {item.participant_id: item.role for item in profile.participants}
    assert participants[streams["stream-user"].participant_id] == "user"
    assert participants[streams["stream-agent"].participant_id] == "agent"
    assert profile.manifest.completeness == "complete"


def _stereo_recording(**changes):
    """The real LiveKit 1.8 stereo Ogg Opus recording (ch0 user, ch1 agent), stored."""

    from audio_support import LIVEKIT_RECORDING
    from earshot.storage_recordings import Recording

    body = LIVEKIT_RECORDING.read_bytes()
    fields = {
        "project_id": "default",
        "recording_id": "lk-1.call",
        "call_id": CALL_ID,
        "speaker": "mixed",
        "content_type": "audio/ogg",
        "label": None,
        "started_at_unix_nano": "1792407986846424000",
        "state": "stored",
        "blob_key": "recordings/default/stereo-attempt",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "duration_ms": 34_010,
        "created_unix_nano": 1,
        "stored_unix_nano": 2,
        "channel_speakers": ("user", "agent"),
        **changes,
    }
    return Recording(**fields)


def test_a_stereo_recording_is_one_stream_and_media_reference_per_mapped_channel() -> None:
    from earshot.validation import validate_incident

    stereo = _stereo_recording()
    bundle = assert_capture_conforms(lambda: _materialize(_snapshot(recordings=(stereo,))))
    profile = bundle.profile
    result = validate_incident(bundle)

    assert result.ok, result.issues
    assert not result.warnings, result.warnings
    refs = sorted(profile.media_refs, key=lambda ref: ref.media_id)
    assert [
        (ref.media_id, ref.stream_id, ref.channel_index, ref.sha256, ref.size_bytes) for ref in refs
    ] == [
        ("recording-lk-1.call.ch0", "stream-user", 0, stereo.sha256, stereo.size),
        ("recording-lk-1.call.ch1", "stream-agent", 1, stereo.sha256, stereo.size),
    ]
    # One file, one media timeline: both channels share its clock domain and relation.
    assert {ref.clock_domain_id for ref in refs} == {"media-lk-1.call"}
    assert {ref.content_type for ref in refs} == {"audio/ogg"}
    [relation] = [
        item for item in profile.clock_relations if item.from_clock_domain_id == "media-lk-1.call"
    ]
    assert relation.offset_nano == stereo.started_at_unix_nano
    streams = {stream.stream_id: stream for stream in profile.audio_streams}
    participants = {item.participant_id: item.role for item in profile.participants}
    assert {
        key: (participants[streams[key].participant_id], streams[key].direction)
        for key in ("stream-user", "stream-agent")
    } == {"stream-user": ("user", "inbound"), "stream-agent": ("agent", "outbound")}
    assert "stream-mixed" not in streams
    # The channel survives the wire: a reader sees which channel each reference is.
    from earshot.codec import decode_incident_protobuf, encode_incident_protobuf

    decoded = decode_incident_protobuf(encode_incident_protobuf(bundle))
    assert sorted(ref.channel_index for ref in decoded.profile.media_refs) == [0, 1]


def test_a_one_channel_map_is_that_speakers_stream_on_channel_0() -> None:
    bundle = _materialize(
        _snapshot([], recordings=(_stereo_recording(channel_speakers=("agent",)),))
    )

    [ref] = bundle.profile.media_refs
    assert (ref.media_id, ref.stream_id, ref.channel_index) == (
        "recording-lk-1.call.ch0",
        "stream-agent",
        0,
    )


@pytest.mark.parametrize("content_type", ["audio/mpeg", "audio/webm"])
def test_a_channel_map_the_upload_could_not_verify_gives_one_media_reference(
    content_type: str,
) -> None:
    """The upload reads no MP3 or WebM header, so their channel count is unverified:
    the file is one reference under its own speaker, never one per mapped channel."""

    recording = _stereo_recording(content_type=content_type, duration_ms=None)

    bundle = _materialize(_snapshot([], recordings=(recording,)))

    assert [
        (ref.media_id, ref.stream_id, ref.channel_index) for ref in bundle.profile.media_refs
    ] == [("recording-lk-1.call", "stream-mixed", None)]


def test_a_mono_recordings_media_reference_carries_no_channel_index() -> None:
    from earshot.codec import encode_incident_json

    bundle = _materialize(_snapshot(recordings=(_recording("user"), _recording("agent"))))

    assert {ref.channel_index for ref in bundle.profile.media_refs} == {None}
    assert b"channel_index" not in encode_incident_json(bundle)


def test_a_call_holding_more_recordings_than_a_revision_takes_builds_with_its_first_sixteen() -> (
    None
):
    """Each recording takes one of the recorder's 16
    media clock domains; a 17th left a clock relation and a media reference naming a
    domain never declared, so every build of the call failed, for good. The API now
    refuses a 17th; a call that has more still builds: its first 16 recordings by
    creation are media references, and the others are declared omitted."""

    from earshot.validation import validate_incident

    # Created in the reverse order of their IDs, so creation order is what selects.
    recordings = [
        _recording("user", recording_id=f"rec-{index:02d}", created_unix_nano=100 - index)
        for index in range(18)
    ]
    bundle = _materialize(_snapshot(recordings=recordings))
    result = validate_incident(bundle)

    assert result.ok, result.issues
    assert sorted(ref.media_id for ref in bundle.profile.media_refs) == [
        f"recording-rec-{index:02d}" for index in range(2, 18)
    ]
    [coverage] = [item for item in bundle.profile.coverage if item.signal == "call.recordings"]
    assert (coverage.availability, coverage.reason, coverage.dropped_count) == (
        "partial",
        "recording_limit",
        2,
    )
    omitted = _omitted_fields(bundle)
    for recording_id in ("rec-00", "rec-01"):
        assert omitted[hashlib.sha256(f"recording.{recording_id}".encode()).hexdigest()] == "audio"


def test_a_call_with_only_a_recording_still_materializes() -> None:
    bundle = _materialize(_snapshot([], recordings=(_recording("user"),), final=False))

    assert len(bundle.profile.media_refs) == 1
    assert (
        bundle.profile.session.started_at.source_time_unix_nano
        == (captured_manifest()["audio"]["started_at_unix_nano"])
    )


def test_under_metadata_only_the_bundle_captures_no_content_class() -> None:
    from earshot.call_capture import apply_capture_preset

    stored = [apply_capture_preset("metadata_only", span) for span in _captured_spans()]
    bundle = _materialize(
        _snapshot(stored, preset="metadata_only", recordings=(_recording("user"),))
    )
    captured = {
        policy.capture_class for policy in bundle.profile.privacy.capture_classes if policy.captured
    }

    assert captured == {"metadata"}
    assert bundle.profile.privacy.policy_id == "earshot.metadata-only"
    assert bundle.profile.media_refs == ()
    [coverage] = [item for item in bundle.profile.coverage if item.signal == "call.transcript"]
    assert (coverage.availability, coverage.reason) == ("unavailable", "not_captured_by_preset")


def test_a_full_capture_declares_its_transcript_and_content_classes() -> None:
    bundle = _materialize(_snapshot())
    captured = {
        policy.capture_class for policy in bundle.profile.privacy.capture_classes if policy.captured
    }

    assert {"transcript", "model_payload"} <= captured
    [coverage] = [item for item in bundle.profile.coverage if item.signal == "call.transcript"]
    assert coverage.availability == "available"


# --- the host profile --------------------------------------------------------------------------


def _host_call_spans(**turn_fields):
    from host_profile_support import HostStage, HostTurn, host_request

    start = 1_800_000_000_000_000_000
    second = 1_000_000_000
    turn = HostTurn(
        "turn-1",
        start,
        start + 4 * second,
        user_speech=((start, start + second),),
        agent_speech=(start + 2 * second, start + 4 * second),
        stages=(
            HostStage(
                "llm",
                start + second,
                start + 2 * second,
                {"gen_ai.provider.name": "openai", "gen_ai.usage.input_tokens": 120},
            ),
        ),
        reported_latency_ms=950.0,
        timeline_continuous=True,
        **turn_fields,
    )
    return spans_from_request(host_request([turn]))


def test_host_spans_are_recognized_and_each_becomes_an_operation_of_its_turn() -> None:
    spans = _host_call_spans()
    bundle = _materialize(_snapshot(spans))
    profile = bundle.profile

    assert [adapter.name for adapter in profile.manifest.adapters] == ["earshot.host"]
    assert not [item for item in profile.coverage if item.signal == "call.spans"]
    assert {(op.trace_id, op.span_id) for op in profile.operations} == {
        (span.span.trace_id.hex(), span.span.span_id.hex()) for span in spans
    }
    by_name = {op.operation_name: op for op in profile.operations}
    assert set(by_name) == {"call", "turn", "user_speech", "agent_speech", "llm"}
    assert {by_name[name].turn_id for name in ("turn", "user_speech", "agent_speech", "llm")} == {
        "turn-1"
    }
    assert by_name["turn"].attributes["earshot.turn.reported_latency_ms"] == 950.0
    assert by_name["turn"].attributes["earshot.turn.timeline_continuous"] is True
    assert by_name["llm"].attributes["gen_ai.usage.input_tokens"] == 120
    assert profile.manifest.completeness == "complete"


def test_a_host_call_is_a_valid_deterministic_metadata_only_revision_under_both_presets() -> None:
    from earshot.call_capture import apply_capture_preset

    for preset in ("full", "metadata_only"):
        stored = [apply_capture_preset(preset, span) for span in _host_call_spans()]
        snapshot = _snapshot(stored, preset=preset)
        # The profile carries nothing a revision leaves out, so nothing is omitted.
        bundle = assert_capture_conforms(
            lambda snapshot=snapshot: _materialize(snapshot), require_omission=False
        )
        captured = {
            policy.capture_class
            for policy in bundle.profile.privacy.capture_classes
            if policy.captured
        }
        assert captured == {"metadata"}, preset
        [coverage] = [item for item in bundle.profile.coverage if item.signal == "call.transcript"]
        expected = "not_observed" if preset == "full" else "unavailable"
        assert coverage.availability == expected, preset
