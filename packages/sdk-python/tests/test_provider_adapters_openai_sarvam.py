"""Fused Realtime and Sarvam provider events retain only honest evidence."""

from __future__ import annotations

import hashlib

import pytest

import earshot
from earshot.adapters.providers import OpenAIRealtimeAdapter, SarvamAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, encode_incident_protobuf
from earshot.storage import IncidentStore
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

START = 1_752_800_000_000_000_000
IDENTITY_KEY = b"provider-adapter-test-identity-key"


def test_openai_realtime_stays_fused_and_uses_same_clock_receipt_delta() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    events = [
        (
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": "event-stop",
                "item_id": "item-private",
                "audio_end_ms": 850,
            },
            1_000,
        ),
        (
            {
                "type": "response.created",
                "event_id": "event-created",
                "response": {"id": "response-private"},
            },
            1_020,
        ),
        (
            {
                "type": "response.output_audio.delta",
                "event_id": "event-audio",
                "response_id": "response-private",
                "delta": "base64-private-audio-must-not-survive",
            },
            1_410,
        ),
        (
            {
                "type": "response.done",
                "event_id": "event-done",
                "response": {"id": "response-private", "status": "completed"},
            },
            1_500,
        ),
    ]

    session = earshot.pipeline(session_id="realtime", started_at_unix_nano=START)
    with session.turn() as turn:
        for payload, received_at_ms in events:
            assert adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    bundle = session.close()

    assert validate_incident(bundle).ok
    [operation] = bundle.profile.operations
    assert operation.operation_name == "agent"
    assert operation.status == "ok"
    assert operation.attributes["gen_ai.provider.name"] == "openai"
    assert {item.operation_name for item in bundle.profile.operations}.isdisjoint(
        {"stt", "llm", "tts"}
    )
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    response = analysis.projections.turns[0].metrics.response_latency
    assert response.value == 410
    assert response.confidence == "estimated"
    direct_response = next(
        sample
        for sample in bundle.profile.quality_samples
        if sample.measurements[0].name == "earshot.turn.response_latency"
    )
    assert direct_response.evidence.source_field == "response.output_audio.delta"
    canonical = encode_incident_protobuf(bundle)
    assert b"base64-private-audio" not in canonical
    assert b"response-private" not in canonical
    [omission] = bundle.profile.privacy.omissions
    assert omission.capture_class == "audio"
    assert (
        omission.attributes["field_key_sha256"]
        == hashlib.sha256(b"openai.realtime.response.output_audio.delta.delta").hexdigest()
    )


def test_realtime_speech_start_detects_but_does_not_accept_interruption() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    created = adapter.adapt(
        {"type": "response.created", "response": {"id": "active-response"}},
        received_at_ms=100,
    )
    session = earshot.pipeline(session_id="interrupt", started_at_unix_nano=START)
    with session.turn() as turn:
        created.apply(turn)
        speech = adapter.adapt(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "next-item",
                "audio_start_ms": 75,
            },
            received_at_ms=200,
        )
        speech.apply(turn)
    bundle = session.close()

    names = {event.event_name for event in bundle.profile.events}
    assert "earshot.interruption.detected" in names
    assert "earshot.interruption.accepted" not in names


def test_realtime_cancelled_response_is_not_reported_completed() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    created = adapter.adapt(
        {"type": "response.created", "response": {"id": "cancelled-response"}},
        received_at_ms=100,
    )
    session = earshot.pipeline(session_id="cancelled", started_at_unix_nano=START)
    with session.turn() as turn:
        created.apply(turn)
        done = adapter.adapt(
            {
                "type": "response.done",
                "response": {"id": "cancelled-response", "status": "cancelled"},
            },
            received_at_ms=150,
        )
        done.apply(turn)

    [operation] = session.close().profile.operations
    assert operation.operation_name == "agent"
    assert operation.status == "cancelled"


