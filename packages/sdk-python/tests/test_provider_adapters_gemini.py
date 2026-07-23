"""Gemini Live native speech-to-speech events retain only honest, fused evidence.

Gemini streams no discrete STT/LLM/TTS boundary, so one model turn must project
into exactly one fused ``agent`` operation, mirroring OpenAI Realtime. These tests
drive synthetic ``BidiGenerateContent`` frames (the exact shapes the adapter reads)
and assert the governed facts, interruption gating, privacy omissions, shared
conformance, and provider-namespace parity with the Realtime adapter.
"""

from __future__ import annotations

import pytest

import earshot
from adapter_conformance import assert_capture_conforms
from earshot.adapters.providers import GeminiLiveAdapter, OpenAIRealtimeAdapter
from earshot.analysis import analyze_incident
from earshot.clock import ManualClock
from earshot.codec import analysis_input_sha256, encode_incident_json, encode_incident_protobuf
from earshot.recorder import RecorderConfig
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration

START = 1_752_800_000_000_000_000
IDENTITY_KEY = b"gemini-adapter-test-identity-key"
MODEL = "gemini-2.5-flash-native-audio"

PRIVATE_AUDIO = "base64-gemini-audio-must-not-survive"
PRIVATE_USER_TRANSCRIPT = "user secret must never survive"
PRIVATE_AGENT_TRANSCRIPT = "agent secret must never survive"
PRIVATE_TOOL_ARG = "private-tool-argument"
PRIVATE_TOOL_ID = "call-private-1"
PRIVATE_RESUME = "private-session-resume-handle"


def _usage_metadata() -> dict[str, object]:
    return {
        "promptTokenCount": 42,
        "responseTokenCount": 17,
        "totalTokenCount": 59,
        "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 42}],
        "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 17}],
    }


def _normal_events() -> list[tuple[dict[str, object], int]]:
    """A completed native turn: client stop -> server audio -> tool -> turn end."""

    return [
        ({"setupComplete": {}}, 900),
        ({"realtimeInput": {"activityEnd": {}}}, 1_000),
        (
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": PRIVATE_AUDIO}}]
                    },
                    "inputTranscription": {"text": PRIVATE_USER_TRANSCRIPT},
                    "outputTranscription": {"text": PRIVATE_AGENT_TRANSCRIPT},
                }
            },
            1_410,
        ),
        (
            {
                "toolCall": {
                    "functionCalls": [
                        {
                            "id": PRIVATE_TOOL_ID,
                            "name": "lookup",
                            "args": {"query": PRIVATE_TOOL_ARG},
                        }
                    ]
                }
            },
            1_450,
        ),
        (
            {
                "serverContent": {"turnComplete": True, "generationComplete": True},
                "usageMetadata": _usage_metadata(),
            },
            1_500,
        ),
    ]


