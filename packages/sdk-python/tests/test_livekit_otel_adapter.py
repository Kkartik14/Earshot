from __future__ import annotations

import builtins
import json
from types import SimpleNamespace

import pytest

from earshot.adapters import LiveKitAdapter
from earshot.adapters.base import AdapterDependencyError
from earshot.analysis import analyze_incident
from earshot.privacy import CaptureClass, CapturePolicy
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident
from incident_factory import ROOT_SPAN_ID, SECRET_SENTINEL, TRACE_ID, point

pytestmark = pytest.mark.unit


def recorder() -> IncidentRecorder:
    return IncidentRecorder(config=RecorderConfig(clock_domain_id="server-clock"))


def test_native_livekit_span_preserves_otel_envelope_and_interruption_fact() -> None:
    target_trace = int("5" * 32, 16)
    target_span = int("6" * 16, 16)
    adapter = LiveKitAdapter(recorder(), framework_version="1.5.3")
    source = SimpleNamespace(
        name="tts_node",
        context=SimpleNamespace(trace_id=int(TRACE_ID, 16), span_id=int(ROOT_SPAN_ID, 16)),
        parent=SimpleNamespace(span_id=int("f" * 16, 16), is_remote=True),
        status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
        start_time=1_800_000_000_000_000_000,
        end_time=1_800_000_000_150_000_000,
        attributes={
            "earshot.turn.id": "turn-livekit",
            "gen_ai.request.model": "voice-model",
            "lk.interrupted": True,
            "lk.response.text": "not retained by metadata-only policy",
        },
        resource=SimpleNamespace(
            attributes={"service.name": "voice-app", "unsafe.resource": "drop me"},
            schema_url="https://opentelemetry.io/schemas/1.29.0",
        ),
        instrumentation_scope=SimpleNamespace(
            name="livekit-agents",
            version="1.5.3",
            attributes={"earshot.framework.name": "livekit"},
            schema_url="https://opentelemetry.io/schemas/1.30.0",
        ),
        links=(
            SimpleNamespace(
                context=SimpleNamespace(trace_id=target_trace, span_id=target_span),
                attributes={
                    "earshot.link.type": "related",
                    "earshot.link.target_scope": "external",
                },
            ),
        ),
    )

    operation_id = adapter.consume_span(source)
    bundle = adapter.recorder.close()
    operation = bundle.profile.operations[0]
    interruption = bundle.profile.events[0]

    assert operation.operation_id == operation_id
    assert operation.operation_name == "tts"
    assert operation.trace_id == TRACE_ID
    assert operation.span_id == ROOT_SPAN_ID
    assert operation.parent_span_id == "f" * 16
    assert operation.parent_scope == "external"
    assert operation.started_at.source_time_unix_nano == "1800000000000000000"
    assert operation.started_at.clock_domain_id == "server-clock"
    assert operation.ended_at is not None
    assert operation.ended_at.source_time_unix_nano == "1800000000150000000"
    assert operation.ended_at.clock_domain_id == "server-clock"
    assert operation.resource == {"service.name": "voice-app"}
    assert operation.resource_schema_url == "https://opentelemetry.io/schemas/1.29.0"
    assert operation.instrumentation_scope_name == "livekit-agents"
    assert operation.instrumentation_scope_version == "1.5.3"
    assert operation.instrumentation_scope_attributes == {
        "earshot.framework.name": "livekit"
    }
    assert operation.schema_url == "https://opentelemetry.io/schemas/1.30.0"
    assert operation.links[0].trace_id == "5" * 32
    assert operation.links[0].span_id == "6" * 16
    assert "lk.response.text" not in operation.attributes
    assert interruption.event_name == "earshot.interruption.accepted"
    assert interruption.operation_id == operation_id
    assert interruption.trace_id == TRACE_ID
    assert interruption.span_id == ROOT_SPAN_ID
    assert interruption.resource == {"service.name": "voice-app"}
    assert interruption.resource_schema_url == operation.resource_schema_url
    assert interruption.instrumentation_scope_attributes == (
        operation.instrumentation_scope_attributes
    )
    assert interruption.evidence is not None
    assert interruption.evidence.method == "native_interruption_signal"
    assert validate_incident(bundle).ok


