"""Fused Realtime and Sarvam provider events retain only honest evidence."""

from __future__ import annotations

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
    adapter = OpenAIRealtimeAdapter(
        model="gpt-realtime", identity_key=IDENTITY_KEY
    )
    updates = [
        adapter.adapt(
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": "event-stop",
                "item_id": "item-private",
                "audio_end_ms": 850,
            },
            received_at_ms=1_000,
        ),
        adapter.adapt(
            {
                "type": "response.created",
                "event_id": "event-created",
                "response": {"id": "response-private"},
            },
            received_at_ms=1_020,
        ),
        adapter.adapt(
            {
                "type": "response.output_audio.delta",
                "event_id": "event-audio",
                "response_id": "response-private",
                "delta": "base64-private-audio-must-not-survive",
            },
            received_at_ms=1_410,
        ),
        adapter.adapt(
            {
                "type": "response.done",
                "event_id": "event-done",
                "response": {"id": "response-private", "status": "completed"},
            },
            received_at_ms=1_500,
        ),
    ]

    session = earshot.pipeline(session_id="realtime", started_at_unix_nano=START)
    with session.turn() as turn:
        assert all(update.apply(turn) for update in updates)
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


def test_realtime_speech_start_detects_but_does_not_accept_interruption() -> None:
    adapter = OpenAIRealtimeAdapter(
        model="gpt-realtime", identity_key=IDENTITY_KEY
    )
    created = adapter.adapt(
        {"type": "response.created", "response": {"id": "active-response"}},
        received_at_ms=100,
    )
    speech = adapter.adapt(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "next-item",
            "audio_start_ms": 75,
        },
        received_at_ms=200,
    )
    session = earshot.pipeline(session_id="interrupt", started_at_unix_nano=START)
    with session.turn() as turn:
        created.apply(turn)
        speech.apply(turn)
    bundle = session.close()

    names = {event.event_name for event in bundle.profile.events}
    assert "earshot.interruption.detected" in names
    assert "earshot.interruption.accepted" not in names


def test_realtime_cancelled_response_is_not_reported_completed() -> None:
    adapter = OpenAIRealtimeAdapter(
        model="gpt-realtime", identity_key=IDENTITY_KEY
    )
    created = adapter.adapt(
        {"type": "response.created", "response": {"id": "cancelled-response"}},
        received_at_ms=100,
    )
    done = adapter.adapt(
        {
            "type": "response.done",
            "response": {"id": "cancelled-response", "status": "cancelled"},
        },
        received_at_ms=150,
    )
    session = earshot.pipeline(session_id="cancelled", started_at_unix_nano=START)
    with session.turn() as turn:
        created.apply(turn)
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


def test_sarvam_auto_detected_language_probability_is_governed() -> None:
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    update = adapter.adapt(
        {
            "type": "data",
            "data": {
                "request_id": "request-auto",
                "transcript": "private",
                "language_code": "unknown",
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
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
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