def _barge_in_events(
    final_server_content: dict[str, object],
) -> list[tuple[dict[str, object], int]]:
    """A response the user barges into; the final frame decides its disposition."""

    return [
        ({"realtimeInput": {"activityEnd": {}}}, 1_000),
        (
            {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": PRIVATE_AUDIO}}]}}},
            1_410,
        ),
        ({"realtimeInput": {"activityStart": {}}}, 1_460),
        ({"serverContent": final_server_content}, 1_500),
    ]


def _drive(
    events: list[tuple[dict[str, object], int]],
    *,
    session_id: str,
) -> earshot.IncidentBundle:
    adapter = GeminiLiveAdapter(model=MODEL, identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id=session_id, started_at_unix_nano=START)
    with session.turn() as turn:
        for payload, received_at_ms in events:
            adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    return session.close()


def _agent_operation(bundle: earshot.IncidentBundle):
    return next(op for op in bundle.profile.operations if op.operation_name == "agent")


def _response_latency_source(bundle: earshot.IncidentBundle) -> str:
    return next(
        sample.evidence.source_field
        for sample in bundle.profile.quality_samples
        if sample.measurements[0].name == "earshot.turn.response_latency"
    )


def test_gemini_native_s2s_stays_fused_and_authors_receipt_latency() -> None:
    bundle = _drive(_normal_events(), session_id="gemini-normal")

    assert validate_incident(bundle).ok
    agents = [op for op in bundle.profile.operations if op.operation_name == "agent"]
    assert len(agents) == 1
    [agent] = agents
    assert agent.status == "ok"
    assert agent.attributes["gen_ai.provider.name"] == "gemini"
    assert agent.attributes["gen_ai.request.model"] == MODEL
    # A native runtime exposes no separately observable STT/LLM/TTS boundary.
    assert {op.operation_name for op in bundle.profile.operations}.isdisjoint({"stt", "llm", "tts"})
    tools = [op for op in bundle.profile.operations if op.operation_name == "tool"]
    assert len(tools) == 1
    assert tools[0].status == "ok"

    assert "earshot.audio.first_packet_received" in [e.event_name for e in bundle.profile.events]

    measurements = {
        measurement.name: measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements["earshot.turn.response_latency"].value == 410
    assert _response_latency_source(bundle) == "serverContent.modelTurn.inlineData"
    # Per-modality token usage projects into the shared gen_ai usage namespace.
    assert {name for name in measurements if name.startswith("gen_ai.usage.")} == {
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.input_audio_tokens",
        "gen_ai.usage.output_audio_tokens",
    }

    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    turn_metrics = analysis.projections.turns[0].metrics
    assert turn_metrics.response_latency.value == 410
    assert turn_metrics.response_latency.confidence == "estimated"
    # A self-instrumented server pipeline never observes the client's playout.
    assert turn_metrics.render_start_response_latency.availability == "not_observed"
    coverage = {item.signal: item.availability for item in bundle.profile.coverage}
    assert coverage["client.render"] == "not_observed"


def test_gemini_barge_in_accepts_only_when_response_is_interrupted() -> None:
    interrupted = _drive(_barge_in_events({"interrupted": True}), session_id="gemini-interrupted")
    completed = _drive(_barge_in_events({"turnComplete": True}), session_id="gemini-completed")

    interrupted_events = [event.event_name for event in interrupted.profile.events]
    completed_events = [event.event_name for event in completed.profile.events]

    assert "earshot.interruption.detected" in interrupted_events
    assert "earshot.interruption.accepted" in interrupted_events
    assert _agent_operation(interrupted).status == "cancelled"

    # A verified barge-in gesture that ends in a normal turn is detection, never
    # acceptance: the model was not actually cut off.
    assert "earshot.interruption.detected" in completed_events
    assert "earshot.interruption.accepted" not in completed_events
    assert _agent_operation(completed).status == "ok"


def test_gemini_never_retains_transcript_audio_or_tool_content() -> None:
    events: list[tuple[dict[str, object], int]] = [
        ({"setupComplete": {}}, 900),
        ({"sessionResumptionUpdate": {"newHandle": PRIVATE_RESUME}}, 950),
        ({"realtimeInput": {"activityEnd": {}}}, 1_000),
        (
            {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": PRIVATE_USER_TRANSCRIPT}]}],
                    "turnComplete": False,
                }
            },
            1_010,
        ),
        (
            {
                "serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {"data": PRIVATE_AUDIO}}]},
                    "inputTranscription": {"text": PRIVATE_USER_TRANSCRIPT},
                    "outputTranscription": {"text": PRIVATE_AGENT_TRANSCRIPT},
                }
            },
            1_410,
        ),
        (
            {
                "toolCall": {
                    "functionCalls": [{"id": PRIVATE_TOOL_ID, "args": {"query": PRIVATE_TOOL_ARG}}]
                }
            },
            1_450,
        ),
        (
            {
                "serverContent": {"turnComplete": True},
                "usageMetadata": _usage_metadata(),
            },
            1_500,
        ),
    ]
    bundle = _drive(events, session_id="gemini-privacy")

    protobuf = encode_incident_protobuf(bundle)
    json_bytes = encode_incident_json(bundle)
    text = repr(bundle)
    for sentinel in (
        PRIVATE_AUDIO,
        PRIVATE_USER_TRANSCRIPT,
        PRIVATE_AGENT_TRANSCRIPT,
        PRIVATE_TOOL_ARG,
        PRIVATE_TOOL_ID,
        PRIVATE_RESUME,
    ):
        assert sentinel.encode() not in protobuf
        assert sentinel.encode() not in json_bytes
        assert sentinel not in text
    # Every discarded content field is a value-free omission, one class each.
    classes = {omission.capture_class for omission in bundle.profile.privacy.omissions}
    assert {"audio", "transcript", "tool_payload", "model_payload", "diagnostic_payload"} <= classes


