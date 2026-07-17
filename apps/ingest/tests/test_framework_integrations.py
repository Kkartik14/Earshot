from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from importlib.metadata import version

import pytest

from earshot.adapters import LiveKitAdapter, PipecatAdapter
from earshot.analysis import analyze_incident
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident

pytestmark = pytest.mark.integration


def recorder() -> IncidentRecorder:
    return IncidentRecorder(config=RecorderConfig(clock_domain_id="framework-integration"))


class ListenerSession:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, name: str):
        def register(callback: object) -> None:
            self.listeners[name] = callback

        return register


def test_livekit_1_6_metric_models_are_consumed_as_real_objects() -> None:
    pytest.importorskip("livekit.agents")
    from livekit.agents.metrics import EOUMetrics, LLMMetrics, STTMetrics, TTSMetrics

    assert version("livekit-agents").startswith("1.6.")
    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    metrics = (
        EOUMetrics(
            timestamp=1_800_000_000.0,
            end_of_utterance_delay=0.2,
            transcription_delay=0.05,
            on_user_turn_completed_delay=0.01,
            speech_id="speech-integration",
        ),
        STTMetrics(
            label="stt",
            request_id="stt-request",
            timestamp=1_800_000_000.2,
            duration=0.2,
            audio_duration=0.2,
            streamed=False,
        ),
        LLMMetrics(
            label="llm",
            request_id="llm-request",
            timestamp=1_800_000_000.5,
            duration=0.25,
            ttft=0.1,
            cancelled=False,
            completion_tokens=8,
            prompt_tokens=12,
            prompt_cached_tokens=0,
            total_tokens=20,
            tokens_per_second=32.0,
            speech_id="speech-integration",
        ),
        TTSMetrics(
            label="tts",
            request_id="tts-request",
            timestamp=1_800_000_000.7,
            ttfb=0.05,
            duration=0.15,
            audio_duration=0.15,
            cancelled=False,
            characters_count=24,
            streamed=True,
            speech_id="speech-integration",
        ),
    )
    for metric in metrics:
        adapter.consume_metric(metric)
    bundle = adapter.recorder.close()
    assert [operation.operation_name for operation in bundle.profile.operations] == [
        "turn_detection",
        "stt",
        "llm",
        "tts",
    ]
    assert validate_incident(bundle).ok


def test_pipecat_1_5_real_readable_spans_have_lifecycle_root_and_sibling_stages() -> None:
    pytest.importorskip("pipecat")
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")

    assert version("pipecat-ai").startswith("1.5.")
    adapter = PipecatAdapter(recorder(), framework_version=version("pipecat-ai"))
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "pipecat-integration"})
    )
    adapter.attach(provider)
    tracer = provider.get_tracer("pipecat.turn", version("pipecat-ai"))
    try:
        with tracer.start_as_current_span("turn", attributes={"turn.number": 1}):
            with tracer.start_as_current_span("stt"):
                pass
            with tracer.start_as_current_span("llm", attributes={"metrics.ttfb": 0.1}):
                pass
            with tracer.start_as_current_span("tts", attributes={"metrics.ttfb": 0.05}):
                pass
    finally:
        provider.shutdown()

    bundle = adapter.recorder.close()
    root = next(
        operation
        for operation in bundle.profile.operations
        if operation.attributes.get("earshot.framework.operation.name") == "turn"
    )
    children = [
        operation
        for operation in bundle.profile.operations
        if operation.operation_id != root.operation_id
    ]
    assert root.operation_name == "framework_operation"
    assert {operation.parent_span_id for operation in children} == {root.span_id}
    assert {operation.operation_name for operation in children} == {"stt", "llm", "tts"}
    assert validate_incident(bundle).ok


