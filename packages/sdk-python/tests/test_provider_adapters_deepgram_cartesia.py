"""Provider event adapters preserve native semantics without retaining content."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

import earshot
from earshot.adapters.providers import AdapterUpdate, CartesiaAdapter, DeepgramAdapter
from earshot.storage import IncidentStore

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


def _flux_turn(event: str, sequence_id: int, *, turn_index: int = 0) -> dict[str, object]:
    return {
        "type": "TurnInfo",
        "request_id": "flux-request-private",
        "sequence_id": sequence_id,
        "event": event,
        "turn_index": turn_index,
        "audio_window_start": 0.0,
        "audio_window_end": 1.25,
        "transcript": "private flux transcript",
        "words": [{"word": "private", "confidence": 0.9}],
        "end_of_turn_confidence": 0.88,
        "languages": ["en"],
    }


def test_deepgram_final_result_records_stt_without_fabricating_ttfb() -> None:
    adapter = DeepgramAdapter(model="nova-3")
    payload = _deepgram_result(
        transcript="customer secret must never be retained",
        request_id="native-request-private-42",
    )

    update = adapter.adapt(payload, received_at_ms=2_100)
    assert isinstance(update, AdapterUpdate)
    captured = tuple(
        cell.cell_contents for cell in (update._apply_update.__closure__ or ())
    )
    assert "customer secret" not in repr(captured)

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
        item for item in bundle.profile.events if item.event_name == "earshot.transcript.final"
    )
    assert event.event_name == "earshot.transcript.final"
    assert event.participant_id == "participant-user"
    assert event.evidence.source == "app"
    assert event.evidence.confidence == "estimated"
    assert "earshot.turn.committed" in {item.event_name for item in bundle.profile.events}

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
    omission_hashes = {
        omission.attributes["field_key_sha256"] for omission in bundle.profile.privacy.omissions
    }
    assert (
        hashlib.sha256(b"deepgram.Results.channel.alternatives[0].transcript").hexdigest()
        in omission_hashes
    )
    assert all(
        omission.reason == "adapter_payload_omitted"
        for omission in bundle.profile.privacy.omissions
    )


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


def test_deepgram_flux_eager_end_is_cancelled_and_only_end_commits(tmp_path) -> None:
    adapter = DeepgramAdapter(model="flux-general-en", identity_key=b"f" * 32)
    session = earshot.pipeline(session_id="flux", started_at_unix_nano=START)
    with session.turn() as turn:
        for index, event in enumerate(
            ["Update", "StartOfTurn", "EagerEndOfTurn", "TurnResumed", "EndOfTurn"]
        ):
            update = adapter.adapt(_flux_turn(event, index), received_at_ms=100 + index * 50)
            assert update.turn_commit is (event == "EndOfTurn")
            update.apply(turn)
    bundle = session.close()

    names = [event.event_name for event in bundle.profile.events]
    assert names.count("earshot.turn.proposed") == 1
    assert names.count("earshot.turn.cancelled") == 1
    assert names.count("earshot.turn.committed") == 1
    assert names.count("earshot.transcript.final") == 1
    assert [operation.operation_name for operation in bundle.profile.operations] == ["stt"]
    assert bundle.profile.operations[0].attributes["earshot.language.code"] == "en"
    assert "private flux transcript" not in repr(bundle)
    assert "flux-request-private" not in repr(bundle)
    store = IncidentStore(tmp_path)
    store.ingest(bundle)
    assert store.list_turn_facts()[0].language == "en"


def test_deepgram_flux_rejects_resumption_without_speculative_end() -> None:
    adapter = DeepgramAdapter(model="flux-general-en")
    session = earshot.pipeline(session_id="flux-order", started_at_unix_nano=START)
    with session.turn() as turn:
        adapter.adapt(_flux_turn("StartOfTurn", 1), received_at_ms=100).apply(turn)
        resumed = adapter.adapt(_flux_turn("TurnResumed", 2), received_at_ms=150)
        with pytest.raises(ValueError, match="invalid Deepgram Flux transition"):
            resumed.apply(turn)


def test_deepgram_flux_allows_a_second_eager_cycle_after_resume() -> None:
    adapter = DeepgramAdapter(model="flux-general-en")
    session = earshot.pipeline(session_id="flux-eager-cycle", started_at_unix_nano=START)
    lifecycle = [
        "StartOfTurn",
        "EagerEndOfTurn",
        "TurnResumed",
        "Update",
        "EagerEndOfTurn",
        "EndOfTurn",
    ]
    with session.turn() as turn:
        for sequence_id, event in enumerate(lifecycle):
            adapter.adapt(
                _flux_turn(event, sequence_id),
                received_at_ms=100 + sequence_id * 50,
            ).apply(turn)
    names = [event.event_name for event in session.close().profile.events]

    assert names.count("earshot.turn.proposed") == 2
    assert names.count("earshot.turn.committed") == 1


def test_deepgram_flux_start_can_conditionally_detect_interruption() -> None:
    adapter = DeepgramAdapter(model="flux-general-en")
    session = earshot.pipeline(session_id="flux-interruption", started_at_unix_nano=START)
    with session.turn() as turn:
        adapter.adapt(
            _flux_turn("StartOfTurn", 1),
            received_at_ms=100,
            agent_output_active=True,
        ).apply(turn)
    names = [event.event_name for event in session.close().profile.events]

    assert names == ["earshot.speech.started", "earshot.interruption.detected"]
    assert "earshot.interruption.accepted" not in names


def test_deepgram_flux_conflicting_sequence_identity_is_rejected() -> None:
    adapter = DeepgramAdapter(model="flux-general-en", identity_key=b"f" * 32)
    payload = _flux_turn("StartOfTurn", 7)
    assert adapter.adapt(payload, received_at_ms=100) is adapter.adapt(
        payload, received_at_ms=200
    )
    with pytest.raises(ValueError, match="conflicting provider update identity"):
        adapter.adapt(dict(payload, transcript="different private"), received_at_ms=200)


def test_deepgram_flux_multilingual_turn_keeps_count_without_guessing_language() -> None:
    adapter = DeepgramAdapter(model="flux-general-en")
    start = _flux_turn("StartOfTurn", 1)
    start["languages"] = ["en", "hi"]
    end = _flux_turn("EndOfTurn", 2)
    end["languages"] = ["en", "hi"]
    session = earshot.pipeline(session_id="flux-multilingual", started_at_unix_nano=START)
    with session.turn() as turn:
        adapter.adapt(start, received_at_ms=100).apply(turn)
        adapter.adapt(end, received_at_ms=200).apply(turn)
    bundle = session.close()

    [operation] = bundle.profile.operations
    assert "earshot.language.code" not in operation.attributes
    assert any(
        measurement.name == "deepgram.flux.language_count" and measurement.value == 2
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    )


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
    [omission] = bundle.profile.privacy.omissions
    assert omission.capture_class == "audio"
    assert (
        omission.attributes["field_key_sha256"]
        == hashlib.sha256(b"cartesia.chunk.data").hexdigest()
    )


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
    missing_send = adapter.adapt(valid, received_at_ms=100)
    failed_session = earshot.pipeline(
        session_id="cartesia-missing-send", started_at_unix_nano=START
    )
    with (
        failed_session.turn() as turn,
        pytest.raises(ValueError, match="request_sent_at_ms is required"),
    ):
        missing_send.apply(turn)

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


def test_cartesia_unapplied_chunk_does_not_reserve_context() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    payload = {
        "type": "chunk",
        "context_id": "context-one",
        "data": "private-audio",
        "step_time": 9,
    }
    first = adapter.adapt(payload, received_at_ms=100, request_sent_at_ms=20)
    second = adapter.adapt(dict(payload, data="other-private-audio"), received_at_ms=140)
    session = earshot.pipeline(session_id="cartesia-order", started_at_unix_nano=START)
    with session.turn() as turn:
        with pytest.raises(ValueError, match="request_sent_at_ms is required"):
            second.apply(turn)
        first.apply(turn)
        assert second.apply(turn)
    bundle = session.close()

    names = [
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    ]
    assert names.count("earshot.tts.ttfb") == 1


def test_cartesia_concurrent_first_chunks_author_one_first_audio() -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    updates = [
        adapter.adapt(
            {
                "type": "chunk",
                "context_id": "context-race",
                "data": f"private-audio-{index}",
                "step_time": 9,
            },
            received_at_ms=100 + index,
            request_sent_at_ms=20,
        )
        for index in range(2)
    ]
    session = earshot.pipeline(session_id="cartesia-race", started_at_unix_nano=START)
    with session.turn() as turn, ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(lambda update: update.apply(turn), updates))
    bundle = session.close()

    assert len(bundle.profile.operations) == 1
    assert len(bundle.profile.events) == 1
    names = [
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    ]
    assert names.count("earshot.tts.ttfb") == 1


@pytest.mark.parametrize(
    ("event_type", "field_name", "label_name", "labels"),
    [
        ("timestamps", "word_timestamps", "words", ["private", "words"]),
        (
            "phoneme_timestamps",
            "phoneme_timestamps",
            "phonemes",
            ["private-phoneme"],
        ),
    ],
)
def test_cartesia_timestamp_frames_keep_only_counts_and_timing(
    event_type: str,
    field_name: str,
    label_name: str,
    labels: list[str],
) -> None:
    adapter = CartesiaAdapter(model="sonic-3")
    starts = [index * 0.1 for index in range(len(labels))]
    ends = [start + 0.08 for start in starts]
    update = adapter.adapt(
        {
            "type": event_type,
            "done": False,
            "status_code": 206,
            "context_id": "context-private",
            field_name: {label_name: labels, "start": starts, "end": ends},
        },
        received_at_ms=200,
    )
    captured = tuple(
        cell.cell_contents for cell in (update._apply_update.__closure__ or ())
    )
    assert "private" not in repr(captured)
    session = earshot.pipeline(session_id=f"cartesia-{event_type}", started_at_unix_nano=START)
    with session.turn() as turn:
        update.apply(turn)
    bundle = session.close()

    measurements = {
        measurement.name: measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements[f"cartesia.tts.{label_name}_timestamp_count"] == len(labels)
    assert measurements["cartesia.tts.status_code"] == 206
    assert "private" not in repr(bundle)
    assert bundle.profile.operations == ()
    assert bundle.profile.events == ()


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


def test_cartesia_terminal_retry_at_a_later_receipt_is_an_exact_replay() -> None:
    adapter = CartesiaAdapter(model="sonic-3", identity_key=b"c" * 32)
    payload = {
        "type": "done",
        "done": True,
        "status_code": 206,
        "context_id": "private-done-context",
    }
    first = adapter.adapt(payload, received_at_ms=300)
    retry = adapter.adapt(payload, received_at_ms=900)
    assert retry is first
    session = earshot.pipeline(session_id="cartesia-done-retry", started_at_unix_nano=START)
    with session.turn() as turn:
        assert first.apply(turn)
        assert not retry.apply(turn)
    assert len(session.close().profile.events) == 1


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
    captured = tuple(
        cell.cell_contents for cell in (update._apply_update.__closure__ or ())
    )
    assert "Private diagnostic" not in repr(captured)

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
