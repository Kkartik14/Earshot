from __future__ import annotations

import builtins
import json
from types import SimpleNamespace

import pytest

from earshot.adapters import LiveKitAdapter, routing
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
    assert operation.instrumentation_scope_attributes == {"earshot.framework.name": "livekit"}
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
                schema_url=(f"https://opentelemetry.io:{SECRET_SENTINEL}/schemas/1.30.0"),
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


@pytest.mark.parametrize(
    "metric_type",
    [
        "vadmetrics",
        "vad_metrics",
        "eoumetrics",
        "eou_metrics",
        "eotinferencemetrics",
        "eot_inference_metrics",
        "sttmetrics",
        "stt_metrics",
        "llmmetrics",
        "llm_metrics",
        "realtimemodelmetrics",
        "realtime_model_metrics",
        "ttsmetrics",
        "tts_metrics",
        "interruptionmetrics",
        "interruption_metrics",
        "avatarmetrics",
        "avatar_metrics",
    ],
)
def test_supported_livekit_metric_alias_keeps_readable_provenance(
    metric_type: str,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": metric_type,
            "request_id": f"request-{metric_type}",
            "timestamp": 1_800_000_000,
            "duration": 0.1,
            "idle_time": 0.2,
        }
    )
    bundle = adapter.recorder.close()
    evidence = (
        bundle.profile.operations[0].evidence
        if bundle.profile.operations
        else bundle.profile.quality_samples[0].evidence
    )
    assert evidence is not None
    assert evidence.source_field == metric_type


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