def test_livekit_source_controlled_scope_link_and_schema_labels_cannot_leak() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        SimpleNamespace(
            name="llm_node",
            context=SimpleNamespace(
                trace_id=int(TRACE_ID, 16),
                span_id=int(ROOT_SPAN_ID, 16),
            ),
            parent=None,
            parent_scope=SECRET_SENTINEL,
            status="ok",
            start_time=1_800_000_000_000_000_000,
            end_time=1_800_000_000_100_000_000,
            attributes={},
            resource=SimpleNamespace(
                attributes={},
                schema_url=(
                    f"https://opentelemetry.io:{SECRET_SENTINEL}/schemas/1.30.0"
                ),
            ),
            instrumentation_scope=SimpleNamespace(
                name=SECRET_SENTINEL,
                version=SECRET_SENTINEL,
                attributes={"vendor.scope.secret": SECRET_SENTINEL},
                schema_url=f"https://opentelemetry.io/schemas/{SECRET_SENTINEL}",
            ),
            links=(
                SimpleNamespace(
                    context=SimpleNamespace(
                        trace_id=int("5" * 32, 16),
                        span_id=int("6" * 16, 16),
                    ),
                    attributes={
                        "earshot.link.type": SECRET_SENTINEL,
                        "earshot.link.target_scope": SECRET_SENTINEL,
                    },
                ),
            ),
            events=(),
        )
    )
    bundle = adapter.recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.parent_scope == "unknown"
    assert operation.instrumentation_scope_name is not None
    assert operation.instrumentation_scope_name.startswith("sha256:")
    assert operation.instrumentation_scope_version is not None
    assert operation.instrumentation_scope_version.startswith("sha256:")
    assert operation.instrumentation_scope_attributes == {}
    assert operation.schema_url is None
    assert operation.resource_schema_url is None
    assert "earshot.source.schema_url_sha256" in operation.attributes
    assert "earshot.source.resource_schema_url_sha256" in operation.attributes
    assert operation.links[0].relationship.startswith("sha256:")
    assert operation.links[0].target_scope == "unknown"
    assert SECRET_SENTINEL not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_current_user_interruption_event_is_normalized_as_accepted() -> None:
    adapter = LiveKitAdapter(recorder(), framework_version="current")
    event_id = adapter.consume_interruption_event(
        SimpleNamespace(
            type="user_interruption_detected",
            timestamp=1_800_000_000.25,
            probability=0.92,
        )
    )
    bundle = adapter.recorder.close()
    event = bundle.profile.events[0]
    assert event.event_id == event_id
    assert event.event_name == "earshot.interruption.accepted"
    assert event.attributes["earshot.metric.interruption.probability"] == 0.92
    assert validate_incident(bundle).ok


def test_livekit_current_payload_keys_are_fail_closed_and_opt_in_works() -> None:
    default_adapter = LiveKitAdapter(recorder())
    default_adapter.consume_span(
        {
            "name": "eou_detection",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
            "attributes": {
                "lk.user_transcript": "PRIVATE_TRANSCRIPT",
                "lk.transcript_confidence": 0.91,
            },
        }
    )
    default_bundle = default_adapter.recorder.close()
    default_operation = default_bundle.profile.operations[0]
    assert "speech.text" not in default_operation.attributes
    assert default_operation.attributes["lk.transcript_confidence"] == 0.91
    assert "PRIVATE_TRANSCRIPT" not in default_bundle.model_dump_json()

    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.MODEL_PAYLOAD}))
    model_recorder = IncidentRecorder(
        config=RecorderConfig(clock_domain_id="server-clock", capture_policy=policy)
    )
    model_adapter = LiveKitAdapter(model_recorder)
    model_adapter.consume_span(
        {
            "name": "llm_node",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
            "attributes": {
                "lk.chat_ctx": [{"role": "user", "content": "PRIVATE_INPUT"}],
                "lk.response.text": "PRIVATE_OUTPUT",
            },
        }
    )
    model_bundle = model_recorder.close()
    model_operation = model_bundle.profile.operations[0]
    assert model_operation.capture_class == "model_payload"
    assert model_operation.attributes["gen_ai.input"][0]["content"] == "PRIVATE_INPUT"
    assert model_operation.attributes["gen_ai.output.messages"] == "PRIVATE_OUTPUT"
    assert validate_incident(model_bundle).ok


