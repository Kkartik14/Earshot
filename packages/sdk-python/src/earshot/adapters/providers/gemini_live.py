"""Gemini Live (BidiGenerateContent) event normalization without invented stages.

Gemini Live is a native speech-to-speech runtime: one bidirectional session streams
user audio up and model audio down with no separately observable STT, LLM, or TTS
boundary. This adapter therefore projects a model turn into exactly ONE fused
``agent`` operation, mirroring :mod:`earshot.adapters.providers.openai_realtime`.

Unlike OpenAI Realtime, Gemini emits no discrete server ``speech_stopped`` message
and no per-response identifier. The turn boundary that anchors response latency is a
client turn signal the application already owns on its half of the bidi socket
(``realtimeInput.activityEnd`` or ``clientContent.turnComplete``); the fused response
is correlated by an opaque session-scoped id because Gemini exposes no native one.
Interruption acceptance is authored only when a real client speech gesture led to a
provider ``interrupted`` signal, exactly as OpenAI Realtime gates acceptance on a
speech-started gesture that ends in a cancelled response.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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

_MISSING: Any = object()

# Gemini reports token usage per modality; map each governed input/output modality
# to the canonical name already used by the LiveKit and Pipecat adapters. Names are
# reused verbatim so a Gemini session and a framework session aggregate identically.
_INPUT_MODALITY_METRICS = {
    "AUDIO": "gen_ai.usage.input_audio_tokens",
    "TEXT": "gen_ai.usage.input_text_tokens",
    "IMAGE": "gen_ai.usage.input_image_tokens",
}
_OUTPUT_MODALITY_METRICS = {
    "AUDIO": "gen_ai.usage.output_audio_tokens",
    "TEXT": "gen_ai.usage.output_text_tokens",
    "IMAGE": "gen_ai.usage.output_image_tokens",
}
_USAGE_COUNTERS = (
    (("promptTokenCount", "prompt_token_count"), "gen_ai.usage.input_tokens"),
    (
        (
            "responseTokenCount",
            "response_token_count",
            "candidatesTokenCount",
            "candidates_token_count",
        ),
        "gen_ai.usage.output_tokens",
    ),
    (("totalTokenCount", "total_token_count"), "earshot.metric.model.total_tokens"),
)


def _pluck(mapping: Mapping[str, object], *names: str) -> object:
    """Return the first present value across camelCase/snake_case field spellings."""

    for name in names:
        if name in mapping:
            return mapping[name]
    return _MISSING


def _present(mapping: Mapping[str, object], *names: str) -> bool:
    return any(name in mapping for name in names)


def _flag(mapping: Mapping[str, object], *names: str) -> bool:
    """Read a Gemini boolean control flag; absent means false, present must be bool."""

    value = _pluck(mapping, *names)
    if value is _MISSING:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{names[0]} must be a boolean")
    return value


class GeminiLiveAdapter(ProviderAdapter):
    """Map one Gemini Live stream into fused ``agent`` response evidence."""

    def __init__(
        self,
        *,
        model: str,
        identity_key: bytes | None = None,
    ) -> None:
        super().__init__("gemini", identity_key=identity_key)
        self.model = sanitize_semantic_label(require_string(model, "model"))
        self._pending_speech_stopped_ms: float | None = None
        self._response_open = False
        self._response_started_ms: float | None = None
        self._response_first_audio = False
        self._response_speech_stopped_ms: float | None = None
        self._open_response_gesture: int | None = None
        self._next_gesture = 0

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
    ) -> AdapterUpdate:
        """Validate one bidi message and return a content-free recorder update.

        Dispatch is on message shape: the server messages Gemini streams down, plus
        the client turn signals the application observes on its own upstream half.
        """

        payload = require_mapping(payload, "payload")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")

        if _present(payload, "setupComplete", "setup_complete"):
            return self._setup_complete(payload, receipt_ms)
        server_content = _pluck(payload, "serverContent", "server_content")
        if server_content is not _MISSING:
            return self._server_content(
                payload, require_mapping(server_content, "serverContent"), receipt_ms
            )
        tool_call = _pluck(payload, "toolCall", "tool_call")
        if tool_call is not _MISSING:
            return self._tool_call(payload, require_mapping(tool_call, "toolCall"), receipt_ms)
        cancellation = _pluck(payload, "toolCallCancellation", "tool_call_cancellation")
        if cancellation is not _MISSING:
            return self._tool_call_cancellation(
                payload, require_mapping(cancellation, "toolCallCancellation"), receipt_ms
            )
        usage = _pluck(payload, "usageMetadata", "usage_metadata")
        if usage is not _MISSING:
            return self._usage_metadata(
                payload, require_mapping(usage, "usageMetadata"), receipt_ms
            )
        if _present(payload, "goAway", "go_away"):
            return self._go_away(payload, receipt_ms)
        if _present(payload, "sessionResumptionUpdate", "session_resumption_update"):
            return self._session_resumption(payload, receipt_ms)
        realtime_input = _pluck(payload, "realtimeInput", "realtime_input")
        if realtime_input is not _MISSING:
            return self._realtime_input(
                payload, require_mapping(realtime_input, "realtimeInput"), receipt_ms
            )
        client_content = _pluck(payload, "clientContent", "client_content")
        if client_content is not _MISSING:
            return self._client_content(
                payload, require_mapping(client_content, "clientContent"), receipt_ms
            )
        raise ValueError("unsupported Gemini Live message shape")

    # -- server messages -----------------------------------------------------

    def _setup_complete(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        event_type = "setupComplete"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("session", "live")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_event(
                    "gemini.live.setup_complete",
                    at_ms=receipt_ms,
                    participant="agent",
                    source="app",
                    confidence="estimated",
                    source_field="setupComplete",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _server_content(
        self,
        payload: Mapping[str, object],
        content: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        model_turn = _pluck(content, "modelTurn", "model_turn")
        has_model_turn = model_turn is not _MISSING
        audio_paths: tuple[str, ...] = ()
        content_paths: tuple[str, ...] = ()
        has_audio = False
        if has_model_turn:
            audio_paths, content_paths, has_audio = self._parse_parts(
                require_mapping(model_turn, "modelTurn")
            )
        input_transcript = _present(content, "inputTranscription", "input_transcription")
        output_transcript = _present(content, "outputTranscription", "output_transcription")
        interrupted = _flag(content, "interrupted")
        generation_complete = _flag(content, "generationComplete", "generation_complete")
        turn_complete = _flag(content, "turnComplete", "turn_complete")
        # Gemini delivers usageMetadata as a top-level sibling of serverContent, not
        # nested inside it; read it from the whole message so a fused turn's tokens
        # are captured on the same server frame that closes the turn.
        usage = _pluck(payload, "usageMetadata", "usage_metadata")
        usage_measurements = (
            self._parse_usage(require_mapping(usage, "usageMetadata"))
            if usage is not _MISSING
            else ()
        )
        event_type = "serverContent"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("session", "live")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for path in audio_paths:
                    turn.record_omission(path, capture_class="audio")
                for path in content_paths:
                    turn.record_omission(path, capture_class="model_payload")
                if input_transcript:
                    turn.record_omission(
                        "gemini.live.serverContent.inputTranscription",
                        capture_class="transcript",
                    )
                if output_transcript:
                    turn.record_omission(
                        "gemini.live.serverContent.outputTranscription",
                        capture_class="transcript",
                    )
                if has_model_turn and not self._response_open:
                    self._open_response(receipt_ms)
                if has_audio and self._response_open and not self._response_first_audio:
                    self._author_first_audio(turn, receipt_ms, attributes)
                for name, value in usage_measurements:
                    self._record_usage_measurement(turn, name, value, attributes, receipt_ms)
                if interrupted:
                    self._close_response(
                        turn,
                        status="cancelled",
                        receipt_ms=receipt_ms,
                        attributes=attributes,
                        interrupted=True,
                        source_field="serverContent.interrupted",
                    )
                    turn.record_event(
                        "gemini.live.interrupted",
                        at_ms=receipt_ms,
                        participant="agent",
                        source="app",
                        confidence="estimated",
                        source_field="serverContent.interrupted",
                        attributes=attributes,
                    )
                elif turn_complete or generation_complete:
                    self._close_response(
                        turn,
                        status="ok",
                        receipt_ms=receipt_ms,
                        attributes=attributes,
                        interrupted=False,
                        source_field="serverContent.turnComplete"
                        if turn_complete
                        else "serverContent.generationComplete",
                    )
                if generation_complete:
                    turn.record_event(
                        "gemini.live.generation_complete",
                        at_ms=receipt_ms,
                        participant="agent",
                        source="app",
                        confidence="estimated",
                        source_field="serverContent.generationComplete",
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
                turn_commit=turn_complete,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _tool_call(
        self,
        payload: Mapping[str, object],
        tool_call: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        calls = _pluck(tool_call, "functionCalls", "function_calls")
        if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)) or not calls:
            raise ValueError("toolCall.functionCalls must be a non-empty array")
        parsed: list[tuple[str | None, bool, int]] = []
        for index, item in enumerate(calls):
            call = require_mapping(item, f"toolCall.functionCalls[{index}]")
            raw_id = _pluck(call, "id")
            native_id = optional_string(None if raw_id is _MISSING else raw_id, "id")
            parsed.append((native_id, _present(call, "args"), index))
        base_native = next((cid for cid, _, _ in parsed if cid is not None), None)
        event_type = "toolCall"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("tool_call", base_native or update_id)
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for _native_id, args_present, index in parsed:
                    if args_present:
                        turn.record_omission(
                            f"gemini.live.toolCall.functionCalls[{index}].args",
                            capture_class="tool_payload",
                        )
                    turn.record_stage(
                        "tool",
                        "gemini",
                        model=self.model,
                        status="ok",
                        at_ms=receipt_ms,
                        source="app",
                        confidence="estimated",
                        source_field="toolCall.functionCalls",
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _tool_call_cancellation(
        self,
        payload: Mapping[str, object],
        cancellation: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        ids = _pluck(cancellation, "ids")
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)) or not ids:
            raise ValueError("toolCallCancellation.ids must be a non-empty array")
        cancelled_ids = tuple(
            require_string(value, f"toolCallCancellation.ids[{index}]")
            for index, value in enumerate(ids)
        )
        event_type = "toolCallCancellation"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("tool_call", cancelled_ids[0])
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for _native_id in cancelled_ids:
                    turn.record_stage(
                        "tool",
                        "gemini",
                        model=self.model,
                        status="cancelled",
                        at_ms=receipt_ms,
                        source="app",
                        confidence="estimated",
                        source_field="toolCallCancellation.ids",
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _usage_metadata(
        self,
        payload: Mapping[str, object],
        usage: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        measurements = self._parse_usage(usage)
        event_type = "usageMetadata"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("session", "live")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for name, value in measurements:
                    self._record_usage_measurement(turn, name, value, attributes, receipt_ms)

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _go_away(self, payload: Mapping[str, object], receipt_ms: float) -> AdapterUpdate:
        event_type = "goAway"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("session", "live")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                self._close_response(
                    turn,
                    status="incomplete",
                    receipt_ms=receipt_ms,
                    attributes=attributes,
                    interrupted=False,
                    source_field="goAway",
                )
                turn.record_event(
                    "gemini.live.go_away",
                    at_ms=receipt_ms,
                    participant="agent",
                    source="app",
                    confidence="estimated",
                    source_field="goAway",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _session_resumption(
        self, payload: Mapping[str, object], receipt_ms: float
    ) -> AdapterUpdate:
        update = _pluck(payload, "sessionResumptionUpdate", "session_resumption_update")
        handle_present = isinstance(update, Mapping) and _present(update, "newHandle", "new_handle")
        event_type = "sessionResumptionUpdate"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("session", "live")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                if handle_present:
                    turn.record_omission(
                        "gemini.live.sessionResumptionUpdate.newHandle",
                        capture_class="diagnostic_payload",
                    )
                turn.record_event(
                    "gemini.live.session_resumption_update",
                    at_ms=receipt_ms,
                    participant="agent",
                    source="app",
                    confidence="estimated",
                    source_field="sessionResumptionUpdate",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    # -- client turn signals -------------------------------------------------

    def _realtime_input(
        self,
        payload: Mapping[str, object],
        realtime_input: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        has_start = _present(realtime_input, "activityStart", "activity_start")
        has_end = _present(realtime_input, "activityEnd", "activity_end")
        media_paths = tuple(
            f"gemini.live.realtimeInput.{field}"
            for field in ("audio", "video", "text", "mediaChunks", "media_chunks")
            if field in realtime_input
        )
        if not has_start and not has_end and not media_paths:
            raise ValueError("realtimeInput must carry activityStart, activityEnd, or media")
        event_type = "realtimeInput"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("stream", "client")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for path in media_paths:
                    turn.record_omission(path, capture_class="audio")
                if has_start:
                    turn.record_event(
                        "earshot.speech.started",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="realtimeInput.activityStart",
                        attributes=attributes,
                    )
                    if self._response_open and self._open_response_gesture is None:
                        self._open_response_gesture = self._next_gesture
                        self._next_gesture += 1
                        turn.record_event(
                            "earshot.interruption.detected",
                            at_ms=receipt_ms,
                            participant="user",
                            source="app",
                            confidence="estimated",
                            source_field="realtimeInput.activityStart",
                            attributes=attributes,
                        )
                if has_end:
                    turn.record_event(
                        "earshot.speech.ended",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="realtimeInput.activityEnd",
                        attributes=attributes,
                    )
                    self._pending_speech_stopped_ms = receipt_ms

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _client_content(
        self,
        payload: Mapping[str, object],
        client_content: Mapping[str, object],
        receipt_ms: float,
    ) -> AdapterUpdate:
        turns_present = _present(client_content, "turns")
        turn_complete = _flag(client_content, "turnComplete", "turn_complete")
        event_type = "clientContent"

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("stream", "client")
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                if turns_present:
                    turn.record_omission(
                        "gemini.live.clientContent.turns",
                        capture_class="model_payload",
                    )
                if turn_complete:
                    turn.record_event(
                        "earshot.speech.ended",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="clientContent.turnComplete",
                        attributes=attributes,
                    )
                    self._pending_speech_stopped_ms = receipt_ms

            return AdapterUpdate(
                provider=self.provider,
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
                turn_commit=turn_complete,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    # -- fused response lifecycle -------------------------------------------

    def _open_response(self, receipt_ms: float) -> None:
        self._response_open = True
        self._response_started_ms = receipt_ms
        self._response_first_audio = False
        self._response_speech_stopped_ms = self._pending_speech_stopped_ms
        self._open_response_gesture = None

    def _author_first_audio(
        self, turn: TurnRecorder, receipt_ms: float, attributes: dict[str, Any]
    ) -> None:
        speech_stopped_ms = self._response_speech_stopped_ms
        if speech_stopped_ms is not None and receipt_ms < speech_stopped_ms:
            raise ValueError("first audio receipt precedes speech-stopped receipt")
        turn.record_event(
            "earshot.audio.first_packet_received",
            at_ms=receipt_ms,
            participant="agent",
            source="app",
            confidence="estimated",
            source_field="serverContent.modelTurn.inlineData",
            attributes=attributes,
        )
        if speech_stopped_ms is not None:
            turn.record_measurement(
                "earshot.turn.response_latency",
                receipt_ms - speech_stopped_ms,
                unit="ms",
                source="app",
                confidence="estimated",
                source_field="serverContent.modelTurn.inlineData",
                basis="client_turn_stop_receipt_to_first_audio_receipt",
                at_ms=receipt_ms,
                attributes=attributes,
            )
        self._response_first_audio = True

    def _close_response(
        self,
        turn: TurnRecorder,
        *,
        status: str,
        receipt_ms: float,
        attributes: dict[str, Any],
        interrupted: bool,
        source_field: str,
    ) -> None:
        if not self._response_open:
            return
        started_ms = self._response_started_ms
        assert started_ms is not None
        if receipt_ms < started_ms:
            raise ValueError("response completion precedes its start")
        turn.record_stage(
            "agent",
            "gemini",
            model=self.model,
            status=status,
            at_ms=started_ms,
            ended_at_ms=receipt_ms,
            source="app",
            confidence="estimated",
            source_field=source_field,
            attributes=attributes,
        )
        if interrupted and self._open_response_gesture is not None:
            turn.record_event(
                "earshot.interruption.accepted",
                at_ms=receipt_ms,
                participant="user",
                source="app",
                confidence="estimated",
                source_field="serverContent.interrupted",
                attributes=attributes,
            )
        self._response_open = False
        self._response_started_ms = None
        self._response_first_audio = False
        self._response_speech_stopped_ms = None
        self._open_response_gesture = None

    # -- parsing helpers -----------------------------------------------------

    def _parse_parts(
        self, model_turn: Mapping[str, object]
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        parts = _pluck(model_turn, "parts")
        if parts is _MISSING:
            return (), (), False
        if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
            raise ValueError("modelTurn.parts must be an array")
        audio_paths: list[str] = []
        content_paths: list[str] = []
        has_audio = False
        for index, item in enumerate(parts):
            part = require_mapping(item, f"modelTurn.parts[{index}]")
            inline = _pluck(part, "inlineData", "inline_data")
            text = _pluck(part, "text")
            base = f"gemini.live.serverContent.modelTurn.parts[{index}]"
            if inline is not _MISSING:
                inline_data = require_mapping(inline, f"{base}.inlineData")
                data = _pluck(inline_data, "data")
                if data is not _MISSING:
                    require_string(data, f"{base}.inlineData.data", allow_empty=True)
                audio_paths.append(f"{base}.inlineData")
                has_audio = True
            elif text is not _MISSING:
                require_string(text, f"{base}.text", allow_empty=True)
                content_paths.append(f"{base}.text")
            else:
                content_paths.append(base)
        return tuple(audio_paths), tuple(content_paths), has_audio

    def _parse_usage(self, usage: Mapping[str, object]) -> tuple[tuple[str, int], ...]:
        measurements: list[tuple[str, int]] = []
        for names, metric in _USAGE_COUNTERS:
            raw = _pluck(usage, *names)
            if raw is not _MISSING:
                measurements.append((metric, require_nonnegative_integer(raw, names[0])))
        for detail_names, modality_metrics in (
            (("promptTokensDetails", "prompt_tokens_details"), _INPUT_MODALITY_METRICS),
            (("responseTokensDetails", "response_tokens_details"), _OUTPUT_MODALITY_METRICS),
        ):
            details = _pluck(usage, *detail_names)
            if details is _MISSING:
                continue
            if not isinstance(details, Sequence) or isinstance(details, (str, bytes)):
                raise ValueError(f"{detail_names[0]} must be an array")
            seen: set[str] = set()
            for index, item in enumerate(details):
                detail = require_mapping(item, f"{detail_names[0]}[{index}]")
                modality_raw = _pluck(detail, "modality")
                modality = require_string(
                    None if modality_raw is _MISSING else modality_raw,
                    f"{detail_names[0]}[{index}].modality",
                )
                count_raw = _pluck(detail, "tokenCount", "token_count")
                count = require_nonnegative_integer(
                    None if count_raw is _MISSING else count_raw,
                    f"{detail_names[0]}[{index}].tokenCount",
                )
                metric = modality_metrics.get(modality.upper())
                if metric is None:
                    continue
                if metric in seen:
                    raise ValueError(f"duplicate {detail_names[0]} modality: {modality}")
                seen.add(metric)
                measurements.append((metric, count))
        return tuple(measurements)

    def _record_usage_measurement(
        self,
        turn: TurnRecorder,
        name: str,
        value: int,
        attributes: dict[str, Any],
        receipt_ms: float,
    ) -> None:
        turn.record_measurement(
            name,
            value,
            unit="1",
            source="provider",
            confidence="measured",
            source_field="usageMetadata",
            at_ms=receipt_ms,
            attributes=attributes,
        )