def test_attached_adaptive_interruption_surfaces_author_one_fact_per_overlap() -> None:
    adapter = LiveKitAdapter(recorder(), framework_version="1.6.5")
    listeners: dict[str, object] = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    adapter.attach_session_listeners(Session())

    first_metric = {
        "type": "interruption_metrics",
        # LiveKit creates this timestamp separately after consuming the overlap
        # event; it is intentionally not equal to detected_at.
        "timestamp": 1_800_000_000.03,
        "total_duration": 0.05,
        "prediction_duration": 0.02,
        "detection_delay": 0.1,
        "num_interruptions": 1,
        "num_backchannels": 0,
        "num_requests": 2,
    }
    first_overlap = {
        "type": "overlapping_speech",
        "created_at": 1_800_000_000.02,
        "detected_at": 1_800_000_000,
        "is_interruption": True,
        "total_duration": 0.05,
        "prediction_duration": 0.02,
        "detection_delay": 0.1,
        "probability": 0.9,
        "num_requests": 2,
    }
    second_metric = {**first_metric, "timestamp": 1_800_000_001.03}
    second_overlap = {
        **first_overlap,
        "created_at": 1_800_000_001.02,
        "detected_at": 1_800_000_001,
    }

    # Exercise both possible cross-emitter callback orders. Equal detector values
    # must not collapse genuinely separate interruptions one second apart.
    listeners["metrics_collected"]({"metrics": first_metric})  # type: ignore[operator]
    listeners["overlapping_speech"](first_overlap)  # type: ignore[operator]
    listeners["overlapping_speech"](second_overlap)  # type: ignore[operator]
    listeners["metrics_collected"]({"metrics": second_metric})  # type: ignore[operator]

    bundle = adapter.recorder.close()
    assert len(bundle.profile.operations) == 2
    assert [event.event_name for event in bundle.profile.events] == [
        "earshot.interruption.detected",
        "earshot.interruption.detected",
    ]
    assert [event.evidence.source_field for event in bundle.profile.events if event.evidence] == [
        "overlapping_speech",
        "overlapping_speech",
    ]
    assert [event.time.source_time_unix_nano for event in bundle.profile.events] == [
        "1800000000000000000",
        "1800000001000000000",
    ]
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    "invalid_count",
    [True, -1, 1.5, float("nan"), float("inf"), 9_007_199_254_740_992],
)
def test_interruption_session_request_count_requires_an_i_json_integer(
    invalid_count: object,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_interruption_event(
        {
            "type": "overlapping_speech",
            "detected_at": 1_800_000_000,
            "is_interruption": False,
            "num_requests": invalid_count,
        }
    )
    event = adapter.recorder.close().profile.events[0]
    assert "earshot.metric.interruption.request_count" not in event.attributes


@pytest.mark.parametrize(
    "source_key",
    [
        "total_duration",
        "prediction_duration",
        "detection_delay",
        "lk.interruption.total_duration",
        "lk.interruption.prediction_duration",
        "lk.interruption.detection_delay",
    ],
)
@pytest.mark.parametrize("invalid_duration", [True, -0.1, float("nan"), float("inf")])
def test_interruption_durations_require_non_negative_finite_numbers(
    source_key: str,
    invalid_duration: object,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_interruption_event(
        {
            "type": "overlapping_speech",
            "detected_at": 1_800_000_000,
            "is_interruption": False,
            source_key: invalid_duration,
        }
    )
    event = adapter.recorder.close().profile.events[0]
    assert all(
        name not in event.attributes
        for name in {
            "earshot.duration.interruption.total_seconds",
            "earshot.duration.interruption.prediction_seconds",
            "earshot.duration.interruption.detection_delay_seconds",
        }
    )


@pytest.mark.parametrize("source_key", ["probability", "lk.interruption.probability"])
@pytest.mark.parametrize("invalid_probability", [True, -0.01, 1.01, float("nan"), float("inf")])
def test_interruption_probability_requires_a_unit_interval_number(
    source_key: str,
    invalid_probability: object,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_interruption_event(
        {
            "type": "overlapping_speech",
            "detected_at": 1_800_000_000,
            "is_interruption": False,
            source_key: invalid_probability,
        }
    )
    event = adapter.recorder.close().profile.events[0]
    assert "earshot.metric.interruption.probability" not in event.attributes


@pytest.mark.parametrize("probability", [0, 0.5, 1])
def test_interruption_probability_keeps_unit_interval_boundaries(
    probability: object,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_interruption_event(
        {
            "type": "overlapping_speech",
            "detected_at": 1_800_000_000,
            "is_interruption": False,
            "probability": probability,
        }
    )
    event = adapter.recorder.close().profile.events[0]
    assert event.attributes["earshot.metric.interruption.probability"] == probability


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


def test_per_request_livekit_usage_and_character_counts_sum_as_deltas() -> None:
    adapter = LiveKitAdapter(recorder())
    for index, token_count in enumerate((3, 4), start=1):
        adapter.consume_metric(
            {
                "type": "llm_metrics",
                "request_id": f"llm-{index}",
                "speech_id": "speech-deltas",
                "duration": 0.1,
                "prompt_tokens": token_count,
            },
            observed_at=point(1_800_000_000_000_000_000 + index),
        )
    for index, character_count in enumerate((5, 6), start=1):
        adapter.consume_metric(
            {
                "type": "tts_metrics",
                "request_id": f"tts-{index}",
                "speech_id": "speech-deltas",
                "duration": 0.1,
                "characters_count": character_count,
            },
            observed_at=point(1_800_000_000_100_000_000 + index),
        )

    bundle = adapter.recorder.close()
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    provider = analysis.projections.turns[0].metrics.provider_measurements
    assert provider["gen_ai.usage.input_tokens"].value == 7
    assert provider["gen_ai.usage.input_tokens"].basis == "provider_delta_sum"
    assert len(provider["gen_ai.usage.input_tokens"].evidence_ids) == 2
    assert provider["livekit.tts.character_count"].value == 11
    assert provider["livekit.tts.character_count"].basis == "provider_delta_sum"
    assert len(provider["livekit.tts.character_count"].evidence_ids) == 2
    assert validate_incident(bundle).ok


def test_realtime_nested_modality_and_cached_usage_are_retained_as_deltas() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": "realtime_model_metrics",
            "request_id": "realtime-details",
            "speech_id": "speech-realtime-details",
            "timestamp": 1_800_000_000,
            "duration": 0.2,
            "ttft": 0.05,
            "input_tokens": 19,
            "output_tokens": 11,
            "input_token_details": {
                "audio_tokens": 3,
                "text_tokens": 7,
                "image_tokens": 2,
                "cached_tokens": 5,
                "cached_tokens_details": {
                    "audio_tokens": 1,
                    "text_tokens": 3,
                    "image_tokens": 1,
                },
            },
            "output_token_details": {
                "audio_tokens": 4,
                "text_tokens": 6,
                "image_tokens": 1,
            },
        }
    )
    bundle = adapter.recorder.close()
    measurements = {item.name: item for item in bundle.profile.quality_samples[0].measurements}
    expected = {
        "gen_ai.usage.input_audio_tokens": 3,
        "gen_ai.usage.input_text_tokens": 7,
        "gen_ai.usage.input_image_tokens": 2,
        "gen_ai.usage.input_cached_tokens": 5,
        "gen_ai.usage.input_cached_audio_tokens": 1,
        "gen_ai.usage.input_cached_text_tokens": 3,
        "gen_ai.usage.input_cached_image_tokens": 1,
        "gen_ai.usage.output_audio_tokens": 4,
        "gen_ai.usage.output_text_tokens": 6,
        "gen_ai.usage.output_image_tokens": 1,
    }
    for name, expected_value in expected.items():
        assert measurements[name].value == expected_value
        assert measurements[name].unit == "count"
        assert measurements[name].aggregation == "delta"
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    "invalid_count",
    [True, -1, 1.5, float("nan"), float("inf"), 9_007_199_254_740_992],
)
def test_realtime_nested_usage_requires_i_json_counters(invalid_count: object) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": "realtime_model_metrics",
            "request_id": "realtime-invalid-details",
            "timestamp": 1_800_000_000,
            "duration": 0.2,
            "ttft": 0.05,
            "input_token_details": {
                "audio_tokens": invalid_count,
                "text_tokens": invalid_count,
                "image_tokens": invalid_count,
                "cached_tokens": invalid_count,
                "cached_tokens_details": {
                    "audio_tokens": invalid_count,
                    "text_tokens": invalid_count,
                    "image_tokens": invalid_count,
                },
            },
            "output_token_details": {
                "audio_tokens": invalid_count,
                "text_tokens": invalid_count,
                "image_tokens": invalid_count,
            },
        }
    )
    bundle = adapter.recorder.close()
    nested_names = {
        "gen_ai.usage.input_audio_tokens",
        "gen_ai.usage.input_text_tokens",
        "gen_ai.usage.input_image_tokens",
        "gen_ai.usage.input_cached_tokens",
        "gen_ai.usage.input_cached_audio_tokens",
        "gen_ai.usage.input_cached_text_tokens",
        "gen_ai.usage.input_cached_image_tokens",
        "gen_ai.usage.output_audio_tokens",
        "gen_ai.usage.output_text_tokens",
        "gen_ai.usage.output_image_tokens",
    }
    assert all(
        nested_names.isdisjoint(operation.attributes) for operation in bundle.profile.operations
    )
    assert all(
        nested_names.isdisjoint(item.name for item in sample.measurements)
        for sample in bundle.profile.quality_samples
    )
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("metric_type", "measurement_name"),
    [
        ("stt_metrics", "livekit.stt.connection_acquire_time"),
        ("tts_metrics", "livekit.tts.connection_acquire_time"),
        ("realtime_model_metrics", "livekit.realtime.connection_acquire_time"),
    ],
)
def test_livekit_stage_connection_acquisition_is_an_instant_measurement(
    metric_type: str,
    measurement_name: str,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": metric_type,
            "request_id": f"request-{metric_type}",
            "timestamp": 1_800_000_000,
            "duration": 0.2,
            "ttft": 0.05,
            "acquire_time": 0.03,
            "connection_reused": True,
        }
    )
    sample = adapter.recorder.close().profile.quality_samples[0]
    measurement = next(item for item in sample.measurements if item.name == measurement_name)
    assert measurement.value == 0.03
    assert measurement.unit == "s"
    assert measurement.aggregation == "instant"


