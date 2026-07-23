"""Generate deterministic, valid incident artifacts for the M1 fault corpus."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from earshot.codec import encode_incident_json  # noqa: E402
from earshot.contract import (  # noqa: E402
    Adapter,
    AudioStream,
    BundleManifest,
    CaptureClassPolicy,
    CausalLink,
    ClockDomain,
    Coverage,
    Event,
    Evidence,
    IncidentBundle,
    IncidentProfile,
    Omission,
    Operation,
    Participant,
    PrivacyManifest,
    Producer,
    QualityMeasurement,
    QualitySample,
    Session,
    TimePoint,
    TimeRange,
)

OUTPUT = ROOT / "fixtures" / "faults"
WALL_ORIGIN = 1_800_000_000_000_000_000
CLOCK_DOMAIN = "fault-fixture-clock"
TRACE_ID = "a" * 32


def point(milliseconds: int) -> TimePoint:
    nanoseconds = milliseconds * 1_000_000
    return TimePoint(
        source_time_unix_nano=str(WALL_ORIGIN + nanoseconds),
        monotonic_time_nano=str(nanoseconds),
        clock_domain_id=CLOCK_DOMAIN,
    )


def evidence(
    source: str = "fixture_runtime",
    method: str = "deterministic_fault_injection",
    *,
    availability: str = "available",
) -> Evidence:
    return Evidence(
        source=source,
        observer="test_harness",
        method=method,
        method_version="1",
        confidence="measured",
        availability=availability,
    )


def operation(
    operation_id: str,
    name: str,
    start_ms: int,
    end_ms: int,
    *,
    status: str = "ok",
    stream_id: str | None = None,
    participant_id: str | None = None,
    span_digit: str = "1",
    links: tuple[CausalLink, ...] = (),
) -> Operation:
    if participant_id is None:
        if stream_id == "stream-input":
            participant_id = "participant-user"
        elif stream_id == "stream-output":
            participant_id = "participant-agent"
    return Operation(
        operation_id=operation_id,
        session_id="fixture-session",
        operation_name=name,
        status=status,
        started_at=point(start_ms),
        ended_at=point(end_ms),
        participant_id=participant_id,
        stream_id=stream_id,
        turn_id="turn-1",
        trace_id=TRACE_ID,
        span_id=span_digit * 16,
        parent_scope="external",
        links=links,
        evidence=evidence(),
    )


def event(
    event_id: str,
    name: str,
    milliseconds: int,
    *,
    operation_id: str | None = None,
    source: str = "fixture_runtime",
) -> Event:
    return Event(
        event_id=event_id,
        session_id="fixture-session",
        event_name=name,
        time=point(milliseconds),
        operation_id=operation_id,
        turn_id="turn-1",
        evidence=evidence(source),
    )


def profile(
    scenario_id: str,
    *,
    operations: tuple[Operation, ...] = (),
    events: tuple[Event, ...] = (),
    quality_samples: tuple[QualitySample, ...] = (),
    coverage: tuple[Coverage, ...] = (),
    omissions: tuple[Omission, ...] = (),
    participants: tuple[Participant, ...] | None = None,
    audio_streams: tuple[AudioStream, ...] | None = None,
) -> IncidentBundle:
    policies = tuple(
        CaptureClassPolicy(
            capture_class=name,
            decision="allow" if name == "metadata" else "deny",
            captured=name == "metadata",
        )
        for name in (
            "metadata",
            "extension_payload",
            "transcript",
            "audio",
            "tool_payload",
            "model_payload",
            "diagnostic_payload",
            "identity",
            "raw_otlp",
        )
    )
    return IncidentBundle(
        profile=IncidentProfile(
            manifest=BundleManifest(
                bundle_id=f"fault-{scenario_id}",
                session_id="fixture-session",
                created_at_unix_nano=str(WALL_ORIGIN),
                producer=Producer(name="earshot-fault-corpus", version="1.0.0"),
                adapters=(
                    Adapter(
                        name="earshot.fixture",
                        version="1.0.0",
                        framework="deterministic_harness",
                    ),
                ),
                attributes={"earshot.framework.name": "deterministic_harness"},
            ),
            session=Session(
                session_id="fixture-session",
                status="completed",
                started_at=point(0),
                ended_at=point(5_000),
            ),
            privacy=PrivacyManifest(
                policy_id="fault-corpus-metadata-only",
                policy_version="1",
                capture_classes=policies,
                omissions=omissions,
            ),
            participants=participants
            if participants is not None
            else (
                Participant(
                    participant_id="participant-user",
                    session_id="fixture-session",
                    role="user",
                    endpoint_kind="browser",
                ),
                Participant(
                    participant_id="participant-agent",
                    session_id="fixture-session",
                    role="agent",
                    endpoint_kind="server",
                ),
            ),
            audio_streams=audio_streams
            if audio_streams is not None
            else (
                AudioStream(
                    stream_id="stream-input",
                    session_id="fixture-session",
                    participant_id="participant-user",
                    direction="input",
                ),
                AudioStream(
                    stream_id="stream-output",
                    session_id="fixture-session",
                    participant_id="participant-agent",
                    direction="output",
                ),
            ),
            clock_domains=(
                ClockDomain(
                    clock_domain_id=CLOCK_DOMAIN,
                    kind="process_monotonic",
                    observer="fixture_harness",
                    monotonic_origin_nano="0",
                    wall_origin_unix_nano=str(WALL_ORIGIN),
                    uncertainty_nano="0",
                ),
            ),
            coverage=coverage,
            operations=operations,
            events=events,
            quality_samples=quality_samples,
        )
    )


def cascaded_delay_profile(
    scenario_id: str,
    *,
    stt: tuple[int, int],
    llm: tuple[int, int],
    tts: tuple[int, int],
) -> IncidentBundle:
    send = (tts[1], tts[1] + 50)
    receive = (send[1], send[1] + 50)
    render = (receive[1], receive[1] + 100)
    return profile(
        scenario_id,
        operations=(
            operation("op-stt", "stt", *stt, stream_id="stream-input", span_digit="1"),
            operation("op-llm", "llm", *llm, span_digit="2"),
            operation("op-tts", "tts", *tts, stream_id="stream-output", span_digit="3"),
            operation(
                "op-send",
                "transport_send",
                *send,
                stream_id="stream-output",
                span_digit="4",
            ),
            operation(
                "op-receive",
                "transport_receive",
                *receive,
                stream_id="stream-output",
                span_digit="5",
            ),
            operation(
                "op-render",
                "render",
                *render,
                stream_id="stream-output",
                span_digit="6",
            ),
        ),
        events=(
            event(
                "event-first-token",
                "earshot.response.first_token",
                llm[1] - 50,
                operation_id="op-llm",
            ),
            event(
                "event-first-audio",
                "earshot.response.first_audio_generated",
                tts[1] - 50,
                operation_id="op-tts",
            ),
            event(
                "event-first-byte",
                "earshot.audio.first_byte_sent",
                send[0] + 20,
                operation_id="op-send",
            ),
            event(
                "event-first-packet",
                "earshot.audio.first_packet_received",
                receive[0] + 20,
                operation_id="op-receive",
            ),
            event(
                "event-render-started",
                "earshot.audio.render.started",
                render[0] + 20,
                operation_id="op-render",
            ),
        ),
    )


def scenarios() -> dict[str, IncidentBundle]:
    webrtc_quality = QualitySample(
        sample_id="quality-webrtc",
        session_id="fixture-session",
        quality_kind="transport.quality",
        sample_window=TimeRange(start=point(1_000), end=point(2_000)),
        measurements=(
            QualityMeasurement(
                name="packet_loss_ratio",
                value=0.18,
                unit="1",
                aggregation="delta",
                raw_counter=18,
            ),
            QualityMeasurement(name="jitter", value=42, unit="ms"),
            QualityMeasurement(name="round_trip_time", value=180, unit="ms"),
        ),
        evidence=evidence("webrtc_stats", "RTCPeerConnection.getStats"),
        participant_id="participant-user",
        stream_id="stream-input",
    )
    retry_link = CausalLink(
        relationship="retries",
        target_scope="internal",
        target_operation_id="op-tool-attempt-1",
    )
    retry_downstream_link = CausalLink(
        relationship="consumes",
        target_scope="internal",
        target_operation_id="op-tool-attempt-2",
    )
    duplicate_message_link = CausalLink(
        relationship="duplicates",
        target_scope="internal",
        target_operation_id="op-ws-message-original",
    )
    out_of_order_link = CausalLink(
        relationship="supersedes",
        target_scope="internal",
        target_operation_id="op-ws-message-original",
    )
    privacy_omission = Omission(
        omission_id="omission-transcript",
        capture_class="transcript",
        reason="capture_class_disabled",
        count=1,
        digest="d" * 64,
        attributes={"field_key_sha256": "e" * 64},
    )
    telephony_participants = (
        Participant(
            participant_id="participant-caller",
            session_id="fixture-session",
            role="user",
            endpoint_kind="pstn",
        ),
        Participant(
            participant_id="participant-bot",
            session_id="fixture-session",
            role="agent",
            endpoint_kind="sip",
        ),
        Participant(
            participant_id="participant-human",
            session_id="fixture-session",
            role="human_operator",
            endpoint_kind="pstn",
        ),
    )
    telephony_streams = (
        AudioStream(
            stream_id="stream-caller-inbound",
            session_id="fixture-session",
            participant_id="participant-caller",
            direction="input",
            transport_ref="sip-leg-inbound",
        ),
        AudioStream(
            stream_id="stream-bot-outbound",
            session_id="fixture-session",
            participant_id="participant-bot",
            direction="output",
            transport_ref="sip-leg-bot",
        ),
        AudioStream(
            stream_id="stream-human-outbound",
            session_id="fixture-session",
            participant_id="participant-human",
            direction="output",
            transport_ref="sip-leg-human",
        ),
    )
    handoff_link = CausalLink(
        relationship="handoff",
        target_scope="internal",
        target_operation_id="op-bot-leg",
    )
    return {
        "fast_endpointing": profile(
            "fast-endpointing",
            operations=(
                operation("op-vad", "vad", 100, 300, stream_id="stream-input"),
                operation(
                    "op-turn",
                    "turn_detection",
                    300,
                    380,
                    stream_id="stream-input",
                    span_digit="2",
                ),
            ),
            events=(
                event("event-speech-ended", "earshot.speech.ended", 300, operation_id="op-vad"),
                event(
                    "event-turn-committed",
                    "earshot.turn.committed",
                    380,
                    operation_id="op-turn",
                ),
            ),
        ),
        "slow_endpointing": profile(
            "slow-endpointing",
            operations=(
                operation("op-vad", "vad", 100, 300, stream_id="stream-input"),
                operation(
                    "op-turn",
                    "turn_detection",
                    300,
                    1_600,
                    stream_id="stream-input",
                    span_digit="2",
                ),
            ),
            events=(
                event("event-speech-ended", "earshot.speech.ended", 300, operation_id="op-vad"),
                event(
                    "event-turn-committed",
                    "earshot.turn.committed",
                    1_600,
                    operation_id="op-turn",
                ),
            ),
        ),
        "barge_in": profile(
            "barge-in",
            operations=(
                operation("op-agent", "agent", 400, 960, status="cancelled", span_digit="1"),
                operation(
                    "op-tts",
                    "tts",
                    500,
                    980,
                    status="cancelled",
                    stream_id="stream-output",
                    span_digit="2",
                ),
                operation(
                    "op-render",
                    "render",
                    600,
                    1_000,
                    status="cancelled",
                    stream_id="stream-output",
                    span_digit="3",
                ),
            ),
            events=(
                event("event-interruption-detected", "earshot.interruption.detected", 900),
                event("event-interruption-accepted", "earshot.interruption.accepted", 940),
                event(
                    "event-model-cancelled",
                    "earshot.model.cancelled",
                    960,
                    operation_id="op-agent",
                ),
                event(
                    "event-audio-discarded",
                    "earshot.audio.queued.discarded",
                    980,
                    operation_id="op-tts",
                ),
                event(
                    "event-render-stopped",
                    "earshot.audio.render.stopped",
                    1_000,
                    operation_id="op-render",
                ),
            ),
        ),
        "stt_delay": cascaded_delay_profile(
            "stt-delay",
            stt=(300, 2_400),
            llm=(2_400, 2_700),
            tts=(2_700, 3_000),
        ),
        "llm_delay": cascaded_delay_profile(
            "llm-delay",
            stt=(300, 600),
            llm=(600, 2_800),
            tts=(2_800, 3_100),
        ),
        "tts_delay": cascaded_delay_profile(
            "tts-delay",
            stt=(300, 600),
            llm=(600, 900),
            tts=(900, 3_400),
        ),
        "tool_timeout_retry": profile(
            "tool-timeout-retry",
            operations=(
                operation("op-tool-attempt-1", "tool", 600, 1_600, status="timeout"),
                operation(
                    "op-tool-attempt-2",
                    "tool",
                    1_700,
                    2_000,
                    span_digit="2",
                    links=(retry_link,),
                ),
                operation(
                    "op-downstream-agent",
                    "agent",
                    2_050,
                    2_300,
                    span_digit="3",
                    links=(retry_downstream_link,),
                ),
            ),
            events=(
                event(
                    "event-retry-resumed",
                    "earshot.tool.retry.downstream_resumed",
                    2_050,
                    operation_id="op-downstream-agent",
                ),
            ),
        ),
        "webrtc_degradation": profile(
            "webrtc-degradation",
            quality_samples=(webrtc_quality,),
        ),
        "websocket_reconnect": profile(
            "websocket-reconnect",
            operations=(
                operation(
                    "op-ws-message-original",
                    "transport_receive",
                    900,
                    1_000,
                    stream_id="stream-output",
                    span_digit="1",
                ),
                operation(
                    "op-ws-message-duplicate",
                    "transport_receive",
                    1_050,
                    1_100,
                    stream_id="stream-output",
                    span_digit="2",
                    links=(duplicate_message_link,),
                ),
                operation(
                    "op-ws-message-out-of-order",
                    "transport_receive",
                    1_100,
                    1_150,
                    stream_id="stream-output",
                    span_digit="3",
                    links=(out_of_order_link,),
                ),
            ),
            events=(
                event(
                    "event-transport-reconnecting",
                    "earshot.transport.reconnecting",
                    1_200,
                    source="websocket",
                ),
                event(
                    "event-message-duplicate",
                    "earshot.transport.message.duplicate",
                    1_250,
                    operation_id="op-ws-message-duplicate",
                    source="websocket",
                ),
                event(
                    "event-message-out-of-order",
                    "earshot.transport.message.out_of_order",
                    1_300,
                    operation_id="op-ws-message-out-of-order",
                    source="websocket",
                ),
            ),
        ),
        "device_unavailable": profile(
            "device-unavailable",
            events=(
                event(
                    "event-permission-denied",
                    "earshot.device.permission_denied",
                    100,
                    source="client_device",
                ),
                event(
                    "event-audio-context-suspended",
                    "earshot.device.audio_context_suspended",
                    150,
                    source="client_device",
                ),
            ),
            coverage=(
                Coverage(
                    signal="device.microphone",
                    availability="not_observed",
                    reason="permission_denied",
                ),
                Coverage(
                    signal="capture",
                    availability="not_observed",
                    reason="device_unavailable",
                ),
                Coverage(
                    signal="client.render",
                    availability="not_observed",
                    reason="app_backgrounded",
                ),
            ),
        ),
        "native_s2s_interruption": profile(
            "native-s2s-interruption",
            operations=(operation("op-native-agent", "agent", 500, 2_000),),
            events=(event("event-native-interruption", "earshot.interruption.accepted", 1_100),),
            coverage=(
                Coverage(
                    signal="render",
                    availability="not_observed",
                    reason="server_native_s2s_no_client_render_observer",
                ),
                Coverage(
                    signal="stt.llm.tts",
                    availability="not_applicable",
                    reason="native_s2s_no_cascaded_stages",
                ),
            ),
        ),
        "render_delay": profile(
            "render-delay",
            operations=(
                operation("op-vad", "vad", 100, 300, stream_id="stream-input", span_digit="1"),
                operation(
                    "op-turn",
                    "turn_detection",
                    300,
                    380,
                    stream_id="stream-input",
                    span_digit="2",
                ),
                operation("op-llm", "llm", 380, 520, span_digit="3"),
                operation("op-tts", "tts", 520, 680, stream_id="stream-output", span_digit="4"),
                operation(
                    "op-send",
                    "transport_send",
                    680,
                    720,
                    stream_id="stream-output",
                    span_digit="5",
                ),
                operation(
                    "op-receive",
                    "transport_receive",
                    720,
                    760,
                    stream_id="stream-output",
                    span_digit="6",
                ),
                # Upstream stages finish quickly; audio is not rendered until
                # much later, isolating the delay to the render boundary.
                operation(
                    "op-render",
                    "render",
                    2_400,
                    2_600,
                    stream_id="stream-output",
                    span_digit="7",
                ),
            ),
            events=(
                event("event-speech-ended", "earshot.speech.ended", 300, operation_id="op-vad"),
                event(
                    "event-turn-committed",
                    "earshot.turn.committed",
                    380,
                    operation_id="op-turn",
                ),
                event(
                    "event-first-token",
                    "earshot.response.first_token",
                    500,
                    operation_id="op-llm",
                ),
                event(
                    "event-first-audio",
                    "earshot.response.first_audio_generated",
                    660,
                    operation_id="op-tts",
                ),
                event(
                    "event-first-byte",
                    "earshot.audio.first_byte_sent",
                    700,
                    operation_id="op-send",
                ),
                event(
                    "event-first-packet",
                    "earshot.audio.first_packet_received",
                    740,
                    operation_id="op-receive",
                ),
                event(
                    "event-render-started",
                    "earshot.audio.render.started",
                    2_450,
                    operation_id="op-render",
                ),
            ),
        ),
        "false_interruption": profile(
            "false-interruption",
            operations=(operation("op-agent", "agent", 400, 2_000, span_digit="1"),),
            events=(
                # Detected but never accepted: the agent kept speaking because
                # the detector self-classified the interruption as false.
                event("event-interruption-detected", "earshot.interruption.detected", 900),
                event("event-interruption-ignored", "earshot.interruption.ignored", 940),
            ),
        ),
        "stale_buffer_playback": profile(
            "stale-buffer-playback",
            operations=(
                operation(
                    "op-render",
                    "render",
                    600,
                    1_000,
                    stream_id="stream-output",
                    span_digit="1",
                ),
            ),
            events=(
                event(
                    "event-render-stale",
                    "earshot.audio.render.stale",
                    800,
                    operation_id="op-render",
                ),
            ),
        ),
        "privacy_opt_out": profile(
            "privacy-opt-out",
            operations=(operation("op-metadata-only", "agent", 500, 900),),
            omissions=(privacy_omission,),
        ),
        "telephony_handoff": profile(
            "telephony-handoff",
            participants=telephony_participants,
            audio_streams=telephony_streams,
            operations=(
                operation(
                    "op-inbound-leg",
                    "transport_receive",
                    100,
                    1_800,
                    stream_id="stream-caller-inbound",
                    participant_id="participant-caller",
                    span_digit="5",
                ),
                operation(
                    "op-bot-leg",
                    "transport_send",
                    300,
                    900,
                    stream_id="stream-bot-outbound",
                    participant_id="participant-bot",
                    span_digit="6",
                ),
                operation(
                    "op-human-leg",
                    "transport_send",
                    950,
                    1_800,
                    stream_id="stream-human-outbound",
                    participant_id="participant-human",
                    span_digit="7",
                    links=(handoff_link,),
                ),
            ),
            events=(
                event(
                    "event-dtmf",
                    "earshot.telephony.dtmf.received",
                    500,
                    operation_id="op-inbound-leg",
                    source="carrier_gateway",
                ),
                event(
                    "event-voicemail",
                    "earshot.telephony.voicemail.detected",
                    700,
                    operation_id="op-bot-leg",
                    source="carrier_gateway",
                ),
            ),
        ),
    }


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for scenario_id, bundle in scenarios().items():
        path = OUTPUT / f"{scenario_id}.incident.json"
        path.write_bytes(encode_incident_json(bundle, indent=2) + b"\n")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
