"""Pipecat normalization without a second trace root.

The adapter consumes exported/native span dictionaries or observer facts. It keeps
the original trace/span identity and authors only Earshot classification,
provenance, and coverage that Pipecat does not already express.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
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
from ..versions import PIPECAT_ADAPTER_VERSION
from . import routing
from .base import (
    DEFAULT_ADAPTER_TRACKING_ENTRIES,
    AdapterDependencyError,
    AdapterTrackingStatus,
    stable_id,
    validate_tracking_limit,
    value,
)

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

# Governed allowlist of vendor metric attributes lifted into quality samples.
# This is deliberately NOT a namespace wildcard: an unknown numeric attribute
# (say a custom ``metrics.customer_phone``) must stay subject to the operation's
# metadata privacy rules and must never be recreated verbatim as a measurement.
#
# Names are stage-scoped because Pipecat reports both LLM and TTS first-byte
# latency under the same ``metrics.ttfb`` key, and the analyzer keys provider
# measurements by name -- unscoped, one stage would silently overwrite the other.
# Units are declared here rather than guessed from the key, so a millisecond
# field can never be relabelled as seconds.
_VENDOR_QUALITY_METRICS: dict[str, tuple[str, str, str, frozenset[str]]] = {
    # source attribute -> (stage-scoped suffix, unit, aggregation, native stages)
    "metrics.ttfb": ("ttfb", "s", "instant", frozenset({"stt", "llm", "tts"})),
    "metrics.character_count": (
        "character_count",
        "count",
        "delta",
        frozenset({"tts"}),
    ),
    # Pipecat's turn observer measures actual user speech stop to server-side
    # BotStartedSpeakingFrame. Keep the public name turn-scoped even though the
    # native turn span is intentionally normalized as a framework container.
    "turn.user_bot_latency_seconds": (
        "user_bot_latency",
        "s",
        "instant",
        frozenset({"framework_operation"}),
    ),
}

# Standard OTel GenAI usage counters are LLM-only (already stage-unique) and are
# understood by generic backends, so they keep their canonical names.
_STANDARD_QUALITY_METRICS: dict[str, tuple[str, str]] = {
    "gen_ai.usage.input_tokens": ("count", "delta"),
    "gen_ai.usage.output_tokens": ("count", "delta"),
    "gen_ai.usage.cache_read.input_tokens": ("count", "delta"),
    "gen_ai.usage.cache_creation.input_tokens": ("count", "delta"),
    "gen_ai.usage.reasoning_tokens": ("count", "delta"),
}
_IJSON_INTEGER_MAX = 9_007_199_254_740_991
_MAX_DURATION_SECONDS = ((1 << 64) - 1) / 1_000_000_000


@dataclass(frozen=True, slots=True)
class _SpanProvenance:
    """Correlation and OTel ownership shared by facts emitted from one span."""

    operation_id: str
    trace_id: str | None
    span_id: str | None
    turn_id: str | None
    resource: Mapping[str, Any]
    resource_schema_url: str | None
    instrumentation_scope_name: str | None
    instrumentation_scope_version: str | None
    instrumentation_scope_attributes: Mapping[str, Any]
    schema_url: str | None


def _nonnegative_finite_number(raw: object) -> bool:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    if isinstance(raw, int) and raw > _IJSON_INTEGER_MAX:
        return False
    return raw >= 0 and math.isfinite(float(raw)) and float(raw) <= _MAX_DURATION_SECONDS


def _nonnegative_integer(raw: object) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= _IJSON_INTEGER_MAX


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
    def __init__(
        self,
        recorder: IncidentRecorder,
        *,
        framework_version: str = "unknown",
        max_tracking_entries: int = DEFAULT_ADAPTER_TRACKING_ENTRIES,
    ):
        self.recorder = recorder
        self.framework_version = framework_version
        self._max_tracking_entries = validate_tracking_limit(max_tracking_entries)
        self._render_coverage_written = False
        self._seen: dict[tuple[str, str], str] = {}
        self._seen_interruption_frames: set[int] = set()
        self._accepted_interruption_frames: set[int] = set()
        self._seen_source_events: set[str] = set()
        self._saturated_ledgers: set[str] = set()
        self._routing_handle: routing.RoutingHandle | None = None
        self._lock = threading.RLock()
        self.recorder.register_adapter(
            Adapter(
                name="earshot.pipecat",
                version=PIPECAT_ADAPTER_VERSION,
                framework="pipecat",
                framework_version=framework_version,
            )
        )

    def tracking_status(self) -> AdapterTrackingStatus:
        """Return content-free sizes for all persistent identity ledgers."""

        with self._lock:
            return AdapterTrackingStatus(
                limit_per_ledger=self._max_tracking_entries,
                entries=(
                    (
                        "accepted_interruption_frames",
                        len(self._accepted_interruption_frames),
                    ),
                    ("interruption_frames", len(self._seen_interruption_frames)),
                    ("source_events", len(self._seen_source_events)),
                    ("spans", len(self._seen)),
                ),
                saturated_ledgers=tuple(sorted(self._saturated_ledgers)),
            )

    def _tracking_has_capacity_locked(
        self,
        ledger_name: str,
        ledger: Mapping[object, object] | set[object],
    ) -> bool:
        if len(ledger) < self._max_tracking_entries:
            return True
        if ledger_name not in self._saturated_ledgers:
            self._saturated_ledgers.add(ledger_name)
            self._record_tracking_saturation(ledger_name)
        return False

    def _record_tracking_saturation(self, ledger_name: str) -> None:
        """Best-effort, content-free notice that identity tracking is partial."""

        try:
            self.recorder.record_coverage(
                f"pipecat.tracking.{ledger_name}",
                "partial",
                "max_tracking_entries",
            )
        except Exception:
            return

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
        provenance = _SpanProvenance(
            operation_id=operation_id,
            trace_id=str(trace_id) if trace_id else None,
            span_id=str(span_id) if span_id else None,
            turn_id=turn_id,
            resource=resource,
            resource_schema_url=(str(resource_schema_url) if resource_schema_url else None),
            instrumentation_scope_name=str(scope_name) if scope_name else None,
            instrumentation_scope_version=str(scope_version) if scope_version else None,
            instrumentation_scope_attributes=scope_attributes,
            schema_url=str(schema_url) if schema_url else None,
        )
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
            if not self._tracking_has_capacity_locked("spans", self._seen):
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
            operation = self.recorder.record_operation(
                operation_id=provenance.operation_id,
                operation_name=_classify(span),
                status=str(status),
                started_at=start,
                ended_at=end,
                participant_id=value(span, "participant_id"),
                stream_id=value(span, "stream_id"),
                turn_id=provenance.turn_id,
                trace_id=provenance.trace_id,
                span_id=provenance.span_id,
                parent_span_id=str(parent_span_id) if parent_span_id else None,
                parent_scope=parent_scope,
                links=tuple(links),
                resource=provenance.resource,
                resource_schema_url=provenance.resource_schema_url,
                instrumentation_scope_name=provenance.instrumentation_scope_name,
                instrumentation_scope_version=provenance.instrumentation_scope_version,
                instrumentation_scope_attributes=(provenance.instrumentation_scope_attributes),
                schema_url=provenance.schema_url,
                evidence=evidence,
                attributes=attributes,
            )
            self._seen[identity] = fingerprint
            self._record_span_metric_quality(
                operation_name=operation.operation_name,
                attributes=operation.attributes,
                start=start,
                end=end,
                provenance=provenance,
            )
            self._record_source_span_events(
                span,
                default_time=end or start,
                provenance=provenance,
            )
            self._mark_render_unobserved()
        return operation_id

    def _record_span_metric_quality(
        self,
        *,
        operation_name: str,
        attributes: Mapping[str, Any],
        start: TimePoint,
        end: TimePoint | None,
        provenance: _SpanProvenance,
    ) -> None:
        """Surface Pipecat's span-attribute metrics as a pipeline quality sample.

        Pipecat carries per-stage metrics (``metrics.ttfb``, ``gen_ai.usage.*``) as
        span attributes rather than through a metrics event bus. Lifting the governed
        ones into a quality sample gives the analyzer the same ``provider_measurements``
        it derives from LiveKit's metrics callbacks, so both runtimes reach parity.
        The metric stays on the operation too; this only adds the normalized view.

        Only fields in the explicit allowlists are lifted, so an unknown attribute
        cannot bypass the operation's metadata privacy rules by reappearing here.
        """

        measurements: list[tuple[str, QualityMeasurement]] = []
        for source, (suffix, unit, aggregation, stages) in _VENDOR_QUALITY_METRICS.items():
            if operation_name not in stages:
                continue
            raw = attributes.get(source)
            valid = (
                _nonnegative_integer(raw) if unit == "count" else _nonnegative_finite_number(raw)
            )
            if not valid:
                continue
            measurements.append(
                (
                    source,
                    QualityMeasurement(
                        name=(
                            "pipecat.turn.user_bot_latency"
                            if source == "turn.user_bot_latency_seconds"
                            else f"pipecat.{operation_name}.{suffix}"
                        ),
                        value=raw,
                        unit=unit,
                        aggregation=aggregation,
                    ),
                )
            )
        if operation_name == "llm":
            for source, (unit, aggregation) in _STANDARD_QUALITY_METRICS.items():
                raw = attributes.get(source)
                if not _nonnegative_integer(raw):
                    continue
                measurements.append(
                    (
                        source,
                        QualityMeasurement(
                            name=source,
                            value=raw,
                            unit=unit,
                            aggregation=aggregation,
                        ),
                    )
                )
        if not measurements:
            return
        measurements.sort(key=lambda item: item[1].name)

        sample_attributes: dict[str, Any] = {"earshot.operation.id": provenance.operation_id}
        if provenance.turn_id is not None:
            sample_attributes["earshot.turn.id"] = provenance.turn_id
        for source, measurement in measurements:
            self.recorder.record_quality_sample(
                QualitySample(
                    sample_id=stable_id(
                        "pipecat-span-metric",
                        provenance.trace_id or "",
                        provenance.span_id or provenance.operation_id,
                        source,
                    ),
                    session_id=self.recorder.session_id,
                    quality_kind="pipeline.metric",
                    sample_window=TimeRange(start=start, end=end or start),
                    measurements=(measurement,),
                    evidence=Evidence(
                        source="pipecat",
                        observer="server",
                        method="native_otel_attribute",
                        method_version=self.framework_version,
                        confidence="measured",
                        availability="available",
                        source_field=source,
                        attributes={"earshot.framework.metric.name": source},
                    ),
                    resource=dict(provenance.resource),
                    resource_schema_url=provenance.resource_schema_url,
                    instrumentation_scope_name=provenance.instrumentation_scope_name,
                    instrumentation_scope_version=(provenance.instrumentation_scope_version),
                    instrumentation_scope_attributes=dict(
                        provenance.instrumentation_scope_attributes
                    ),
                    schema_url=provenance.schema_url,
                    attributes=sample_attributes,
                )
            )

    def _record_source_span_events(
        self,
        span: object,
        *,
        default_time: TimePoint,
        provenance: _SpanProvenance,
    ) -> None:
        for index, native_event in enumerate(value(span, "events", ()) or ()):
            native_name = str(value(native_event, "name", "unnamed")) or "unnamed"
            raw_time = value(native_event, "timestamp")
            event_time = self._time_point(raw_time) if raw_time is not None else default_time
            event_id = stable_id(
                "pipecat-source-event",
                provenance.trace_id or provenance.operation_id,
                provenance.span_id or provenance.operation_id,
                index,
                native_name,
                event_time.source_time_unix_nano or event_time.monotonic_time_nano or "",
            )
            if event_id in self._seen_source_events:
                continue
            if not self._tracking_has_capacity_locked(
                "source_events",
                self._seen_source_events,
            ):
                break
            native_attributes = dict(value(native_event, "attributes", {}) or {})
            native_attributes["earshot.source.event.name"] = sanitize_source_label(native_name)
            self.recorder.record_event(
                "otel.span_event",
                event_id=event_id,
                time=event_time,
                operation_id=provenance.operation_id,
                turn_id=provenance.turn_id,
                trace_id=provenance.trace_id,
                span_id=provenance.span_id,
                resource=provenance.resource,
                resource_schema_url=provenance.resource_schema_url,
                instrumentation_scope_name=provenance.instrumentation_scope_name,
                instrumentation_scope_version=provenance.instrumentation_scope_version,
                instrumentation_scope_attributes=(provenance.instrumentation_scope_attributes),
                schema_url=provenance.schema_url,
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

    def consume_interruption_frame(
        self,
        frame: object,
        *,
        observed_at: TimePoint | int | None,
        bot_was_speaking: bool,
        interrupted_turn_id: str | int | None = None,
    ) -> str | None:
        """Classify a Pipecat interruption using explicit bot-playout state.

        Pipecat broadcasts ``InterruptionFrame`` at the beginning of an ordinary
        first user turn, so the frame alone is not evidence of barge-in. An
        accepted interruption is authored only when a native bot-speaking frame
        established that playout was active when this frame was first observed.
        """

        frame_type = value(frame, "type") or type(frame).__name__
        if str(frame_type).lower() != "interruptionframe":
            raise TypeError("unsupported Pipecat interruption frame")
        frame_id = value(frame, "id")
        sibling_id = value(frame, "broadcast_sibling_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
            raise TypeError("Pipecat interruption frame requires a nonnegative integer ID")
        if sibling_id is not None and (
            isinstance(sibling_id, bool) or not isinstance(sibling_id, int) or sibling_id < 0
        ):
            raise TypeError("Pipecat interruption sibling ID must be a nonnegative integer")
        canonical_id = min(frame_id, sibling_id) if sibling_id is not None else frame_id
        event_id = stable_id("pipecat-interruption-frame", canonical_id)
        if observed_at is None:
            # ``FramePushed.timestamp`` belongs to Pipecat's independently
            # originated pipeline clock. Let the recorder timestamp observer
            # receipt in its own declared clock domain instead of conflating them.
            event_time = None
        elif isinstance(observed_at, TimePoint):
            event_time = observed_at
        elif (
            isinstance(observed_at, int)
            and not isinstance(observed_at, bool)
            and 0 <= observed_at <= 18_446_744_073_709_551_615
        ):
            event_time = TimePoint(
                monotonic_time_nano=str(observed_at),
                clock_domain_id=self.recorder.clock_domain_id,
            )
        else:
            raise TypeError("Pipecat observer timestamp must be a nonnegative integer")
        if not isinstance(bot_was_speaking, bool):
            raise TypeError("Pipecat bot-speaking state must be a boolean")
        if isinstance(interrupted_turn_id, bool) or (
            interrupted_turn_id is not None and not isinstance(interrupted_turn_id, (str, int))
        ):
            raise TypeError("Pipecat interrupted turn ID must be a string or integer")
        turn_id = str(interrupted_turn_id) if interrupted_turn_id is not None else None

        with self._lock:
            if canonical_id in self._seen_interruption_frames:
                return event_id if canonical_id in self._accepted_interruption_frames else None
            if not self._tracking_has_capacity_locked(
                "interruption_frames",
                self._seen_interruption_frames,
            ):
                return None
            # Classify the broadcast on first observation. Without this guard, a
            # normal first-turn frame could traverse another edge after bot
            # playout starts and be retroactively mislabeled as a barge-in.
            self._seen_interruption_frames.add(canonical_id)
            if not bot_was_speaking:
                return None
            self.recorder.record_event(
                "earshot.interruption.accepted",
                event_id=event_id,
                time=event_time,
                turn_id=turn_id,
                evidence=Evidence(
                    source="pipecat",
                    observer="server",
                    method="native_frame_overlap_observer",
                    method_version=self.framework_version,
                    confidence="inferred",
                    availability="available",
                    source_field="InterruptionFrame+BotStartedSpeakingFrame",
                    attributes={"earshot.framework.operation.name": "interruption_frame"},
                ),
                attributes={"earshot.metric.interruption.accepted": True},
            )
            self._accepted_interruption_frames.add(canonical_id)
        return event_id

    def create_observer(self) -> object:
        """Create a Pipecat 1.5 observer for explicit pipeline-frame facts."""

        try:
            from pipecat.frames.frames import (
                BotStartedSpeakingFrame,
                BotStoppedSpeakingFrame,
                CancelFrame,
                EndFrame,
                InterruptionFrame,
                StartFrame,
                UserStartedSpeakingFrame,
            )
            from pipecat.observers.base_observer import BaseObserver, FramePushed
        except ImportError as error:  # pragma: no cover - optional dependency
            raise AdapterDependencyError(
                "Pipecat frame observation requires pipecat-ai>=1.5,<1.6"
            ) from error

        adapter = self

        class EarshotPipecatObserver(BaseObserver):
            def __init__(self) -> None:
                super().__init__()
                self._active_bot_playouts = 0
                self._current_turn_number: int | None = None
                self._has_bot_spoken = False
                self._turn_bot_speaking = False
                self._pending_interrupted_turn: str | None = None
                self._seen_playout_frames: set[int] = set()
                self._seen_turn_frames: set[int] = set()
                self._saturated_ledgers: set[str] = set()
                self._tracking_disabled = False
                self._tracking_lock = threading.RLock()

            def tracking_status(self) -> AdapterTrackingStatus:
                """Return content-free sizes for this observer's ledgers."""

                with self._tracking_lock:
                    return AdapterTrackingStatus(
                        limit_per_ledger=adapter._max_tracking_entries,
                        entries=(
                            ("playout_frames", len(self._seen_playout_frames)),
                            ("turn_frames", len(self._seen_turn_frames)),
                        ),
                        saturated_ledgers=tuple(sorted(self._saturated_ledgers)),
                    )

            def _reset_state(self) -> None:
                self._active_bot_playouts = 0
                self._current_turn_number = None
                self._has_bot_spoken = False
                self._turn_bot_speaking = False
                self._pending_interrupted_turn = None

            def _admit_frame(
                self,
                ledger_name: str,
                ledger: set[int],
                canonical_id: int,
            ) -> bool:
                if canonical_id in ledger or self._tracking_disabled:
                    return False
                if len(ledger) >= adapter._max_tracking_entries:
                    self._saturated_ledgers.add(ledger_name)
                    self._tracking_disabled = True
                    self._reset_state()
                    adapter._record_tracking_saturation(f"observer.{ledger_name}")
                    return False
                ledger.add(canonical_id)
                return True

            @staticmethod
            def _canonical_frame_id(frame: object) -> int:
                frame_id = value(frame, "id")
                sibling_id = value(frame, "broadcast_sibling_id")
                if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
                    raise TypeError("Pipecat observer frame requires a nonnegative integer ID")
                if sibling_id is not None and (
                    isinstance(sibling_id, bool)
                    or not isinstance(sibling_id, int)
                    or sibling_id < 0
                ):
                    raise TypeError("Pipecat observer sibling ID must be a nonnegative integer")
                return min(frame_id, sibling_id) if sibling_id is not None else frame_id

            async def on_push_frame(self, data: FramePushed) -> None:
                try:
                    with self._tracking_lock:
                        if isinstance(data.frame, (EndFrame, CancelFrame)):
                            self._reset_state()
                            return
                        if self._tracking_disabled:
                            return
                        if isinstance(data.frame, StartFrame):
                            canonical_id = self._canonical_frame_id(data.frame)
                            if not self._admit_frame(
                                "turn_frames",
                                self._seen_turn_frames,
                                canonical_id,
                            ):
                                return
                            self._reset_state()
                            self._current_turn_number = 1
                            return
                        if isinstance(data.frame, UserStartedSpeakingFrame):
                            canonical_id = self._canonical_frame_id(data.frame)
                            if not self._admit_frame(
                                "turn_frames",
                                self._seen_turn_frames,
                                canonical_id,
                            ):
                                return
                            if self._current_turn_number is None:
                                self._current_turn_number = 1
                            if self._turn_bot_speaking:
                                self._pending_interrupted_turn = str(self._current_turn_number)
                                self._current_turn_number += 1
                                self._has_bot_spoken = False
                                self._turn_bot_speaking = False
                            elif self._has_bot_spoken:
                                self._current_turn_number += 1
                                self._has_bot_spoken = False
                            return
                        if isinstance(
                            data.frame,
                            (BotStartedSpeakingFrame, BotStoppedSpeakingFrame),
                        ):
                            canonical_id = self._canonical_frame_id(data.frame)
                            if not self._admit_frame(
                                "playout_frames",
                                self._seen_playout_frames,
                                canonical_id,
                            ):
                                return
                            if isinstance(data.frame, BotStartedSpeakingFrame):
                                if self._current_turn_number is None:
                                    self._current_turn_number = 1
                                self._active_bot_playouts += 1
                                self._has_bot_spoken = True
                                self._turn_bot_speaking = True
                            else:
                                self._active_bot_playouts = max(
                                    0,
                                    self._active_bot_playouts - 1,
                                )
                                self._turn_bot_speaking = self._active_bot_playouts > 0
                                if self._active_bot_playouts == 0:
                                    self._pending_interrupted_turn = None
                            return
                        if not isinstance(data.frame, InterruptionFrame):
                            return
                        interrupted_turn = self._pending_interrupted_turn
                        if interrupted_turn is None and self._current_turn_number is not None:
                            interrupted_turn = str(self._current_turn_number)
                        adapter.consume_interruption_frame(
                            data.frame,
                            observed_at=None,
                            bot_was_speaking=self._active_bot_playouts > 0,
                            interrupted_turn_id=interrupted_turn,
                        )
                        self._pending_interrupted_turn = None
                except Exception:
                    adapter._record_coverage_safe(
                        "pipecat.interruption",
                        "unsupported_frame_shape",
                    )

        return EarshotPipecatObserver()

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
        """Reject the unsafe legacy recorder-bound processor factory."""

        raise RuntimeError(
            "recorder-bound span processors bypass session isolation; "
            "use attach(existing_tracer_provider)"
        )

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

    def _consume_routed_span(self, span: object) -> None:
        try:
            self.consume_span(span)
        except Exception:
            self._record_coverage_safe("pipecat.span", "unsupported_span_shape")

    def _record_routing_loss(self, reason: str) -> None:
        """Surface content-free router loss without escaping an OTel callback."""

        try:
            self.recorder.record_coverage("pipecat.span.routing", "partial", reason)
        except Exception:
            return

    def attach(self, tracer_provider: object) -> routing.RoutingHandle:
        """Route this session's spans through one shared, process-scoped processor.

        Installs exactly one Earshot router processor per provider and registers
        this recorder as the owning session sink, so concurrent sessions never
        ingest one another's spans and release routing state on :meth:`detach`.
        """

        try:
            handle = routing.attach_adapter(
                tracer_provider,
                "pipecat",
                self._is_pipecat_span,
                self.recorder.session_id,
                self._consume_routed_span,
                self._record_routing_loss,
            )
        except ImportError as error:  # pragma: no cover - optional dependency
            raise AdapterDependencyError(
                "Pipecat OTel integration requires opentelemetry-sdk"
            ) from error
        with self._lock:
            previous = self._routing_handle
            self._routing_handle = handle
        if previous is not None:
            previous.close()
        return handle

    def detach(self) -> None:
        """Release this session's routing state from the shared provider."""

        with self._lock:
            handle = self._routing_handle
            self._routing_handle = None
        if handle is not None:
            handle.close()