def test_current_chat_message_metrics_are_retained_without_message_content() -> None:
    adapter = LiveKitAdapter(recorder(), framework_version="current")
    item = SimpleNamespace(
        id="chat-turn-7",
        role="assistant",
        interrupted=True,
        content=[SECRET_SENTINEL],
        metrics={
            "llm_node_ttft": 0.11,
            "tts_node_ttfb": 0.07,
            "e2e_latency": 0.42,
            "started_speaking_at": 1_800_000_000.0,
            "stopped_speaking_at": 1_800_000_001.0,
        },
    )
    first = adapter.consume_conversation_item(SimpleNamespace(item=item))
    second = adapter.consume_conversation_item(SimpleNamespace(item=item))
    assert first == second
    bundle = adapter.recorder.close()
    sample = bundle.profile.quality_samples[0]
    assert sample.attributes["earshot.conversation.item.id"] == "chat-turn-7"
    assert "earshot.turn.id" not in sample.attributes
    assert {measurement.name for measurement in sample.measurements} == {
        "livekit.llm_node_ttft",
        "livekit.tts_node_ttfb",
        "livekit.e2e_latency",
    }
    assert bundle.profile.events[0].event_name == "earshot.interruption.accepted"
    assert SECRET_SENTINEL not in str(bundle.model_dump(mode="python"))
    assert validate_incident(bundle).ok
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    assert analysis.projections["turns"] == []
    unassigned = analysis.projections["unassigned_provider_measurements"][sample.sample_id]
    assert unassigned["livekit.e2e_latency"]["value"] == 420.0
    assert unassigned["livekit.llm_node_ttft"]["value"] == 110.0


def test_chat_message_metrics_use_only_explicit_turn_correlation() -> None:
    adapter = LiveKitAdapter(recorder())
    sample_id = adapter.consume_conversation_item(
        SimpleNamespace(
            turn_id="explicit-turn",
            item=SimpleNamespace(
                id="item-independent",
                role="assistant",
                interrupted=False,
                metrics={"e2e_latency": 0.3},
            ),
        )
    )
    bundle = adapter.recorder.close()
    assert sample_id is not None
    assert bundle.profile.quality_samples[0].attributes["earshot.turn.id"] == "explicit-turn"
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    assert analysis.projections["turns"][0]["turn_id"] == "explicit-turn"


def test_interrupted_chat_message_without_metrics_still_records_the_decision() -> None:
    adapter = LiveKitAdapter(recorder())
    event_id = adapter.consume_conversation_item(
        SimpleNamespace(
            item=SimpleNamespace(
                id="interrupted-without-metrics",
                role="assistant",
                interrupted=True,
                metrics=None,
            )
        )
    )
    bundle = adapter.recorder.close()
    assert event_id is not None
    assert [event.event_name for event in bundle.profile.events] == [
        "earshot.interruption.accepted"
    ]
    assert bundle.profile.quality_samples == ()
    assert validate_incident(bundle).ok


def test_session_listener_bundle_includes_current_and_compatibility_surfaces() -> None:
    listeners = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    adapter = LiveKitAdapter(recorder())
    adapter.attach_session_listeners(Session())
    assert {
        "conversation_item_added",
        "metrics_collected",
        "overlapping_speech",
        "agent_false_interruption",
    } <= set(listeners)


