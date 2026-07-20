"""Cartesia WebSocket TTS event normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...pipeline import TurnRecorder
from ...privacy import sanitize_semantic_label
from .base import (
    AdapterUpdate,
    ProviderAdapter,
    optional_string,
    require_bool,
    require_mapping,
    require_nonnegative_integer,
    require_nonnegative_number,
    require_string,
    safe_attributes,
)


class CartesiaAdapter(ProviderAdapter):
    """Map Cartesia JSON messages without importing the Cartesia SDK."""

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        identity_key: bytes | None = None,
    ) -> None:
        super().__init__("cartesia", identity_key=identity_key)
        self.model = sanitize_semantic_label(optional_string(model, "model"))
        self.voice = sanitize_semantic_label(optional_string(voice, "voice"))
        self._audio_contexts: set[str] = set()

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        received_at_ms: float,
        request_sent_at_ms: float | None = None,
    ) -> AdapterUpdate:
        """Validate one WebSocket message and return an idempotent recorder update."""

        payload = require_mapping(payload, "payload")
        event_type = require_string(payload.get("type"), "type")
        receipt_ms = require_nonnegative_number(received_at_ms, "received_at_ms")
        if request_sent_at_ms is None:
            sent_ms = None
        else:
            sent_ms = require_nonnegative_number(request_sent_at_ms, "request_sent_at_ms")
            if sent_ms > receipt_ms:
                raise ValueError("request_sent_at_ms must not follow received_at_ms")
        if event_type in {"done", "error"}:
            return self._adapt_terminal(payload, event_type=event_type, receipt_ms=receipt_ms)
        if event_type in {"timestamps", "phoneme_timestamps"}:
            return self._adapt_timestamps(
                payload,
                event_type=event_type,
                receipt_ms=receipt_ms,
            )
        if event_type != "chunk":
            raise ValueError(f"unsupported Cartesia event type: {event_type}")

        native_context_id = require_string(payload.get("context_id"), "context_id")
        require_string(payload.get("data"), "data")
        step_time_ms = require_nonnegative_number(payload.get("step_time"), "step_time")
        if "done" in payload:
            require_bool(payload["done"], "done")
        correlation_id = self._opaque_id("context", native_context_id)

        def create_update(update_id: str) -> AdapterUpdate:
            attributes = safe_attributes(correlation_id, event_type)
            if self.voice is not None:
                attributes["earshot.tts.voice"] = self.voice

            def apply_update(turn: TurnRecorder) -> None:
                is_first_audio = correlation_id not in self._audio_contexts
                if is_first_audio and sent_ms is None:
                    raise ValueError("request_sent_at_ms is required for a context's first chunk")
                turn.record_omission(
                    "cartesia.chunk.data",
                    capture_class="audio",
                )
                operation_id: str | None = None
                if is_first_audio:
                    assert sent_ms is not None
                    operation_id = turn.record_stage(
                        "tts",
                        "cartesia",
                        model=self.model,
                        at_ms=sent_ms,
                        source="app",
                        confidence="inferred",
                        source_field="cartesia.request.sent",
                        attributes=attributes,
                    )
                turn.record_measurement(
                    "cartesia.tts.step_time",
                    step_time_ms,
                    unit="ms",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="step_time",
                    basis="per_chunk_server_processing",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                if is_first_audio:
                    assert sent_ms is not None
                    turn.record_measurement(
                        "earshot.tts.ttfb",
                        receipt_ms - sent_ms,
                        unit="ms",
                        operation_id=operation_id,
                        source="app",
                        confidence="estimated",
                        source_field="cartesia.chunk.receipt",
                        basis="request_send_to_first_audio_chunk_receipt",
                        at_ms=receipt_ms,
                        quality_kind="provider_latency",
                        attributes=attributes,
                    )
                    turn.record_event(
                        "earshot.audio.first_packet_received",
                        at_ms=receipt_ms,
                        participant="agent",
                        source="app",
                        confidence="estimated",
                        source_field="cartesia.chunk.receipt",
                        attributes=attributes,
                    )
                self._audio_contexts.add(correlation_id)

            return AdapterUpdate(
                provider="cartesia",
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
            )

        return self._remember(
            payload,
            create_update,
            observed_at_ms=receipt_ms,
            fingerprint_context={"request_sent_at_ms": sent_ms},
        )

    def _adapt_timestamps(
        self,
        payload: Mapping[str, object],
        *,
        event_type: str,
        receipt_ms: float,
    ) -> AdapterUpdate:
        if require_bool(payload.get("done"), "done") is not False:
            raise ValueError(f"Cartesia {event_type} must set done=false")
        status_code = require_nonnegative_integer(payload.get("status_code"), "status_code")
        native_context_id = require_string(payload.get("context_id"), "context_id")
        field_name = "word_timestamps" if event_type == "timestamps" else "phoneme_timestamps"
        label_name = "words" if event_type == "timestamps" else "phonemes"
        timestamps = require_mapping(payload.get(field_name), field_name)
        labels = timestamps.get(label_name)
        starts = timestamps.get("start")
        ends = timestamps.get("end")
        arrays = {label_name: labels, "start": starts, "end": ends}
        for name, values in arrays.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ValueError(f"{field_name}.{name} must be an array")
        assert isinstance(labels, Sequence)
        assert isinstance(starts, Sequence)
        assert isinstance(ends, Sequence)
        if len(labels) != len(starts) or len(starts) != len(ends):
            raise ValueError(f"{field_name} arrays must have equal lengths")
        normalized_starts: list[float] = []
        normalized_ends: list[float] = []
        for index, label in enumerate(labels):
            require_string(label, f"{field_name}.{label_name}[{index}]", allow_empty=True)
            start = require_nonnegative_number(starts[index], f"{field_name}.start[{index}]")
            end = require_nonnegative_number(ends[index], f"{field_name}.end[{index}]")
            if end < start:
                raise ValueError(f"{field_name}.end[{index}] must not precede start")
            normalized_starts.append(start)
            normalized_ends.append(end)
        label_count = len(labels)
        timeline_duration = (
            max(normalized_ends) - min(normalized_starts) if normalized_starts else None
        )
        correlation_id = self._opaque_id("context", native_context_id)

        def create_update(update_id: str) -> AdapterUpdate:
            attributes = safe_attributes(correlation_id, event_type)

            def apply_update(turn: TurnRecorder) -> None:
                turn.record_omission(
                    f"cartesia.{field_name}.{label_name}",
                    capture_class="transcript",
                )
                turn.record_measurement(
                    f"cartesia.tts.{label_name}_timestamp_count",
                    label_count,
                    unit="1",
                    source="provider",
                    confidence="measured",
                    source_field=f"{field_name}.{label_name}",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                if timeline_duration is not None:
                    turn.record_measurement(
                        f"cartesia.tts.{label_name}_timeline_duration",
                        timeline_duration,
                        unit="s",
                        source="provider",
                        confidence="measured",
                        source_field=field_name,
                        basis="provider_timestamp_frame",
                        at_ms=receipt_ms,
                        attributes=attributes,
                    )
                turn.record_measurement(
                    "cartesia.tts.status_code",
                    status_code,
                    unit="1",
                    source="provider",
                    confidence="measured",
                    source_field="status_code",
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

        return self._remember(
            payload,
            create_update,
            observed_at_ms=receipt_ms,
        )

    def _adapt_terminal(
        self,
        payload: Mapping[str, object],
        *,
        event_type: str,
        receipt_ms: float,
    ) -> AdapterUpdate:
        if require_bool(payload.get("done"), "done") is not True:
            raise ValueError(f"Cartesia {event_type} must set done=true")
        status_code = require_nonnegative_integer(payload.get("status_code"), "status_code")
        native_context_id = optional_string(payload.get("context_id"), "context_id")
        native_request_id = optional_string(payload.get("request_id"), "request_id")
        if event_type == "done" and native_context_id is None:
            raise ValueError("Cartesia done requires context_id")
        if event_type == "error" and native_request_id is None:
            raise ValueError("Cartesia error requires request_id")

        error_code: str | None = None
        has_title = "title" in payload
        has_message = "message" in payload
        if event_type == "error":
            error_code = optional_string(payload.get("error_code"), "error_code")
            if has_title:
                require_string(payload["title"], "title")
            if has_message:
                require_string(payload["message"], "message", allow_empty=True)
        native_correlation = native_context_id or native_request_id
        assert native_correlation is not None
        correlation_kind = "context" if native_context_id is not None else "request"
        correlation_id = self._opaque_id(correlation_kind, native_correlation)

        def create_update(update_id: str) -> AdapterUpdate:
            attributes = safe_attributes(correlation_id, event_type)
            if error_code is not None:
                attributes["error.type"] = sanitize_semantic_label(error_code)

            def apply_update(turn: TurnRecorder) -> None:
                if has_title:
                    turn.record_omission(
                        "cartesia.error.title",
                        capture_class="diagnostic_payload",
                    )
                if has_message:
                    turn.record_omission(
                        "cartesia.error.message",
                        capture_class="diagnostic_payload",
                    )
                operation_id: str | None = None
                if event_type == "error":
                    operation_id = turn.record_stage(
                        "tts",
                        "cartesia",
                        model=self.model,
                        status="error",
                        at_ms=receipt_ms,
                        source="app",
                        confidence="inferred",
                        source_field="cartesia.error.receipt",
                        attributes=attributes,
                    )
                turn.record_measurement(
                    "cartesia.tts.status_code",
                    status_code,
                    unit="1",
                    operation_id=operation_id,
                    source="provider",
                    confidence="measured",
                    source_field="status_code",
                    at_ms=receipt_ms,
                    attributes=attributes,
                )
                turn.record_event(
                    f"cartesia.tts.{event_type}",
                    at_ms=receipt_ms,
                    participant="agent",
                    source="app",
                    confidence="estimated",
                    source_field=f"cartesia.{event_type}.receipt",
                    attributes=attributes,
                )

            return AdapterUpdate(
                provider="cartesia",
                event_type=event_type,
                update_id=update_id,
                correlation_id=correlation_id,
                _apply_update=apply_update,
                terminal=True,
            )

        return self._remember(
            payload,
            create_update,
            native_update_id=f"{correlation_kind}:{native_correlation}:{event_type}",
        )