def _capture_gemini(status: str = "completed") -> earshot.IncidentBundle:
    adapter = GeminiLiveAdapter(model=MODEL, identity_key=IDENTITY_KEY)
    session = earshot.pipeline(
        session_id="gemini-conformance",
        bundle_id="gemini-conformance-bundle",
        clock=ManualClock(wall=START, monotonic=0),
        config=RecorderConfig(clock_domain_id="conformance-clock"),
    )
    with session.turn(turn_id="turn-0") as turn:
        for payload, received_at_ms in _normal_events():
            adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    return session.close(status)


def test_gemini_sanitized_synthetic_capture_meets_shared_conformance() -> None:
    bundle = assert_capture_conforms(
        _capture_gemini,
        forbidden_values=(
            PRIVATE_AUDIO,
            PRIVATE_USER_TRANSCRIPT,
            PRIVATE_AGENT_TRANSCRIPT,
            PRIVATE_TOOL_ARG,
            PRIVATE_TOOL_ID,
        ),
    )

    assert {op.operation_name for op in bundle.profile.operations} == {"agent", "tool"}


def test_gemini_incomplete_close_remains_canonical() -> None:
    bundle = assert_capture_conforms(
        lambda: _capture_gemini("failed"),
        expected_completeness="incomplete",
    )

    assert bundle.profile.session.status == "failed"


def _openai_native_s2s() -> earshot.IncidentBundle:
    adapter = OpenAIRealtimeAdapter(model="gpt-realtime", identity_key=IDENTITY_KEY)
    session = earshot.pipeline(session_id="parity-openai", started_at_unix_nano=START)
    events = [
        ({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 850}, 1_000),
        ({"type": "response.created", "response": {"id": "parity-response"}}, 1_020),
        (
            {
                "type": "response.output_audio.delta",
                "response_id": "parity-response",
                "delta": "private-openai-audio",
            },
            1_410,
        ),
        (
            {"type": "response.done", "response": {"id": "parity-response", "status": "completed"}},
            1_500,
        ),
    ]
    with session.turn() as turn:
        for payload, received_at_ms in events:
            adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    return session.close()


def _gemini_native_s2s() -> earshot.IncidentBundle:
    events: list[tuple[dict[str, object], int]] = [
        ({"realtimeInput": {"activityEnd": {}}}, 1_000),
        (
            {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": PRIVATE_AUDIO}}]}}},
            1_410,
        ),
        ({"serverContent": {"turnComplete": True}}, 1_500),
    ]
    return _drive(events, session_id="parity-gemini")


def test_gemini_and_openai_native_s2s_project_the_same_governed_turn_facts() -> None:
    gemini_bundle = _gemini_native_s2s()
    openai_bundle = _openai_native_s2s()
    for bundle in (gemini_bundle, openai_bundle):
        assert validate_incident(bundle).ok

    def _governed(bundle: earshot.IncidentBundle):
        analysis = analyze_incident(
            bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
        )
        metrics = analysis.projections.turns[0].metrics
        return metrics.response_latency, metrics.render_start_response_latency

    gemini_response, gemini_render = _governed(gemini_bundle)
    openai_response, openai_render = _governed(openai_bundle)

    # Same governed turn fact: a native user-stop -> first-audio receipt latency.
    assert gemini_response.value == openai_response.value == 410
    assert gemini_response.confidence == openai_response.confidence == "estimated"
    assert gemini_render.availability == openai_render.availability == "not_observed"

    # Each keeps its own provider-native identity and wire-event vocabulary.
    assert _agent_operation(gemini_bundle).attributes["gen_ai.provider.name"] == "gemini"
    assert _agent_operation(openai_bundle).attributes["gen_ai.provider.name"] == "openai"
    assert _response_latency_source(gemini_bundle) == "serverContent.modelTurn.inlineData"
    assert _response_latency_source(openai_bundle) == "response.output_audio.delta"

    def _native_measurements(bundle: earshot.IncidentBundle, prefix: str) -> set[str]:
        return {
            measurement.name
            for sample in bundle.profile.quality_samples
            for measurement in sample.measurements
            if measurement.name.startswith(prefix)
        }

    assert "openai.realtime.audio_end" in _native_measurements(openai_bundle, "openai.")
    assert not _native_measurements(gemini_bundle, "openai.")