def test_livekit_span_callback_is_idempotent_and_conflicts_are_rejected() -> None:
    adapter = LiveKitAdapter(recorder())
    span = {
        "name": "llm_node",
        "trace_id": TRACE_ID,
        "span_id": ROOT_SPAN_ID,
        "status": "ok",
        "start_time": 1_800_000_000_000_000_000,
        "end_time": 1_800_000_000_100_000_000,
        "instrumentation_scope": {"name": "livekit-agents"},
    }
    first = adapter.consume_span(span)
    second = adapter.consume_span(span)
    assert first == second

    with pytest.raises(ValueError, match="conflicting duplicate"):
        adapter.consume_span({**span, "end_time": 1_800_000_000_200_000_000})

    assert len(adapter.recorder.close().profile.operations) == 1


def test_livekit_native_span_event_is_correlated_and_drops_event_payload() -> None:
    secret = "SENSITIVE_TRANSCRIPT_SENTINEL"
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        SimpleNamespace(
            name="user_turn",
            context=SimpleNamespace(trace_id=int(TRACE_ID, 16), span_id=int(ROOT_SPAN_ID, 16)),
            parent=None,
            status=SimpleNamespace(status_code=SimpleNamespace(name="UNSET")),
            start_time=1_800_000_000_000_000_000,
            end_time=1_800_000_000_100_000_000,
            attributes={"earshot.turn.id": "turn-event"},
            resource=SimpleNamespace(attributes={"service.name": "voice-app"}),
            instrumentation_scope=SimpleNamespace(
                name="livekit-agents", version="1", schema_url=None
            ),
            links=(),
            events=(
                SimpleNamespace(
                    name="earshot.interruption.detected",
                    timestamp=1_800_000_000_050_000_000,
                    attributes={"probability": 0.88, "transcript": secret},
                ),
            ),
        )
    )
    bundle = adapter.recorder.close()
    event = next(
        item for item in bundle.profile.events if item.event_name == "earshot.interruption.detected"
    )
    source_event = next(
        item for item in bundle.profile.events if item.event_name == "otel.span_event"
    )
    assert event.operation_id == bundle.profile.operations[0].operation_id
    assert event.time.source_time_unix_nano == "1800000000050000000"
    assert event.attributes == {"earshot.metric.interruption.probability": 0.88}
    assert source_event.attributes["earshot.source.event.name"].startswith("sha256:")
    assert "transcript" not in source_event.attributes
    assert secret not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_current_snake_case_interruption_metric_uses_provider_time_and_emits_fact() -> None:
    adapter = LiveKitAdapter(recorder(), framework_version="current")
    operation_id = adapter.consume_metric(
        {
            "type": "interruption_metrics",
            "timestamp": 1_800_000_000,
            "total_duration": 0.05,
            "prediction_duration": 0.02,
            "detection_delay": 0.1,
            "num_interruptions": 1,
            "num_backchannels": 2,
            "num_requests": 3,
            "metadata": {"model_name": "adaptive", "model_provider": "livekit"},
        },
        observed_at=point(900_000_000),
    )
    bundle = adapter.recorder.close()
    operation = bundle.profile.operations[0]

    assert operation.operation_id == operation_id
    assert operation.operation_name == "interruption_detection"
    assert operation.started_at.source_time_unix_nano == "1799999999950000000"
    assert operation.ended_at is not None
    assert operation.ended_at.source_time_unix_nano == "1800000000000000000"
    assert operation.evidence is not None
    assert operation.evidence.confidence == "estimated"
    assert operation.evidence.method == "provider_end_minus_duration"
    assert operation.attributes["earshot.metric.interruption.count"] == 1
    assert operation.attributes["gen_ai.request.model"] == "adaptive"
    assert bundle.profile.events[0].event_name == "earshot.interruption.detected"
    assert bundle.profile.events[0].operation_id == operation_id
    assert validate_incident(bundle).ok


