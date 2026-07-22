"""LiveKit native OpenTelemetry and metric normalization.

The adapter is deliberately additive: it installs a span processor on the
application's existing provider and never creates a tracer provider or trace
root. LiveKit server telemetry does not prove client render, so render remains
explicitly unobserved until a client collector supplies that evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from typing import Any

from ..contract import (
    Adapter,
    CausalLink,
    Evidence,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from ..privacy import sanitize_semantic_label, sanitize_source_label
from ..recorder import IncidentRecorder
from ..versions import LIVEKIT_ADAPTER_VERSION
from . import routing
from .base import AdapterDependencyError, seconds_to_nano, stable_id, value

_METRIC_TYPES = {
    "vadmetrics": "vad",
    "vad_metrics": "vad",
    "eoumetrics": "turn_detection",
    "eou_metrics": "turn_detection",
    "eotinferencemetrics": "turn_detection",
    "eot_inference_metrics": "turn_detection",
    "sttmetrics": "stt",
    "stt_metrics": "stt",
    "llmmetrics": "llm",
    "llm_metrics": "llm",
    "realtimemodelmetrics": "agent",
    "realtime_model_metrics": "agent",
    "ttsmetrics": "tts",
    "tts_metrics": "tts",
    "interruptionmetrics": "interruption_detection",
    "interruption_metrics": "interruption_detection",
    "avatarmetrics": "avatar",
    "avatar_metrics": "avatar",
}

_NATIVE_METRIC_ATTRIBUTES = {
    "lk.llm_metrics": "llm_metrics",
    "lk.tts_metrics": "tts_metrics",
    "lk.realtime_model_metrics": "realtime_model_metrics",
}

_PER_REQUEST_DELTA_MEASUREMENTS = frozenset(
    {
        "earshot.metric.interruption.backchannel_count",
        "earshot.metric.interruption.count",
        "earshot.metric.interruption.request_count",
        "earshot.metric.model.total_tokens",
        "earshot.metric.turn_detection.request_count",
        "gen_ai.usage.input_audio_tokens",
        "gen_ai.usage.input_cached_tokens",
        "gen_ai.usage.input_cached_audio_tokens",
        "gen_ai.usage.input_cached_image_tokens",
        "gen_ai.usage.input_cached_text_tokens",
        "gen_ai.usage.input_image_tokens",
        "gen_ai.usage.input_text_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_audio_tokens",
        "gen_ai.usage.output_image_tokens",
        "gen_ai.usage.output_text_tokens",
        "gen_ai.usage.output_tokens",
        "livekit.realtime.session_duration",
        "livekit.stt.audio_duration",
        "livekit.stt.input_tokens",
        "livekit.stt.output_tokens",
        "livekit.tts.character_count",
        "livekit.tts.audio_duration",
        "livekit.tts.input_tokens",
        "livekit.tts.output_tokens",
    }
)

_METRIC_COUNTER_FIELDS = frozenset(
    {
        "characters_count",
        "completion_tokens",
        "inference_count",
        "input_tokens",
        "num_backchannels",
        "num_interruptions",
        "num_requests",
        "output_tokens",
        "prompt_cached_tokens",
        "prompt_tokens",
        "total_tokens",
    }
)
_IJSON_INTEGER_MAX = 9_007_199_254_740_991


def _is_i_json_counter(raw: object) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= _IJSON_INTEGER_MAX


def _is_non_negative_finite_number(raw: object) -> bool:
    if isinstance(raw, bool):
        return False
    if isinstance(raw, int):
        return 0 <= raw <= _IJSON_INTEGER_MAX
    return isinstance(raw, float) and math.isfinite(raw) and raw >= 0


def _is_unit_interval_number(raw: object) -> bool:
    return _is_non_negative_finite_number(raw) and isinstance(raw, (int, float)) and raw <= 1


def _is_zero_number(raw: object) -> bool:
    return (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(float(raw))
        and raw == 0
    )


_SPAN_NAMES = {
    "function_tool": "tool",
    "tool": "tool",
    # LiveKit wraps pipeline and realtime generation work in agent_turn. It is the
    # agent's own generation scope and is NOT equivalent to Pipecat's
    # conversation-wide `turn` container, so the two runtimes classify differently
    # on purpose; do not collapse it to framework_operation for symmetry.
    "agent_turn": "agent",
    "eou_detection": "turn_detection",
    # These spans contain whole turn lifecycles. Only eou_detection is a
    # detector interval; lifecycle containers must not become latency anchors.
    "user_turn": "framework_operation",
    "turn": "framework_operation",
    "vad": "vad",
    "stt": "stt",
    "llm": "llm",
    "tts": "tts",
    "realtime": "agent",
}


def _classify_span(span: object) -> str:
    attributes = value(span, "attributes", {}) or {}
    explicit = attributes.get("earshot.operation.name") if isinstance(attributes, Mapping) else None
    if isinstance(explicit, str) and explicit:
        return explicit
    name = str(value(span, "name", "unknown")).lower()
    for token, operation in _SPAN_NAMES.items():
        if token in name:
            return operation
    return "framework_operation"


def _metric_type(metric: object) -> str:
    native_type = value(metric, "type")
    if isinstance(native_type, str) and native_type:
        return native_type.lower()
    if isinstance(metric, Mapping):
        fallback = metric.get("metric_type", "Metric")
        return str(fallback).lower()
    return type(metric).__name__.lower()


def _is_connection_acquisition_metric(metric: object) -> bool:
    """Identify LiveKit's zero-usage connection timing callback.

    LiveKit 1.6 emits STT and realtime connection acquisition as the ordinary
    metric class with an empty request ID. No operation request exists for that
    callback; ``acquire_time`` is the only timing fact it represents.
    """

    metric_type = _metric_type(metric)
    if metric_type not in {
        "sttmetrics",
        "stt_metrics",
        "realtimemodelmetrics",
        "realtime_model_metrics",
    }:
        return False
    acquire_time = value(metric, "acquire_time")
    if not (
        value(metric, "request_id") == ""
        and _is_non_negative_finite_number(acquire_time)
        and isinstance(acquire_time, (int, float))
    ):
        return False
    if metric_type in {"sttmetrics", "stt_metrics"}:
        return value(metric, "streamed") is True and all(
            _is_zero_number(value(metric, field))
            for field in ("duration", "audio_duration", "input_tokens", "output_tokens")
        )

    ttft = value(metric, "ttft")
    if (
        not isinstance(ttft, (int, float))
        or isinstance(ttft, bool)
        or not math.isfinite(float(ttft))
        or ttft >= 0
        or value(metric, "cancelled") is not False
        or not all(
            _is_zero_number(value(metric, field))
            for field in (
                "duration",
                "session_duration",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "tokens_per_second",
            )
        )
    ):
        return False
    input_details = value(metric, "input_token_details")
    output_details = value(metric, "output_token_details")
    if input_details is not None and not all(
        _is_zero_number(value(input_details, field, 0))
        for field in ("audio_tokens", "text_tokens", "image_tokens", "cached_tokens")
    ):
        return False
    cached_details = value(input_details, "cached_tokens_details")
    if cached_details is not None and not all(
        _is_zero_number(value(cached_details, field, 0))
        for field in ("audio_tokens", "text_tokens", "image_tokens")
    ):
        return False
    return output_details is None or all(
        _is_zero_number(value(output_details, field, 0))
        for field in ("audio_tokens", "text_tokens", "image_tokens")
    )


def _normalize_native_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Map current LiveKit payload fields into explicit capture-class namespaces."""

    normalized = dict(attributes)
    mappings = {
        "lk.chat_ctx": "gen_ai.input",
        "lk.function_tool.arguments": "tool.arguments",
        "lk.function_tool.output": "tool.result",
        "lk.function_tools": "gen_ai.input.function_tools",
        "lk.input_text": "speech.text",
        "lk.instructions": "gen_ai.input.system_instruction",
        "lk.participant_identity": "participant.name",
        "lk.provider_tools": "gen_ai.input.provider_tools",
        "lk.response.text": "gen_ai.output.messages",
        "lk.tool_sets": "gen_ai.input.tool_sets",
        "lk.user_input": "speech.text",
        "lk.user_transcript": "speech.text",
    }
    for source, target in mappings.items():
        if source not in normalized:
            continue
        normalized.setdefault(target, normalized[source])
        del normalized[source]
    return normalized


