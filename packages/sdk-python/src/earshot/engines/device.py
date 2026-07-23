"""Deterministic device / audio-graph diagnostics engine.

Turns AudioContext and device-lifecycle events (Web Audio ``state`` changes,
``getUserMedia`` permission outcomes, ``devicechange`` / sink switches, sample-rate
mismatches and buffer under-runs, ``baseLatency`` / ``outputLatency`` readings)
into governed earshot facts the existing analyzer already diagnoses:

* a denied microphone permission emits ``earshot.device.permission_denied`` and a
  ``device.microphone`` *not-observed* coverage note; a suspended/interrupted
  context emits ``earshot.device.audio_context_suspended``; a device/sink switch
  emits ``earshot.device.route_changed`` -- all fire ``device.unavailable``.
* a sample-rate mismatch or a render under-run/glitch emits
  ``earshot.audio.render.stale``, which fires ``audio.stale_playback``.
* ``baseLatency`` (a deterministic context property, ``measured``) and
  ``outputLatency`` (a W3C *estimate*, ``estimated``) become governed latency
  measurements -- the estimate is never relabelled as measured.

As in the WebRTC engine, an absent field is *unknown* (no fact), never a zero.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..pipeline import TurnRecorder
from .base import (
    EngineCoverage,
    EngineEvent,
    EngineMeasurement,
    _CoverageLedger,
    apply_facts,
)

_SOURCE = "app"
_QUALITY_KIND = "device.audio_graph"

# Event names the capture/render-boundary rules consume.
_PERMISSION_DENIED = "earshot.device.permission_denied"
_CONTEXT_SUSPENDED = "earshot.device.audio_context_suspended"
_DEVICE_ROUTE_CHANGED = "earshot.device.route_changed"
_RENDER_STALE = "earshot.audio.render.stale"

# Governed latency measurement names (no T2 rule reads these).
_BASE_LATENCY = "audio.base_latency"
_OUTPUT_LATENCY = "audio.output_latency"

_MICROPHONE = "device.microphone"

_SUSPENDED_STATES = frozenset({"suspended", "interrupted"})

_PERMISSION_TYPES = frozenset({"permission", "permission_denied", "getusermedia"})
_CONTEXT_TYPES = frozenset(
    {"audiocontext_state", "statechange", "audiocontext", "audiocontextstatechange"}
)
_DEVICE_CHANGE_TYPES = frozenset(
    {"device_change", "devicechange", "sink_change", "sinkchange", "output_change"}
)
_SAMPLE_RATE_TYPES = frozenset({"sample_rate_mismatch", "samplerate_mismatch", "sample_rate"})
_UNDERRUN_TYPES = frozenset({"underrun", "glitch", "dropped_frames", "xrun", "buffer_underrun"})
_LATENCY_TYPES = frozenset({"latency", "audiocontext_latency", "audio_latency"})


@dataclass(frozen=True, slots=True)
class DeviceFacts:
    """Immutable, deterministic result of one audio-graph derivation."""

    measurements: tuple[EngineMeasurement, ...]
    events: tuple[EngineEvent, ...]
    coverage: tuple[EngineCoverage, ...]
    permission_denied: bool = False
    context_suspended: bool = False
    route_changed: bool = False
    stale: bool = False

    def apply(self, turn: TurnRecorder) -> None:
        """Write every derived fact onto ``turn`` (coverage, then samples, events)."""

        apply_facts(turn, self.coverage, self.measurements, self.events)


def analyze_audio_graph(events: Sequence[Mapping[str, Any]]) -> DeviceFacts:
    """Derive governed facts from ordered AudioContext/device lifecycle events.

    Each event is a ``{type, timestamp_ms, ...}`` mapping. Unknown or malformed
    events are skipped (fail-open, no raise).
    """

    normalized = _normalize_events(events)
    measurements: list[EngineMeasurement] = []
    emitted: list[EngineEvent] = []
    coverage = _CoverageLedger()
    if not normalized:
        return DeviceFacts((), (), ())

    base_ms = normalized[0][0]
    flags = {"permission_denied": False, "context_suspended": False, "route": False, "stale": False}

    for ts_ms, event_type, event in normalized:
        at_ms = max(0.0, ts_ms - base_ms)
        _dispatch(event_type, event, at_ms, measurements, emitted, coverage, flags)

    return DeviceFacts(
        measurements=tuple(measurements),
        events=tuple(emitted),
        coverage=coverage.as_tuple(),
        permission_denied=flags["permission_denied"],
        context_suspended=flags["context_suspended"],
        route_changed=flags["route"],
        stale=flags["stale"],
    )


def apply_audio_graph(turn: TurnRecorder, events: Sequence[Mapping[str, Any]]) -> DeviceFacts:
    """Derive and record audio-graph facts onto ``turn``; return the facts."""

    facts = analyze_audio_graph(events)
    facts.apply(turn)
    return facts


# -- dispatch ------------------------------------------------------------------


def _dispatch(
    event_type: str,
    event: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    emitted: list[EngineEvent],
    coverage: _CoverageLedger,
    flags: dict[str, bool],
) -> None:
    if event_type in _PERMISSION_TYPES:
        state = _lower(event.get("state"))
        if event_type == "permission_denied" or state == "denied":
            emitted.append(_device_event(_PERMISSION_DENIED, at_ms, "getUserMedia"))
            coverage.note(_MICROPHONE, "not_observed", "permission_denied")
            flags["permission_denied"] = True
        return
    if event_type in _CONTEXT_TYPES:
        if _lower(event.get("state")) in _SUSPENDED_STATES:
            emitted.append(_device_event(_CONTEXT_SUSPENDED, at_ms, "AudioContext.state"))
            flags["context_suspended"] = True
        return
    if event_type in _DEVICE_CHANGE_TYPES:
        emitted.append(_device_event(_DEVICE_ROUTE_CHANGED, at_ms, "devicechange"))
        flags["route"] = True
        return
    if event_type in _SAMPLE_RATE_TYPES:
        if _is_sample_rate_mismatch(event):
            emitted.append(_render_event(at_ms, "sampleRate"))
            flags["stale"] = True
        return
    if event_type in _UNDERRUN_TYPES:
        emitted.append(_render_event(at_ms, "audioContext.underrun"))
        flags["stale"] = True
        return
    if event_type in _LATENCY_TYPES:
        _emit_latency(event, at_ms, measurements)


def _is_sample_rate_mismatch(event: Mapping[str, Any]) -> bool:
    configured = _first_number(event, ("configured_hz", "context_hz", "expected_hz"))
    actual = _first_number(event, ("actual_hz", "track_hz", "observed_hz"))
    if configured is None or actual is None:
        # The event type already asserts a mismatch; absent operands do not
        # let us disprove it, so we honour the asserted stale-render signal.
        return True
    return configured != actual


def _emit_latency(
    event: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
) -> None:
    base = _first_number(event, ("base_latency_s", "base_latency", "baseLatency"))
    if base is not None:
        measurements.append(
            _measurement(_BASE_LATENCY, base, at_ms, "measured", "baseLatency", "web_audio")
        )
    output = _first_number(event, ("output_latency_s", "output_latency", "outputLatency"))
    if output is not None:
        # W3C: outputLatency is an estimate, so the confidence is estimated, never
        # measured, no matter how precise the reported number looks.
        measurements.append(
            _measurement(
                _OUTPUT_LATENCY, output, at_ms, "estimated", "outputLatency", "web_audio_estimate"
            )
        )


# -- fact builders -------------------------------------------------------------


def _device_event(name: str, at_ms: float, source_field: str) -> EngineEvent:
    return EngineEvent(
        name=name,
        at_ms=at_ms,
        participant="user",
        source=_SOURCE,
        confidence="measured",
        source_field=source_field,
    )


def _render_event(at_ms: float, source_field: str) -> EngineEvent:
    return EngineEvent(
        name=_RENDER_STALE,
        at_ms=at_ms,
        participant="agent",
        source=_SOURCE,
        confidence="measured",
        source_field=source_field,
    )


def _measurement(
    name: str,
    value_seconds: float,
    at_ms: float,
    confidence: str,
    source_field: str,
    basis: str,
) -> EngineMeasurement:
    return EngineMeasurement(
        name=name,
        value=value_seconds,
        unit="s",
        at_ms=at_ms,
        source=_SOURCE,
        confidence=confidence,
        quality_kind=_QUALITY_KIND,
        source_field=source_field,
        basis=basis,
    )


# -- primitives ----------------------------------------------------------------


def _normalize_events(
    events: Sequence[Mapping[str, Any]],
) -> list[tuple[float, str, Mapping[str, Any]]]:
    normalized: list[tuple[float, str, Mapping[str, Any]]] = []
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return normalized
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = _lower(event.get("type"))
        if event_type is None:
            continue
        timestamp = event.get("timestamp_ms")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            continue
        if not math.isfinite(timestamp):
            continue
        normalized.append((float(timestamp), event_type, event))
    return normalized


def _first_number(event: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if value < 0:
            continue
        return float(value)
    return None


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None