def test_interruption_session_event_is_idempotent_and_never_retains_audio() -> None:
    adapter = LiveKitAdapter(recorder())
    event = {
        "type": "overlapping_speech",
        "detected_at": 1_800_000_000,
        "is_interruption": False,
        "probability": 0.25,
        "detection_delay": 0.12,
        "num_requests": 2,
        "speech_input": "RAW_AUDIO_SENTINEL",
        "probabilities": "MODEL_VECTOR_SENTINEL",
    }
    first = adapter.consume_interruption_event(event)
    second = adapter.consume_interruption_event(event)
    bundle = adapter.recorder.close()

    assert first == second
    assert len(bundle.profile.events) == 1
    assert bundle.profile.events[0].event_name == "earshot.interruption.ignored"
    serialized = bundle.model_dump_json()
    assert "RAW_AUDIO_SENTINEL" not in serialized
    assert "MODEL_VECTOR_SENTINEL" not in serialized
    assert validate_incident(bundle).ok


def test_normal_agent_turn_is_agent_not_false_interruption_and_reconciles_speech_id() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        {
            "name": "agent_turn",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
            "attributes": {
                "lk.generation_id": "speech_abc123_2",
                "lk.interrupted": False,
            },
        }
    )
    adapter.consume_metric(
        {
            "type": "llm_metrics",
            "speech_id": "speech_abc123",
            "timestamp": 1_800_000_000.2,
            "duration": 0.1,
            "ttft": 0.02,
        }
    )
    bundle = adapter.recorder.close()
    assert [item.operation_name for item in bundle.profile.operations] == ["agent", "llm"]
    assert {item.turn_id for item in bundle.profile.operations} == {"speech_abc123"}
    assert bundle.profile.events == ()
    assert validate_incident(bundle).ok


def test_realtime_metric_timestamp_is_response_start_not_completion() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": "realtime_model_metrics",
            "request_id": "response-1",
            "timestamp": 1_800_000_000.0,
            "duration": 0.25,
            "ttft": 0.05,
        }
    )
    operation = adapter.recorder.close().profile.operations[0]
    assert operation.started_at.source_time_unix_nano == "1800000000000000000"
    assert operation.ended_at is not None
    assert operation.ended_at.source_time_unix_nano == "1800000000250000000"


def test_native_realtime_metrics_author_first_audio_not_text_token() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        {
            "name": "eou_detection",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
            "attributes": {"lk.speech_id": "speech-realtime"},
        }
    )
    adapter.consume_span(
        {
            "name": "realtime_session",
            "trace_id": TRACE_ID,
            "span_id": "4444444444444444",
            "parent_span_id": ROOT_SPAN_ID,
            "parent_scope": "internal",
            "start_time": 1_800_000_000_100_000_000,
            "end_time": 1_800_000_000_500_000_000,
            "attributes": {
                "lk.realtime_model_metrics": json.dumps(
                    {
                        "type": "realtime_model_metrics",
                        "request_id": "response-realtime",
                        "timestamp": 1_800_000_000.1,
                        "duration": 0.4,
                        "ttft": 0.15,
                        "input_tokens": 10,
                        "output_tokens": 4,
                    }
                )
            },
        }
    )
    bundle = adapter.recorder.close()
    assert all(
        "lk.realtime_model_metrics" not in operation.attributes
        for operation in bundle.profile.operations
    )
    assert any(
        event.event_name == "earshot.response.first_audio_generated"
        for event in bundle.profile.events
    )
    assert not any(
        event.event_name == "earshot.response.first_token" for event in bundle.profile.events
    )
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    turn = analysis.projections["turns"][0]
    assert turn["metrics"]["generated_response_latency"]["availability"] == "available"
    assert turn["metrics"]["first_token_latency"]["availability"] == "not_observed"
    assert validate_incident(bundle).ok


