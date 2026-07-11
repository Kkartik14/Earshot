"""Pipecat normalization without a second trace root.

The adapter consumes exported/native span dictionaries or observer facts. It keeps
the original trace/span identity and authors only Earshot classification,
provenance, and coverage that Pipecat does not already express.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from typing import Any

from ..contract import Adapter, CausalLink, Evidence, TimePoint
from ..privacy import sanitize_semantic_label, sanitize_source_label
from ..recorder import IncidentRecorder
from .base import AdapterDependencyError, stable_id, value

_EXACT_SPAN_NAMES = {
    # Current Pipecat native speech-to-speech tool telemetry. Exact matching is
    # deliberately evaluated before the broader ``llm`` fallback below.
    "llm_tool_call": "tool",
    "llm_tool_result": "tool",
}

_SPAN_NAMES = {
    "stt": "stt",
    "asr": "stt",
    "tool": "tool",
    "llm": "llm",
    "tts": "tts",
    # Pipecat's ``turn`` span contains the complete STT→LLM→TTS lifecycle. It
    # is not an endpoint-detector interval and must not become the latency anchor.
    "turn": "framework_operation",
    "vad": "vad",
}


def _classify(span: object) -> str:
    attributes = value(span, "attributes", {}) or {}
    explicit = attributes.get("earshot.operation.name") if isinstance(attributes, Mapping) else None
    if isinstance(explicit, str) and explicit:
        return explicit
    name = str(value(span, "name", "unknown")).lower()
    if name in _EXACT_SPAN_NAMES:
        return _EXACT_SPAN_NAMES[name]
    for token, operation in _SPAN_NAMES.items():
        if token in name:
            return operation
    return "framework_operation"


def _normalize_native_attributes(span: object, attributes: dict[str, object]) -> dict[str, object]:
    """Give ambiguous Pipecat payload keys an explicit governed namespace."""

    normalized = dict(attributes)
    operation = _classify(span)
    mappings: dict[str, str] = {}
    if operation in {"llm", "agent"}:
        mappings = {
            "input": "gen_ai.input",
            "output": "gen_ai.output.messages",
            "tools": "gen_ai.input.tools",
        }
    elif operation == "tool":
        mappings = {"input": "tool.arguments", "output": "tool.result"}
    elif operation in {"stt", "tts", "turn_detection"}:
        mappings = {"text": "speech.text"}
        if operation == "tts":
            mappings["input"] = "speech.text"
    for source, target in mappings.items():
        if source not in normalized:
            continue
        normalized.setdefault(target, normalized[source])
        del normalized[source]
    return normalized


class PipecatAdapter:
    def __init__(self, recorder: IncidentRecorder, *, framework_version: str = "unknown"):
        self.recorder = recorder
        self.framework_version = framework_version
        self._render_coverage_written = False
        self._seen: dict[tuple[str, str], str] = {}
        self._seen_interruptions: set[str] = set()
        self._seen_source_events: set[str] = set()
        self._lock = threading.RLock()
        self.recorder.register_adapter(
            Adapter(
                name="earshot.pipecat",
                version="0.1.0",
                framework="pipecat",
                framework_version=framework_version,
            )
        )

    def consume_span(self, span: object) -> str:
        """Consume a normalized/native Pipecat span-shaped object."""

        attributes = _normalize_native_attributes(
            span,
            dict(value(span, "attributes", {}) or {}),
        )
        native_name = str(value(span, "name", "unknown"))
        attributes.setdefault(
            "earshot.framework.operation.name",
            sanitize_source_label(native_name),
        )
        context = value(span, "context")
        if context is not None and hasattr(context, "trace_id"):
            trace_id = f"{int(context.trace_id):032x}"
            span_id = f"{int(context.span_id):016x}"
        else:
            trace_id = value(span, "trace_id")
            span_id = value(span, "span_id")
        operation_id = str(
            value(span, "operation_id")
            or stable_id("pipecat", trace_id or "", span_id or "", value(span, "name", ""))
        )
        raw_start = value(span, "started_at", value(span, "start_time"))
        raw_end = value(span, "ended_at", value(span, "end_time"))
        start = self._time_point(raw_start)
        end = self._time_point(raw_end) if raw_end is not None else None
        parent = value(span, "parent")
        parent_span_id = value(span, "parent_span_id")
        if parent_span_id is None and parent is not None and hasattr(parent, "span_id"):
            parent_span_id = f"{int(parent.span_id):016x}"
        status = value(span, "status", "unset")
        status_code = value(status, "status_code")
        if status_code is not None:
            status = str(value(status_code, "name", status_code)).lower()
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
            if link_context is None:
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
                    trace_id=f"{int(value(link_context, 'trace_id')):032x}",
                    span_id=f"{int(value(link_context, 'span_id')):016x}",
                    attributes=link_attributes,
                )
            )

        identity = (str(trace_id or ""), str(span_id or operation_id))
        turn_value = value(span, "turn_id")
        if turn_value is None:
            turn_value = attributes.get("turn.id")
        if turn_value is None:
            turn_value = attributes.get("turn.number")
        turn_id = str(turn_value) if turn_value is not None else None
        raw_parent_scope = str(value(span, "parent_scope", "unknown"))
        parent_scope = (
            raw_parent_scope
            if raw_parent_scope in {"internal", "external", "unknown"}
            else "unknown"
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "name": value(span, "name", ""),
                    "operation_name": _classify(span),
                    "status": str(status),
                    "start": start.model_dump(mode="json"),
                    "end": end.model_dump(mode="json") if end else None,
                    "participant_id": value(span, "participant_id"),
                    "stream_id": value(span, "stream_id"),
                    "turn_id": turn_id,
                    "parent_span_id": str(parent_span_id) if parent_span_id else None,
                    "parent_scope": parent_scope,
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
        with self._lock:
            previous = self._seen.get(identity)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("conflicting duplicate Pipecat span identity")
                return operation_id
            evidence = Evidence(
                source="pipecat",
                observer="server",
                method="native_otel",
                method_version=self.framework_version,
                confidence="measured",
                availability="available",
                source_field=native_name,
            )
            self.recorder.record_operation(
                operation_id=operation_id,
                operation_name=_classify(span),
                status=str(status),
                started_at=start,
                ended_at=end,
                participant_id=value(span, "participant_id"),
                stream_id=value(span, "stream_id"),
                turn_id=turn_id,
                trace_id=str(trace_id) if trace_id else None,
                span_id=str(span_id) if span_id else None,
                parent_span_id=str(parent_span_id) if parent_span_id else None,
                parent_scope=parent_scope,
                links=tuple(links),
                resource=resource,
                resource_schema_url=(
                    str(resource_schema_url) if resource_schema_url else None
                ),
                instrumentation_scope_name=str(scope_name) if scope_name else None,
                instrumentation_scope_version=str(scope_version) if scope_version else None,
                instrumentation_scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
                evidence=evidence,
                attributes=attributes,
            )
            self._seen[identity] = fingerprint
            self._record_source_span_events(
                span,
                operation_id=operation_id,
                trace_id=str(trace_id) if trace_id else None,
                span_id=str(span_id) if span_id else None,
                turn_id=turn_id,
                default_time=end or start,
                resource=resource,
                resource_schema_url=(
                    str(resource_schema_url) if resource_schema_url else None
                ),
                scope_name=str(scope_name) if scope_name else None,
                scope_version=str(scope_version) if scope_version else None,
                scope_attributes=scope_attributes,
                schema_url=str(schema_url) if schema_url else None,
            )
            if (
                attributes.get("turn.was_interrupted") is True
                and attributes.get("turn.ended_by_conversation_end") is not True
            ):
                interruption_key = turn_id or f"{trace_id}:{span_id}"
                if interruption_key not in self._seen_interruptions:
                    self.recorder.record_event(
                        "earshot.interruption.accepted",
                        event_id=stable_id("pipecat-interruption", interruption_key),
                        time=end or start,
                        operation_id=operation_id,
                        turn_id=turn_id,
                        trace_id=str(trace_id) if trace_id else None,
                        span_id=str(span_id) if span_id else None,
                        resource=resource,
                        resource_schema_url=(
                            str(resource_schema_url) if resource_schema_url else None
                        ),
                        instrumentation_scope_name=(str(scope_name) if scope_name else None),
                        instrumentation_scope_version=(
                            str(scope_version) if scope_version else None
                        ),
                        instrumentation_scope_attributes=scope_attributes,
                        schema_url=str(schema_url) if schema_url else None,
                        evidence=Evidence(
                            source="pipecat",
                            observer="server",
                            method="native_otel_attribute",
                            method_version=self.framework_version,
                            confidence="measured",
                            availability="available",
                            source_field="turn.was_interrupted",
                        ),
                        attributes={"earshot.metric.interruption.accepted": True},
                    )
                    self._seen_interruptions.add(interruption_key)
            self._mark_render_unobserved()
        return operation_id

    def _record_source_span_events(
        self,
        span: object,
        *,
        operation_id: str,
        trace_id: str | None,
        span_id: str | None,
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
            event_time = self._time_point(raw_time) if raw_time is not None else default_time
            event_id = stable_id(
                "pipecat-source-event",
                trace_id or operation_id,
                span_id or operation_id,
                index,
                native_name,
                event_time.source_time_unix_nano or event_time.monotonic_time_nano or "",
            )
            if event_id in self._seen_source_events:
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
                    source="pipecat",
                    observer="server",
                    method="native_otel_span_event",
                    method_version=self.framework_version,
                    confidence="measured" if raw_time is not None else "estimated",
                    availability="available",
                    source_field=native_name,
                ),
                attributes=native_attributes,
            )
            self._seen_source_events.add(event_id)

    def mark_unobserved(self, signal: str, reason: str = "not_exposed") -> None:
        self.recorder.record_coverage(signal, "not_observed", reason)

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
        """Best-effort diagnostics must never escape an OTel callback."""

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
        raise TypeError("Pipecat span lacks a supported timestamp")

    def create_span_processor(self) -> object:
        """Create a processor to add to the application's existing provider."""

        try:
            from opentelemetry.sdk.trace import ReadableSpan, Span
            from opentelemetry.sdk.trace.export import SpanProcessor
        except ImportError as error:  # pragma: no cover - optional dependency
            raise AdapterDependencyError(
                "Pipecat OTel integration requires opentelemetry-sdk"
            ) from error

        adapter = self

        class EarshotPipecatSpanProcessor(SpanProcessor):
            def on_start(self, span: Span, parent_context: object | None = None) -> None:
                del span, parent_context

            def on_end(self, span: ReadableSpan) -> None:
                if not adapter._is_pipecat_span(span):
                    return
                try:
                    adapter.consume_span(span)
                except Exception:
                    adapter._record_coverage_safe(
                        "pipecat.span",
                        "unsupported_span_shape",
                    )

            def shutdown(self) -> None:
                return None

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                del timeout_millis
                return True

        return EarshotPipecatSpanProcessor()

    @staticmethod
    def _is_pipecat_span(span: object) -> bool:
        scope = value(span, "instrumentation_scope") or value(span, "instrumentation_info")
        scope_name = str(value(scope, "name", "")).lower()
        if scope_name == "pipecat" or scope_name.startswith("pipecat."):
            return True
        attributes = value(span, "attributes", {}) or {}
        return isinstance(attributes, Mapping) and any(
            key in attributes
            for key in (
                "conversation.id",
                "turn.number",
                "turn.was_interrupted",
                "metrics.ttfb",
            )
        )

    def attach(self, tracer_provider: object) -> object:
        add = getattr(tracer_provider, "add_span_processor", None)
        if not callable(add):
            raise TypeError("tracer provider does not support span processors")
        processor = self.create_span_processor()
        add(processor)
        return processor
