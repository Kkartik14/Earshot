"""ElevenLabs finalized webhook authentication and normalization."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any

from earshot.clock import ManualClock
from earshot.contract import (
    Adapter,
    Evidence,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from earshot.recorder import IncidentRecorder, RecorderConfig

from .types import (
    DeliveryPayloadError,
    DeliveryTrustError,
    ExternalIdentityInput,
    NormalizationContext,
    NormalizedProviderDelivery,
    RawProviderDelivery,
)

ADAPTER_VERSION = "1.0.0"
_HEX_PATTERN = re.compile(r"^[0-9a-f]+$")
_SESSION_STATUS = {
    "initiated": "in_progress",
    "in-progress": "in_progress",
    "processing": "processing",
    "done": "completed",
    "failed": "failed",
}


@dataclass(frozen=True, slots=True)
class _OtlpSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_nano: int
    end_nano: int
    status: str


def _header_values(delivery: RawProviderDelivery, name: bytes) -> tuple[bytes, ...]:
    lowered = name.lower()
    return tuple(value for key, value in delivery.headers if key.lower() == lowered)


class ElevenLabsConnectorAdapter:
    provider = "elevenlabs"
    normalizer_version = ADAPTER_VERSION

    def authenticate(
        self,
        delivery: RawProviderDelivery,
        secrets: tuple[str, ...],
        *,
        now_unix_seconds: int,
        tolerance_seconds: int,
    ) -> None:
        signature_headers = _header_values(delivery, b"elevenlabs-signature")
        if len(signature_headers) != 1 or not secrets:
            raise DeliveryTrustError
        try:
            pieces = [piece.strip() for piece in signature_headers[0].decode("ascii").split(",")]
            timestamps = [piece[2:] for piece in pieces if piece.startswith("t=")]
            signatures = [piece[3:] for piece in pieces if piece.startswith("v0=")]
            if (
                len(timestamps) != 1
                or not signatures
                or not timestamps[0].isdigit()
                or any(len(value) != 64 for value in signatures)
            ):
                raise ValueError
            timestamp = int(timestamps[0])
        except (UnicodeDecodeError, ValueError):
            raise DeliveryTrustError from None
        if abs(now_unix_seconds - timestamp) > tolerance_seconds:
            raise DeliveryTrustError
        signed = str(timestamp).encode("ascii") + b"." + delivery.body
        valid = False
        for secret in secrets:
            expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            for supplied in signatures:
                valid = hmac.compare_digest(expected, supplied) or valid
        if not valid:
            raise DeliveryTrustError

    def normalize(
        self,
        payload: dict[str, Any],
        context: NormalizationContext,
    ) -> NormalizedProviderDelivery:
        event_type = payload.get("type")
        event_timestamp = payload.get("event_timestamp")
        data = payload.get("data")
        if (
            not isinstance(event_type, str)
            or not isinstance(event_timestamp, int)
            or isinstance(event_timestamp, bool)
            or event_timestamp < 0
            or not isinstance(data, dict)
        ):
            raise DeliveryPayloadError
        conversation_id = data.get("conversation_id")
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 512
        ):
            raise DeliveryPayloadError
        delivery_key = f"{event_type}:{conversation_id}:{event_timestamp}"
        identities = [ExternalIdentityInput("conversation_id", conversation_id)]
        agent_id = data.get("agent_id")
        if isinstance(agent_id, str) and agent_id and len(agent_id) <= 512:
            identities.append(ExternalIdentityInput("agent_id", agent_id))

        if event_type == "post_call_audio":
            return NormalizedProviderDelivery(
                delivery_key=delivery_key,
                event_type=event_type,
                disposition="ignore",
                external_identities=tuple(identities),
                bundle=None,
            )
        if event_type == "post_call_transcription_otel":
            bundle = self._otel_bundle(
                data,
                conversation_id=conversation_id,
                delivery_key=delivery_key,
                context=context,
            )
            return NormalizedProviderDelivery(
                delivery_key=delivery_key,
                event_type=event_type,
                disposition="incident",
                external_identities=tuple(identities),
                bundle=bundle,
            )
        if event_type != "post_call_transcription":
            raise DeliveryPayloadError

        bundle = self._transcription_bundle(
            data,
            event_timestamp=event_timestamp,
            conversation_id=conversation_id,
            delivery_key=delivery_key,
            context=context,
        )
        return NormalizedProviderDelivery(
            delivery_key=delivery_key,
            event_type=event_type,
            disposition="incident",
            external_identities=tuple(identities),
            bundle=bundle,
        )

    @staticmethod
    def _otel_bundle(
        data: dict[str, Any],
        *,
        conversation_id: str,
        delivery_key: str,
        context: NormalizationContext,
    ):
        traces = data.get("otlp_traces")
        resource_spans = traces.get("resourceSpans") if isinstance(traces, dict) else None
        if not isinstance(resource_spans, list) or len(resource_spans) > 256:
            raise DeliveryPayloadError

        spans: list[_OtlpSpan] = []
        identities: set[tuple[str, str]] = set()
        for resource_span in resource_spans:
            if not isinstance(resource_span, dict):
                raise DeliveryPayloadError
            scope_spans = resource_span.get("scopeSpans")
            if not isinstance(scope_spans, list) or len(scope_spans) > 1_024:
                raise DeliveryPayloadError
            for scope_span in scope_spans:
                if not isinstance(scope_span, dict):
                    raise DeliveryPayloadError
                raw_spans = scope_span.get("spans")
                if not isinstance(raw_spans, list):
                    raise DeliveryPayloadError
                for raw_span in raw_spans:
                    if len(spans) >= 100_000 or not isinstance(raw_span, dict):
                        raise DeliveryPayloadError
                    span = ElevenLabsConnectorAdapter._parse_otlp_span(raw_span)
                    identity = (span.trace_id, span.span_id)
                    if identity in identities:
                        raise DeliveryPayloadError
                    identities.add(identity)
                    spans.append(span)
        if not spans:
            raise DeliveryPayloadError

        earliest = min(span.start_nano for span in spans)
        latest = max(span.end_nano for span in spans)
        session_fingerprint = context.fingerprint("elevenlabs:conversation", conversation_id)
        delivery_fingerprint = context.fingerprint("elevenlabs:delivery", delivery_key)
        clock = ManualClock(wall=earliest, monotonic=0)
        recorder = IncidentRecorder(
            session_id=f"session-{session_fingerprint[:32]}",
            bundle_id=f"bundle-{delivery_fingerprint[:32]}",
            clock=clock,
            config=RecorderConfig(
                producer_name="earshot.connector.elevenlabs",
                producer_version=ADAPTER_VERSION,
                adapters=(
                    Adapter(
                        name="earshot.elevenlabs.otlp",
                        version=ADAPTER_VERSION,
                        framework="elevenlabs_agents",
                    ),
                ),
            ),
        )
        recorder.add_participant("participant-agent", role="agent", endpoint_kind="provider")
        recorder.record_coverage("provider.trace", "available")
        for signal in (
            "stt.final",
            "turn.end",
            "tts.first_audio",
            "client.render",
            "interruption",
        ):
            recorder.record_coverage(signal, "not_exposed", "provider_field_absent")

        agent_spans = sorted(
            (
                span
                for span in spans
                if span.name == "elevenlabs.recv.agent_response"
            ),
            key=lambda span: (span.start_nano, span.span_id),
        )
        turn_by_span = {
            (span.trace_id, span.span_id): f"turn-{index}"
            for index, span in enumerate(agent_spans)
        }
        for span in spans:
            turn_id = turn_by_span.get((span.trace_id, span.span_id))
            if turn_id is None and span.name.startswith("elevenlabs.tool."):
                turn_id = turn_by_span.get((span.trace_id, span.parent_span_id or ""))
            started = TimePoint(
                source_time_unix_nano=str(span.start_nano),
                monotonic_time_nano=str(span.start_nano - earliest),
                clock_domain_id=recorder.clock_domain_id,
                uncertainty_nano="1000000",
            )
            ended = TimePoint(
                source_time_unix_nano=str(span.end_nano),
                monotonic_time_nano=str(span.end_nano - earliest),
                clock_domain_id=recorder.clock_domain_id,
                uncertainty_nano="1000000",
            )
            if span.name == "elevenlabs.conversation":
                operation_name = "agent"
            elif span.name == "elevenlabs.recv.agent_response":
                operation_name = "agent_response"
            elif span.name == "elevenlabs.recv.user_transcript":
                operation_name = "provider_transcript"
            elif span.name.startswith("elevenlabs.tool."):
                operation_name = "tool"
            else:
                operation_name = "provider_span"
            recorder.record_operation(
                operation_id=f"operation-{span.span_id}",
                operation_name=operation_name,
                status=span.status,
                started_at=started,
                ended_at=ended,
                participant_id="participant-agent",
                turn_id=turn_id,
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                parent_scope=(
                    "internal"
                    if span.parent_span_id
                    and (span.trace_id, span.parent_span_id) in identities
                    else "external"
                    if span.parent_span_id
                    else "unknown"
                ),
                instrumentation_scope_name="elevenlabs.convai",
                evidence=Evidence(
                    source="provider",
                    observer="server",
                    method="provider_otlp_json",
                    method_version=ADAPTER_VERSION,
                    source_field="data.otlp_traces.resourceSpans",
                    confidence="measured",
                    availability="available",
                ),
                attributes={"gen_ai.provider.name": "elevenlabs"},
            )
        clock.advance(latest - earliest)
        session_status = "failed" if any(span.status == "error" for span in spans) else "completed"
        return recorder.close(status=session_status)

    @staticmethod
    def _parse_otlp_span(value: dict[str, Any]) -> _OtlpSpan:
        def identifier(field: str, length: int, *, optional: bool = False) -> str | None:
            raw = value.get(field)
            if optional and raw in {None, ""}:
                return None
            if not isinstance(raw, str):
                raise DeliveryPayloadError
            normalized = raw.lower()
            if (
                len(normalized) != length
                or not _HEX_PATTERN.fullmatch(normalized)
                or set(normalized) == {"0"}
            ):
                raise DeliveryPayloadError
            return normalized

        def timestamp(field: str) -> int:
            raw = value.get(field)
            if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 20:
                raise DeliveryPayloadError
            parsed = int(raw)
            if parsed > (1 << 64) - 1:
                raise DeliveryPayloadError
            return parsed

        trace_id = identifier("traceId", 32)
        span_id = identifier("spanId", 16)
        parent_span_id = identifier("parentSpanId", 16, optional=True)
        name = value.get("name")
        if not isinstance(name, str) or not name or len(name) > 512:
            raise DeliveryPayloadError
        start_nano = timestamp("startTimeUnixNano")
        end_nano = timestamp("endTimeUnixNano")
        if end_nano < start_nano:
            raise DeliveryPayloadError
        status_value = value.get("status")
        status_code = status_value.get("code") if isinstance(status_value, dict) else None
        status = "error" if status_code in {2, "STATUS_CODE_ERROR"} else "ok"
        assert trace_id is not None and span_id is not None
        return _OtlpSpan(
            trace_id,
            span_id,
            parent_span_id,
            name,
            start_nano,
            end_nano,
            status,
        )

    @staticmethod
    def _transcription_bundle(
        data: dict[str, Any],
        *,
        event_timestamp: int,
        conversation_id: str,
        delivery_key: str,
        context: NormalizationContext,
    ):
        metadata = data.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        start_seconds = metadata.get("start_time_unix_secs", event_timestamp)
        duration_seconds = metadata.get("call_duration_secs", 0)
        if (
            not isinstance(start_seconds, int)
            or isinstance(start_seconds, bool)
            or start_seconds < 0
            or not isinstance(duration_seconds, (int, float))
            or isinstance(duration_seconds, bool)
            or duration_seconds < 0
            or duration_seconds > 86_400
        ):
            raise DeliveryPayloadError
        provider_status = data.get("status")
        if not isinstance(provider_status, str):
            raise DeliveryPayloadError
        session_status = _SESSION_STATUS.get(provider_status)
        if session_status is None:
            raise DeliveryPayloadError

        session_fingerprint = context.fingerprint("elevenlabs:conversation", conversation_id)
        delivery_fingerprint = context.fingerprint("elevenlabs:delivery", delivery_key)
        clock = ManualClock(wall=start_seconds * 1_000_000_000, monotonic=0)
        recorder = IncidentRecorder(
            session_id=f"session-{session_fingerprint[:32]}",
            bundle_id=f"bundle-{delivery_fingerprint[:32]}",
            clock=clock,
            config=RecorderConfig(
                producer_name="earshot.connector.elevenlabs",
                producer_version=ADAPTER_VERSION,
                adapters=(
                    Adapter(
                        name="earshot.elevenlabs",
                        version=ADAPTER_VERSION,
                        framework="elevenlabs_agents",
                    ),
                ),
            ),
        )
        recorder.add_participant("participant-user", role="user", endpoint_kind="provider")
        recorder.add_participant("participant-agent", role="agent", endpoint_kind="provider")
        recorder.record_coverage("provider.transcript", "available")
        for signal in (
            "stt.final",
            "turn.end",
            "tts.first_audio",
            "client.render",
            "interruption",
        ):
            recorder.record_coverage(signal, "not_exposed", "provider_field_absent")

        transcript = data.get("transcript")
        if not isinstance(transcript, list) or len(transcript) > 100_000:
            raise DeliveryPayloadError
        agent_turn_index = 0
        for entry in transcript:
            if not isinstance(entry, dict) or entry.get("role") != "agent":
                continue
            offset = entry.get("time_in_call_secs")
            if (
                not isinstance(offset, (int, float))
                or isinstance(offset, bool)
                or offset < 0
                or offset > duration_seconds + 300
            ):
                raise DeliveryPayloadError
            turn_id = f"turn-{agent_turn_index}"
            operation_id = f"operation-agent-{agent_turn_index}"
            provider_nano = int(offset * 1_000_000_000)
            point = TimePoint(
                source_time_unix_nano=str(start_seconds * 1_000_000_000 + provider_nano),
                monotonic_time_nano=str(provider_nano),
                clock_domain_id=recorder.clock_domain_id,
                uncertainty_nano="1000000000",
            )
            evidence = Evidence(
                source="provider",
                observer="server",
                method="provider_webhook",
                method_version=ADAPTER_VERSION,
                source_field="data.transcript.time_in_call_secs",
                confidence="measured",
                availability="available",
            )
            recorder.record_operation(
                operation_id=operation_id,
                operation_name="provider_turn_anchor",
                status="ok",
                started_at=point,
                ended_at=point,
                participant_id="participant-agent",
                turn_id=turn_id,
                evidence=evidence,
                attributes={"gen_ai.provider.name": "elevenlabs"},
            )
            turn_metrics = entry.get("conversation_turn_metrics")
            if isinstance(turn_metrics, dict):
                llm_ttfb = turn_metrics.get("convai_llm_service_ttfb")
                elapsed = llm_ttfb.get("elapsed_time") if isinstance(llm_ttfb, dict) else None
                if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                    if elapsed < 0 or elapsed > 600:
                        raise DeliveryPayloadError
                    recorder.record_quality_sample(
                        QualitySample(
                            sample_id=f"quality-llm-{agent_turn_index}",
                            session_id=recorder.session_id,
                            quality_kind="provider_latency",
                            sample_window=TimeRange(start=point, end=point),
                            measurements=(
                                QualityMeasurement(
                                    name="earshot.llm.ttft",
                                    value=float(elapsed) * 1_000,
                                    unit="ms",
                                ),
                            ),
                            evidence=Evidence(
                                source="provider",
                                observer="server",
                                method="provider_webhook",
                                method_version=ADAPTER_VERSION,
                                source_field=(
                                    "data.transcript.conversation_turn_metrics."
                                    "convai_llm_service_ttfb.elapsed_time"
                                ),
                                confidence="measured",
                                availability="available",
                            ),
                            participant_id="participant-agent",
                            attributes={
                                "earshot.turn.id": turn_id,
                                "earshot.operation.id": operation_id,
                                "earshot.correlation": "provider_turn_scalar",
                                "earshot.chronology": "not_exposed",
                            },
                        )
                    )
            agent_turn_index += 1
        clock.advance(int(duration_seconds * 1_000_000_000))
        return recorder.close(status=session_status)