def test_dual_native_and_listener_surfaces_do_not_duplicate_provider_operations(
    monkeypatch,
) -> None:
    adapter = LiveKitAdapter(recorder())
    monkeypatch.setattr(adapter, "create_span_processor", lambda: object())

    class Provider:
        def add_span_processor(self, _processor):
            return None

    listeners = {}

    class Session:
        def on(self, name):
            def register(callback):
                listeners[name] = callback

            return register

    adapter.attach_span_processor(Provider())
    adapter.attach_session_listeners(Session())
    listener_metric = {
        "type": "llm_metrics",
        "label": "llm",
        "request_id": "shared-request",
        "speech_id": "speech-shared",
        "timestamp": 1_800_000_000.2,
        "duration": 0.1,
        "ttft": 0.02,
        "cancelled": False,
        "completion_tokens": 2,
        "prompt_tokens": 3,
        "prompt_cached_tokens": 0,
        "total_tokens": 5,
        "tokens_per_second": 20.0,
    }
    native_metric = {**listener_metric, "speech_id": None}
    listeners["metrics_collected"]({"metrics": listener_metric})
    adapter.consume_span(
        {
            "name": "llm_node",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "parent_span_id": "5555555555555555",
            "parent_scope": "internal",
            "start_time": 1_800_000_000_100_000_000,
            "end_time": 1_800_000_000_300_000_000,
            "attributes": {
                "lk.llm_metrics": json.dumps(native_metric),
            },
        }
    )
    adapter.consume_span(
        {
            "name": "agent_turn",
            "trace_id": TRACE_ID,
            "span_id": "5555555555555555",
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_400_000_000,
            "attributes": {"lk.speech_id": "speech-shared"},
        }
    )
    bundle = adapter.recorder.close()
    assert [operation.operation_name for operation in bundle.profile.operations] == [
        "llm",
        "agent",
    ]
    assert len(bundle.profile.quality_samples) == 1
    sample = bundle.profile.quality_samples[0]
    assert sample.attributes["earshot.operation.id"] == bundle.profile.operations[0].operation_id
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    turn = analysis.projections["turns"][0]
    assert turn["turn_id"] == "speech-shared"
    assert "lk.response.ttft" in turn["metrics"]["provider_measurements"]
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    "native_name",
    [
        "llm_node",
        "llm_request",
        "llm_request_run",
        "tts_node",
        "tts_request",
        "tts_request_run",
    ],
)
def test_current_livekit_nested_span_layer_is_retained(native_name: str) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        {
            "name": native_name,
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
        }
    )
    operation = adapter.recorder.close().profile.operations[0]
    assert operation.attributes["earshot.framework.operation.name"] == native_name
    assert operation.evidence is not None
    assert operation.evidence.source_field == native_name