@pytest.mark.parametrize(
    ("metric_type", "measurement_name"),
    [
        ("stt_metrics", "livekit.stt.connection_acquire_time"),
        ("tts_metrics", "livekit.tts.connection_acquire_time"),
        ("realtime_model_metrics", "livekit.realtime.connection_acquire_time"),
    ],
)
def test_livekit_zero_connection_acquisition_sentinel_is_omitted(
    metric_type: str,
    measurement_name: str,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": metric_type,
            "request_id": f"request-{metric_type}",
            "timestamp": 1_800_000_000,
            "duration": 0.2,
            "ttft": 0.05,
            "acquire_time": 0,
        }
    )
    sample = adapter.recorder.close().profile.quality_samples[0]
    assert measurement_name not in {item.name for item in sample.measurements}


def test_real_connection_only_realtime_metric_survives_dual_surface_ownership() -> None:
    pytest.importorskip("livekit.agents")
    from livekit.agents.metrics import RealtimeModelMetrics

    adapter = LiveKitAdapter(recorder(), framework_version="1.6.5")

    class Provider:
        def add_span_processor(self, processor: object) -> None:
            del processor

    listeners: dict[str, object] = {}

    class Session:
        def on(self, name: str):
            def register(callback):
                listeners[name] = callback

            return register

    adapter.attach_span_processor(Provider())
    adapter.attach_session_listeners(Session())
    metric = RealtimeModelMetrics(
        request_id="",
        timestamp=1_800_000_000,
        acquire_time=0.125,
        connection_reused=False,
        input_token_details=RealtimeModelMetrics.InputTokenDetails(),
        output_token_details=RealtimeModelMetrics.OutputTokenDetails(),
    )
    listeners["metrics_collected"]({"metrics": metric})  # type: ignore[operator]

    bundle = adapter.recorder.close()
    assert bundle.profile.operations == ()
    assert len(bundle.profile.quality_samples) == 1
    measurements = {item.name: item for item in bundle.profile.quality_samples[0].measurements}
    assert measurements["livekit.realtime.connection_acquire_time"].value == 0.125
    assert measurements["livekit.realtime.connection_acquire_time"].aggregation == "instant"
    assert not any(name.endswith("_tokens") for name in measurements)
    assert "livekit.realtime.session_duration" not in measurements
    assert not any(
        item.signal == "livekit.response.first_audio_generated" for item in bundle.profile.coverage
    )
    assert validate_incident(bundle).ok


