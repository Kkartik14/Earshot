"""Provider event adapters preserve native semantics without retaining content."""

from __future__ import annotations

import pytest

import earshot
from earshot.adapters.providers import AdapterUpdate, CartesiaAdapter, DeepgramAdapter

pytestmark = pytest.mark.integration

START = 1_752_800_000_000_000_000


def _deepgram_result(
    *,
    transcript: str = "synthetic words",
    is_final: bool = True,
    speech_final: bool = True,
    from_finalize: bool = False,
    request_id: str = "request-42",
) -> dict[str, object]:
    return {
        "type": "Results",
        "start": 1.25,
        "duration": 0.75,
        "is_final": is_final,
        "speech_final": speech_final,
        "from_finalize": from_finalize,
        "channel_index": [0, 1],
        "channel": {
            "alternatives": [
                {
                    "transcript": transcript,
                    "confidence": 0.97,
                    "words": [{"word": "synthetic", "start": 1.25, "end": 1.5}],
                }
            ]
        },
        "metadata": {"request_id": request_id},
    }


def test_deepgram_final_result_records_stt_without_fabricating_ttfb() -> None:
    adapter = DeepgramAdapter(model="nova-3")
    payload = _deepgram_result(
        transcript="customer secret must never be retained",
        request_id="native-request-private-42",
    )

    update = adapter.adapt(payload, received_at_ms=2_100)
    assert isinstance(update, AdapterUpdate)

    session = earshot.pipeline(session_id="deepgram", started_at_unix_nano=START)
    with session.turn() as turn:
        assert update.apply(turn) is True
    bundle = session.close()

    assert earshot.validate_incident(bundle).ok
    [operation] = bundle.profile.operations
    assert operation.operation_name == "stt"
    assert operation.ended_at is None
    assert operation.attributes["gen_ai.provider.name"] == "deepgram"
    assert operation.attributes["gen_ai.request.model"] == "nova-3"

    event = next(
        item
        for item in bundle.profile.events
        if item.event_name == "earshot.transcript.final"
    )
    assert event.event_name == "earshot.transcript.final"
    assert event.participant_id == "participant-user"
    assert event.evidence.source == "app"
    assert event.evidence.confidence == "estimated"
    assert "earshot.turn.committed" in {
        item.event_name for item in bundle.profile.events
    }

    measurements = {
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert "deepgram.stt.segment_start" in measurements
    assert "deepgram.stt.segment_duration" in measurements
    assert "deepgram.stt.transcript_confidence" in measurements
    assert "earshot.stt.ttfb" not in measurements
    assert "customer secret" not in repr(bundle)
    assert "native-request-private-42" not in repr(bundle)


def test_deepgram_segment_final_is_not_promoted_to_utterance_final() -> None:
    adapter = DeepgramAdapter(model="nova-3")
    update = adapter.adapt(
        _deepgram_result(is_final=True, speech_final=False),
        received_at_ms=2_100,
    )

    session = earshot.pipeline(session_id="deepgram-segment", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    assert bundle.profile.events == ()


def test_deepgram_audio_cursor_signals_remain_native_measurements() -> None:
    adapter = DeepgramAdapter(model="nova-3")
    speech_started = adapter.adapt(
        {"type": "SpeechStarted", "timestamp": 0.64, "channel": [0, 1]},
        received_at_ms=700,
    )
    utterance_end = adapter.adapt(
        {"type": "UtteranceEnd", "last_word_end": 1.1, "channel": [0, 1]},
        received_at_ms=1_200,
    )

    session = earshot.pipeline(session_id="deepgram-signals", started_at_unix_nano=START)
    with session.turn() as turn:
        speech_started.apply(turn)
        utterance_end.apply(turn)
    bundle = session.close()

    assert [event.event_name for event in bundle.profile.events] == [
        "deepgram.speech_started",
        "deepgram.utterance_end",
    ]
    measurements = {
        measurement.name: measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements == {
        "deepgram.stt.speech_started_offset": 0.64,
        "deepgram.stt.last_word_end_offset": 1.1,
    }
    assert all(sample.evidence.source == "provider" for sample in bundle.profile.quality_samples)
    assert "earshot.stt.ttfb" not in measurements
    assert "earshot.speech.ended" not in {event.event_name for event in bundle.profile.events}


def test_deepgram_exact_replay_applies_once_and_keeps_opaque_correlation() -> None:
    adapter = DeepgramAdapter(model="nova-3", identity_key=b"d" * 32)
    payload = _deepgram_result(
        transcript="first private result",
        request_id="private-request-correlation",
    )
    first = adapter.adapt(payload, received_at_ms=2_100)
    replay = adapter.adapt(payload, received_at_ms=2_100)
    changed = adapter.adapt(
        _deepgram_result(
            transcript="second private result",
            request_id="private-request-correlation",
        ),
        received_at_ms=2_200,
    )

    assert replay is first
    assert changed is not first
    assert changed.correlation_id == first.correlation_id
    assert "private-request-correlation" not in repr(first)

    session = earshot.pipeline(session_id="deepgram-replay", started_at_unix_nano=START)
    with session.turn() as turn:
        assert first.apply(turn) is True
        assert replay.apply(turn) is False
        assert changed.apply(turn) is True
    bundle = session.close()

    assert len(bundle.profile.operations) == 2
    assert [event.event_name for event in bundle.profile.events].count(
        "earshot.transcript.final"
    ) == 2
    assert [event.event_name for event in bundle.profile.events].count(
        "earshot.turn.committed"
    ) == 2


def test_deepgram_forced_finalize_commits_without_fabricating_natural_speech_end() -> None:
    adapter = DeepgramAdapter(model="nova-3")
    update = adapter.adapt(
        _deepgram_result(
            is_final=True,
            speech_final=False,
            from_finalize=True,
        ),
        received_at_ms=2_100,
    )

    assert update.turn_commit is True
    session = earshot.pipeline(session_id="deepgram-finalize", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    event_names = [event.event_name for event in bundle.profile.events]
    assert event_names == [
        "earshot.transcript.final",
        "deepgram.finalize_completed",
    ]
    assert "earshot.speech.ended" not in event_names


def test_cartesia_first_chunk_keeps_step_time_distinct_from_app_ttfb() -> None:
    adapter = CartesiaAdapter(model="sonic-3", voice="voice-safe")
    payload = {
        "type": "chunk",
        "context_id": "native-context-private-7",
        "data": "private-base64-audio-must-not-be-retained",
        "step_time": 12.5,
        "done": False,
    }

    update = adapter.adapt(
        payload,
        received_at_ms=140,
        request_sent_at_ms=50,
    )
    session = earshot.pipeline(session_id="cartesia", started_at_unix_nano=START)
    with session.turn() as turn:
        assert update.apply(turn) is True
    bundle = session.close()

    assert earshot.validate_incident(bundle).ok
    [operation] = bundle.profile.operations
    assert operation.operation_name == "tts"
    assert operation.attributes["gen_ai.provider.name"] == "cartesia"
    assert operation.attributes["gen_ai.request.model"] == "sonic-3"
    assert operation.attributes["earshot.tts.voice"] == "voice-safe"

    measurements = {
        measurement.name: (measurement, sample)
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    step_time, step_sample = measurements["cartesia.tts.step_time"]
    assert step_time.value == 12.5
    assert step_time.unit == "ms"
    assert step_sample.evidence.source == "provider"
    assert step_sample.evidence.confidence == "measured"
    assert step_sample.evidence.source_field == "step_time"
    assert step_sample.attributes["earshot.metric.basis"] == "per_chunk_server_processing"

    app_ttfb, ttfb_sample = measurements["earshot.tts.ttfb"]
    assert app_ttfb.value == 90
    assert app_ttfb.unit == "ms"
    assert ttfb_sample.evidence.source == "app"
    assert ttfb_sample.evidence.confidence == "estimated"
    assert ttfb_sample.attributes["earshot.metric.basis"] == (
        "request_send_to_first_audio_chunk_receipt"
    )

    [event] = bundle.profile.events
    assert event.event_name == "earshot.audio.first_packet_received"
    assert "private-base64-audio" not in repr(bundle)
    assert "native-context-private-7" not in repr(bundle)


def test_cartesia_context_emits_app_ttfb_only_for_its_first_audio_chunk() -> None:
    adapter = CartesiaAdapter(model="sonic-3", identity_key=b"c" * 32)
    first = adapter.adapt(
        {
            "type": "chunk",
            "context_id": "context-one",
            "data": "private-audio-one",
            "step_time": 11,
        },
        received_at_ms=140,
        request_sent_at_ms=50,
    )
    later = adapter.adapt(
        {
            "type": "chunk",
            "context_id": "context-one",
            "data": "private-audio-two",
            "step_time": 8,
        },
        received_at_ms=180,
    )
    assert first.correlation_id == later.correlation_id

    session = earshot.pipeline(session_id="cartesia-context", started_at_unix_nano=START)
    with session.turn() as turn:
        first.apply(turn)
        later.apply(turn)
    bundle = session.close()

    names = [
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    ]
    assert names.count("cartesia.tts.step_time") == 2
    assert names.count("earshot.tts.ttfb") == 1
    assert len(bundle.profile.operations) == 1
    assert len(bundle.profile.events) == 1


def test_malformed_cartesia_chunk_does_not_reserve_its_context() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    malformed = {
        "type": "chunk",
        "context_id": "context-after-error",
        "data": "private-audio",
        "step_time": -1,
    }
    with pytest.raises(ValueError, match="non-negative"):
        adapter.adapt(malformed, received_at_ms=100, request_sent_at_ms=20)

    valid = dict(malformed, step_time=9)
    with pytest.raises(ValueError, match="request_sent_at_ms is required"):
        adapter.adapt(valid, received_at_ms=100)

    update = adapter.adapt(valid, received_at_ms=100, request_sent_at_ms=20)
    session = earshot.pipeline(session_id="cartesia-atomic", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    names = {
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert names == {"cartesia.tts.step_time", "earshot.tts.ttfb"}


def test_identical_cartesia_audio_chunks_at_distinct_receipt_times_are_not_replays() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    payload = {
        "type": "chunk",
        "context_id": "repeating-context",
        "data": "same-audio-bytes",
        "step_time": 7,
    }
    first = adapter.adapt(payload, received_at_ms=100, request_sent_at_ms=20)
    second = adapter.adapt(payload, received_at_ms=140)

    assert second is not first
    session = earshot.pipeline(session_id="cartesia-repeat", started_at_unix_nano=START)
    with session.turn() as turn:
        first.apply(turn)
        second.apply(turn)
    bundle = session.close()

    names = [
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    ]
    assert names.count("cartesia.tts.step_time") == 2
    assert names.count("earshot.tts.ttfb") == 1


def test_cartesia_done_is_an_explicit_terminal_context_signal() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    update = adapter.adapt(
        {
            "type": "done",
            "done": True,
            "status_code": 206,
            "context_id": "private-done-context",
        },
        received_at_ms=300,
    )

    assert update.terminal is True
    session = earshot.pipeline(session_id="cartesia-done", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    assert bundle.profile.operations == ()
    assert [event.event_name for event in bundle.profile.events] == ["cartesia.tts.done"]
    assert "private-done-context" not in repr(bundle)


def test_cartesia_error_retains_codes_but_not_provider_messages_or_ids() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    update = adapter.adapt(
        {
            "type": "error",
            "done": True,
            "status_code": 400,
            "error_code": "model_not_found",
            "title": "Private title",
            "message": "Private diagnostic with customer content",
            "request_id": "private-native-request",
            "context_id": "private-error-context",
        },
        received_at_ms=80,
    )

    assert update.terminal is True
    session = earshot.pipeline(session_id="cartesia-error", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    [operation] = bundle.profile.operations
    assert operation.operation_name == "tts"
    assert operation.status == "error"
    assert operation.attributes["error.type"] == "model_not_found"
    [event] = bundle.profile.events
    assert event.event_name == "cartesia.tts.error"
    [sample] = bundle.profile.quality_samples
    [measurement] = sample.measurements
    assert (measurement.name, measurement.value, measurement.unit) == (
        "cartesia.tts.status_code",
        400,
        "1",
    )
    rendered = repr(bundle)
    assert "Private diagnostic" not in rendered
    assert "private-native-request" not in rendered
    assert "private-error-context" not in rendered