def test_native_agent_turn_e2e_latency_is_correlated_provider_quality() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        SimpleNamespace(
            name="agent_turn",
            trace_id=TRACE_ID,
            span_id=ROOT_SPAN_ID,
            start_time=1_800_000_000_000_000_000,
            end_time=1_800_000_000_500_000_000,
            attributes={
                "lk.speech_id": "speech-e2e",
                "lk.e2e_latency": 0.42,
            },
            resource=SimpleNamespace(
                attributes={"service.name": "voice-app"},
                schema_url="https://opentelemetry.io/schemas/1.29.0",
            ),
            instrumentation_scope=SimpleNamespace(
                name="livekit-agents",
                version="1.6.5",
                attributes={"earshot.framework.name": "livekit"},
                schema_url="https://opentelemetry.io/schemas/1.30.0",
            ),
        )
    )
    adapter.consume_metric(
        {
            "type": "eou_metrics",
            "speech_id": "speech-e2e",
            "timestamp": 1_800_000_000.0,
            "end_of_utterance_delay": 0.2,
            "transcription_delay": 0.05,
            "on_user_turn_completed_delay": 0.01,
        }
    )
    bundle = adapter.recorder.close()
    e2e_sample = next(
        sample
        for sample in bundle.profile.quality_samples
        if any(item.name == "livekit.e2e_latency" for item in sample.measurements)
    )
    assert e2e_sample.attributes["earshot.turn.id"] == "speech-e2e"
    assert e2e_sample.resource == {"service.name": "voice-app"}
    assert e2e_sample.resource_schema_url == "https://opentelemetry.io/schemas/1.29.0"
    assert e2e_sample.instrumentation_scope_attributes == {
        "earshot.framework.name": "livekit"
    }
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    response = analysis.projections["turns"][0]["metrics"]["response_latency"]
    assert response["value"] == 420.0
    assert response["basis"] == "provider_direct"
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("metric", "coverage_reason"),
    [
        (
            {
                "type": "eou_metrics",
                "timestamp": 1_800_000_000.0,
                "end_of_utterance_delay": 0.0,
                "transcription_delay": 0.0,
                "on_user_turn_completed_delay": 0.0,
            },
            "speech_end_not_detected",
        ),
        (
            {
                "type": "stt_metrics",
                "timestamp": 1_800_000_000.0,
                "duration": 0.0,
                "streamed": True,
            },
            "streaming_duration_not_exposed",
        ),
    ],
)
def test_livekit_provider_zero_sentinels_are_missing_duration_not_measured_zero(
    metric,
    coverage_reason: str,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(metric)
    bundle = adapter.recorder.close()
    operation = bundle.profile.operations[0]
    assert operation.ended_at is None
    assert bundle.profile.coverage[0].reason == coverage_reason
    if metric["type"] == "eou_metrics":
        assert "lk.eou.endpointing_delay" not in operation.attributes
        assert "lk.eou.transcription_delay" not in operation.attributes
    assert validate_incident(bundle).ok


def test_eou_metric_preserves_user_turn_callback_delay() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": "eou_metrics",
            "timestamp": 1_800_000_000.0,
            "speech_id": "speech-eou",
            "end_of_utterance_delay": 0.2,
            "transcription_delay": 0.05,
            "on_user_turn_completed_delay": 0.03,
        }
    )
    operation = adapter.recorder.close().profile.operations[0]
    assert operation.attributes["earshot.duration.turn_callback_seconds"] == 0.03


def test_native_participant_sid_and_kind_become_typed_ownership_without_identity() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_span(
        {
            "name": "llm_node",
            "trace_id": TRACE_ID,
            "span_id": ROOT_SPAN_ID,
            "start_time": 1_800_000_000_000_000_000,
            "end_time": 1_800_000_000_100_000_000,
            "attributes": {
                "lk.participant_id": "PA_opaque_sid",
                "lk.participant_kind": "sip",
                "lk.participant_identity": SECRET_SENTINEL,
            },
        }
    )
    bundle = adapter.recorder.close()
    assert bundle.profile.operations[0].participant_id == "PA_opaque_sid"
    assert bundle.profile.participants[0].participant_id == "PA_opaque_sid"
    assert bundle.profile.participants[0].endpoint_kind == "sip"
    assert SECRET_SENTINEL not in bundle.model_dump_json()
    assert validate_incident(bundle).ok


