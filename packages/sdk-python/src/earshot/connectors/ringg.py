"""Ringg finalized-call authentication and privacy-minimal normalization."""

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
_FINAL_EVENT = "all_processing_completed"
_PROGRESS_EVENTS = {
    "call_started",
    "call_completed",
    "recording_completed",
    "platform_analysis_completed",
    "client_analysis_completed",
}
_FINAL_STATUSES = {"completed", "failed", "error", "cancelled", "forwarded"}


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


def _nonnegative_number(value: object, *, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        or float(value) > maximum
    ):
        raise DeliveryPayloadError
    return float(value)


class RinggConnectorAdapter:
    """Normalize only Ringg's consolidated, terminal delivery."""

    provider = "ringg"
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
        configured_header = _header_values(delivery, b"x-webhook-secret")
        if not secrets or bool(authorization) == bool(configured_header):
            raise DeliveryTrustError
        values = authorization or configured_header
        if len(values) != 1 or len(values[0]) > 4096:
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
        event_type = payload.get("event_type")
        if not isinstance(event_type, str) or event_type not in {
            _FINAL_EVENT,
            *_PROGRESS_EVENTS,
        }:
            raise DeliveryPayloadError
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            raise DeliveryPayloadError
        call_sid = payload.get("call_sid")
        if not isinstance(call_sid, str) or not call_sid or len(call_sid) > 512:
            raise DeliveryPayloadError
        external_identities = (
            ExternalIdentityInput("call_id", call_id),
            ExternalIdentityInput("call_sid", call_sid),
        )
        delivery_key = f"{call_id}:{event_type}"
        if event_type in _PROGRESS_EVENTS:
            return NormalizedProviderDelivery(
                delivery_key=delivery_key,
                event_type=event_type,
                disposition="ignore",
                external_identities=external_identities,
                bundle=None,
            )
        status = payload.get("status")
        if not isinstance(status, str) or status not in _FINAL_STATUSES:
            raise DeliveryPayloadError
        return NormalizedProviderDelivery(
            delivery_key=delivery_key,
            event_type=_FINAL_EVENT,
            disposition="incident",
            external_identities=external_identities,
            bundle=self._final_bundle(
                payload,
                call_id=call_id,
                delivery_key=delivery_key,
                status=status,
                context=context,
            ),
        )

    @staticmethod
    def _final_bundle(
        payload: dict[str, Any],
        *,
        call_id: str,
        delivery_key: str,
        status: str,
        context: NormalizationContext,
    ):
        start_nano = _iso_nano(payload.get("called_on"))
        duration_seconds = _nonnegative_number(
            payload.get("call_duration"), maximum=86_400.0
        )
        duration_nano = int(duration_seconds * 1_000_000_000)
        end_nano = start_nano + duration_nano
        if end_nano > (1 << 64) - 1:
            raise DeliveryPayloadError
        overall_latency = _nonnegative_number(
            payload.get("overall_latency_seconds"), maximum=86_400.0
        )
        first_utterance = _nonnegative_number(
            payload.get("first_utterance_seconds"), maximum=86_400.0
        )

        session_fingerprint = context.fingerprint("ringg:call", call_id)
        delivery_fingerprint = context.fingerprint("ringg:delivery", delivery_key)
        clock = ManualClock(wall=start_nano, monotonic=0)
        recorder = IncidentRecorder(
            session_id=f"session-{session_fingerprint[:32]}",
            bundle_id=f"bundle-{delivery_fingerprint[:32]}",
            clock=clock,
            config=RecorderConfig(
                producer_name="earshot.connector.ringg",
                producer_version=ADAPTER_VERSION,
                adapters=(
                    Adapter(
                        name="earshot.ringg",
                        version=ADAPTER_VERSION,
                        framework="ringg",
                    ),
                ),
            ),
        )
        recorder.add_participant("participant-agent", role="agent", endpoint_kind="provider")
        recorder.record_coverage("provider.session_metrics", "available")
        for signal in (
            "stt.final",
            "turn.end",
            "tts.first_audio",
            "client.render",
            "interruption.per_turn",
        ):
            recorder.record_coverage(
                signal,
                "not_exposed",
                "provider_exports_session_aggregate_only",
            )

        session_range = TimeRange(
            start=TimePoint(
                source_time_unix_nano=str(start_nano),
                monotonic_time_nano="0",
                clock_domain_id=recorder.clock_domain_id,
            ),
            end=TimePoint(
                source_time_unix_nano=str(end_nano),
                monotonic_time_nano=str(duration_nano),
                clock_domain_id=recorder.clock_domain_id,
            ),
        )
        recorder.record_quality_sample(
            QualitySample(
                sample_id="quality-session",
                session_id=recorder.session_id,
                quality_kind="provider_session_aggregate",
                sample_window=session_range,
                measurements=(
                    QualityMeasurement(
                        name="ringg.call_duration",
                        value=duration_seconds,
                        unit="s",
                    ),
                    QualityMeasurement(
                        name="ringg.overall_latency",
                        value=overall_latency,
                        unit="s",
                    ),
                    QualityMeasurement(
                        name="ringg.first_utterance",
                        value=first_utterance,
                        unit="s",
                    ),
                ),
                evidence=Evidence(
                    source="provider",
                    observer="server",
                    method="provider_webhook",
                    method_version=ADAPTER_VERSION,
                    source_field=(
                        "call_duration+overall_latency_seconds+first_utterance_seconds"
                    ),
                    confidence="measured",
                    availability="available",
                ),
                participant_id="participant-agent",
                attributes={
                    "earshot.correlation": "session_only",
                    "earshot.chronology": "provider_session_window",
                    "earshot.unit_basis": "provider_schema_seconds",
                },
            )
        )
        clock.advance(duration_nano)
        return recorder.close(status=status)