def test_zero_acquire_connection_reuse_is_quality_only_not_an_stt_operation() -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(
        {
            "type": "stt_metrics",
            "request_id": "",
            "timestamp": 1_800_000_000,
            "duration": 0,
            "audio_duration": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "streamed": True,
            "acquire_time": 0,
            "connection_reused": True,
        }
    )

    bundle = adapter.recorder.close()
    assert bundle.profile.operations == ()
    assert len(bundle.profile.quality_samples) == 1
    measurements = {
        item.name: item.value for item in bundle.profile.quality_samples[0].measurements
    }
    assert measurements == {"earshot.metric.connection.reused": True}
    assert validate_incident(bundle).ok


@pytest.mark.parametrize(
    ("metric", "operation_name", "measurement_name", "expected_value"),
    [
        (
            {
                "type": "stt_metrics",
                "request_id": "",
                "timestamp": 1_800_000_000,
                "duration": 0.5,
                "audio_duration": 2.0,
                "input_tokens": 12,
                "output_tokens": 4,
                "streamed": True,
                "acquire_time": 0,
            },
            "stt",
            "livekit.stt.audio_duration",
            2.0,
        ),
        (
            {
                "type": "realtime_model_metrics",
                "request_id": "",
                "timestamp": 1_800_000_000,
                "duration": 0.5,
                "session_duration": 0.5,
                "ttft": 0.1,
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "acquire_time": 0,
            },
            "agent",
            "gen_ai.usage.input_tokens",
            12,
        ),
    ],
)
def test_empty_request_response_usage_is_not_connection_only(
    metric: dict[str, object],
    operation_name: str,
    measurement_name: str,
    expected_value: int | float,
) -> None:
    adapter = LiveKitAdapter(recorder())
    adapter.consume_metric(metric)

    bundle = adapter.recorder.close()
    assert [operation.operation_name for operation in bundle.profile.operations] == [operation_name]
    measurements = {
        item.name: item.value
        for sample in bundle.profile.quality_samples
        for item in sample.measurements
    }
    assert measurements[measurement_name] == expected_value
    assert validate_incident(bundle).ok