def test_attach_session_listeners_supports_decorator_api() -> None:
    adapter = LiveKitAdapter(recorder())
    listeners: dict[str, object] = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    adapter.attach_session_listeners(Session())
    assert set(listeners) == {
        "conversation_item_added",
        "metrics_collected",
        "overlapping_speech",
        "agent_false_interruption",
    }
    listeners["metrics_collected"](  # type: ignore[operator]
        {
            "metrics": {
                "type": "vad_metrics",
                "timestamp": 1_800_000_000,
                "inference_count": 12,
                "inference_duration_total": 0.4,
            }
        }
    )
    listeners["agent_false_interruption"](  # type: ignore[operator]
        {"type": "agent_false_interruption", "created_at": 1_800_000_001, "resumed": True}
    )
    bundle = adapter.recorder.close()
    # VAD is a continuous background signal: it is recorded as a pipeline quality
    # sample, not one operation per callback (which floods the incident).
    assert [item.operation_name for item in bundle.profile.operations] == []
    assert [sample.quality_kind for sample in bundle.profile.quality_samples] == ["pipeline.metric"]
    vad = bundle.profile.quality_samples[0]
    vad_measurements = {measurement.name: measurement for measurement in vad.measurements}
    assert vad_measurements["earshot.metric.inference.count"].aggregation == "delta"
    assert vad_measurements["earshot.duration.inference_seconds"].aggregation == "delta"
    assert vad.attributes["earshot.framework.name"] == "livekit"
    assert vad.attributes["earshot.framework.version"] == "unknown"
    assert [item.event_name for item in bundle.profile.events] == ["earshot.interruption.ignored"]
    assert [(item.signal, item.availability) for item in bundle.profile.coverage] == [
        ("client.render", "not_observed")
    ]


def test_span_processor_attach_is_additive_and_validates_provider(monkeypatch) -> None:
    adapter = LiveKitAdapter(recorder())
    sentinel = object()
    monkeypatch.setattr(adapter, "create_span_processor", lambda: sentinel)

    class Provider:
        def __init__(self) -> None:
            self.processors: list[object] = []

        def add_span_processor(self, processor: object) -> None:
            self.processors.append(processor)

    provider = Provider()
    assert adapter.attach_span_processor(provider) is sentinel
    assert provider.processors == [sentinel]
    with pytest.raises(TypeError, match="does not support"):
        adapter.attach_span_processor(object())


def test_optional_livekit_span_processor_has_actionable_dependency_error(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry.sdk.trace"):
            raise ImportError("forced missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_otel)
    with pytest.raises(AdapterDependencyError, match="opentelemetry-sdk"):
        LiveKitAdapter(recorder()).create_span_processor()


def test_span_processor_filter_accepts_only_livekit_scopes_or_attributes() -> None:
    livekit_scope = SimpleNamespace(
        instrumentation_scope=SimpleNamespace(name="livekit-agents"), attributes={}
    )
    livekit_attribute = SimpleNamespace(
        instrumentation_scope=SimpleNamespace(name="application"),
        attributes={"lk.speech_id": "speech"},
    )
    unrelated = SimpleNamespace(
        instrumentation_scope=SimpleNamespace(name="application"),
        attributes={"gen_ai.request.model": "model"},
    )
    assert LiveKitAdapter._is_livekit_span(livekit_scope)
    assert LiveKitAdapter._is_livekit_span(livekit_attribute)
    assert not LiveKitAdapter._is_livekit_span(unrelated)


def test_real_otel_provider_path_preserves_native_identity_without_app_spans() -> None:
    resources = pytest.importorskip("opentelemetry.sdk.resources")
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")

    adapter = LiveKitAdapter(recorder())
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": "livekit-test"})
    )
    adapter.attach_span_processor(provider)
    try:
        app_tracer = provider.get_tracer("application")
        with app_tracer.start_as_current_span("llm_node"):
            pass

        livekit_tracer = provider.get_tracer(
            "livekit-agents",
            "1.5.3",
            schema_url="https://opentelemetry.io/schemas/1.30.0",
        )
        with livekit_tracer.start_as_current_span(
            "llm_node", attributes={"gen_ai.request.model": "test-model"}
        ) as span:
            expected_trace_id = f"{span.get_span_context().trace_id:032x}"
            expected_span_id = f"{span.get_span_context().span_id:016x}"

        bundle = adapter.recorder.close()
        assert len(bundle.profile.operations) == 1
        operation = bundle.profile.operations[0]
        assert operation.trace_id == expected_trace_id
        assert operation.span_id == expected_span_id
        assert operation.instrumentation_scope_name == "livekit-agents"
        assert operation.resource["service.name"] == "livekit-test"
        assert validate_incident(bundle).ok
    finally:
        provider.shutdown()