def test_sarvam_transcription_converts_latency_once_and_exposes_language(tmp_path) -> None:
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    update = adapter.adapt(
        {
            "type": "data",
            "data": {
                "request_id": "sarvam-request-private",
                "transcript": "customer secret must never survive",
                "language_code": "hi-IN",
                "language_probability": None,
                "metrics": {"audio_duration": 1.25, "processing_latency": 0.084},
            },
        },
        received_at_ms=1_500,
    )
    session = earshot.pipeline(session_id="sarvam", started_at_unix_nano=START)
    with session.turn() as turn:
        assert update.apply(turn)
        assert not update.apply(turn)
    bundle = session.close()

    assert validate_incident(bundle).ok
    [operation] = bundle.profile.operations
    assert operation.attributes["gen_ai.request.model"] == "saaras:v3"
    assert operation.attributes["earshot.language.code"] == "hi-IN"
    measurements = {
        measurement.name: measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements["sarvam.stt.processing_latency"].value == 84
    assert measurements["sarvam.stt.processing_latency"].unit == "ms"
    processing_sample = next(
        sample
        for sample in bundle.profile.quality_samples
        if sample.measurements[0].name == "sarvam.stt.processing_latency"
    )
    assert processing_sample.evidence.source_field == "metrics.processing_latency"
    assert "earshot.stt.ttfb" not in measurements
    store = IncidentStore(tmp_path)
    store.ingest(bundle, encode_incident_protobuf(bundle))
    [fact] = store.list_turn_facts()
    assert fact.language == "hi-IN"
    [summary] = store.summarize_turn_metric("response_ms", group_by="language")
    assert summary.group == "hi-IN"
    canonical = encode_incident_protobuf(bundle)
    assert b"customer secret" not in canonical
    assert b"sarvam-request-private" not in canonical
    [omission] = bundle.profile.privacy.omissions
    assert omission.capture_class == "transcript"
    assert omission.reason == "adapter_payload_omitted"


def test_sarvam_auto_detected_language_probability_is_governed() -> None:
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    update = adapter.adapt(
        {
            "type": "data",
            "data": {
                "request_id": "request-auto",
                "transcript": "private",
                "language_code": "hi-IN",
                "language_probability": 0.93,
                "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
            },
        },
        received_at_ms=500,
    )
    session = earshot.pipeline(session_id="language", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    [operation] = session.close().profile.operations

    assert operation.attributes["earshot.language.probability"] == 0.93


def test_sarvam_rejects_probability_for_a_fixed_language_before_recording() -> None:
    adapter = SarvamAdapter(language_code="en-IN", identity_key=IDENTITY_KEY)
    with pytest.raises(ValueError, match="must be null"):
        adapter.adapt(
            {
                "type": "data",
                "data": {
                    "request_id": "request-fixed",
                    "transcript": "private",
                    "language_code": "en-IN",
                    "language_probability": 0.99,
                    "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
                },
            },
            received_at_ms=500,
        )


def test_sarvam_vad_and_error_frames_never_retain_provider_content() -> None:
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    started = adapter.adapt(
        {"type": "events", "data": {"signal_type": "START_SPEECH"}},
        received_at_ms=100,
    )
    ended = adapter.adapt(
        {"type": "events", "data": {"signal_type": "END_SPEECH"}},
        received_at_ms=400,
    )
    error = adapter.adapt(
        {
            "type": "error",
            "data": {
                "code": "invalid_audio",
                "error": "private provider diagnostic with customer content",
            },
        },
        received_at_ms=450,
    )
    session = earshot.pipeline(session_id="sarvam-events", started_at_unix_nano=START)
    with session.turn() as turn:
        started.apply(turn)
        ended.apply(turn)
        error.apply(turn)
    bundle = session.close()

    assert [event.event_name for event in bundle.profile.events] == [
        "earshot.speech.started",
        "earshot.speech.ended",
    ]
    [operation] = bundle.profile.operations
    assert operation.status == "error"
    assert operation.attributes["error.type"] == "invalid_audio"
    assert b"private provider diagnostic" not in encode_incident_protobuf(bundle)


def test_realtime_binds_each_response_to_its_own_speech_stop() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="overlap", started_at_unix_nano=START)
    events = [
        ({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 900}, 1_000),
        ({"type": "response.created", "response": {"id": "response-a"}}, 1_020),
        ({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 1_100}, 1_200),
        ({"type": "response.created", "response": {"id": "response-b"}}, 1_220),
        (
            {
                "type": "response.output_audio.delta",
                "response_id": "response-a",
                "delta": "private-audio-a",
            },
            1_410,
        ),
        (
            {
                "type": "response.output_audio.delta",
                "response_id": "response-b",
                "delta": "private-audio-b",
            },
            1_500,
        ),
    ]
    with session.turn() as turn:
        for payload, received_at_ms in events:
            adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    bundle = session.close()

    values = sorted(
        measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name == "earshot.turn.response_latency"
    )
    assert values == [300, 410]


def test_realtime_cancelled_interrupted_response_accepts_barge_in() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="accepted", started_at_unix_nano=START)
    with session.turn() as turn:
        adapter.adapt(
            {"type": "response.created", "response": {"id": "response-a"}},
            received_at_ms=100,
        ).apply(turn)
        adapter.adapt(
            {
                "type": "input_audio_buffer.speech_started",
                "audio_start_ms": 80,
            },
            received_at_ms=200,
        ).apply(turn)
        adapter.adapt(
            {
                "type": "response.done",
                "response": {"id": "response-a", "status": "cancelled"},
            },
            received_at_ms=250,
        ).apply(turn)
    names = [event.event_name for event in session.close().profile.events]

    assert "earshot.interruption.detected" in names
    assert "earshot.interruption.accepted" in names