@pytest.mark.parametrize("invalid_count", [True, 1.5, 9_007_199_254_740_992])
def test_invalid_livekit_counters_are_omitted_and_cannot_author_events(
    invalid_count: object,
) -> None:
    adapter = LiveKitAdapter(recorder())
    common = {
        "speech_id": "speech-invalid-counters",
        "duration": 0.1,
    }
    adapter.consume_metric(
        {
            **common,
            "type": "llm_metrics",
            "request_id": "llm-invalid-counters",
            "prompt_tokens": invalid_count,
            "completion_tokens": invalid_count,
            "prompt_cached_tokens": invalid_count,
            "total_tokens": invalid_count,
        }
    )
    adapter.consume_metric(
        {
            **common,
            "type": "stt_metrics",
            "request_id": "stt-invalid-counters",
            "input_tokens": invalid_count,
            "output_tokens": invalid_count,
        }
    )
    adapter.consume_metric(
        {
            **common,
            "type": "tts_metrics",
            "request_id": "tts-invalid-counters",
            "characters_count": invalid_count,
            "input_tokens": invalid_count,
            "output_tokens": invalid_count,
        }
    )
    adapter.consume_metric(
        {
            **common,
            "type": "interruption_metrics",
            "request_id": "interruption-invalid-counters",
            "num_interruptions": invalid_count,
            "num_backchannels": invalid_count,
            "num_requests": invalid_count,
        }
    )
    adapter.consume_metric(
        {
            **common,
            "type": "eot_inference_metrics",
            "request_id": "eot-invalid-counters",
            "num_requests": invalid_count,
        }
    )

    bundle = adapter.recorder.close()
    counter_names = {
        "earshot.metric.interruption.backchannel_count",
        "earshot.metric.interruption.count",
        "earshot.metric.interruption.request_count",
        "earshot.metric.model.total_tokens",
        "earshot.metric.turn_detection.request_count",
        "gen_ai.usage.input_cached_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "livekit.stt.input_tokens",
        "livekit.stt.output_tokens",
        "livekit.tts.character_count",
        "livekit.tts.input_tokens",
        "livekit.tts.output_tokens",
    }
    assert all(
        counter_names.isdisjoint(operation.attributes) for operation in bundle.profile.operations
    )
    assert all(
        counter_names.isdisjoint(measurement.name for measurement in sample.measurements)
        for sample in bundle.profile.quality_samples
    )
    assert bundle.profile.events == ()
    assert validate_incident(bundle).ok


