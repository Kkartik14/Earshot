"""Deepgram streaming STT event normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...pipeline import TurnRecorder
from ...privacy import sanitize_semantic_label
from .base import (
    AdapterUpdate,
    ProviderAdapter,
    optional_probability,
    optional_string,
    require_bool,
    require_mapping,
    require_nonnegative_integer,
    require_nonnegative_number,
    require_string,
    safe_attributes,
)


class DeepgramAdapter(ProviderAdapter):
    """Map Deepgram JSON events without importing the Deepgram SDK."""

    def __init__(
        self,
        *,
        model: str | None = None,
        identity_key: bytes | None = None,
    ) -> None:
        super().__init__("deepgram", identity_key=identity_key)
        self.model = sanitize_semantic_label(optional_string(model, "model"))
        self._flux_turn_states: dict[int, str] = {}

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
        agent_output_active: bool = False,
    ) -> AdapterUpdate:
        """Validate one provider event and return an idempotent recorder update."""

        payload = require_mapping(payload, "payload")
        event_type = require_string(payload.get("type"), "type")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")
        output_active = require_bool(agent_output_active, "agent_output_active")
        if event_type == "TurnInfo":
            return self._adapt_flux_turn(
                payload,
                receipt_ms,
                agent_output_active=output_active,
            )
        cursor_signals = {
            "SpeechStarted": (
                "timestamp",
                "deepgram.stt.speech_started_offset",
                "deepgram.speech_started",
            ),
            "UtteranceEnd": (
                "last_word_end",
                "deepgram.stt.last_word_end_offset",
                "deepgram.utterance_end",
            ),
        }
        if event_type in cursor_signals:
            field_name, measurement_name, event_name = cursor_signals[event_type]
            cursor_seconds = require_nonnegative_number(payload.get(field_name), field_name)

            def create_cursor_update(update_id: str) -> AdapterUpdate:
                correlation_id = self._opaque_id("stream", "active")
                attributes = safe_attributes(correlation_id, event_type)

                def apply_update(turn: TurnRecorder) -> None:
                    turn.record_measurement(
                        measurement_name,
                        cursor_seconds,
                        unit="s",
                        source="provider",
                        confidence="measured",
                        source_field=field_name,
                        basis="audio_stream_cursor",
                        at_ms=receipt_ms,
                        attributes=attributes,
                    )
                    turn.record_event(
                        event_name,
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field=f"deepgram.{event_type}.receipt",
                        attributes=attributes,
                    )

                return AdapterUpdate(
                    provider="deepgram",
                    event_type=event_type,
                    update_id=update_id,
                    correlation_id=correlation_id,
                    _apply_update=apply_update,
                )

            return self._remember(payload, create_cursor_update, observed_at_ms=receipt_ms)
        if event_type != "Results":
            raise ValueError(f"unsupported Deepgram event type: {event_type}")

        start_seconds = require_nonnegative_number(payload.get("start"), "start")
        duration_seconds = require_nonnegative_number(payload.get("duration"), "duration")
        is_final = require_bool(payload.get("is_final"), "is_final")
        speech_final = require_bool(payload.get("speech_final"), "speech_final")
        from_finalize = require_bool(payload.get("from_finalize", False), "from_finalize")
        turn_commit = is_final and (speech_final or from_finalize)
        if (speech_final or from_finalize) and not is_final:
            raise ValueError("speech_final/from_finalize requires is_final")
        channel = require_mapping(payload.get("channel"), "channel")
        alternatives = channel.get("alternatives")
        if (
            not isinstance(alternatives, Sequence)
            or isinstance(alternatives, (str, bytes))
            or not alternatives
        ):
            raise ValueError("channel.alternatives must be a non-empty array")
        omission_paths: list[str] = []
        transcript_confidence: float | None = None
        for index, item in enumerate(alternatives):
            alternative = require_mapping(item, f"channel.alternatives[{index}]")
            require_string(
                alternative.get("transcript"),
                f"channel.alternatives[{index}].transcript",
                allow_empty=True,
            )
            omission_paths.append(
                f"deepgram.Results.channel.alternatives[{index}].transcript"
            )
            if "words" in alternative:
                omission_paths.append(
                    f"deepgram.Results.channel.alternatives[{index}].words"
                )
            if index == 0:
                transcript_confidence = optional_probability(
                    alternative.get("confidence"), "confidence"
                )
        retained_omission_paths = tuple(omission_paths)
        metadata = require_mapping(payload.get("metadata", {}), "metadata")
        native_request_id = optional_string(metadata.get("request_id"), "request_id")

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("request", native_request_id or update_id)
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                for field_name in retained_omission_paths:
                    turn.record_omission(
                        field_name,
                        capture_class="transcript",
                    )
                operation_id = turn.record_stage(
                    "stt",
                    "deepgram",
                    model=self.model,
                    at_ms=receipt_ms,
                    source="app",
                    confidence="inferred",
                    source_field="deepgram.Results",
                    attributes=attributes,
                )
                turn.record_measurement(
                    "deepgram.stt.segment_start",
                    start_seconds,
                    unit="s",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="start",
                    basis="audio_stream_cursor",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "deepgram.stt.segment_duration",
                    duration_seconds,
                    unit="s",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="duration",
                    basis="audio_stream_cursor",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                if transcript_confidence is not None:
                    turn.record_measurement(
                        "deepgram.stt.transcript_confidence",
                        transcript_confidence,
                        unit="1",
                        operation_id=operation_id,
                        source="provider",
                        confidence="measured",
                        source_field="channel.alternatives.confidence",
                        at_ms=receipt_ms,
                        attributes=attributes,
                    )
                if turn_commit:
                    turn.record_event(
                        "earshot.transcript.final",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="deepgram.Results.is_final.receipt",
                        attributes=attributes,
                    )
                if speech_final:
                    turn.record_event(
                        "earshot.turn.committed",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="deepgram.Results.speech_final.receipt",
                        attributes=attributes,
                    )
                if from_finalize:
                    turn.record_event(
                        "deepgram.finalize_completed",
                        at_ms=receipt_ms,
                        participant="user",
                        source="app",
                        confidence="estimated",
                        source_field="deepgram.Results.from_finalize.receipt",
                        attributes=attributes,
                    )

            return AdapterUpdate(
                provider="deepgram",
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
                turn_commit=turn_commit,
            )

        return self._remember(payload, create_update, observed_at_ms=receipt_ms)

    def _adapt_flux_turn(
        self,
        payload: Mapping[str, object],
        receipt_ms: float,
        *,
        agent_output_active: bool,
    ) -> AdapterUpdate:
        request_id = require_string(payload.get("request_id"), "request_id")
        sequence_id = require_nonnegative_integer(payload.get("sequence_id"), "sequence_id")
        lifecycle = require_string(payload.get("event"), "event")
        if lifecycle not in {
            "Update",
            "StartOfTurn",
            "EagerEndOfTurn",
            "TurnResumed",
            "EndOfTurn",
        }:
            raise ValueError(f"unsupported Deepgram Flux lifecycle event: {lifecycle}")
        turn_index = require_nonnegative_integer(payload.get("turn_index"), "turn_index")
        window_start = require_nonnegative_number(
            payload.get("audio_window_start"), "audio_window_start"
        )
        window_end = require_nonnegative_number(payload.get("audio_window_end"), "audio_window_end")
        if window_end < window_start:
            raise ValueError("audio_window_end must not precede audio_window_start")
        require_string(
            payload.get("transcript"),
            "transcript",
            allow_empty=lifecycle != "StartOfTurn",
        )
        words = payload.get("words")
        if not isinstance(words, Sequence) or isinstance(words, (str, bytes)):
            raise ValueError("words must be an array")
        for index, word in enumerate(words):
            require_mapping(word, f"words[{index}]")
        end_of_turn_confidence = optional_probability(
            payload.get("end_of_turn_confidence"), "end_of_turn_confidence"
        )
        if end_of_turn_confidence is None:
            raise ValueError("end_of_turn_confidence is required")
        detected_languages: tuple[str, ...] = ()
        languages_observed = "languages" in payload
        for field_name in ("languages", "languages_hinted"):
            values = payload.get(field_name)
            if values is None:
                continue
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ValueError(f"{field_name} must be an array")
            validated = tuple(
                require_string(value, f"{field_name}[{index}]")
                for index, value in enumerate(values)
            )
            if field_name == "languages":
                detected_languages = validated

        event_names = {
            "Update": "deepgram.flux.update",
            "StartOfTurn": "earshot.speech.started",
            "EagerEndOfTurn": "earshot.turn.proposed",
            "TurnResumed": "earshot.turn.cancelled",
            "EndOfTurn": "earshot.turn.committed",
        }

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id("request", request_id)
            attributes = safe_attributes(correlation_id, f"TurnInfo.{lifecycle}")
            if len(detected_languages) == 1:
                attributes["earshot.language.code"] = detected_languages[0]

            def apply_update(turn: TurnRecorder) -> None:
                prior = self._flux_turn_states.get(turn_index)
                allowed_prior = {
                    "Update": {None, "update", "started", "eager", "resumed"},
                    "StartOfTurn": {None, "update"},
                    "EagerEndOfTurn": {"started", "resumed"},
                    "TurnResumed": {"eager"},
                    "EndOfTurn": {"started", "eager", "resumed"},
                }[lifecycle]
                if prior not in allowed_prior:
                    raise ValueError(f"invalid Deepgram Flux transition {prior!r} -> {lifecycle}")
                turn.record_omission(
                    "deepgram.TurnInfo.transcript",
                    capture_class="transcript",
                )
                turn.record_omission(
                    "deepgram.TurnInfo.words",
                    capture_class="transcript",
                )
                operation_id: str | None = None
                if lifecycle == "EndOfTurn":
                    operation_id = turn.record_stage(
                        "stt",
                        "deepgram",
                        model=self.model,
                        at_ms=receipt_ms,
                        source="provider",
                        confidence="inferred",
                        source_field="deepgram.TurnInfo.EndOfTurn",
                        attributes=attributes,
                    )
                    turn.record_event(
                        "earshot.transcript.final",
                        at_ms=receipt_ms,
                        participant="user",
                        source="provider",
                        confidence="inferred",
                        source_field="deepgram.TurnInfo.EndOfTurn",
                        attributes=attributes,
                    )
                    if languages_observed:
                        turn.record_measurement(
                            "deepgram.flux.language_count",
                            len(detected_languages),
                            unit="1",
                            operation_id=operation_id,
                            source="provider",
                            confidence="measured",
                            source_field="languages",
                            at_ms=receipt_ms,
                            attributes=attributes,
                        )
                turn.record_measurement(
                    "deepgram.flux.audio_window_start",
                    window_start,
                    unit="s",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="audio_window_start",
                    basis="audio_stream_cursor",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "deepgram.flux.audio_window_end",
                    window_end,
                    unit="s",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="audio_window_end",
                    basis="audio_stream_cursor",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_measurement(
                    "deepgram.flux.end_of_turn_confidence",
                    end_of_turn_confidence,
                    unit="1",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="end_of_turn_confidence",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_event(
                    event_names[lifecycle],
                    at_ms=receipt_ms,
                    participant="user",
                    source="provider",
                    confidence="inferred",
                    source_field=f"deepgram.TurnInfo.{lifecycle}",
                    attributes=attributes,
                )
                if lifecycle == "StartOfTurn" and agent_output_active:
                    turn.record_event(
                        "earshot.interruption.detected",
                        at_ms=receipt_ms,
                        participant="user",
                        source="provider",
                        confidence="inferred",
                        source_field="deepgram.TurnInfo.StartOfTurn",
                        attributes=attributes,
                    )
                next_state = {
                    "Update": prior or "update",
                    "StartOfTurn": "started",
                    "EagerEndOfTurn": "eager",
                    "TurnResumed": "resumed",
                    "EndOfTurn": "ended",
                }[lifecycle]
                self._flux_turn_states[turn_index] = next_state

            return AdapterUpdate(
                provider=self.provider,
                event_type=f"TurnInfo.{lifecycle}",
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
                turn_commit=lifecycle == "EndOfTurn",
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=f"{request_id}:{sequence_id}",
            fingerprint_context={"agent_output_active": agent_output_active},
        )
