"""Sarvam streaming STT event normalization."""

from __future__ import annotations

from collections.abc import Mapping

from ...pipeline import TurnRecorder
from ...privacy import sanitize_semantic_label
from .base import (
    AdapterUpdate,
    ProviderAdapter,
    optional_probability,
    optional_string,
    require_mapping,
    require_nonnegative_number,
    require_string,
    safe_attributes,
)


class SarvamAdapter(ProviderAdapter):
    """Map Sarvam VAD and transcription responses without retaining content."""

    def __init__(
        self,
        *,
        model: str = "saaras:v3",
        mode: str = "transcribe",
        language_code: str = "unknown",
        identity_key: bytes | None = None,
    ) -> None:
        super().__init__("sarvam", identity_key=identity_key)
        self.model = require_string(model, "model")
        self.mode = sanitize_semantic_label(require_string(mode, "mode"))
        self.language_code = require_string(language_code, "language_code")

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
    ) -> AdapterUpdate:
        """Validate one WebSocket response and return a content-free update."""

        payload = require_mapping(payload, "payload")
        event_type = require_string(payload.get("type"), "type")
        data = require_mapping(payload.get("data"), "data")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")
        if event_type == "events":
            return self._vad_event(payload, data, receipt_ms)
        if event_type == "data":
            return self._transcription(payload, data, receipt_ms)
        if event_type == "error":
            return self._error(payload, data, receipt_ms)
        raise ValueError(f"unsupported Sarvam event type: {event_type}")

    def _vad_event(
        self,
        payload: Mapping[str, object],
        data: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        signal = require_string(data.get("signal_type"), "data.signal_type")
        event_name = {
            "START_SPEECH": "earshot.speech.started",
            "END_SPEECH": "earshot.speech.ended",
        }.get(signal)
        if event_name is None:
            raise ValueError(f"unsupported Sarvam VAD signal: {signal}")

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("event", update_id)
            attributes = safe_attributes(correlation_id, signal)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_event(
                    event_name,
                    at_ms=receipt_ms,
                    participant="user",
                    source="app",
                    confidence="estimated",
                    source_field=f"events.{signal}.receipt",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=signal,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            observed_at_ms=receipt_ms,
        )

    def _transcription(
        self,
        payload: Mapping[str, object],
        data: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        request_id = require_string(data.get("request_id"), "data.request_id")
        require_string(data.get("transcript"), "data.transcript", allow_empty=True)
        metrics = require_mapping(data.get("metrics"), "data.metrics")
        audio_duration = require_nonnegative_number(
            metrics.get("audio_duration"), "metrics.audio_duration"
        )
        processing_seconds = require_nonnegative_number(
            metrics.get("processing_latency"), "metrics.processing_latency"
        )
        language_code = optional_string(data.get("language_code"), "data.language_code")
        language_probability = optional_probability(
            data.get("language_probability"), "data.language_probability"
        )
        if self.language_code != "unknown" and language_probability is not None:
            raise ValueError(
                "language_probability must be null when a specific language is supplied"
            )

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("request", request_id)
            attributes = safe_attributes(correlation_id, "data")
            attributes["earshot.stt.mode"] = self.mode
            if language_code is not None:
                attributes["earshot.language.code"] = language_code
            if language_probability is not None:
                attributes["earshot.language.probability"] = language_probability

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_omission(
                    "sarvam.data.transcript",
                    capture_class="transcript",
                )
                operation_id = turn.record_stage(
                    "stt",
                    "sarvam",
                    model=self.model,
                    at_ms=receipt_ms,
                    source="app",
                    confidence="inferred",
                    source_field="sarvam.data.receipt",
                    attributes=attributes,
                )
                turn.record_measurement(
                    "sarvam.stt.audio_duration",
                    audio_duration,
                    unit="s",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="metrics.audio_duration",
                    basis="provider_schema_seconds",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "sarvam.stt.processing_latency",
                    processing_seconds * 1000,
                    unit="ms",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="metrics.processing_latency",
                    basis="provider_schema_seconds_converted_once",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_event(
                    "earshot.transcript.final",
                    at_ms=receipt_ms,
                    participant="user",
                    source="provider",
                    confidence="inferred",
                    source_field="sarvam.data",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type="data",
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=request_id,
        )

    def _error(
        self,
        payload: Mapping[str, object],
        data: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        require_string(data.get("error"), "data.error")
        code = sanitize_semantic_label(require_string(data.get("code"), "data.code"))

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("error", update_id)
            attributes = safe_attributes(correlation_id, "error")
            if code is not None:
                attributes["error.type"] = code

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_omission(
                    "sarvam.data.error",
                    capture_class="diagnostic_payload",
                )
                turn.record_stage(
                    "stt",
                    "sarvam",
                    model=self.model,
                    status="error",
                    at_ms=receipt_ms,
                    source="provider",
                    confidence="inferred",
                    source_field="sarvam.error.code",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type="error",
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)