def test_stage_specific_livekit_usage_and_duration_windows_sum_without_conflation() -> None:
    adapter = LiveKitAdapter(recorder())
    observed = 1_800_000_000_000_000_000
    metrics = (
        ("llm_metrics", "llm-1", {"prompt_tokens": 3, "completion_tokens": 2}),
        ("llm_metrics", "llm-2", {"prompt_tokens": 4, "completion_tokens": 1}),
        (
            "realtime_model_metrics",
            "realtime-1",
            {"input_tokens": 5, "output_tokens": 2, "session_duration": 0.2},
        ),
        (
            "realtime_model_metrics",
            "realtime-2",
            {"input_tokens": 6, "output_tokens": 3, "session_duration": 0.3},
        ),
        (
            "stt_metrics",
            "stt-1",
            {"input_tokens": 7, "output_tokens": 2, "audio_duration": 0.4},
        ),
        (
            "stt_metrics",
            "stt-2",
            {"input_tokens": 8, "output_tokens": 3, "audio_duration": 0.6},
        ),
        (
            "tts_metrics",
            "tts-1",
            {
                "input_tokens": 9,
                "output_tokens": 4,
                "audio_duration": 1.5,
                "characters_count": 10,
            },
        ),
        (
            "tts_metrics",
            "tts-2",
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "audio_duration": 2.5,
                "characters_count": 11,
            },
        ),
    )
    for index, (metric_type, request_id, values) in enumerate(metrics, start=1):
        adapter.consume_metric(
            {
                "type": metric_type,
                "request_id": request_id,
                "speech_id": "speech-stage-deltas",
                "duration": 0.1,
                **values,
            },
            observed_at=point(observed + index),
        )

    bundle = adapter.recorder.close()
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000005000000000",
    )
    provider = analysis.projections.turns[0].metrics.provider_measurements
    expected = {
        # LLM and realtime-model usage share the canonical GenAI token namespace.
        "gen_ai.usage.input_tokens": (18, "count", 4),
        "gen_ai.usage.output_tokens": (8, "count", 4),
        "livekit.realtime.session_duration": (500, "ms", 2),
        "livekit.stt.audio_duration": (1_000, "ms", 2),
        "livekit.stt.input_tokens": (15, "count", 2),
        "livekit.stt.output_tokens": (5, "count", 2),
        "livekit.tts.audio_duration": (4_000, "ms", 2),
        "livekit.tts.character_count": (21, "count", 2),
        "livekit.tts.input_tokens": (19, "count", 2),
        "livekit.tts.output_tokens": (9, "count", 2),
    }
    for name, (expected_value, expected_unit, expected_evidence_count) in expected.items():
        measurement = provider[name]
        assert measurement.value == expected_value
        assert measurement.unit == expected_unit
        assert measurement.basis == "provider_delta_sum"
        assert len(measurement.evidence_ids) == expected_evidence_count
    assert "earshot.duration.audio_seconds" not in provider
    assert validate_incident(bundle).ok


def test_repeated_livekit_interruption_and_eot_counts_sum_as_deltas() -> None:
    adapter = LiveKitAdapter(recorder())
    observed = 1_800_000_000_000_000_000
    for index, (interruptions, backchannels, requests) in enumerate(
        ((1, 2, 3), (2, 1, 4)),
        start=1,
    ):
        adapter.consume_metric(
            {
                "type": "interruption_metrics",
                "request_id": f"interruption-{index}",
                "speech_id": "speech-count-deltas",
                "duration": 0.1,
                "num_interruptions": interruptions,
                "num_backchannels": backchannels,
                "num_requests": requests,
            },
            observed_at=point(observed + index),
        )
    for index, requests in enumerate((5, 6), start=1):
        adapter.consume_metric(
            {
                "type": "eot_inference_metrics",
                "request_id": f"eot-{index}",
                "speech_id": "speech-count-deltas",
                "duration": 0.1,
                "num_requests": requests,
            },
            observed_at=point(observed + 100 + index),
        )

    bundle = adapter.recorder.close()
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000005000000000",
    )
    provider = analysis.projections.turns[0].metrics.provider_measurements
    expected = {
        "earshot.metric.interruption.count": 3,
        "earshot.metric.interruption.backchannel_count": 3,
        "earshot.metric.interruption.request_count": 7,
        "earshot.metric.turn_detection.request_count": 11,
    }
    for name, expected_value in expected.items():
        measurement = provider[name]
        assert measurement.value == expected_value
        assert measurement.unit == "count"
        assert measurement.basis == "provider_delta_sum"
        assert len(measurement.evidence_ids) == 2
    assert [event.event_name for event in bundle.profile.events] == [
        "earshot.interruption.detected",
        "earshot.interruption.detected",
    ]
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
    assert e2e_sample.instrumentation_scope_attributes == {"earshot.framework.name": "livekit"}
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


def test_span_processor_attach_is_additive_and_validates_provider() -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[object] = []

        def add_span_processor(self, processor: object) -> None:
            self.processors.append(processor)

    provider = Provider()
    handle = LiveKitAdapter(recorder()).attach_span_processor(provider)
    assert isinstance(handle, routing.RoutingHandle)
    # Additive: exactly one shared router processor is installed on the provider.
    assert len(provider.processors) == 1
    # A second concurrent session reuses the one processor, never adds another.
    LiveKitAdapter(recorder()).attach_span_processor(provider)
    assert len(provider.processors) == 1
    with pytest.raises(TypeError, match="does not support"):
        LiveKitAdapter(recorder()).attach_span_processor(object())


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