def _turn_id_from_span(span: object, attributes: Mapping[str, Any]) -> str | None:
    explicit = value(span, "turn_id") or attributes.get("earshot.turn.id")
    if explicit is not None:
        return str(explicit)
    speech_id = attributes.get("lk.speech_id")
    if speech_id is not None:
        return str(speech_id)
    generation_id = attributes.get("lk.generation_id")
    if generation_id is None:
        return None
    candidate = str(generation_id)
    base, separator, step = candidate.rpartition("_")
    if separator and base.startswith("speech_") and step.isdigit():
        return base
    return candidate


def _otel_id(raw: object, width: int) -> str | None:
    if isinstance(raw, int):
        if raw <= 0 or raw >= 1 << (width * 4):
            return None
        return f"{raw:0{width}x}"
    if isinstance(raw, str):
        candidate = raw.lower().removeprefix("0x")
        if len(candidate) == width and candidate != "0" * width:
            try:
                int(candidate, 16)
            except ValueError:
                return None
            return candidate
    return None


class LiveKitAdapter:
    def __init__(self, recorder: IncidentRecorder, *, framework_version: str = "unknown"):
        self.recorder = recorder
        self.framework_version = framework_version
        self._render_coverage_written = False
        self._native_spans_enabled = False
        self._session_listeners_enabled = False
        self._interruption_listeners_enabled = False
        self._seen_operations: dict[str, str] = {}
        self._seen_spans: dict[tuple[str, str], str] = {}
        self._seen_events: set[str] = set()
        self._seen_quality: dict[str, str] = {}
        self._seen_participants: set[str] = set()
        self._routing_handle: routing.RoutingHandle | None = None
        self._lock = threading.RLock()
        self.recorder.register_adapter(
            Adapter(
                name="earshot.livekit",
                version=LIVEKIT_ADAPTER_VERSION,
                framework="livekit",
                framework_version=framework_version,
            )
        )

    def consume_span(self, span: object) -> str:
        """Normalize one ended LiveKit ``ReadableSpan`` without changing its identity."""

        native_attributes = dict(value(span, "attributes", {}) or {})
        attributes = _normalize_native_attributes(native_attributes)
        native_name = str(value(span, "name", "unknown")) or "unknown"
        attributes.setdefault(
            "earshot.framework.operation.name",
            sanitize_source_label(native_name),
        )
        native_metrics = self._pop_native_metrics(attributes)
        for metric in native_metrics:
            attributes.update(self._metric_attributes(metric))
            request_id = value(metric, "request_id")
            if request_id is not None:
                attributes.setdefault("earshot.request.id", str(request_id))
        context = value(span, "context")
        trace_id = _otel_id(value(context, "trace_id"), 32) if context is not None else None
        span_id = _otel_id(value(context, "span_id"), 16) if context is not None else None
        trace_id = trace_id or _otel_id(value(span, "trace_id"), 32)
        span_id = span_id or _otel_id(value(span, "span_id"), 16)
        if trace_id is None or span_id is None:
            raise TypeError("LiveKit span lacks a valid OpenTelemetry trace/span identity")

        raw_start = value(span, "started_at", value(span, "start_time"))
        raw_end = value(span, "ended_at", value(span, "end_time"))
        start = self._time_point(raw_start)
        end = self._time_point(raw_end) if raw_end is not None else None

        parent = value(span, "parent")
        parent_span_id = _otel_id(value(parent, "span_id"), 16) if parent is not None else None
        parent_span_id = parent_span_id or _otel_id(value(span, "parent_span_id"), 16)
        supplied_parent_scope = value(span, "parent_scope")
        if supplied_parent_scope:
            raw_parent_scope = str(supplied_parent_scope)
            parent_scope = (
                raw_parent_scope
                if raw_parent_scope in {"internal", "external", "unknown"}
                else "unknown"
            )
        elif parent is not None and bool(value(parent, "is_remote", False)):
            parent_scope = "external"
        else:
            # A local parent may belong to application instrumentation excluded by
            # this processor. Do not claim that it must exist inside the bundle.
            parent_scope = "unknown"

        resource_object = value(span, "resource")
        resource_attributes = value(resource_object, "attributes")
        if resource_attributes is None and isinstance(resource_object, Mapping):
            resource_attributes = resource_object
        resource = dict(resource_attributes or {})
        resource_schema_url = value(resource_object, "schema_url")
        scope = value(span, "instrumentation_scope") or value(span, "instrumentation_info")
        scope_name = value(scope, "name") if scope is not None else None
        scope_version = value(scope, "version") if scope is not None else None
        scope_attributes = dict(value(scope, "attributes", {}) or {}) if scope is not None else {}
        schema_url = value(scope, "schema_url") if scope is not None else None

        links: list[CausalLink] = []
        for native_link in value(span, "links", ()) or ():
            link_context = value(native_link, "context")
            linked_trace_id = _otel_id(value(link_context, "trace_id"), 32)
            linked_span_id = _otel_id(value(link_context, "span_id"), 16)
            if linked_trace_id is None or linked_span_id is None:
                continue
            link_attributes = dict(value(native_link, "attributes", {}) or {})
            relationship = (
                sanitize_semantic_label(str(link_attributes.pop("earshot.link.type", "related")))
                or "related"
            )
            raw_target_scope = str(link_attributes.pop("earshot.link.target_scope", "unknown"))
            target_scope = (
                raw_target_scope
                if raw_target_scope in {"internal", "external", "unknown"}
                else "unknown"
            )
            links.append(
                CausalLink(
                    relationship=relationship,
                    target_scope=target_scope,
                    trace_id=linked_trace_id,
                    span_id=linked_span_id,
                    attributes=link_attributes,
                )
            )

        status_object = value(span, "status", "unset")
        status_code = value(status_object, "status_code")
        if status_code is not None:
            status = str(value(status_code, "name", status_code)).lower()
        elif isinstance(status_object, str):
            status = status_object.lower()
        else:
            status = "unset"

        operation_id = str(
            value(span, "operation_id") or stable_id("livekit-span", trace_id, span_id)
        )
        turn_id = _turn_id_from_span(span, attributes)
        participant_id = value(span, "participant_id") or attributes.get("lk.participant_id")
        participant_kind = attributes.get("lk.participant_kind")
        if participant_id is not None:
            participant_id = str(participant_id)
            kind_label = (
                str(participant_kind).strip().lower().replace(" ", "_")
                if participant_kind is not None
                else "livekit_participant"
            )
            with self._lock:
                if participant_id not in self._seen_participants:
                    self.recorder.add_participant(
                        participant_id,
                        role="agent" if "agent" in kind_label else "participant",
                        endpoint_kind=kind_label,
                        attributes={
                            "lk.participant_id": participant_id,
                            **(
                                {"lk.participant_kind": participant_kind}
                                if participant_kind is not None
                                else {}
                            ),
                        },
                    )
                    self._seen_participants.add(participant_id)
        evidence = Evidence(
            source="livekit",
            observer="server",
            method="native_otel",
            method_version=self.framework_version,
            confidence="measured",
            availability="available",
            source_field=str(value(span, "name", "LiveKit span")),
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "name": value(span, "name", ""),
                    "operation_name": _classify_span(span),
                    "status": status,
                    "start": start.model_dump(mode="json"),
                    "end": end.model_dump(mode="json") if end else None,
                    "parent": parent_span_id,
                    "parent_scope": parent_scope,
                    "participant_id": participant_id,
                    "stream_id": value(span, "stream_id"),
                    "turn_id": turn_id,
                    "attributes": attributes,
                    "resource": resource,
                    "resource_schema_url": resource_schema_url,
                    "scope": [scope_name, scope_version, scope_attributes, schema_url],
                    "links": [item.model_dump(mode="json") for item in links],
                    "events": [
                        {
                            "name": value(event, "name", ""),
                            "timestamp": value(event, "timestamp"),
                            "attributes": dict(value(event, "attributes", {}) or {}),
                        }
                        for event in (value(span, "events", ()) or ())
                    ],
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        identity = (trace_id, span_id)

        with self._lock:
            previous = self._seen_spans.get(identity)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("conflicting duplicate LiveKit span identity")
                return operation_id
            self.recorder.record_operation(
                operation_id=operation_id,
                operation_name=_classify_span(span),
                status=status,
                started_at=start,
                ended_at=end,
                participant_id=participant_id,
                stream_id=value(span, "stream_id"),
                turn_id=turn_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                parent_scope=parent_scope,
                links=tuple(links),
                resource=resource,
                resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                instrumentation_scope_name=str(scope_name) if scope_name else None,
                instrumentation_scope_version=str(scope_version) if scope_version else None,
                instrumentation_scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
                evidence=evidence,
                attributes=attributes,
            )
            self._seen_spans[identity] = fingerprint
            self._record_source_span_events(
                span,
                operation_id=operation_id,
                trace_id=trace_id,
                span_id=span_id,
                turn_id=turn_id,
                default_time=end or start,
                resource=resource,
                resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                scope_name=str(scope_name) if scope_name else None,
                scope_version=str(scope_version) if scope_version else None,
                scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
            )
            self._record_native_interruption_events(
                span,
                operation_id=operation_id,
                trace_id=trace_id,
                span_id=span_id,
                turn_id=turn_id,
                default_time=end or start,
                resource=resource,
                resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                scope_name=str(scope_name) if scope_name else None,
                scope_version=str(scope_version) if scope_version else None,
                scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
                include_span_events=not self._session_listeners_enabled,
            )
            self._record_span_latency_quality(
                native_attributes,
                operation_id=operation_id,
                turn_id=turn_id,
                time=end or start,
                resource=resource,
                resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                scope_name=str(scope_name) if scope_name else None,
                scope_version=str(scope_version) if scope_version else None,
                scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
            )
            for metric in native_metrics:
                self._record_metric_quality(
                    metric,
                    turn_id=turn_id,
                    operation_id=operation_id,
                    method="native_otel_attribute",
                    resource=resource,
                    resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                    scope_name=str(scope_name) if scope_name else None,
                    scope_version=str(scope_version) if scope_version else None,
                    scope_attributes=scope_attributes,
                    schema_url=str(schema_url) if schema_url else None,
                )
                self._record_realtime_first_audio(
                    metric,
                    operation_id=operation_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    resource=resource,
                    resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
                    scope_name=str(scope_name) if scope_name else None,
                    scope_version=str(scope_version) if scope_version else None,
                    scope_attributes=scope_attributes,
                    schema_url=str(schema_url) if schema_url else None,
                )
            self._mark_render_unobserved()
        return operation_id

    def _record_source_span_events(
        self,
        span: object,
        *,
        operation_id: str,
        trace_id: str,
        span_id: str,
        turn_id: str | None,
        default_time: TimePoint,
        resource: Mapping[str, Any],
        resource_schema_url: str | None,
        scope_name: str | None,
        scope_version: str | None,
        scope_attributes: Mapping[str, Any],
        schema_url: str | None,
    ) -> None:
        for index, native_event in enumerate(value(span, "events", ()) or ()):
            native_name = str(value(native_event, "name", "unnamed")) or "unnamed"
            raw_time = value(native_event, "timestamp")
            event_time = self._nanosecond_time_point(raw_time) or default_time
            event_id = stable_id(
                "livekit-source-event",
                trace_id,
                span_id,
                index,
                native_name,
                event_time.source_time_unix_nano or event_time.monotonic_time_nano or "",
            )
            if event_id in self._seen_events:
                continue
            native_attributes = dict(value(native_event, "attributes", {}) or {})
            native_attributes["earshot.source.event.name"] = sanitize_source_label(native_name)
            self.recorder.record_event(
                "otel.span_event",
                event_id=event_id,
                time=event_time,
                operation_id=operation_id,
                turn_id=turn_id,
                trace_id=trace_id,
                span_id=span_id,
                resource=resource,
                resource_schema_url=resource_schema_url,
                instrumentation_scope_name=scope_name,
                instrumentation_scope_version=scope_version,
                instrumentation_scope_attributes=scope_attributes,
                schema_url=schema_url,
                evidence=Evidence(
                    source="livekit",
                    observer="server",
                    method="native_otel_span_event",
                    method_version=self.framework_version,
                    confidence="measured" if raw_time is not None else "estimated",
                    availability="available",
                    source_field=native_name,
                ),
                attributes=native_attributes,
            )
            self._seen_events.add(event_id)

    def _pop_native_metrics(self, attributes: dict[str, Any]) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        for attribute_name, metric_type in _NATIVE_METRIC_ATTRIBUTES.items():
            raw = attributes.pop(attribute_name, None)
            if raw is None:
                continue
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError, json.JSONDecodeError):
                self._record_coverage_safe("livekit.native_metric", "unsupported_metric_shape")
                continue
            if not isinstance(parsed, Mapping):
                self._record_coverage_safe("livekit.native_metric", "unsupported_metric_shape")
                continue
            metric = dict(parsed)
            metric.setdefault("type", metric_type)
            metrics.append(metric)
        return metrics

    def _record_metric_quality(
        self,
        metric: object,
        *,
        turn_id: str | None = None,
        operation_id: str | None = None,
        observed_at: TimePoint | None = None,
        method: str = "metrics_listener",
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        scope_name: str | None = None,
        scope_version: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
    ) -> str | None:
        metric_type = _metric_type(metric)
        attributes = self._metric_attributes(metric)
        attributes["earshot.framework.version"] = self.framework_version
        request_id = value(metric, "request_id") or value(metric, "segment_id")
        speech_id = value(metric, "speech_id") or value(metric, "sequence_id")
        resolved_turn = turn_id or (str(speech_id) if speech_id is not None else None)
        if metric_type in {"eoumetrics", "eou_metrics"} and resolved_turn is not None:
            self._record_eou_commit_event(
                metric,
                resolved_turn,
                resource=resource,
                resource_schema_url=resource_schema_url,
                scope_name=scope_name,
                scope_version=scope_version,
                scope_attributes=scope_attributes,
                schema_url=schema_url,
            )
        ttft = value(metric, "ttft")
        if (
            metric_type in {"realtimemodelmetrics", "realtime_model_metrics"}
            and not _is_connection_acquisition_metric(metric)
            and isinstance(ttft, (int, float))
            and not isinstance(ttft, bool)
            and math.isfinite(float(ttft))
            and float(ttft) < 0
        ):
            self._record_coverage_safe(
                "livekit.response.first_audio_generated",
                "no_audio_token",
            )
        if metric_type in {"eoumetrics", "eou_metrics"} and (
            value(metric, "end_of_utterance_delay") == 0
            and value(metric, "transcription_delay") == 0
        ):
            attributes.pop("lk.eou.endpointing_delay", None)
            attributes.pop("lk.eou.transcription_delay", None)
            self.recorder.record_coverage(
                "livekit.turn_detection.duration",
                "not_observed",
                "speech_end_not_detected",
            )

        measurements: list[QualityMeasurement] = []
        for name, raw in sorted(attributes.items()):
            if not isinstance(raw, (bool, int, float)) or (
                isinstance(raw, float) and not math.isfinite(raw)
            ):
                continue
            unit = (
                "s"
                if any(
                    token in name
                    for token in (
                        "ttf",
                        "duration",
                        "delay",
                        "latency",
                        "transcription",
                        "acquire_time",
                    )
                )
                else ("count" if isinstance(raw, int) and not isinstance(raw, bool) else "1")
            )
            aggregation = (
                "delta"
                if (
                    name in _PER_REQUEST_DELTA_MEASUREMENTS
                    or (
                        metric_type in {"vadmetrics", "vad_metrics"}
                        and name
                        in {
                            "earshot.duration.inference_seconds",
                            "earshot.metric.inference.count",
                        }
                    )
                )
                else "instant"
            )
            measurements.append(
                QualityMeasurement(
                    name=name,
                    value=raw,
                    unit=unit,
                    aggregation=aggregation,
                )
            )
        if not measurements:
            return None

        provider_time = self._seconds_time_point(value(metric, "timestamp"))
        explicit_time = provider_time or observed_at
        observed = explicit_time or self.recorder._time()
        dimensions = {name: raw for name, raw in attributes.items() if isinstance(raw, str)}
        time_identity = (
            explicit_time.model_dump(mode="json", exclude_none=True)
            if explicit_time is not None
            else None
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "type": metric_type,
                    "request_id": request_id,
                    "speech_id": speech_id,
                    "turn_id": resolved_turn,
                    "operation_id": operation_id,
                    "time": time_identity,
                    "dimensions": dimensions,
                    "measurements": [item.model_dump(mode="json") for item in measurements],
                    "resource": dict(resource or {}),
                    "resource_schema_url": resource_schema_url,
                    "scope_name": scope_name,
                    "scope_version": scope_version,
                    "scope_attributes": dict(scope_attributes or {}),
                    "schema_url": schema_url,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        identity_time = (
            json.dumps(time_identity, sort_keys=True) if time_identity is not None else fingerprint
        )
        sample_id = stable_id(
            "livekit-provider-metric",
            metric_type,
            request_id or "",
            speech_id or "",
            operation_id or "",
            resolved_turn or "",
            json.dumps(dimensions, sort_keys=True),
            identity_time,
        )
        sample_attributes: dict[str, Any] = dict(dimensions)
        if resolved_turn is not None:
            sample_attributes["earshot.turn.id"] = resolved_turn
        if operation_id is not None:
            sample_attributes["earshot.operation.id"] = operation_id
        if request_id is not None:
            sample_attributes["earshot.request.id"] = str(request_id)

        with self._lock:
            previous = self._seen_quality.get(sample_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("conflicting duplicate LiveKit provider metric identity")
                return sample_id
            self.recorder.record_quality_sample(
                QualitySample(
                    sample_id=sample_id,
                    session_id=self.recorder.session_id,
                    quality_kind="pipeline.metric",
                    sample_window=TimeRange(start=observed, end=observed),
                    measurements=tuple(measurements),
                    evidence=Evidence(
                        source="livekit",
                        observer="server",
                        method=method,
                        method_version=self.framework_version,
                        confidence="measured",
                        availability="available",
                        source_field=metric_type,
                    ),
                    resource=dict(resource or {}),
                    resource_schema_url=resource_schema_url,
                    instrumentation_scope_name=scope_name,
                    instrumentation_scope_version=scope_version,
                    instrumentation_scope_attributes=dict(scope_attributes or {}),
                    schema_url=schema_url,
                    attributes=sample_attributes,
                )
            )
            self._seen_quality[sample_id] = fingerprint
        return sample_id

    def _record_eou_commit_event(
        self,
        metric: object,
        turn_id: str,
        *,
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        scope_name: str | None = None,
        scope_version: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
    ) -> str:
        """Author the speech-correlated turn point absent from native EOU spans."""

        timestamp = self._seconds_time_point(value(metric, "timestamp")) or self.recorder._time()
        callback_delay = value(metric, "on_user_turn_completed_delay")
        event_time = timestamp
        attributes: dict[str, Any] = {}
        if (
            isinstance(callback_delay, (int, float))
            and not isinstance(callback_delay, bool)
            and math.isfinite(float(callback_delay))
            and float(callback_delay) >= 0
        ):
            event_time = self._shift_source_time(
                timestamp,
                -(seconds_to_nano(callback_delay) or 0),
            )
            attributes["earshot.duration.turn_callback_seconds"] = callback_delay
        event_id = stable_id("livekit-turn-committed", turn_id)
        if event_id in self._seen_events:
            return event_id
        self.recorder.record_event(
            "earshot.turn.committed",
            event_id=event_id,
            time=event_time,
            turn_id=turn_id,
            resource=resource,
            resource_schema_url=resource_schema_url,
            instrumentation_scope_name=scope_name,
            instrumentation_scope_version=scope_version,
            instrumentation_scope_attributes=dict(scope_attributes or {}),
            schema_url=schema_url,
            evidence=Evidence(
                source="livekit",
                observer="server",
                method="provider_timestamp_minus_callback_duration",
                method_version=self.framework_version,
                confidence="estimated",
                availability="available",
                source_field=_metric_type(metric),
            ),
            attributes=attributes,
        )
        self._seen_events.add(event_id)
        return event_id

    def _record_span_latency_quality(
        self,
        attributes: Mapping[str, Any],
        *,
        operation_id: str,
        turn_id: str | None,
        time: TimePoint,
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        scope_name: str | None = None,
        scope_version: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
    ) -> str | None:
        raw = attributes.get("lk.e2e_latency")
        if (
            not isinstance(raw, (int, float))
            or isinstance(raw, bool)
            or not math.isfinite(float(raw))
            or float(raw) < 0
        ):
            return None
        sample_id = stable_id("livekit-span-metric", operation_id, "e2e_latency")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation_id": operation_id, "value": raw, "time": time.model_dump(mode="json")},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        sample_attributes: dict[str, Any] = {"earshot.operation.id": operation_id}
        if turn_id is not None:
            sample_attributes["earshot.turn.id"] = turn_id
        previous = self._seen_quality.get(sample_id)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError("conflicting duplicate LiveKit span metric identity")
            return sample_id
        self.recorder.record_quality_sample(
            QualitySample(
                sample_id=sample_id,
                session_id=self.recorder.session_id,
                quality_kind="pipeline.latency",
                sample_window=TimeRange(start=time, end=time),
                measurements=(QualityMeasurement(name="livekit.e2e_latency", value=raw, unit="s"),),
                evidence=Evidence(
                    source="livekit",
                    observer="server",
                    method="native_otel_attribute",
                    method_version=self.framework_version,
                    confidence="measured",
                    availability="available",
                    source_field="lk.e2e_latency",
                ),
                resource=dict(resource or {}),
                resource_schema_url=resource_schema_url,
                instrumentation_scope_name=scope_name,
                instrumentation_scope_version=scope_version,
                instrumentation_scope_attributes=dict(scope_attributes or {}),
                schema_url=schema_url,
                attributes=sample_attributes,
            )
        )
        self._seen_quality[sample_id] = fingerprint
        return sample_id

    def _record_realtime_first_audio(
        self,
        metric: object,
        *,
        operation_id: str,
        turn_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        resource: Mapping[str, Any],
        scope_name: str | None,
        scope_version: str | None,
        schema_url: str | None,
        resource_schema_url: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        method: str = "native_otel_attribute",
        source_field: str = "lk.realtime_model_metrics",
    ) -> str | None:
        if _metric_type(metric) not in {"realtimemodelmetrics", "realtime_model_metrics"}:
            return None
        if _is_connection_acquisition_metric(metric):
            return None
        ttft = value(metric, "ttft")
        timestamp = self._seconds_time_point(value(metric, "timestamp"))
        if (
            timestamp is None
            or not isinstance(ttft, (int, float))
            or isinstance(ttft, bool)
            or not math.isfinite(float(ttft))
            or float(ttft) < 0
        ):
            if (
                isinstance(ttft, (int, float))
                and not isinstance(ttft, bool)
                and math.isfinite(float(ttft))
                and float(ttft) < 0
            ):
                self._record_coverage_safe(
                    "livekit.response.first_audio_generated",
                    "no_audio_token",
                )
            return None
        first_audio = self._shift_source_time(timestamp, seconds_to_nano(ttft) or 0)
        request_id = value(metric, "request_id")
        event_id = stable_id(
            "livekit-realtime-first-audio",
            request_id or value(metric, "speech_id") or operation_id,
            value(metric, "timestamp"),
        )
        if event_id in self._seen_events:
            return event_id
        self.recorder.record_event(
            "earshot.response.first_audio_generated",
            event_id=event_id,
            time=first_audio,
            operation_id=operation_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=span_id,
            resource=resource,
            resource_schema_url=resource_schema_url,
            instrumentation_scope_name=scope_name,
            instrumentation_scope_version=scope_version,
            instrumentation_scope_attributes=dict(scope_attributes or {}),
            schema_url=schema_url,
            evidence=Evidence(
                source="livekit",
                observer="server",
                method=method,
                method_version=self.framework_version,
                confidence="measured",
                availability="available",
                source_field=source_field,
            ),
            attributes={
                **({"earshot.request.id": str(request_id)} if request_id is not None else {})
            },
        )
        self._seen_events.add(event_id)
        return event_id

    def consume_metric(
        self,
        metric: object,
        *,
        observed_at: TimePoint | None = None,
    ) -> str | None:
        metric_type = _metric_type(metric)
        operation_name = _METRIC_TYPES.get(metric_type, "framework_metric")
        if _is_connection_acquisition_metric(metric):
            # LiveKit emits connection establishment as a zero-usage metric with
            # an empty request ID. It is a quality point, not an STT/TTS/agent
            # operation, and must not synthesize zero token/audio deltas.
            try:
                return self._record_metric_quality(
                    metric,
                    observed_at=observed_at,
                    method="metrics_listener",
                )
            finally:
                self._mark_render_unobserved()
        if operation_name == "vad":
            # Voice activity detection is a continuous background signal, not a
            # discrete sub-step. Emitting one operation per callback floods the
            # incident (dozens per turn); record it as a pipeline quality sample
            # instead so the waterfall and turn analysis stay clean.
            try:
                return self._record_metric_quality(
                    metric,
                    observed_at=observed_at,
                    method="metrics_listener",
                )
            finally:
                self._mark_render_unobserved()
        observed = observed_at or self.recorder._time()  # adapter shares recorder clock
        duration_value = value(metric, "duration")
        if duration_value is None:
            duration_value = value(metric, "total_duration")
        duration_unavailable_reason: str | None = None
        if metric_type in {"eoumetrics", "eou_metrics"}:
            endpointing_delay = value(metric, "end_of_utterance_delay")
            transcription_delay = value(metric, "transcription_delay")
            if endpointing_delay == 0 and transcription_delay == 0:
                duration_value = None
                duration_unavailable_reason = "speech_end_not_detected"
            elif duration_value is None:
                duration_value = endpointing_delay
        elif (
            metric_type in {"sttmetrics", "stt_metrics"}
            and value(metric, "streamed") is True
            and duration_value == 0
        ):
            duration_value = None
            duration_unavailable_reason = "streaming_duration_not_exposed"
        duration = seconds_to_nano(duration_value)

        provider_time = self._seconds_time_point(value(metric, "timestamp"))
        start = provider_time or observed
        end: TimePoint | None = observed
        confidence = "measured"
        method = "metrics_listener"
        if provider_time is not None and duration is not None:
            confidence = "estimated"
            if metric_type in {"realtimemodelmetrics", "realtime_model_metrics"}:
                # RealtimeModelMetrics uniquely documents timestamp as response start.
                end = self._shift_source_time(provider_time, duration)
                method = "provider_start_plus_duration"
            else:
                # Ordinary LiveKit metrics are emitted after work completes.
                end = provider_time
                start = self._shift_source_time(provider_time, -duration)
                method = "provider_end_minus_duration"
        elif provider_time is not None:
            # Point metrics (including zero-delay EOU/VAD facts) are closed.
            end = provider_time
            method = "provider_point_timestamp"
        if duration_unavailable_reason is not None:
            start = provider_time or observed
            end = None
            method = "provider_point_timestamp"
        elif (
            provider_time is None
            and duration is not None
            and observed.monotonic_time_nano is not None
        ):
            monotonic_start = int(observed.monotonic_time_nano) - duration
            if monotonic_start >= 0:
                update: dict[str, str | None] = {
                    "monotonic_time_nano": str(monotonic_start),
                    "observed_time_unix_nano": None,
                }
                if observed.source_time_unix_nano is not None:
                    update["source_time_unix_nano"] = str(
                        int(observed.source_time_unix_nano) - duration
                    )
                start = observed.model_copy(update=update)
            elif observed.source_time_unix_nano is not None:
                # The metric began before this recorder's monotonic origin. Keep
                # the estimate in the source-wall domain; never fabricate zero.
                start = observed.model_copy(
                    update={
                        "source_time_unix_nano": str(
                            int(observed.source_time_unix_nano) - duration
                        ),
                        "monotonic_time_nano": None,
                        "clock_domain_id": None,
                        "observed_time_unix_nano": None,
                    }
                )
            confidence = "estimated"
            method = "arrival_minus_duration"

        speech_id = value(metric, "speech_id") or value(metric, "sequence_id")
        request_id = value(metric, "request_id") or value(metric, "segment_id")
        attributes = self._metric_attributes(metric)
        if duration_unavailable_reason == "speech_end_not_detected":
            attributes.pop("lk.eou.endpointing_delay", None)
            attributes.pop("lk.eou.transcription_delay", None)
        attributes["earshot.framework.version"] = self.framework_version
        evidence = Evidence(
            source="livekit",
            observer="server",
            method=method,
            method_version=self.framework_version,
            confidence=confidence,
            availability="available",
            source_field=metric_type,
            sample_window=TimeRange(start=start, end=end) if end is not None else None,
        )
        status = "cancelled" if bool(value(metric, "cancelled", False)) else "ok"
        snapshot = self._metric_snapshot(metric, metric_type, attributes)
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        correlation = request_id or speech_id or value(metric, "timestamp") or fingerprint
        operation_id = stable_id("livekit-metric", metric_type, correlation)

        with self._lock:
            previous = self._seen_operations.get(operation_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("conflicting duplicate LiveKit metric identity")
                return operation_id
            self.recorder.record_operation(
                operation_id=operation_id,
                operation_name=operation_name,
                status=status,
                started_at=start,
                ended_at=end,
                turn_id=str(speech_id) if speech_id is not None else None,
                evidence=evidence,
                attributes=attributes,
            )
            self._seen_operations[operation_id] = fingerprint
            if metric_type in {"eoumetrics", "eou_metrics"} and speech_id is not None:
                self._record_eou_commit_event(metric, str(speech_id))
            self._record_realtime_first_audio(
                metric,
                operation_id=operation_id,
                turn_id=str(speech_id) if speech_id is not None else None,
                trace_id=None,
                span_id=None,
                resource={},
                scope_name=None,
                scope_version=None,
                schema_url=None,
                method="metrics_listener",
                source_field=metric_type,
            )
            if duration_unavailable_reason is not None:
                self.recorder.record_coverage(
                    f"livekit.{operation_name}.duration",
                    "not_observed",
                    duration_unavailable_reason,
                )
            interruption_count = attributes.get("earshot.metric.interruption.count", 0)
            if (
                operation_name == "interruption_detection"
                and _is_i_json_counter(interruption_count)
                and interruption_count > 0
                and not self._interruption_listeners_enabled
            ):
                self._record_interruption_fact(
                    event_name="earshot.interruption.detected",
                    source_field=metric_type,
                    time=end or start,
                    operation_id=operation_id,
                    turn_id=str(speech_id) if speech_id is not None else None,
                    attributes=attributes,
                    confidence="measured",
                )
            self._record_metric_quality(
                metric,
                turn_id=str(speech_id) if speech_id is not None else None,
                operation_id=operation_id,
                observed_at=observed_at,
                method="metrics_listener",
            )
            self._mark_render_unobserved()
        return operation_id

    def consume_interruption_event(self, event: object) -> str:
        """Normalize supported LiveKit session interruption events without audio payloads."""

        event_type = str(value(event, "type", type(event).__name__)).lower()
        if event_type in {
            "user_interruption_detected",
            "userinterruptiondetectedevent",
        }:
            event_name = "earshot.interruption.accepted"
            time = (
                self._seconds_time_point(value(event, "timestamp", value(event, "detected_at")))
                or self.recorder._time()
            )
            attributes = self._interruption_attributes(event)
            confidence = "measured"
        elif event_type in {"overlapping_speech", "overlappingspeechevent"}:
            accepted = self._boolean_decision(value(event, "is_interruption", False))
            if not accepted:
                event_name = "earshot.interruption.ignored"
            elif (
                self._interruption_listeners_enabled
                or self._session_listeners_enabled
                or self._native_spans_enabled
            ):
                # Attached sessions also expose the eventual interrupted output
                # through ChatMessage or agent_turn. Preserve overlap as detector
                # evidence so the accepted outcome is not counted twice.
                event_name = "earshot.interruption.detected"
            else:
                # Standalone consumption has no later outcome surface.
                event_name = "earshot.interruption.accepted"
            time = (
                self._seconds_time_point(value(event, "detected_at", value(event, "created_at")))
                or self.recorder._time()
            )
            attributes = self._interruption_attributes(event)
            confidence = "inferred"
        elif event_type in {"agent_false_interruption", "agentfalseinterruptionevent"}:
            event_name = "earshot.interruption.ignored"
            time = self._seconds_time_point(value(event, "created_at")) or self.recorder._time()
            attributes = {
                "earshot.metric.interruption.resumed": bool(value(event, "resumed", False))
            }
            confidence = "measured"
        else:
            raise TypeError("unsupported LiveKit interruption event")

        with self._lock:
            return self._record_interruption_fact(
                event_name=event_name,
                source_field=event_type,
                time=time,
                attributes=attributes,
                confidence=confidence,
            )

    def consume_conversation_item(self, event: object) -> str | None:
        """Retain current ``ChatMessage.metrics`` without capturing message content."""

        item = value(event, "item", event)
        metrics = value(item, "metrics")
        item_id = value(item, "id") or value(item, "item_id")
        explicit_turn = (
            value(event, "turn_id")
            or value(item, "turn_id")
            or (value(metrics, "speech_id") if metrics is not None else None)
        )
        turn_id = str(explicit_turn) if explicit_turn is not None else None
        observed = self.recorder._time()
        if metrics is None:
            with self._lock:
                interruption_id = self._record_conversation_item_interruption(
                    item,
                    time=observed,
                    turn_id=turn_id,
                    item_id=item_id,
                )
                self._mark_render_unobserved()
            return interruption_id
        measurements: list[QualityMeasurement] = []
        for field_name in (
            "transcription_delay",
            "end_of_turn_delay",
            "on_user_turn_completed_delay",
            "llm_node_ttft",
            "tts_node_ttfb",
            "playback_latency",
            "e2e_latency",
        ):
            raw = value(metrics, field_name)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                valid_number = True
            else:
                valid_number = isinstance(raw, float) and math.isfinite(raw) and raw >= 0
            if valid_number:
                measurements.append(
                    QualityMeasurement(
                        name=f"livekit.{field_name}",
                        value=raw,
                        unit="s",
                    )
                )
        if not measurements:
            with self._lock:
                interruption_id = self._record_conversation_item_interruption(
                    item,
                    time=observed,
                    turn_id=turn_id,
                    item_id=item_id,
                )
                self._mark_render_unobserved()
            return interruption_id

        started = self._seconds_time_point(value(metrics, "started_speaking_at"))
        stopped = self._seconds_time_point(value(metrics, "stopped_speaking_at"))
        if started is not None and stopped is not None:
            if int(stopped.source_time_unix_nano or "0") < int(
                started.source_time_unix_nano or "0"
            ):
                start = end = observed
            else:
                start, end = started, stopped
        else:
            start = end = started or stopped or observed

        role = str(value(item, "role", "unknown"))
        snapshot = {
            "role": role,
            "item_id": item_id,
            "measurements": [measurement.model_dump(mode="json") for measurement in measurements],
            "start": start.model_dump(mode="json"),
            "end": end.model_dump(mode="json"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        sample_id = stable_id("livekit-turn-metrics", item_id or fingerprint)
        sample_attributes = {"earshot.conversation.role": role}
        if item_id is not None:
            sample_attributes["earshot.conversation.item.id"] = str(item_id)
        if turn_id is not None:
            sample_attributes["earshot.turn.id"] = turn_id
        with self._lock:
            previous = self._seen_quality.get(sample_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("conflicting duplicate LiveKit turn metrics identity")
                return sample_id
            self.recorder.record_quality_sample(
                QualitySample(
                    sample_id=sample_id,
                    session_id=self.recorder.session_id,
                    quality_kind="pipeline.latency",
                    sample_window=TimeRange(start=start, end=end),
                    measurements=tuple(measurements),
                    evidence=Evidence(
                        source="livekit",
                        observer="server",
                        method="ChatMessage.metrics",
                        method_version=self.framework_version,
                        confidence="measured",
                        availability="available",
                        source_field="conversation_item_added.item.metrics",
                    ),
                    attributes=sample_attributes,
                )
            )
            self._seen_quality[sample_id] = fingerprint
            self._record_conversation_item_interruption(
                item,
                time=end,
                turn_id=turn_id,
                item_id=item_id,
            )
            self._mark_render_unobserved()
        return sample_id

    def _record_conversation_item_interruption(
        self,
        item: object,
        *,
        time: TimePoint,
        turn_id: str | None,
        item_id: object,
    ) -> str | None:
        if not bool(value(item, "interrupted", False)):
            return None
        # In dual-surface mode the native agent_turn span is the only source with
        # stable speech correlation. ChatMessage.interrupted is an unkeyed fallback
        # and would otherwise count the same accepted outcome twice.
        if self._native_spans_enabled:
            return None
        return self._record_interruption_fact(
            event_name="earshot.interruption.accepted",
            source_field="conversation_item_added.item.interrupted",
            time=time,
            turn_id=turn_id,
            attributes={
                "earshot.metric.interruption.accepted": True,
                **({"earshot.conversation.item.id": str(item_id)} if item_id is not None else {}),
            },
            confidence="measured",
        )

    def attach_metrics_listener(self, session: object) -> None:
        """Attach a component/session metrics callback, fail-open.

        Per-plugin ``metrics_collected`` remains current; the same callback on an
        AgentSession is retained only for backwards compatibility.
        """

        def listener(event: object) -> None:
            metric = value(event, "metrics", event)
            try:
                if self._native_spans_enabled:
                    # LLM/TTS/realtime metrics are also serialized on native spans.
                    # The callback objects are mutated later with speech_id while
                    # the serialized copy is not, so recording both is neither
                    # byte-identical nor safely correlatable. Native spans own these
                    # types and attach an operation identity to their quality sample.
                    metric_type = _metric_type(metric)
                    if _is_connection_acquisition_metric(metric):
                        # Connection-only callbacks have no owning operation span;
                        # retain the acquisition point even in native-span mode.
                        self.consume_metric(metric)
                    elif metric_type in {
                        "eoumetrics",
                        "eou_metrics",
                        "eotinferencemetrics",
                        "eot_inference_metrics",
                    }:
                        self._record_metric_quality(metric)
                    elif metric_type not in {
                        "llmmetrics",
                        "llm_metrics",
                        "ttsmetrics",
                        "tts_metrics",
                        "realtimemodelmetrics",
                        "realtime_model_metrics",
                    }:
                        # STT, VAD, interruption and avatar metrics do not have a
                        # guaranteed native operation span in LiveKit 1.6.
                        self.consume_metric(metric)
                    self._mark_render_unobserved()
                else:
                    self.consume_metric(metric)
            except Exception:
                self._record_coverage_safe(
                    "livekit.metric",
                    "unsupported_metric_shape",
                )

        self._attach_listener(session, "metrics_collected", listener)

    def attach_turn_metrics_listener(self, session: object) -> None:
        """Attach the current per-turn ``ChatMessage.metrics`` event surface."""

        def listener(event: object) -> None:
            try:
                self.consume_conversation_item(event)
            except Exception:
                self._record_coverage_safe(
                    "livekit.turn_metrics",
                    "unsupported_turn_metrics_shape",
                )

        self._attach_listener(session, "conversation_item_added", listener)

    def attach_interruption_listeners(self, session: object) -> None:
        """Attach adaptive-interruption callbacks when the session exposes them."""

        # LiveKit 1.6 derives InterruptionMetrics from the same
        # OverlappingSpeechEvent but provides no shared request/speech ID. Once
        # this event surface is attached it exclusively owns interruption point
        # facts; metrics continue to own operations and aggregate quality.
        def listener(event: object) -> None:
            try:
                self.consume_interruption_event(event)
            except Exception:
                self._record_coverage_safe(
                    "livekit.interruption",
                    "unsupported_interruption_event",
                )

        self._attach_listener(session, "overlapping_speech", listener)
        self._attach_listener(session, "agent_false_interruption", listener)
        self._interruption_listeners_enabled = True

    def attach_session_listeners(self, session: object) -> None:
        """Attach both metric and interruption listeners to a LiveKit session."""

        self._session_listeners_enabled = True
        self.attach_turn_metrics_listener(session)
        self.attach_metrics_listener(session)
        self.attach_interruption_listeners(session)

    def create_span_processor(self) -> object:
        """Create a processor for the application's existing OTel provider."""

        try:
            from opentelemetry.sdk.trace import ReadableSpan, Span
            from opentelemetry.sdk.trace.export import SpanProcessor
        except ImportError as error:  # pragma: no cover - optional dependency
            raise AdapterDependencyError(
                "LiveKit OTel integration requires opentelemetry-sdk"
            ) from error

        adapter = self

        class EarshotLiveKitSpanProcessor(SpanProcessor):
            def on_start(self, span: Span, parent_context: object | None = None) -> None:
                del span, parent_context

            def on_end(self, span: ReadableSpan) -> None:
                if not adapter._is_livekit_span(span):
                    return
                try:
                    adapter.consume_span(span)
                except Exception:
                    adapter._record_coverage_safe(
                        "livekit.span",
                        "unsupported_span_shape",
                    )

            def shutdown(self) -> None:
                return None

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                del timeout_millis
                return True

        return EarshotLiveKitSpanProcessor()

    def _consume_routed_span(self, span: object) -> None:
        try:
            self.consume_span(span)
        except Exception:
            self._record_coverage_safe("livekit.span", "unsupported_span_shape")

    def attach_span_processor(self, tracer_provider: object) -> routing.RoutingHandle:
        """Route this session's spans through one shared, process-scoped processor.

        Installs exactly one Earshot router processor per provider (not one per
        session) and registers this recorder as the owning session sink, so
        concurrent sessions never ingest one another's spans and old sessions
        release their routing state on :meth:`detach`.
        """

        handle = routing.attach_adapter(
            tracer_provider,
            "livekit",
            self._is_livekit_span,
            self.recorder.session_id,
            self._consume_routed_span,
        )
        self._native_spans_enabled = True
        self._routing_handle = handle
        return handle

    def attach(self, tracer_provider: object) -> routing.RoutingHandle:
        """Alias matching the other framework adapter."""

        return self.attach_span_processor(tracer_provider)

    def detach(self) -> None:
        """Release this session's routing state from the shared provider."""

        handle = self._routing_handle
        if handle is not None:
            handle.close()
            self._routing_handle = None

    def _record_native_interruption_events(
        self,
        span: object,
        *,
        operation_id: str,
        trace_id: str,
        span_id: str,
        turn_id: str | None,
        default_time: TimePoint,
        resource: Mapping[str, Any],
        scope_name: str | None,
        scope_version: str | None,
        schema_url: str | None,
        resource_schema_url: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        include_span_events: bool = True,
    ) -> None:
        span_attributes = dict(value(span, "attributes", {}) or {})
        decision = span_attributes.get("lk.is_interruption")
        interrupted = span_attributes.get("lk.interrupted")
        # LiveKit writes lk.interrupted=false on every normal agent_turn. That
        # completion state is not evidence of a rejected interruption decision.
        if decision is not None or self._boolean_decision(interrupted):
            accepted = self._boolean_decision(interrupted) or self._boolean_decision(decision)
            self._record_interruption_fact(
                event_name=(
                    "earshot.interruption.accepted" if accepted else "earshot.interruption.ignored"
                ),
                source_field="LiveKit span attributes",
                time=default_time,
                operation_id=operation_id,
                trace_id=trace_id,
                span_id=span_id,
                turn_id=turn_id,
                attributes=self._interruption_attributes(span_attributes),
                confidence="measured",
                resource=resource,
                resource_schema_url=resource_schema_url,
                scope_name=scope_name,
                scope_version=scope_version,
                scope_attributes=scope_attributes,
                schema_url=schema_url,
            )

        if not include_span_events:
            return
        for native_event in value(span, "events", ()) or ():
            native_name = str(value(native_event, "name", ""))
            if "interruption" not in native_name.lower():
                continue
            native_attributes = dict(value(native_event, "attributes", {}) or {})
            explicit_name = native_name if native_name.startswith("earshot.interruption.") else None
            event_decision = native_attributes.get(
                "lk.is_interruption", native_attributes.get("is_interruption")
            )
            if explicit_name is not None:
                event_name = explicit_name
            elif event_decision is None:
                event_name = "earshot.interruption.detected"
            elif self._boolean_decision(event_decision):
                event_name = "earshot.interruption.accepted"
            else:
                event_name = "earshot.interruption.ignored"
            event_time = (
                self._nanosecond_time_point(value(native_event, "timestamp")) or default_time
            )
            self._record_interruption_fact(
                event_name=event_name,
                source_field=native_name,
                time=event_time,
                operation_id=operation_id,
                trace_id=trace_id,
                span_id=span_id,
                turn_id=turn_id,
                attributes=self._interruption_attributes(native_attributes),
                confidence="measured",
                resource=resource,
                resource_schema_url=resource_schema_url,
                scope_name=scope_name,
                scope_version=scope_version,
                scope_attributes=scope_attributes,
                schema_url=schema_url,
            )

    def _record_interruption_fact(
        self,
        *,
        event_name: str,
        source_field: str,
        time: TimePoint,
        attributes: Mapping[str, Any],
        confidence: str,
        operation_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        turn_id: str | None = None,
        resource: Mapping[str, Any] | None = None,
        resource_schema_url: str | None = None,
        scope_name: str | None = None,
        scope_version: str | None = None,
        scope_attributes: Mapping[str, Any] | None = None,
        schema_url: str | None = None,
    ) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "event": event_name,
                    "source": source_field,
                    "time": time.model_dump(mode="json"),
                    "operation": operation_id,
                    "attributes": attributes,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        event_id = stable_id("livekit-event", fingerprint)
        if event_id in self._seen_events:
            return event_id
        self.recorder.record_event(
            event_name,
            event_id=event_id,
            time=time,
            operation_id=operation_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=span_id,
            resource=resource,
            resource_schema_url=resource_schema_url,
            instrumentation_scope_name=scope_name,
            instrumentation_scope_version=scope_version,
            instrumentation_scope_attributes=dict(scope_attributes or {}),
            schema_url=schema_url,
            evidence=Evidence(
                source="livekit",
                observer="server",
                method="native_interruption_signal",
                method_version=self.framework_version,
                confidence=confidence,
                availability="available",
                source_field=source_field,
            ),
            attributes=attributes,
        )
        self._seen_events.add(event_id)
        return event_id

    def _mark_render_unobserved(self) -> None:
        if self._render_coverage_written:
            return
        self.recorder.record_coverage(
            "client.render",
            "not_observed",
            "server_cannot_observe_client_render",
        )
        self._render_coverage_written = True

    def _record_coverage_safe(self, signal: str, reason: str) -> None:
        """Best-effort diagnostics must never escape framework callbacks."""

        try:
            self.recorder.record_coverage(signal, "unavailable", reason)
        except Exception:
            return

    def _time_point(self, raw: object) -> TimePoint:
        if isinstance(raw, TimePoint):
            return raw
        if isinstance(raw, Mapping):
            return TimePoint.model_validate(raw)
        if isinstance(raw, int) and raw >= 0:
            return TimePoint(
                source_time_unix_nano=str(raw),
                clock_domain_id=self.recorder.clock_domain_id,
            )
        raise TypeError("LiveKit span lacks a supported timestamp")

    @staticmethod
    def _shift_source_time(point: TimePoint, delta_nano: int) -> TimePoint:
        source = int(point.source_time_unix_nano or "0") + delta_nano
        if source < 0:
            raise ValueError("LiveKit metric duration precedes the Unix epoch")
        return point.model_copy(update={"source_time_unix_nano": str(source)})

    def _seconds_time_point(self, raw: object) -> TimePoint | None:
        timestamp = getattr(raw, "timestamp", None)
        if callable(timestamp):
            raw = timestamp()
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        seconds = float(raw)
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return TimePoint(
            source_time_unix_nano=str(round(seconds * 1_000_000_000)),
            clock_domain_id=self.recorder.clock_domain_id,
        )

    def _nanosecond_time_point(self, raw: object) -> TimePoint | None:
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return None
        return TimePoint(
            source_time_unix_nano=str(raw),
            clock_domain_id=self.recorder.clock_domain_id,
        )

    @staticmethod
    def _boolean_decision(raw: object) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes"}
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return math.isfinite(float(raw)) and bool(raw)
        return False

    @staticmethod
    def _metric_attributes(metric: object) -> dict[str, Any]:
        metric_type = _metric_type(metric)
        attributes: dict[str, Any] = {"earshot.framework.name": "livekit"}
        connection_only = _is_connection_acquisition_metric(metric)
        mappings: dict[str, str] = {}
        if not connection_only:
            mappings.update(
                {
                    "ttft": "lk.response.ttft",
                    "ttfb": "lk.response.ttfb",
                    "end_of_utterance_delay": "lk.eou.endpointing_delay",
                    "transcription_delay": "lk.eou.transcription_delay",
                    "on_user_turn_completed_delay": "earshot.duration.turn_callback_seconds",
                    "prompt_tokens": "gen_ai.usage.input_tokens",
                    "completion_tokens": "gen_ai.usage.output_tokens",
                    "prompt_cached_tokens": "gen_ai.usage.input_cached_tokens",
                    "total_tokens": "earshot.metric.model.total_tokens",
                    "idle_time": "earshot.duration.vad.idle_seconds",
                    "inference_duration_total": "earshot.duration.inference_seconds",
                    "inference_count": "earshot.metric.inference.count",
                    "num_interruptions": "earshot.metric.interruption.count",
                    "num_backchannels": "earshot.metric.interruption.backchannel_count",
                    "playback_latency": "earshot.duration.avatar.playback_latency_seconds",
                }
            )
        if metric_type in {"realtimemodelmetrics", "realtime_model_metrics"}:
            mappings.update(
                {
                    "acquire_time": "livekit.realtime.connection_acquire_time",
                    **(
                        {
                            "input_tokens": "gen_ai.usage.input_tokens",
                            "output_tokens": "gen_ai.usage.output_tokens",
                            "session_duration": "livekit.realtime.session_duration",
                        }
                        if not connection_only
                        else {}
                    ),
                }
            )
        elif metric_type in {"sttmetrics", "stt_metrics"}:
            mappings.update(
                {
                    "acquire_time": "livekit.stt.connection_acquire_time",
                    **(
                        {
                            "audio_duration": "livekit.stt.audio_duration",
                            "input_tokens": "livekit.stt.input_tokens",
                            "output_tokens": "livekit.stt.output_tokens",
                        }
                        if not connection_only
                        else {}
                    ),
                }
            )
        elif metric_type in {"ttsmetrics", "tts_metrics"}:
            mappings.update(
                {
                    "acquire_time": "livekit.tts.connection_acquire_time",
                    **(
                        {
                            "audio_duration": "livekit.tts.audio_duration",
                            "characters_count": "livekit.tts.character_count",
                            "input_tokens": "livekit.tts.input_tokens",
                            "output_tokens": "livekit.tts.output_tokens",
                        }
                        if not connection_only
                        else {}
                    ),
                }
            )
        if not connection_only and metric_type in {
            "interruptionmetrics",
            "interruption_metrics",
        }:
            mappings.update(
                {
                    "detection_delay": ("earshot.duration.interruption.detection_delay_seconds"),
                    "prediction_duration": "earshot.duration.interruption.prediction_seconds",
                    "total_duration": "earshot.duration.interruption.total_seconds",
                    "num_requests": "earshot.metric.interruption.request_count",
                }
            )
        elif not connection_only and metric_type in {
            "eotinferencemetrics",
            "eot_inference_metrics",
        }:
            mappings.update(
                {
                    "detection_delay": ("earshot.duration.turn_detection.detection_delay_seconds"),
                    "prediction_duration": "earshot.duration.turn_detection.prediction_seconds",
                    "total_duration": "earshot.duration.turn_detection.total_seconds",
                    "num_requests": "earshot.metric.turn_detection.request_count",
                }
            )
        for source, target in mappings.items():
            raw = value(metric, source)
            if source in _METRIC_COUNTER_FIELDS:
                valid = _is_i_json_counter(raw)
            elif source == "acquire_time":
                valid = (
                    _is_non_negative_finite_number(raw)
                    and isinstance(raw, (int, float))
                    and raw > 0
                )
            else:
                valid = _is_non_negative_finite_number(raw)
            if valid:
                attributes[target] = raw

        if not connection_only and metric_type in {
            "realtimemodelmetrics",
            "realtime_model_metrics",
        }:
            input_details = value(metric, "input_token_details")
            cached_details = value(input_details, "cached_tokens_details")
            output_details = value(metric, "output_token_details")
            nested_counters = (
                (input_details, "audio_tokens", "gen_ai.usage.input_audio_tokens"),
                (input_details, "text_tokens", "gen_ai.usage.input_text_tokens"),
                (input_details, "image_tokens", "gen_ai.usage.input_image_tokens"),
                (input_details, "cached_tokens", "gen_ai.usage.input_cached_tokens"),
                (
                    cached_details,
                    "audio_tokens",
                    "gen_ai.usage.input_cached_audio_tokens",
                ),
                (
                    cached_details,
                    "text_tokens",
                    "gen_ai.usage.input_cached_text_tokens",
                ),
                (
                    cached_details,
                    "image_tokens",
                    "gen_ai.usage.input_cached_image_tokens",
                ),
                (output_details, "audio_tokens", "gen_ai.usage.output_audio_tokens"),
                (output_details, "text_tokens", "gen_ai.usage.output_text_tokens"),
                (output_details, "image_tokens", "gen_ai.usage.output_image_tokens"),
            )
            for container, source, target in nested_counters:
                raw = value(container, source)
                if _is_i_json_counter(raw):
                    attributes[target] = raw
        for source, target in (
            ("cancelled", "earshot.metric.request.cancelled"),
            ("streamed", "earshot.metric.request.streamed"),
            ("connection_reused", "earshot.metric.connection.reused"),
        ):
            if connection_only and source != "connection_reused":
                continue
            raw = value(metric, source)
            if isinstance(raw, bool):
                attributes[target] = raw
        metadata = value(metric, "metadata")
        model = value(metadata, "model_name") if metadata is not None else None
        provider = value(metadata, "model_provider") if metadata is not None else None
        if isinstance(model, str) and model:
            attributes["gen_ai.request.model"] = model
        if isinstance(provider, str) and provider:
            attributes["gen_ai.provider.name"] = provider
        label = value(metric, "label")
        if label is not None:
            safe_label = sanitize_semantic_label(str(label))
            if safe_label is not None:
                attributes["earshot.framework.metric.name"] = safe_label
        return attributes

    @staticmethod
    def _interruption_attributes(source: object) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        mappings = {
            "is_interruption": "earshot.metric.interruption.accepted",
            "resumed": "earshot.metric.interruption.resumed",
            "probability": "earshot.metric.interruption.probability",
            "total_duration": "earshot.duration.interruption.total_seconds",
            "prediction_duration": "earshot.duration.interruption.prediction_seconds",
            "detection_delay": "earshot.duration.interruption.detection_delay_seconds",
            "num_requests": "earshot.metric.interruption.request_count",
            "lk.is_interruption": "earshot.metric.interruption.accepted",
            "lk.interrupted": "earshot.metric.interruption.accepted",
            "lk.interruption.probability": "earshot.metric.interruption.probability",
            "lk.interruption.total_duration": "earshot.duration.interruption.total_seconds",
            "lk.interruption.prediction_duration": (
                "earshot.duration.interruption.prediction_seconds"
            ),
            "lk.interruption.detection_delay": (
                "earshot.duration.interruption.detection_delay_seconds"
            ),
        }
        boolean_fields = {
            "is_interruption",
            "resumed",
            "lk.is_interruption",
            "lk.interrupted",
        }
        probability_fields = {
            "probability",
            "lk.interruption.probability",
        }
        for source_key, target in mappings.items():
            raw = value(source, source_key)
            if source_key == "num_requests":
                valid = _is_i_json_counter(raw)
            elif source_key in boolean_fields:
                valid = isinstance(raw, bool)
            elif source_key in probability_fields:
                valid = _is_unit_interval_number(raw)
            else:
                valid = _is_non_negative_finite_number(raw)
            if valid:
                attributes[target] = raw
        return attributes

    @staticmethod
    def _metric_snapshot(
        metric: object, metric_type: str, attributes: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "type": metric_type,
            "request_id": value(metric, "request_id"),
            "segment_id": value(metric, "segment_id"),
            "speech_id": value(metric, "speech_id"),
            "sequence_id": value(metric, "sequence_id"),
            "timestamp": value(metric, "timestamp"),
            "duration": value(metric, "duration", value(metric, "total_duration")),
            "attributes": attributes,
        }

    @staticmethod
    def _is_livekit_span(span: object) -> bool:
        scope = value(span, "instrumentation_scope") or value(span, "instrumentation_info")
        scope_name = str(value(scope, "name", "")).lower()
        if scope_name.startswith("livekit"):
            return True
        attributes = value(span, "attributes", {}) or {}
        return isinstance(attributes, Mapping) and any(
            str(key).startswith("lk.") for key in attributes
        )

    @staticmethod
    def _attach_listener(session: object, event_name: str, listener: object) -> None:
        on = getattr(session, "on", None)
        if on is None:
            raise TypeError("LiveKit session does not expose an event listener API")
        try:
            registration = on(event_name)
        except TypeError:
            registration = None
        if callable(registration):
            registration(listener)
            return
        try:
            on(event_name, listener)
        except TypeError as error:
            raise TypeError("unsupported LiveKit event listener signature") from error