def test_pipecat_real_pipeline_normal_end_frame_does_not_create_interruption() -> None:
    pytest.importorskip("pipecat")
    script = textwrap.dedent(
        """
        import asyncio
        import json

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.processors.filters.identity_filter import IdentityFilter
        from pipecat.workers.runner import WorkerRunner

        from earshot.adapters import PipecatAdapter
        from earshot.recorder import IncidentRecorder, RecorderConfig

        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        recorder = IncidentRecorder(config=RecorderConfig(clock_domain_id="server-clock"))
        adapter = PipecatAdapter(recorder, framework_version="1.5.0")
        adapter.attach(provider)
        worker = PipelineWorker(
            Pipeline([IdentityFilter()]),
            observers=[adapter.create_observer()],
            enable_tracing=True,
            enable_turn_tracking=True,
            enable_rtvi=False,
            conversation_id="normal-end-frame",
            idle_timeout_secs=None,
        )

        async def run():
            runner = WorkerRunner(handle_sigint=False)
            running = asyncio.create_task(runner.run(worker))
            await asyncio.sleep(0.05)
            await worker.stop_when_done()
            await asyncio.wait_for(running, 3)

        asyncio.run(run())
        provider.shutdown()
        bundle = recorder.close()
        turn = next(
            operation
            for operation in bundle.profile.operations
            if operation.attributes.get("earshot.framework.operation.name") == "turn"
        )
        print(
            "EARSHOT_RESULT="
            + json.dumps(
                {
                    "was_interrupted": turn.attributes.get("turn.was_interrupted"),
                    "accepted": sum(
                        event.event_name == "earshot.interruption.accepted"
                        for event in bundle.profile.events
                    ),
                },
                sort_keys=True,
            )
        )
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("EARSHOT_RESULT=")
    )
    assert json.loads(result_line.removeprefix("EARSHOT_RESULT=")) == {
        "accepted": 0,
        "was_interrupted": True,
    }


def test_livekit_dual_surface_correlates_real_eou_metric_to_sibling_agent_turn() -> None:
    pytest.importorskip("livekit.agents")
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    from livekit.agents.metrics import EOUMetrics
    from livekit.agents.voice.events import MetricsCollectedEvent

    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "livekit-dual-surface"})
    )
    session = ListenerSession()
    adapter.attach_span_processor(provider)
    adapter.attach_session_listeners(session)
    tracer = provider.get_tracer("livekit.agents", version("livekit-agents"))
    base = time.time()
    realtime_metric = {
        "type": "realtime_model_metrics",
        "request_id": "response-real-shape",
        "timestamp": base + 0.15,
        "duration": 0.4,
        "ttft": 0.05,
        "input_tokens": 4,
        "output_tokens": 2,
    }
    try:
        with tracer.start_as_current_span("agent_session"):
            with (
                tracer.start_as_current_span("user_turn"),
                tracer.start_as_current_span(
                    "eou_detection",
                    attributes={"lk.eou.endpointing_delay": 0.2},
                ),
            ):
                pass
            with tracer.start_as_current_span(
                "agent_turn",
                attributes={
                    "lk.speech_id": "speech-real-shape",
                    "lk.generation_id": "generation-real-shape",
                    "lk.realtime_model_metrics": json.dumps(realtime_metric),
                },
            ):
                pass
        callback = session.listeners["metrics_collected"]
        callback(  # type: ignore[operator]
            MetricsCollectedEvent(
                metrics=EOUMetrics(
                    timestamp=base + 0.1,
                    end_of_utterance_delay=0.2,
                    transcription_delay=0.05,
                    on_user_turn_completed_delay=0.01,
                    speech_id="speech-real-shape",
                )
            )
        )
    finally:
        provider.shutdown()

    bundle = adapter.recorder.close()
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano=str(time.time_ns()),
    )
    turn = next(item for item in analysis.projections.turns if item.turn_id == "speech-real-shape")
    assert turn.metrics.generated_response_latency.availability == "available"
    assert turn.metrics.response_latency.availability == "available"
    assert any(
        event.event_name == "earshot.turn.committed" and event.turn_id == "speech-real-shape"
        for event in bundle.profile.events
    )
    assert validate_incident(bundle).ok


def test_livekit_dual_surface_records_one_accepted_interruption_outcome() -> None:
    pytest.importorskip("livekit.agents")
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    from livekit.agents.inference import OverlappingSpeechEvent
    from livekit.agents.llm import ChatMessage
    from livekit.agents.voice.events import ConversationItemAddedEvent

    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "livekit-interruption"})
    )
    session = ListenerSession()
    adapter.attach_span_processor(provider)
    adapter.attach_session_listeners(session)
    tracer = provider.get_tracer("livekit.agents", version("livekit-agents"))
    try:
        session.listeners["overlapping_speech"](  # type: ignore[operator]
            OverlappingSpeechEvent(
                detected_at=time.time(),
                is_interruption=True,
                probability=0.9,
            )
        )
        session.listeners["conversation_item_added"](  # type: ignore[operator]
            ConversationItemAddedEvent(
                item=ChatMessage(
                    id="assistant-item",
                    role="assistant",
                    content=[],
                    interrupted=True,
                )
            )
        )
        with tracer.start_as_current_span(
            "agent_turn",
            attributes={"lk.speech_id": "speech-interrupted", "lk.interrupted": True},
        ):
            pass
    finally:
        provider.shutdown()

    bundle = adapter.recorder.close()
    phases = [event.event_name for event in bundle.profile.events]
    assert phases.count("earshot.interruption.detected") == 1
    assert phases.count("earshot.interruption.accepted") == 1
    assert validate_incident(bundle).ok


def test_livekit_realtime_no_audio_sentinel_is_coverage_not_negative_latency() -> None:
    pytest.importorskip("livekit.agents")
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    from livekit.agents.metrics import RealtimeModelMetrics

    metric = RealtimeModelMetrics(
        request_id="response-no-audio",
        timestamp=time.time(),
        ttft=-1,
        input_token_details=RealtimeModelMetrics.InputTokenDetails(),
        output_token_details=RealtimeModelMetrics.OutputTokenDetails(),
    )
    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "livekit-realtime-sentinel"})
    )
    adapter.attach_span_processor(provider)
    tracer = provider.get_tracer("livekit.agents", version("livekit-agents"))
    try:
        with tracer.start_as_current_span(
            "agent_turn",
            attributes={
                "lk.speech_id": "speech-no-audio",
                "lk.realtime_model_metrics": metric.model_dump_json(),
            },
        ):
            pass
    finally:
        provider.shutdown()

    bundle = adapter.recorder.close()
    assert all(
        measurement.name != "lk.response.ttft"
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    )
    assert not any(
        event.event_name == "earshot.response.first_audio_generated"
        for event in bundle.profile.events
    )
    assert any(
        item.signal == "livekit.response.first_audio_generated" and item.reason == "no_audio_token"
        for item in bundle.profile.coverage
    )
    assert validate_incident(bundle).ok


def test_livekit_eot_inference_metrics_are_not_interruption_metrics() -> None:
    pytest.importorskip("livekit.agents")
    from livekit.agents.metrics import EOTInferenceMetrics

    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    adapter.consume_metric(
        EOTInferenceMetrics(
            timestamp=time.time(),
            total_duration=0.2,
            detection_delay=0.08,
            prediction_duration=0.03,
            num_requests=2,
        )
    )
    operation = adapter.recorder.close().profile.operations[0]
    assert operation.operation_name == "turn_detection"
    assert operation.attributes["earshot.duration.turn_detection.total_seconds"] == 0.2
    assert operation.attributes["earshot.metric.turn_detection.request_count"] == 2
    assert not any("interruption" in key for key in operation.attributes)


def test_livekit_dual_attach_keeps_stt_interruption_operations_with_vad_quality() -> None:
    pytest.importorskip("livekit.agents")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    from livekit.agents.metrics import InterruptionMetrics, STTMetrics, VADMetrics
    from livekit.agents.voice.events import MetricsCollectedEvent

    adapter = LiveKitAdapter(recorder(), framework_version=version("livekit-agents"))
    provider = sdk_trace.TracerProvider()
    session = ListenerSession()
    adapter.attach_span_processor(provider)
    adapter.attach_session_listeners(session)
    callback = session.listeners["metrics_collected"]
    now = time.time()
    metrics = (
        VADMetrics(
            label="vad",
            timestamp=now,
            idle_time=0.1,
            inference_duration_total=0.02,
            inference_count=2,
        ),
        STTMetrics(
            label="stt",
            request_id="stt-metric-only",
            timestamp=now + 0.1,
            duration=0.08,
            audio_duration=0.5,
            streamed=False,
        ),
        InterruptionMetrics(
            timestamp=now + 0.2,
            total_duration=0.04,
            prediction_duration=0.02,
            detection_delay=0.03,
            num_interruptions=1,
            num_backchannels=0,
            num_requests=1,
        ),
    )
    try:
        for metric in metrics:
            callback(MetricsCollectedEvent(metrics=metric))  # type: ignore[operator]
    finally:
        provider.shutdown()

    bundle = adapter.recorder.close()
    # VAD is a continuous background signal -> a quality sample, not an operation.
    # STT and interruption remain metric-only operations (no native LiveKit span).
    assert [item.operation_name for item in bundle.profile.operations] == [
        "stt",
        "interruption_detection",
    ]
    # Every listener-only operation now retains the provider measurements that
    # substantiate it, while VAD remains a standalone continuous signal.
    assert {sample.quality_kind for sample in bundle.profile.quality_samples} == {"pipeline.metric"}
    named_quality = {
        sample.attributes.get("earshot.framework.metric.name"): sample
        for sample in bundle.profile.quality_samples
        if "earshot.framework.metric.name" in sample.attributes
    }
    assert set(named_quality) == {"stt", "vad"}

    vad = named_quality["vad"]
    vad_measurements = {measurement.name: measurement for measurement in vad.measurements}
    assert vad_measurements["earshot.metric.inference.count"].aggregation == "delta"
    assert vad_measurements["earshot.duration.inference_seconds"].aggregation == "delta"
    assert vad_measurements["earshot.duration.vad.idle_seconds"].aggregation == "instant"
    assert vad.attributes["earshot.framework.name"] == "livekit"
    assert vad.attributes["earshot.framework.metric.name"] == "vad"
    assert vad.attributes["earshot.framework.version"] == version("livekit-agents")

    stt = named_quality["stt"]
    stt_measurements = {measurement.name: measurement for measurement in stt.measurements}
    assert stt_measurements["livekit.stt.audio_duration"].aggregation == "delta"
    assert stt_measurements["livekit.stt.audio_duration"].value == 0.5
    assert stt.attributes["earshot.request.id"] == "stt-metric-only"

    interruption_samples = [
        sample
        for sample in bundle.profile.quality_samples
        if any(
            measurement.name == "earshot.metric.interruption.count"
            for measurement in sample.measurements
        )
    ]
    assert len(interruption_samples) == 1
    interruption_measurements = {
        measurement.name: measurement for measurement in interruption_samples[0].measurements
    }
    assert interruption_measurements["earshot.metric.interruption.count"].aggregation == "delta"
    assert (
        interruption_measurements["earshot.metric.interruption.request_count"].aggregation
        == "delta"
    )
    assert (
        interruption_measurements["earshot.duration.interruption.total_seconds"].aggregation
        == "instant"
    )
    assert validate_incident(bundle).ok
