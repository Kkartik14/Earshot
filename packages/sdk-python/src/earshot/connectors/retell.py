"""Retell finalized call analysis authentication and conservative normalization."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
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
_SIGNATURE_PATTERN = re.compile(r"^v=([0-9]+),d=([0-9a-fA-F]{64})$")
_LATENCY_COMPONENTS = (
    "e2e",
    "asr",
    "llm",
    "llm_websocket_network_rtt",
    "tts",
    "knowledge_base",
    "s2s",
)


def _header_values(delivery: RawProviderDelivery, name: bytes) -> tuple[bytes, ...]:
    lowered = name.lower()
    return tuple(value for key, value in delivery.headers if key.lower() == lowered)


def _milliseconds(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > (1 << 63) - 1
    ):
        raise DeliveryPayloadError
    return value


def _seconds(value: object, *, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        or float(value) > maximum
    ):
        raise DeliveryPayloadError
    return float(value)


class RetellConnectorAdapter:
    provider = "retell"
    normalizer_version = ADAPTER_VERSION

    def authenticate(
        self,
        delivery: RawProviderDelivery,
        secrets: tuple[str, ...],
        *,
        now_unix_seconds: int,
        tolerance_seconds: int,
    ) -> None:
        values = _header_values(delivery, b"x-retell-signature")
        if len(values) != 1 or not secrets:
            raise DeliveryTrustError
        try:
            header = values[0].decode("ascii")
        except UnicodeDecodeError:
            raise DeliveryTrustError from None
        match = _SIGNATURE_PATTERN.fullmatch(header)
        if match is None:
            raise DeliveryTrustError
        timestamp_text, supplied = match.groups()
        timestamp_ms = int(timestamp_text)
        if abs(now_unix_seconds * 1_000 - timestamp_ms) > tolerance_seconds * 1_000:
            raise DeliveryTrustError
        signed = delivery.body + timestamp_text.encode("ascii")
        valid = False
        for secret in secrets:
            expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            valid = hmac.compare_digest(expected, supplied.lower()) or valid
        if not valid:
            raise DeliveryTrustError

    def normalize(
        self,
        payload: dict[str, Any],
        context: NormalizationContext,
    ) -> NormalizedProviderDelivery:
        if payload.get("event") != "call_analyzed":
            raise DeliveryPayloadError
        call = payload.get("call")
        if not isinstance(call, dict):
            raise DeliveryPayloadError
        call_id = call.get("call_id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            raise DeliveryPayloadError
        identities = [ExternalIdentityInput("call_id", call_id)]
        agent_id = call.get("agent_id")
        if isinstance(agent_id, str) and agent_id and len(agent_id) <= 512:
            identities.append(ExternalIdentityInput("agent_id", agent_id))
        delivery_key = f"call_analyzed:{call_id}"
        bundle = self._call_bundle(
            call,
            call_id=call_id,
            delivery_key=delivery_key,
            context=context,
        )
        return NormalizedProviderDelivery(
            delivery_key=delivery_key,
            event_type="call_analyzed",
            disposition="incident",
            external_identities=tuple(identities),
            bundle=bundle,
        )

    @staticmethod
    def _call_bundle(
        call: dict[str, Any],
        *,
        call_id: str,
        delivery_key: str,
        context: NormalizationContext,
    ):
        start_ms = _milliseconds(call.get("start_timestamp"))
        end_ms = _milliseconds(call.get("end_timestamp"))
        if end_ms < start_ms or end_ms - start_ms > 86_400_000:
            raise DeliveryPayloadError
        start_nano = start_ms * 1_000_000
        end_nano = end_ms * 1_000_000
        session_fingerprint = context.fingerprint("retell:call", call_id)
        delivery_fingerprint = context.fingerprint("retell:delivery", delivery_key)
        clock = ManualClock(wall=start_nano, monotonic=0)
        recorder = IncidentRecorder(
            session_id=f"session-{session_fingerprint[:32]}",
            bundle_id=f"bundle-{delivery_fingerprint[:32]}",
            clock=clock,
            config=RecorderConfig(
                producer_name="earshot.connector.retell",
                producer_version=ADAPTER_VERSION,
                adapters=(
                    Adapter(
                        name="earshot.retell",
                        version=ADAPTER_VERSION,
                        framework="retell",
                    ),
                ),
            ),
        )
        recorder.add_participant("participant-agent", role="agent", endpoint_kind="provider")
        for signal in ("turn.end", "client.render", "interruption"):
            recorder.record_coverage(signal, "not_exposed", "provider_field_absent")

        transcript = call.get("transcript_object", [])
        if not isinstance(transcript, list) or len(transcript) > 100_000:
            raise DeliveryPayloadError
        agent_index = 0
        for utterance in transcript:
            if not isinstance(utterance, dict):
                raise DeliveryPayloadError
            if utterance.get("role") != "agent":
                continue
            words = utterance.get("words")
            if not isinstance(words, list) or not words or len(words) > 100_000:
                raise DeliveryPayloadError
            offsets = []
            previous_end = 0.0
            for word in words:
                if not isinstance(word, dict):
                    raise DeliveryPayloadError
                word_start = _seconds(word.get("start"), maximum=86_400.0)
                word_end = _seconds(word.get("end"), maximum=86_400.0)
                if word_start < previous_end or word_end < word_start:
                    raise DeliveryPayloadError
                offsets.append((word_start, word_end))
                previous_end = word_end
            utterance_start = offsets[0][0]
            utterance_end = offsets[-1][1]
            start_offset_nano = int(utterance_start * 1_000_000_000)
            end_offset_nano = int(utterance_end * 1_000_000_000)
            if end_offset_nano > end_nano - start_nano + 300_000_000_000:
                raise DeliveryPayloadError
            point = TimePoint(
                source_time_unix_nano=str(start_nano + start_offset_nano),
                monotonic_time_nano=str(start_offset_nano),
                clock_domain_id=recorder.clock_domain_id,
                uncertainty_nano="1000000",
            )
            ended = TimePoint(
                source_time_unix_nano=str(start_nano + end_offset_nano),
                monotonic_time_nano=str(end_offset_nano),
                clock_domain_id=recorder.clock_domain_id,
                uncertainty_nano="1000000",
            )
            recorder.record_operation(
                operation_id=f"operation-agent-{agent_index}",
                operation_name="provider_agent_speech_timing",
                status="ok",
                started_at=point,
                ended_at=ended,
                participant_id="participant-agent",
                turn_id=f"turn-{agent_index}",
                evidence=Evidence(
                    source="provider",
                    observer="server",
                    method="provider_webhook",
                    method_version=ADAPTER_VERSION,
                    source_field="call.transcript_object.words",
                    confidence="measured",
                    availability="available",
                ),
                attributes={"gen_ai.provider.name": "retell"},
            )
            agent_index += 1
        recorder.record_coverage(
            "provider.word_timing",
            "available" if agent_index else "not_observed",
            None if agent_index else "provider_field_absent",
        )

        latency = call.get("latency", {})
        if not isinstance(latency, dict):
            raise DeliveryPayloadError
        sample_count = 0
        full_range = TimeRange(
            start=TimePoint(
                source_time_unix_nano=str(start_nano),
                monotonic_time_nano="0",
                clock_domain_id=recorder.clock_domain_id,
            ),
            end=TimePoint(
                source_time_unix_nano=str(end_nano),
                monotonic_time_nano=str(end_nano - start_nano),
                clock_domain_id=recorder.clock_domain_id,
            ),
        )
        for component in _LATENCY_COMPONENTS:
            raw_component = latency.get(component)
            if raw_component is None:
                continue
            if not isinstance(raw_component, dict):
                raise DeliveryPayloadError
            values = raw_component.get("values")
            if not isinstance(values, list):
                raise DeliveryPayloadError
            for value in values:
                if sample_count >= 100_000:
                    raise DeliveryPayloadError
                measured = _seconds(value, maximum=600_000.0)
                recorder.record_quality_sample(
                    QualitySample(
                        sample_id=f"quality-{component}-{sample_count}",
                        session_id=recorder.session_id,
                        quality_kind="provider_latency_uncorrelated",
                        sample_window=full_range,
                        measurements=(
                            QualityMeasurement(
                                name=f"retell.{component}",
                                value=measured,
                                unit="ms",
                            ),
                        ),
                        evidence=Evidence(
                            source="provider",
                            observer="server",
                            method="provider_webhook",
                            method_version=ADAPTER_VERSION,
                            source_field=f"call.latency.{component}.values",
                            confidence="measured",
                            availability="available",
                        ),
                        participant_id="participant-agent",
                        attributes={"earshot.correlation": "session_only"},
                    )
                )
                sample_count += 1
        recorder.record_coverage(
            "provider.latency_samples",
            "available" if sample_count else "not_observed",
            None if sample_count else "provider_field_absent",
        )
        clock.advance(end_nano - start_nano)
        status = "failed" if call.get("call_status") == "error" else "completed"
        return recorder.close(status=status)
