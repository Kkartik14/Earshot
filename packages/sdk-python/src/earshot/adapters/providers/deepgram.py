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

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
    ) -> AdapterUpdate:
        """Validate one provider event and return an idempotent recorder update."""

        payload = require_mapping(payload, "payload")
        event_type = require_string(payload.get("type"), "type")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")
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

            return self._remember(
                payload, create_cursor_update, observed_at_ms=receipt_ms
            )
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
        alternative = require_mapping(alternatives[0], "channel.alternatives[0]")
        require_string(alternative.get("transcript"), "transcript", allow_empty=True)
        transcript_confidence = optional_probability(
            alternative.get("confidence"), "confidence"
        )
        metadata = require_mapping(payload.get("metadata", {}), "metadata")
        native_request_id = optional_string(metadata.get("request_id"), "request_id")

        def create_update(update_id: str) -> AdapterUpdate:
            correlation_id = self._opaque_id(
                "request", native_request_id or update_id
            )
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
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
