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
    require_nonnegative_integer,
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
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        """Clear every per-session response/gesture map at a session boundary.

        These maps grow one entry per response and are only pruned when a
        ``response.done`` for that response is observed. A session that ends with
        responses still in flight would otherwise leave their entries behind for
        the next session on a reused adapter, so ``close()`` resets all of them.
        """

        self._speech_stopped_receipt_ms: float | None = None
        self._response_started_ms: dict[str, float] = {}
        self._response_speech_stopped_ms: dict[str, float | None] = {}
        self._active_responses: set[str] = set()
        self._first_audio_responses: set[str] = set()
        self._response_interruption_gestures: dict[str, int] = {}
        self._accepted_interruption_gestures: set[int] = set()
        self._next_interruption_gesture = 0

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
        if event_type == "response.output_audio.done":
            return self._audio_done(payload, receipt_ms)
        if event_type == "response.done":
            return self._response_done(payload, receipt_ms)
        raise ValueError(f"unsupported OpenAI Realtime event type: {event_type}")

    def _speech_started(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        audio_start_ms = require_nonnegative_number(payload.get("audio_start_ms"), "audio_start_ms")
        event_type = "input_audio_buffer.speech_started"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                interrupted_responses = set(self._active_responses)
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
                if interrupted_responses:
                    turn.record_event(
                        "earshot.interruption.detected",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field=event_type,
                        attributes=attributes,
                    )
                    gesture = self._next_interruption_gesture
                    self._next_interruption_gesture += 1
                    for response_id in interrupted_responses:
                        self._response_interruption_gestures[response_id] = gesture

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _speech_stopped(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        audio_end_ms = require_nonnegative_number(payload.get("audio_end_ms"), "audio_end_ms")
        event_type = "input_audio_buffer.speech_stopped"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

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
                self._speech_stopped_receipt_ms = receipt_ms

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _transcript_final(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        require_string(payload.get("transcript"), "transcript", allow_empty=True)
        event_type = "conversation.item.input_audio_transcription.completed"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_omission(
                    "openai.realtime.conversation.item.input_audio_transcription.completed.transcript",
                    capture_class="transcript",
                )
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

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _response_created(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        response = require_mapping(payload.get("response"), "response")
        response_id = require_string(response.get("id"), "response.id")
        event_type = "response.created"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("response", response_id)

            def apply_update(turn: TurnRecorder) -> None:
                if response_id in self._response_started_ms:
                    raise ValueError("response was already created")
                self._response_started_ms[response_id] = receipt_ms
                self._response_speech_stopped_ms[response_id] = self._speech_stopped_receipt_ms
                self._active_responses.add(response_id)

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _audio_delta(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        response_id = require_string(payload.get("response_id"), "response_id")
        require_string(payload.get("delta"), "delta")
        event_type = "response.output_audio.delta"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("response", response_id)
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                if response_id not in self._response_started_ms:
                    raise ValueError("audio delta references an unknown response")
                if response_id in self._first_audio_responses:
                    turn.record_omission(
                        "openai.realtime.response.output_audio.delta.delta",
                        capture_class="audio",
                    )
                    return
                if response_id not in self._active_responses:
                    raise ValueError("audio delta references an inactive response")
                speech_stopped_ms = self._response_speech_stopped_ms.get(response_id)
                if speech_stopped_ms is not None and receipt_ms < speech_stopped_ms:
                    raise ValueError("first audio receipt precedes speech-stopped receipt")
                turn.record_omission(
                    "openai.realtime.response.output_audio.delta.delta",
                    capture_class="audio",
                )
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
                self._first_audio_responses.add(response_id)

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _audio_done(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        response_id = require_string(payload.get("response_id"), "response_id")
        require_string(payload.get("event_id"), "event_id")
        require_string(payload.get("item_id"), "item_id")
        require_nonnegative_integer(payload.get("output_index"), "output_index")
        require_nonnegative_integer(payload.get("content_index"), "content_index")
        event_type = "response.output_audio.done"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("response", response_id)
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                if response_id not in self._response_started_ms:
                    raise ValueError("audio done references an unknown response")
                if response_id not in self._active_responses:
                    raise ValueError("audio done references an inactive response")
                turn.record_event(
                    "openai.realtime.output_audio.done",
                    at_ms=receipt_ms,
                    participant="agent",
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
                terminal=False,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )

    def _response_done(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
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
        has_output = "output" in response
        has_status_details = "status_details" in response

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("response", response_id)
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                started_ms = self._response_started_ms.get(response_id)
                if started_ms is None:
                    raise ValueError("response.done references an unknown response")
                if receipt_ms < started_ms:
                    raise ValueError("response.done precedes response.created")
                if response_id not in self._active_responses:
                    raise ValueError("response.done references an inactive response")
                if has_output:
                    turn.record_omission(
                        "openai.realtime.response.done.response.output",
                        capture_class="model_payload",
                    )
                if has_status_details:
                    turn.record_omission(
                        "openai.realtime.response.done.response.status_details",
                        capture_class="diagnostic_payload",
                    )
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
                gesture = self._response_interruption_gestures.get(response_id)
                if (
                    provider_status == "cancelled"
                    and gesture is not None
                    and gesture not in self._accepted_interruption_gestures
                ):
                    turn.record_event(
                        "earshot.interruption.accepted",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="response.done.status.cancelled",
                        attributes=attributes,
                    )
                    self._accepted_interruption_gestures.add(gesture)
                self._active_responses.discard(response_id)
                self._response_interruption_gestures.pop(response_id, None)
                if (
                    gesture is not None
                    and gesture not in self._response_interruption_gestures.values()
                ):
                    self._accepted_interruption_gestures.discard(gesture)

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=optional_string(payload.get("event_id"), "event_id"),
        )
