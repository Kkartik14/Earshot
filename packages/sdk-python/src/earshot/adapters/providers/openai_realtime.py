"""OpenAI Realtime event normalization without invented STT/LLM/TTS stages."""

from __future__ import annotations

from collections.abc import Mapping

from ...pipeline import TurnRecorder
from ...privacy import sanitize_semantic_label
from .base import (
    AdapterUpdate,
    ProviderAdapter,
    optional_string,
    require_mapping,
    require_nonnegative_number,
    require_string,
    safe_attributes,
)


class OpenAIRealtimeAdapter(ProviderAdapter):
    """Map one Realtime stream into fused ``agent`` response evidence."""

    def __init__(
        self,
        *,
        model: str,
        identity_key: bytes | None = None,
    ) -> None:
        super().__init__("openai", identity_key=identity_key)
        self.model = sanitize_semantic_label(require_string(model, "model"))
        self._speech_stopped_receipt_ms: float | None = None
        self._response_started_ms: dict[str, float] = {}
        self._active_responses: set[str] = set()
        self._first_audio_responses: set[str] = set()

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
    ) -> AdapterUpdate:
        """Validate one server event and return a content-free recorder update."""

        payload = require_mapping(payload, "payload")
        event_type = require_string(payload.get("type"), "type")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")
        if event_type == "input_audio_buffer.speech_started":
            return self._speech_started(payload, receipt_ms)
        if event_type == "input_audio_buffer.speech_stopped":
            return self._speech_stopped(payload, receipt_ms)
        if event_type == "conversation.item.input_audio_transcription.completed":
            return self._transcript_final(payload, receipt_ms)
        if event_type == "response.created":
            return self._response_created(payload, receipt_ms)
        if event_type == "response.output_audio.delta":
            return self._audio_delta(payload, receipt_ms)
        if event_type == "response.done":
            return self._response_done(payload, receipt_ms)
        raise ValueError(f"unsupported OpenAI Realtime event type: {event_type}")

    def _speech_started(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        audio_start_ms = require_nonnegative_number(
            payload.get("audio_start_ms"), "audio_start_ms"
        )
        event_type = "input_audio_buffer.speech_started"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)
            interrupted = bool(self._active_responses)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_event(
                    "earshot.speech.started",
                    at_ms=receipt_ms,
                    participant="user",
                    source="app",
                    confidence="estimated",
                    source_field=event_type,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "openai.realtime.audio_start",
                    audio_start_ms,
                    unit="ms",
                    source="provider",
                    confidence="measured",
                    source_field="audio_start_ms",
                    basis="session_audio_buffer_offset",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                if interrupted:
                    turn.record_event(
                        "earshot.interruption.detected",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field=event_type,
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)

    def _speech_stopped(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        audio_end_ms = require_nonnegative_number(payload.get("audio_end_ms"), "audio_end_ms")
        event_type = "input_audio_buffer.speech_stopped"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)
            self._speech_stopped_receipt_ms = receipt_ms

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_event(
                    "earshot.speech.ended",
                    at_ms=receipt_ms,
                    participant="user",
                    source="app",
                    confidence="estimated",
                    source_field=event_type,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "openai.realtime.audio_end",
                    audio_end_ms,
                    unit="ms",
                    source="provider",
                    confidence="measured",
                    source_field="audio_end_ms",
                    basis="session_audio_buffer_offset",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)

    def _transcript_final(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        require_string(payload.get("transcript"), "transcript", allow_empty=True)
        event_type = "conversation.item.input_audio_transcription.completed"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_event(
                    "earshot.transcript.final",
                    at_ms=receipt_ms,
                    participant="user",
                    source="app",
                    confidence="estimated",
                    source_field=event_type,
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)

    def _response_created(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        response = require_mapping(payload.get("response"), "response")
        response_id = require_string(response.get("id"), "response.id")
        event_type = "response.created"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("response", response_id)
            self._response_started_ms[response_id] = receipt_ms
            self._active_responses.add(response_id)
            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=lambda turn: None,
            )

        return self._remember(payload, create_update)

    def _audio_delta(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        response_id = require_string(payload.get("response_id"), "response_id")
        require_string(payload.get("delta"), "delta")
        event_type = "response.output_audio.delta"

        def create_update(update_id: str) -> AdapterUpdate:
            if response_id not in self._response_started_ms:
                raise ValueError("audio delta references an unknown response")
            correlation_id = self._opaque_id("response", response_id)
            attributes = safe_attributes(correlation_id, event_type)
            first_audio = response_id not in self._first_audio_responses
            if first_audio:
                self._first_audio_responses.add(response_id)
            speech_stopped_ms = self._speech_stopped_receipt_ms
            if speech_stopped_ms is not None and receipt_ms < speech_stopped_ms:
                raise ValueError("first audio receipt precedes speech-stopped receipt")

            def apply_update(turn: TurnRecorder) -> None:
                if not first_audio:
                    return
                turn.record_event(
                    "earshot.audio.first_packet_received",
                    at_ms=receipt_ms,
                    participant="agent",
                    source="app",
                    confidence="estimated",
                    source_field=event_type,
                    attributes=attributes,
                )
                if speech_stopped_ms is not None:
                    turn.record_measurement(
                        "earshot.turn.response_latency",
                        receipt_ms - speech_stopped_ms,
                        unit="ms",
                        source="app",
                        confidence="estimated",
                        source_field=event_type,
                        basis="server_vad_stop_receipt_to_first_audio_receipt",
                        at_ms=receipt_ms,
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)

    def _response_done(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        response = require_mapping(payload.get("response"), "response")
        response_id = require_string(response.get("id"), "response.id")
        provider_status = require_string(response.get("status"), "response.status")
        status = {
            "completed": "ok",
            "cancelled": "cancelled",
            "failed": "error",
            "incomplete": "incomplete",
        }.get(provider_status)
        if status is None:
            raise ValueError(f"unsupported response status: {provider_status}")
        event_type = "response.done"

        def create_update(update_id: str) -> AdapterUpdate:
            started_ms = self._response_started_ms.get(response_id)
            if started_ms is None:
                raise ValueError("response.done references an unknown response")
            if receipt_ms < started_ms:
                raise ValueError("response.done precedes response.created")
            correlation_id = self._opaque_id("response", response_id)
            attributes = safe_attributes(correlation_id, event_type)
            self._active_responses.discard(response_id)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_stage(
                    "agent",
                    "openai",
                    model=self.model,
                    status=status,
                    at_ms=started_ms,
                    ended_at_ms=receipt_ms,
                    source="app",
                    confidence="estimated",
                    source_field="response.created_to_response.done",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update)
