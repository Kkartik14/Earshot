from __future__ import annotations

import hashlib

from earshot.contract import (
    Adapter,
    AudioFormat,
    AudioStream,
    BundleManifest,
    CaptureClassPolicy,
    ClockDomain,
    Coverage,
    Event,
    Evidence,
    IncidentBundle,
    IncidentProfile,
    Operation,
    Participant,
    PrivacyManifest,
    Producer,
    RawOtlpChunk,
    Session,
    TimePoint,
    TimeRange,
)

TRACE_ID = "1" * 32
ROOT_SPAN_ID = "1" * 16
LLM_SPAN_ID = "2" * 16
TTS_SPAN_ID = "3" * 16
SEND_SPAN_ID = "4" * 16
RENDER_SPAN_ID = "5" * 16
SECRET_SENTINEL = "EARSHOT_TEST_SECRET_7be9d891"


def point(
    nano: int,
    *,
    domain: str = "server-clock",
    wall_origin: int = 1_800_000_000_000_000_000,
    uncertainty: int = 0,
) -> TimePoint:
    return TimePoint(
        source_time_unix_nano=str(wall_origin + nano),
        observed_time_unix_nano=str(wall_origin + nano + 10),
        monotonic_time_nano=str(nano),
        clock_domain_id=domain,
        uncertainty_nano=str(uncertainty),
    )


def evidence(
    *,
    source: str = "framework_otel",
    observer: str = "server",
    method: str = "native_span",
    confidence: str = "measured",
    availability: str = "available",
    start: int | None = None,
    end: int | None = None,
    domain: str = "server-clock",
) -> Evidence:
    window = None
    if start is not None and end is not None:
        window = TimeRange(start=point(start, domain=domain), end=point(end, domain=domain))
    return Evidence(
        source=source,
        observer=observer,
        method=method,
        confidence=confidence,
        availability=availability,
        sample_window=window,
    )


def _event(
    event_id: str,
    name: str,
    nano: int,
    *,
    operation_id: str | None = None,
    turn_id: str = "turn-1",
    provenance: Evidence | None = None,
    domain: str = "server-clock",
) -> Event:
    output_event = any(token in name for token in ("first_audio", "first_byte_sent", "render"))
    return Event(
        event_id=event_id,
        session_id="session-1",
        event_name=name,
        time=point(nano, domain=domain),
        operation_id=operation_id,
        participant_id="participant-agent" if output_event else "participant-user",
        stream_id="stream-output" if output_event else "stream-input",
        turn_id=turn_id,
        evidence=provenance,
    )


