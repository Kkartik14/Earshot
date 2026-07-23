"""Reusable conformance gates for every shipped in-process adapter family."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import earshot
from adapter_conformance import assert_capture_conforms, assert_native_trace_topology
from earshot.adapters import LiveKitAdapter, PipecatAdapter
from earshot.adapters.providers import (
    CartesiaAdapter,
    DeepgramAdapter,
    OpenAIRealtimeAdapter,
    SarvamAdapter,
)
from earshot.clock import ManualClock
from earshot.contract import TimePoint
from earshot.recorder import IncidentRecorder, RecorderConfig
from test_provider_adapters_deepgram_cartesia import _deepgram_result

pytestmark = pytest.mark.integration

START = 1_752_800_000_000_000_000
IDENTITY_KEY = b"adapter-conformance-identity-key"
PRIVATE_TRANSCRIPT = "private conformance transcript"
PRIVATE_AUDIO = "private conformance audio"
ROOT = Path(__file__).resolve().parents[3]


def _session(name: str):
    return earshot.pipeline(
        session_id=f"conformance-{name}",
        bundle_id=f"conformance-{name}-bundle",
        clock=ManualClock(wall=START, monotonic=0),
        config=RecorderConfig(clock_domain_id="conformance-clock"),
    )


def _capture_deepgram(status: str = "completed"):
    adapter = DeepgramAdapter(model="nova-3", identity_key=IDENTITY_KEY)
    session = _session("deepgram")
    with session.turn(turn_id="turn-0") as turn:
        adapter.adapt(
            _deepgram_result(
                transcript=PRIVATE_TRANSCRIPT,
                request_id="private-conformance-request",
            ),
            received_at_ms=2_100,
        ).apply(turn)
    return session.close(status)


def _capture_cartesia(status: str = "completed"):
    adapter = CartesiaAdapter(model="sonic-3", identity_key=IDENTITY_KEY)
    session = _session("cartesia")
    with session.turn(turn_id="turn-0") as turn:
        adapter.adapt(
            {
                "type": "chunk",
                "context_id": "private-cartesia-context",
                "data": PRIVATE_AUDIO,
                "step_time": 12.5,
            },
            request_sent_at_ms=50,
            received_at_ms=140,
        ).apply(turn)
    return session.close(status)


def _capture_openai_realtime(status: str = "completed"):
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    events = (
        (
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": "private-stop-event",
                "item_id": "private-input-item",
                "audio_end_ms": 850,
            },
            1_000,
        ),
        (
            {
                "type": "response.created",
                "event_id": "private-created-event",
                "response": {"id": "private-response"},
            },
            1_020,
        ),
        (
            {
                "type": "response.output_audio.delta",
                "event_id": "private-audio-event",
                "response_id": "private-response",
                "delta": PRIVATE_AUDIO,
            },
            1_410,
        ),
        (
            {
                "type": "response.done",
                "event_id": "private-done-event",
                "response": {"id": "private-response", "status": "completed"},
            },
            1_500,
        ),
    )
    session = _session("openai-realtime")
    with session.turn(turn_id="turn-0") as turn:
        for event, received_at_ms in events:
            adapter.adapt(event, received_at_ms=received_at_ms).apply(turn)
    return session.close(status)


def _capture_sarvam(status: str = "completed"):
    adapter = SarvamAdapter(identity_key=IDENTITY_KEY)
    session = _session("sarvam")
    with session.turn(turn_id="turn-0") as turn:
        adapter.adapt(
            {
                "type": "data",
                "data": {
                    "request_id": "private-sarvam-request",
                    "transcript": PRIVATE_TRANSCRIPT,
                    "language_code": "hi-IN",
                    "language_probability": None,
                    "metrics": {"audio_duration": 1.25, "processing_latency": 0.084},
                },
            },
            received_at_ms=1_500,
        ).apply(turn)
    return session.close(status)


def _framework_recorder(name: str) -> IncidentRecorder:
    return IncidentRecorder(
        session_id=f"conformance-{name}",
        bundle_id=f"conformance-{name}-bundle",
        config=RecorderConfig(clock_domain_id="server-clock"),
        clock=ManualClock(wall=1_800_000_000_000_000_000, monotonic=0),
    )


def _capture_pipecat(status: str = "completed"):
    fixture = json.loads((ROOT / "fixtures" / "golden" / "pipecat_spans.json").read_text())
    recorder = _framework_recorder("pipecat")
    adapter = PipecatAdapter(recorder, framework_version="conformance")
    for span in fixture["spans"]:
        if span["name"] == "llm generation":
            span.setdefault("attributes", {})["input"] = PRIVATE_TRANSCRIPT
        adapter.consume_span(span)
    for observed in fixture["interruption_frames"]:
        adapter.consume_interruption_frame(
            observed["frame"],
            observed_at=TimePoint.model_validate(observed["observed_at"]),
            bot_was_speaking=observed["bot_was_speaking"],
            interrupted_turn_id=observed["interrupted_turn_id"],
        )
    return recorder.close(status)


def _capture_livekit(status: str = "completed"):
    fixture = json.loads((ROOT / "fixtures" / "golden" / "livekit_metrics.json").read_text())
    recorder = _framework_recorder("livekit")
    adapter = LiveKitAdapter(recorder, framework_version="conformance")
    trace_id = "4" * 32
    root_span_id = "a" * 16
    spans = (
        {
            "name": "agent_session",
            "operation_id": "livekit-root",
            "trace_id": trace_id,
            "span_id": root_span_id,
            "parent_scope": "external",
            "status": "ok",
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_002_000_000_000,
        },
        {
            "name": "agent_turn",
            "operation_id": "livekit-agent-turn",
            "trace_id": trace_id,
            "span_id": "b" * 16,
            "parent_span_id": root_span_id,
            "parent_scope": "internal",
            "status": "ok",
            "start_time": 1_800_000_001_000_000_000,
            "end_time": 1_800_000_001_900_000_000,
            "attributes": {
                "lk.speech_id": "speech_7",
                "lk.chat_ctx": PRIVATE_TRANSCRIPT,
                "lk.response.text": PRIVATE_TRANSCRIPT,
            },
        },
    )
    for span in spans:
        adapter.consume_span(span)
    for item in fixture:
        if "metric" in item:
            adapter.consume_metric(
                item["metric"],
                observed_at=TimePoint.model_validate(item["observed_at"]),
            )
        elif "conversation_item" in item:
            adapter.consume_conversation_item(item["conversation_item"])
        else:
            adapter.consume_interruption_event(item["event"])
    return recorder.close(status)


def test_deepgram_sanitized_synthetic_capture_meets_shared_conformance() -> None:
    bundle = assert_capture_conforms(
        _capture_deepgram,
        forbidden_values=(PRIVATE_TRANSCRIPT, "private-conformance-request"),
    )

    assert [operation.operation_name for operation in bundle.profile.operations] == ["stt"]


@pytest.mark.parametrize(
    ("capture", "forbidden_values", "operation_name"),
    (
        (_capture_cartesia, (PRIVATE_AUDIO, "private-cartesia-context"), "tts"),
        (
            _capture_openai_realtime,
            (PRIVATE_AUDIO, "private-response", "private-input-item"),
            "agent",
        ),
        (_capture_sarvam, (PRIVATE_TRANSCRIPT, "private-sarvam-request"), "stt"),
    ),
    ids=("cartesia", "openai-realtime", "sarvam"),
)
def test_streaming_raw_provider_sanitized_synthetic_capture_meets_shared_conformance(
    capture,
    forbidden_values,
    operation_name,
) -> None:
    bundle = assert_capture_conforms(capture, forbidden_values=forbidden_values)

    assert {operation.operation_name for operation in bundle.profile.operations} == {operation_name}


@pytest.mark.parametrize(
    "capture",
    (_capture_deepgram, _capture_cartesia, _capture_openai_realtime, _capture_sarvam),
    ids=("deepgram", "cartesia", "openai-realtime", "sarvam"),
)
def test_streaming_raw_provider_incomplete_close_remains_canonical(capture) -> None:
    bundle = assert_capture_conforms(
        lambda: capture("failed"),
        expected_completeness="incomplete",
    )

    assert bundle.profile.session.status == "failed"


def test_manual_capture_context_preserves_exception_and_cancellation_identity() -> None:
    application_error = RuntimeError("private application detail")
    recorder = IncidentRecorder(
        session_id="conformance-exception",
        bundle_id="conformance-exception-bundle",
    )
    with pytest.raises(RuntimeError) as raised, recorder, recorder.operation("tool"):
        raise application_error
    assert raised.value is application_error
    assert_capture_conforms(
        lambda: recorder.close(),
        forbidden_values=("private application detail",),
        expected_completeness="incomplete",
        require_omission=False,
    )


def test_pipecat_public_consume_surface_meets_shared_conformance() -> None:
    bundle = assert_capture_conforms(
        _capture_pipecat,
        forbidden_values=(PRIVATE_TRANSCRIPT,),
    )

    assert_native_trace_topology(
        bundle,
        trace_id="1" * 32,
        root_span_id="a" * 16,
        child_span_ids=("1" * 16, "2" * 16, "3" * 16),
    )


def test_livekit_public_consume_surfaces_meet_shared_conformance() -> None:
    bundle = assert_capture_conforms(
        _capture_livekit,
        forbidden_values=(PRIVATE_TRANSCRIPT,),
    )

    assert_native_trace_topology(
        bundle,
        trace_id="4" * 32,
        root_span_id="a" * 16,
        child_span_ids=("b" * 16,),
    )


@pytest.mark.parametrize(
    "capture",
    (_capture_pipecat, _capture_livekit),
    ids=("pipecat", "livekit"),
)
def test_framework_incomplete_close_remains_canonical(capture) -> None:
    bundle = assert_capture_conforms(
        lambda: capture("failed"),
        expected_completeness="incomplete",
    )

    assert bundle.profile.session.status == "failed"

    cancellation = asyncio.CancelledError("private cancellation detail")
    cancelled = IncidentRecorder(
        session_id="conformance-cancelled",
        bundle_id="conformance-cancelled-bundle",
    )
    with (
        pytest.raises(asyncio.CancelledError) as raised_cancel,
        cancelled,
        cancelled.operation("agent"),
    ):
        raise cancellation
    assert raised_cancel.value is cancellation
    assert_capture_conforms(
        lambda: cancelled.close(),
        forbidden_values=("private cancellation detail",),
        expected_completeness="incomplete",
        require_omission=False,
    )
