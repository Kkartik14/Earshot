"""Vapi finalized-report authentication and privacy-minimal normalization."""

from __future__ import annotations

import hmac
import math
from datetime import datetime
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
_METRIC_FIELDS = {
    "modelLatency": "vapi.model_latency",
    "voiceLatency": "vapi.voice_latency",
    "transcriberLatency": "vapi.transcriber_latency",
    "endpointingLatency": "vapi.endpointing_latency",
    "turnLatency": "vapi.turn_latency",
}


def _header_values(delivery: RawProviderDelivery, name: bytes) -> tuple[bytes, ...]:
    lowered = name.lower()
    return tuple(value for key, value in delivery.headers if key.lower() == lowered)


def _iso_nano(value: object) -> int:
    if not isinstance(value, str) or len(value) > 64:
        raise DeliveryPayloadError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DeliveryPayloadError from None
    if parsed.tzinfo is None:
        raise DeliveryPayloadError
    timestamp = parsed.timestamp()
    if timestamp < 0:
        raise DeliveryPayloadError
    return int(timestamp * 1_000_000_000)


def _nonnegative_number(value: object, *, maximum: float = 1_000_000_000_000.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        or float(value) > maximum
    ):
        raise DeliveryPayloadError
    return float(value)


class VapiConnectorAdapter:
    provider = "vapi"
    normalizer_version = ADAPTER_VERSION

    def authenticate(
        self,
        delivery: RawProviderDelivery,
        secrets: tuple[str, ...],
        *,
        now_unix_seconds: int,
        tolerance_seconds: int,
    ) -> None:
        del now_unix_seconds, tolerance_seconds
        authorization = _header_values(delivery, b"authorization")
        legacy = _header_values(delivery, b"x-vapi-secret")
        if not secrets or bool(authorization) == bool(legacy):
            raise DeliveryTrustError
        values = authorization or legacy
        if len(values) != 1:
            raise DeliveryTrustError
        try:
            supplied = values[0].decode("utf-8")
        except UnicodeDecodeError:
            raise DeliveryTrustError from None
        if authorization:
            scheme, separator, token = supplied.partition(" ")
            if not separator or scheme.lower() != "bearer" or not token:
                raise DeliveryTrustError
            supplied = token
        supplied_bytes = supplied.encode("utf-8")
        valid = False
        for secret in secrets:
            valid = hmac.compare_digest(secret.encode("utf-8"), supplied_bytes) or valid
        if not valid:
            raise DeliveryTrustError

    def normalize(
        self,
        payload: dict[str, Any],
        context: NormalizationContext,
    ) -> NormalizedProviderDelivery:
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("type") != "end-of-call-report":
            raise DeliveryPayloadError
        call = message.get("call")
        artifact = message.get("artifact")
        if not isinstance(call, dict) or not isinstance(artifact, dict):
            raise DeliveryPayloadError
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            raise DeliveryPayloadError
        identities = [ExternalIdentityInput("call_id", call_id)]
        assistant_id = call.get("assistantId")
        if isinstance(assistant_id, str) and assistant_id and len(assistant_id) <= 512:
            identities.append(ExternalIdentityInput("assistant_id", assistant_id))
        delivery_key = f"end-of-call-report:{call_id}"
        bundle = self._report_bundle(
            message,
            call,
            artifact,
            call_id=call_id,
            delivery_key=delivery_key,
            context=context,
        )
        return NormalizedProviderDelivery(
            delivery_key=delivery_key,
            event_type="end-of-call-report",
            disposition="incident",
            external_identities=tuple(identities),
            bundle=bundle,
        )

    @staticmethod
    def _report_bundle(
        message: dict[str, Any],
        call: dict[str, Any],
        artifact: dict[str, Any],
        *,
        call_id: str,
        delivery_key: str,
        context: NormalizationContext,
    ):
        start_nano = _iso_nano(call.get("startedAt", call.get("createdAt")))
        end_nano = _iso_nano(call.get("endedAt", message.get("timestamp")))
        if end_nano < start_nano or end_nano - start_nano > 86_400 * 1_000_000_000:
            raise DeliveryPayloadError
        performance = artifact.get("performanceMetrics")
        if not isinstance(performance, dict):
            raise DeliveryPayloadError
        raw_turns = performance.get("turnLatencies")
        if not isinstance(raw_turns, list) or len(raw_turns) > 100_000:
            raise DeliveryPayloadError
        raw_messages = artifact.get("messages", [])
        if not isinstance(raw_messages, list) or len(raw_messages) > 100_000:
            raise DeliveryPayloadError
        assistant_offsets: list[float | None] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                raise DeliveryPayloadError
            if raw_message.get("role") in {"assistant", "bot"}:
                offset = raw_message.get("secondsFromStart")
                assistant_offsets.append(
                    _nonnegative_number(offset, maximum=86_400.0)
                    if offset is not None
                    else None
                )

        session_fingerprint = context.fingerprint("vapi:call", call_id)
        delivery_fingerprint = context.fingerprint("vapi:delivery", delivery_key)
        clock = ManualClock(wall=start_nano, monotonic=0)
        recorder = IncidentRecorder(
            session_id=f"session-{session_fingerprint[:32]}",
            bundle_id=f"bundle-{delivery_fingerprint[:32]}",
            clock=clock,
            config=RecorderConfig(
                producer_name="earshot.connector.vapi",
                producer_version=ADAPTER_VERSION,
                adapters=(
                    Adapter(
                        name="earshot.vapi",
                        version=ADAPTER_VERSION,
                        framework="vapi",
                    ),
                ),
            ),
        )
        recorder.add_participant("participant-agent", role="agent", endpoint_kind="provider")
        recorder.record_coverage("provider.performance_metrics", "available")
        for signal in ("stt.final", "turn.end", "tts.first_audio", "client.render"):
            recorder.record_coverage(signal, "not_exposed", "provider_field_absent")
        recorder.record_coverage(
            "interruption.per_turn",
            "not_exposed",
            "provider_exports_session_aggregate_only",
        )

        session_start = TimePoint(
            source_time_unix_nano=str(start_nano),
            monotonic_time_nano="0",
            clock_domain_id=recorder.clock_domain_id,
        )
        session_end = TimePoint(
            source_time_unix_nano=str(end_nano),
            monotonic_time_nano=str(end_nano - start_nano),
            clock_domain_id=recorder.clock_domain_id,
        )
        session_window = TimeRange(start=session_start, end=session_end)

        for index, raw_turn in enumerate(raw_turns):
            if not isinstance(raw_turn, dict):
                raise DeliveryPayloadError
            values = {
                name: _nonnegative_number(raw_turn[field])
                for field, name in _METRIC_FIELDS.items()
                if field in raw_turn
            }
            if "vapi.turn_latency" not in values:
                raise DeliveryPayloadError
            candidate_offset = (
                assistant_offsets[index] if index < len(assistant_offsets) else None
            )
            point: TimePoint | None = None
            turn_id: str | None = None
            operation_id: str | None = None
            sample_attributes: dict[str, Any] = {
                "earshot.turn.index": index,
                "earshot.unit_basis": "provider_schema_unit_undocumented",
                "earshot.chronology": "not_exposed",
            }
            source_field = "message.artifact.performanceMetrics.turnLatencies"
            confidence = "measured"
            evidence_method = "provider_webhook"
            if candidate_offset is not None:
                offset_nano = int(candidate_offset * 1_000_000_000)
                point = TimePoint(
                    source_time_unix_nano=str(start_nano + offset_nano),
                    monotonic_time_nano=str(offset_nano),
                    clock_domain_id=recorder.clock_domain_id,
                    uncertainty_nano="1000000000",
                )
                turn_id = f"turn-{index}"
                operation_id = f"operation-provider-turn-{index}"
                source_field = (
                    "message.artifact.messages.secondsFromStart+"
                    "message.artifact.performanceMetrics.turnLatencies"
                )
                confidence = "inferred"
                evidence_method = "provider_ordered_index_join"
                sample_attributes.update(
                    {
                        "earshot.turn.id": turn_id,
                        "earshot.operation.id": operation_id,
                        "earshot.correlation": "ordered_index_inferred",
                    }
                )
            else:
                sample_attributes["earshot.correlation"] = "provider_order_only"
            evidence = Evidence(
                source="provider",
                observer="server",
                method=evidence_method,
                method_version=ADAPTER_VERSION,
                source_field=source_field,
                confidence=confidence,
                availability="available",
            )
            if point is not None and operation_id is not None and turn_id is not None:
                recorder.record_operation(
                    operation_id=operation_id,
                    operation_name="provider_turn_anchor",
                    status="ok",
                    started_at=point,
                    ended_at=point,
                    participant_id="participant-agent",
                    turn_id=turn_id,
                    evidence=evidence,
                    attributes={"gen_ai.provider.name": "vapi"},
                )
            recorder.record_quality_sample(
                QualitySample(
                    sample_id=f"quality-turn-{index}",
                    session_id=recorder.session_id,
                    quality_kind="provider_latency",
                    sample_window=(
                        TimeRange(start=point, end=point)
                        if point is not None
                        else session_window
                    ),
                    measurements=tuple(
                        QualityMeasurement(
                            name=name,
                            value=value,
                            unit="provider_unit",
                        )
                        for name, value in sorted(values.items())
                    ),
                    evidence=evidence,
                    participant_id="participant-agent",
                    attributes=sample_attributes,
                )
            )

        count_measurements = []
        for field, name in (
            ("numUserInterrupted", "vapi.num_user_interrupted"),
            ("numAssistantInterrupted", "vapi.num_assistant_interrupted"),
        ):
            if field in performance:
                count_measurements.append(
                    QualityMeasurement(
                        name=name,
                        value=_nonnegative_number(performance[field], maximum=1_000_000),
                        unit="count",
                    )
                )
        if count_measurements:
            recorder.record_quality_sample(
                QualitySample(
                    sample_id="quality-interruptions",
                    session_id=recorder.session_id,
                    quality_kind="provider_session_aggregate",
                    sample_window=session_window,
                    measurements=tuple(count_measurements),
                    evidence=Evidence(
                        source="provider",
                        observer="server",
                        method="provider_webhook",
                        method_version=ADAPTER_VERSION,
                        source_field="message.artifact.performanceMetrics",
                        confidence="measured",
                        availability="available",
                    ),
                    participant_id="participant-agent",
                )
            )
        clock.advance(end_nano - start_nano)
        return recorder.close(status="completed")