def make_valid_bundle(
    *,
    bundle_id: str = "bundle-1",
    session_id: str = "session-1",
    include_raw_otlp: bool = True,
    include_render: bool = True,
) -> IncidentBundle:
    metadata = CaptureClassPolicy(capture_class="metadata", decision="allow", captured=True)
    denied = tuple(
        CaptureClassPolicy(capture_class=name, decision="deny", captured=False)
        for name in (
            "extension_payload",
            "transcript",
            "audio",
            "tool_payload",
            "model_payload",
            "diagnostic_payload",
        )
    )
    raw_otlp = CaptureClassPolicy(
        capture_class="raw_otlp",
        decision="allow" if include_raw_otlp else "deny",
        captured=include_raw_otlp,
    )

    operations = [
        Operation(
            operation_id="op-turn",
            session_id=session_id,
            operation_name="turn_detection",
            status="ok",
            started_at=point(900_000_000),
            ended_at=point(1_000_000_000),
            participant_id="participant-user",
            stream_id="stream-input",
            turn_id="turn-1",
            trace_id=TRACE_ID,
            span_id=ROOT_SPAN_ID,
            parent_scope="external",
            resource={"service.name": "fixture-voice-service"},
            resource_schema_url="https://opentelemetry.io/schemas/1.29.0",
            instrumentation_scope_name="earshot.fixture",
            instrumentation_scope_version="1.0.0",
            instrumentation_scope_attributes={"earshot.framework.name": "fixture"},
            schema_url="https://opentelemetry.io/schemas/1.30.0",
            evidence=evidence(source="pipecat", method="native_otel"),
            attributes={"earshot.turn.id": "turn-1"},
        ),
        Operation(
            operation_id="op-llm",
            session_id=session_id,
            operation_name="llm",
            status="ok",
            started_at=point(1_050_000_000),
            ended_at=point(1_300_000_000),
            turn_id="turn-1",
            trace_id=TRACE_ID,
            span_id=LLM_SPAN_ID,
            parent_span_id=ROOT_SPAN_ID,
            parent_scope="internal",
            evidence=evidence(source="pipecat", method="native_otel"),
            attributes={"gen_ai.request.model": "test-model"},
        ),
        Operation(
            operation_id="op-tts",
            session_id=session_id,
            operation_name="tts",
            status="ok",
            started_at=point(1_350_000_000),
            ended_at=point(1_500_000_000),
            stream_id="stream-output",
            turn_id="turn-1",
            trace_id=TRACE_ID,
            span_id=TTS_SPAN_ID,
            parent_span_id=LLM_SPAN_ID,
            parent_scope="internal",
            evidence=evidence(source="pipecat", method="native_otel"),
        ),
        Operation(
            operation_id="op-send",
            session_id=session_id,
            operation_name="transport_send",
            status="ok",
            started_at=point(1_510_000_000),
            ended_at=point(1_550_000_000),
            stream_id="stream-output",
            turn_id="turn-1",
            trace_id=TRACE_ID,
            span_id=SEND_SPAN_ID,
            parent_span_id=TTS_SPAN_ID,
            parent_scope="internal",
            evidence=evidence(source="websocket", method="write_callback"),
        ),
    ]
    if include_render:
        operations.append(
            Operation(
                operation_id="op-render",
                session_id=session_id,
                operation_name="render",
                status="ok",
                started_at=point(1_700_000_000),
                ended_at=point(1_900_000_000),
                stream_id="stream-output",
                turn_id="turn-1",
                trace_id=TRACE_ID,
                span_id=RENDER_SPAN_ID,
                parent_span_id=SEND_SPAN_ID,
                parent_scope="internal",
                evidence=evidence(
                    source="web_audio",
                    observer="browser",
                    method="getOutputTimestamp",
                    confidence="estimated",
                ),
            )
        )

    events = [
        _event("evt-speech-end", "earshot.speech.ended", 950_000_000),
        _event("evt-turn", "earshot.turn.committed", 1_000_000_000, operation_id="op-turn"),
        _event("evt-token", "earshot.response.first_token", 1_150_000_000, operation_id="op-llm"),
        _event(
            "evt-generated",
            "earshot.response.first_audio_generated",
            1_400_000_000,
            operation_id="op-tts",
        ),
        _event(
            "evt-sent",
            "earshot.audio.first_byte_sent",
            1_520_000_000,
            operation_id="op-send",
            provenance=evidence(source="websocket", method="write_callback"),
        ),
    ]
    if include_render:
        events.append(
            _event(
                "evt-render",
                "earshot.audio.render.started",
                1_720_000_000,
                operation_id="op-render",
                provenance=evidence(
                    source="web_audio",
                    observer="browser",
                    method="getOutputTimestamp",
                    confidence="estimated",
                ),
            )
        )

    profile = IncidentProfile(
        manifest=BundleManifest(
            bundle_id=bundle_id,
            session_id=session_id,
            created_at_unix_nano="1800000000000000000",
            producer=Producer(name="earshot-tests", version="1.0.0"),
            adapters=(Adapter(name="pipecat", version="1", framework="pipecat"),),
        ),
        session=Session(
            session_id=session_id,
            status="completed",
            started_at=point(0),
            ended_at=point(2_000_000_000),
        ),
        privacy=PrivacyManifest(
            policy_id="test-metadata-only",
            policy_version="1",
            capture_classes=(metadata, *denied, raw_otlp),
        ),
        participants=(
            Participant(
                participant_id="participant-user",
                session_id=session_id,
                role="user",
                endpoint_kind="browser",
            ),
            Participant(
                participant_id="participant-agent",
                session_id=session_id,
                role="agent",
                endpoint_kind="server",
            ),
        ),
        audio_streams=(
            AudioStream(
                stream_id="stream-input",
                session_id=session_id,
                participant_id="participant-user",
                direction="input",
                format=AudioFormat(encoding="pcm_s16le", sample_rate_hz=16_000, channels=1),
            ),
            AudioStream(
                stream_id="stream-output",
                session_id=session_id,
                participant_id="participant-agent",
                direction="output",
                format=AudioFormat(encoding="pcm_s16le", sample_rate_hz=24_000, channels=1),
            ),
        ),
        clock_domains=(
            ClockDomain(
                clock_domain_id="server-clock",
                kind="process_monotonic",
                observer="test",
                monotonic_origin_nano="0",
                wall_origin_unix_nano="1800000000000000000",
                uncertainty_nano="0",
            ),
        ),
        coverage=(
            Coverage(
                signal="client.render",
                availability="available" if include_render else "not_observed",
                reason=None if include_render else "client_collector_absent",
            ),
        ),
        operations=tuple(operations),
        events=tuple(events),
    )

    chunks: tuple[RawOtlpChunk, ...] = ()
    if include_raw_otlp:
        payload = b"\x0a\x00"
        chunks = (
            RawOtlpChunk(
                chunk_id="chunk-traces",
                signal="traces",
                payload=payload,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )
    return IncidentBundle(profile=profile, raw_otlp_chunks=chunks)
