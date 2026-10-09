"""OpenAI Realtime event normalization without invented STT/LLM/TTS stages."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

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

# Hard cap on how many responses one session tracks the lifecycle of. Realtime
# runs one response at a time, so a session that reaches this has hundreds of
# responses whose ``response.done`` never arrived; the cap is what keeps that
# session's memory bounded instead of proportional to its length.
MAX_TRACKED_RESPONSES = 512

COVERAGE_RESPONSE_LIFECYCLE = "openai.realtime.response_lifecycle"
COVERAGE_REASON_CAP = "tracked_response_cap_exceeded"


@dataclass
class _ResponseState:
    """Everything one ``response.created`` obliges the adapter to remember.

    Held as a single record rather than as five parallel maps so that dropping a
    response is one deletion. Parallel maps make eviction a checklist, and a
    checklist is the kind of thing a later change forgets one line of.
    """

    started_ms: float
    speech_stopped_ms: float | None
    active: bool = True
    first_audio_seen: bool = False
    interruption_gesture: int | None = None


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

        Two different unbounded growths meet here. Across sessions, a reused
        adapter would carry the entries of responses that were still in flight
        when the session ended, so ``close()`` resets all of them. Within one
        session, ``_responses`` is capped by :data:`MAX_TRACKED_RESPONSES` and
        evicts oldest-first, because ``response.done`` is the only thing that
        retires a response and a provider is under no obligation to send one.
        """

        self._speech_stopped_receipt_ms: float | None = None
        self._responses: OrderedDict[str, _ResponseState] = OrderedDict()
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


    def _evict_tracked_responses(self, turn: TurnRecorder) -> None:
        """Bound the per-response map, and say so rather than lose it quietly.

        Eviction is oldest-first: the response created longest ago is the one
        least likely to still receive events. What it must not do is degrade the
        *evidence*. Every later event for an evicted response now takes the same
        path an event for a response that was never created takes, and every one
        of those paths raises rather than guessing — so the adapter cannot emit a
        response latency measured against a start time it no longer has, or an
        agent stage whose duration it would have to invent. Refusing the event is
        louder than a wrong number, and the coverage note below is what turns the
        refusal into a declared limitation instead of an unexplained gap.

        The note deliberately carries no ``dropped_count``. The recorder's
        coverage ledger is first-write-wins for a non-``available`` signal, so a
        count written at the first eviction would freeze at a value every later
        eviction falsifies. An absent count claims nothing; a stale one would.
        """

        if len(self._responses) <= MAX_TRACKED_RESPONSES:
            return
        while len(self._responses) > MAX_TRACKED_RESPONSES:
            _, evicted = self._responses.popitem(last=False)
            self._retire_gesture(evicted.interruption_gesture)
        turn.record_coverage(
            COVERAGE_RESPONSE_LIFECYCLE,
            "partial",
            COVERAGE_REASON_CAP,
        )

    def _retire_gesture(self, gesture: int | None) -> None:
        """Forget an accepted interruption once no tracked response carries it.

        The accepted set exists only to keep one interruption gesture from being
        reported twice, so it is bounded by the responses that still reference a
        gesture -- which the cap above already bounds.
        """

        if gesture is None:
            return
        if any(state.interruption_gesture == gesture for state in self._responses.values()):
            return
        self._accepted_interruption_gestures.discard(gesture)

    def _speech_started(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        audio_start_ms = require_nonnegative_number(payload.get("audio_start_ms"), "audio_start_ms")
        event_type = "input_audio_buffer.speech_started"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "item", optional_string(payload.get("item_id"), "item_id") or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                interrupted_responses = [
                    response_id for response_id, state in self._responses.items() if state.active
                ]
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
                        self._responses[response_id].interruption_gesture = gesture

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
                if response_id in self._responses:
                    raise ValueError("response was already created")
                self._responses[response_id] = _ResponseState(
                    started_ms=receipt_ms,
                    speech_stopped_ms=self._speech_stopped_receipt_ms,
                )
                self._evict_tracked_responses(turn)

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
                state = self._responses.get(response_id)
                if state is None:
                    raise ValueError("audio delta references an unknown response")
                if state.first_audio_seen:
                    turn.record_omission(
                        "openai.realtime.response.output_audio.delta.delta",
                        capture_class="audio",
                    )
                    return
                if not state.active:
                    raise ValueError("audio delta references an inactive response")
                speech_stopped_ms = state.speech_stopped_ms
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
                state.first_audio_seen = True

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
                state = self._responses.get(response_id)
                if state is None:
                    raise ValueError("audio done references an unknown response")
                if not state.active:
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
                state = self._responses.get(response_id)
                if state is None:
                    raise ValueError("response.done references an unknown response")
                started_ms = state.started_ms
                if receipt_ms < started_ms:
                    raise ValueError("response.done precedes response.created")
                if not state.active:
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
                gesture = state.interruption_gesture
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
                state.active = False
                state.interruption_gesture = None
                self._retire_gesture(gesture)

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