def test_realtime_audio_done_is_terminal_but_never_claims_completion() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="audio-done", started_at_unix_nano=START)
    with session.turn() as turn:
        adapter.adapt(
            {"type": "response.created", "response": {"id": "response-a"}},
            received_at_ms=100,
        ).apply(turn)
        done = adapter.adapt(
            {
                "type": "response.output_audio.done",
                "event_id": "event-done",
                "response_id": "response-a",
                "item_id": "item-private",
                "output_index": 0,
                "content_index": 0,
            },
            received_at_ms=200,
        )
        assert done.terminal is False
        done.apply(turn)
    bundle = session.close()

    assert bundle.profile.operations == ()
    assert [event.event_name for event in bundle.profile.events] == [
        "openai.realtime.output_audio.done"
    ]


def test_realtime_parse_without_apply_does_not_activate_response() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    adapter.adapt(
        {"type": "response.created", "response": {"id": "response-a"}},
        received_at_ms=100,
    )
    audio = adapter.adapt(
        {
            "type": "response.output_audio.delta",
            "response_id": "response-a",
            "delta": "private",
        },
        received_at_ms=200,
    )
    session = earshot.pipeline(session_id="dropped", started_at_unix_nano=START)
    with session.turn() as turn, pytest.raises(ValueError, match="unknown response"):
        audio.apply(turn)


def test_openai_and_sarvam_reject_conflicting_native_update_ids() -> None:
    realtime = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    event = {
        "type": "input_audio_buffer.speech_started",
        "event_id": "event-1",
        "audio_start_ms": 10,
    }
    assert realtime.adapt(event, received_at_ms=100) is realtime.adapt(event, received_at_ms=100)
    with pytest.raises(ValueError, match="conflicting provider update identity"):
        realtime.adapt(dict(event, audio_start_ms=20), received_at_ms=100)

    sarvam = SarvamAdapter(identity_key=IDENTITY_KEY)
    payload = {
        "type": "data",
        "data": {
            "request_id": "request-1",
            "transcript": "private",
            "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
        },
    }
    assert sarvam.adapt(payload, received_at_ms=100) is sarvam.adapt(payload, received_at_ms=100)
    changed = {
        **payload,
        "data": {**payload["data"], "transcript": "different private"},
    }
    with pytest.raises(ValueError, match="conflicting provider update identity"):
        sarvam.adapt(changed, received_at_ms=100)


def test_realtime_one_speech_gesture_accepts_once_across_cancelled_responses() -> None:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="one-gesture", started_at_unix_nano=START)
    with session.turn() as turn:
        for response_id in ("response-a", "response-b"):
            adapter.adapt(
                {"type": "response.created", "response": {"id": response_id}},
                received_at_ms=100,
            ).apply(turn)
        adapter.adapt(
            {
                "type": "input_audio_buffer.speech_started",
                "audio_start_ms": 80,
            },
            received_at_ms=200,
        ).apply(turn)
        for response_id in ("response-a", "response-b"):
            adapter.adapt(
                {
                    "type": "response.done",
                    "response": {"id": response_id, "status": "cancelled"},
                },
                received_at_ms=250,
            ).apply(turn)
    names = [event.event_name for event in session.close().profile.events]

    assert names.count("earshot.interruption.detected") == 1
    assert names.count("earshot.interruption.accepted") == 1


def test_sarvam_repeated_vad_cycles_remain_distinct_updates() -> None:
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="sarvam-vad-cycles", started_at_unix_nano=START)
    with session.turn() as turn:
        for received_at_ms in (100, 300):
            adapter.adapt(
                {"type": "events", "data": {"signal_type": "START_SPEECH"}},
                received_at_ms=received_at_ms,
            ).apply(turn)
            adapter.adapt(
                {"type": "events", "data": {"signal_type": "END_SPEECH"}},
                received_at_ms=received_at_ms + 100,
            ).apply(turn)
    names = [event.event_name for event in session.close().profile.events]

    assert names == [
        "earshot.speech.started",
        "earshot.speech.ended",
        "earshot.speech.started",
        "earshot.speech.ended",
    ]
