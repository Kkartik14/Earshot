"""Deterministic WebRTC ``getStats`` delta engine.

Turns an ordered sequence of W3C ``RTCStatsReport`` snapshots into governed
earshot facts that the existing boundary-attribution analyzer already diagnoses.
Every quantity is a *delta over a consecutive pair* of snapshots (loss, jitter
buffer growth, concealment) or an instantaneous per-snapshot reading (inter-arrival
jitter, round-trip time), never an absolute cumulative counter mistaken for a rate.

The engine emits the exact governed names the T2 rules consume:

* ``packet_loss_ratio`` / ``jitter`` / ``round_trip_time`` quality measurements
  feed ``network.degraded``.
* an ICE/DTLS ``disconnected``/``failed`` -> ``connected`` recovery emits
  ``earshot.transport.reconnecting``, which fires ``transport.reconnect``.
* a selected-candidate-pair or local-candidate ``networkType`` change emits an
  ``earshot.transport.route_changed`` event.

Two W3C-mandated disciplines hold throughout: a member absent from a snapshot is
*unknown* (no measurement, never a zero), and a counter that moved backwards is a
reset -- the interval is dropped with a coverage note, never reported as negative.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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

_SOURCE = "webrtc_stats"
_QUALITY_KIND = "transport.quality"

# Measurement names. The first three are exactly what ``network.degraded`` reads;
# the last two are additional governed diagnostic scalars (no T2 rule reads them).
_PACKET_LOSS = "packet_loss_ratio"
_JITTER = "jitter"
_ROUND_TRIP_TIME = "round_trip_time"
_JITTER_BUFFER_DELAY = "jitter_buffer_delay"
_CONCEALMENT = "concealment_ratio"

# Event names the transport-boundary rules consume.
_RECONNECTING = "earshot.transport.reconnecting"
_ROUTE_CHANGED = "earshot.transport.route_changed"

# Coverage signals for dropped intervals (counter resets).
_COV_PACKET_LOSS = "webrtc.packet_loss"
_COV_JITTER_BUFFER = "webrtc.jitter_buffer"
_COV_CONCEALMENT = "webrtc.concealment"

# Normalized ICE/DTLS connection states.
_DOWN_STATES = frozenset({"disconnected", "failed", "closed"})
_UP_STATES = frozenset({"connected", "completed", "succeeded"})

_SECONDS_TO_MS = 1000.0
# Below this the two jitter-buffer averages are equal within float noise.
_GROWTH_EPSILON_MS = 1e-6


@dataclass(frozen=True, slots=True)
class WebRtcFacts:
    """Immutable, deterministic result of one ``getStats`` derivation.

    ``measurements``/``events``/``coverage`` are the governed facts. The booleans
    are convenience summaries of what the state machines observed, so a caller can
    assert on the derivation without re-scanning the recorded incident.
    """

    measurements: tuple[EngineMeasurement, ...]
    events: tuple[EngineEvent, ...]
    coverage: tuple[EngineCoverage, ...]
    jitter_buffer_growth: bool = False
    reconnected: bool = False
    route_changed: bool = False

    def apply(self, turn: TurnRecorder) -> None:
        """Write every derived fact onto ``turn`` (coverage, then samples, events)."""

        apply_facts(turn, self.coverage, self.measurements, self.events)


def analyze_webrtc_stats(snapshots: Sequence[Mapping[str, Any]]) -> WebRtcFacts:
    """Derive governed facts from ordered ``{timestamp_ms, stats}`` snapshots.

    ``stats`` maps a stat id to an ``RTCStats``-shaped dict carrying a ``type``
    (``inbound-rtp``, ``remote-inbound-rtp``, ``candidate-pair``, ``transport``,
    ``local-candidate``). Malformed snapshots are skipped (fail-open, no raise).
    """

    normalized = _normalize_snapshots(snapshots)
    measurements: list[EngineMeasurement] = []
    events: list[EngineEvent] = []
    coverage = _CoverageLedger()
    if not normalized:
        return WebRtcFacts((), (), ())

    base_ms = normalized[0][0]
    last_buffer_avg_ms: dict[str, float] = {}
    jitter_buffer_growth = False
    seen_down = False
    reconnected = False
    route_changed = False
    prev_state: str | None = None
    prev_route: tuple[str | None, str | None] | None = None
    prev_stats: Mapping[str, Mapping[str, Any]] | None = None

    for ts_ms, stats in normalized:
        at_ms = max(0.0, ts_ms - base_ms)

        # --- transport reconnect: an ICE/DTLS drop then recovery --------------
        state = _connection_state(stats)
        if state is not None:
            if state in _DOWN_STATES:
                if prev_state not in _DOWN_STATES:
                    events.append(_transport_event(_RECONNECTING, at_ms, "iceState"))
                seen_down = True
            elif state in _UP_STATES and seen_down:
                reconnected = True
                seen_down = False
            prev_state = state

        # --- route change: a new selected pair or network type ----------------
        route = _selected_route(stats)
        if prev_route is not None and _route_changed(prev_route, route):
            events.append(_transport_event(_ROUTE_CHANGED, at_ms, "selectedCandidatePairId"))
            route_changed = True
        prev_route = _merge_route(prev_route, route)

        # --- per-pair deltas + per-snapshot instants --------------------------
        if prev_stats is not None:
            grew = _emit_deltas(
                prev_stats, stats, at_ms, measurements, coverage, last_buffer_avg_ms
            )
            jitter_buffer_growth = jitter_buffer_growth or grew
        _emit_instants(stats, at_ms, measurements)
        prev_stats = stats

    return WebRtcFacts(
        measurements=tuple(measurements),
        events=tuple(events),
        coverage=coverage.as_tuple(),
        jitter_buffer_growth=jitter_buffer_growth,
        reconnected=reconnected,
        route_changed=route_changed,
    )


def apply_webrtc_stats(turn: TurnRecorder, snapshots: Sequence[Mapping[str, Any]]) -> WebRtcFacts:
    """Derive and record ``getStats`` facts onto ``turn``; return the facts."""

    facts = analyze_webrtc_stats(snapshots)
    facts.apply(turn)
    return facts


# -- delta / instant emission --------------------------------------------------


def _emit_deltas(
    prev_stats: Mapping[str, Mapping[str, Any]],
    curr_stats: Mapping[str, Mapping[str, Any]],
    at_ms: float,
    measurements: list[EngineMeasurement],
    coverage: _CoverageLedger,
    last_buffer_avg_ms: dict[str, float],
) -> bool:
    """Emit loss/jitter-buffer/concealment for each inbound audio id in both snapshots."""

    prev_audio = _audio_inbound(prev_stats)
    curr_audio = _audio_inbound(curr_stats)
    grew = False
    for stat_id in sorted(prev_audio.keys() & curr_audio.keys()):
        previous = prev_audio[stat_id]
        current = curr_audio[stat_id]
        _emit_packet_loss(previous, current, at_ms, measurements, coverage)
        grew = (
            _emit_jitter_buffer(
                stat_id, previous, current, at_ms, measurements, coverage, last_buffer_avg_ms
            )
            or grew
        )
        _emit_concealment(previous, current, at_ms, measurements, coverage)
    return grew


def _emit_packet_loss(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    coverage: _CoverageLedger,
) -> None:
    delta = _counter_delta(previous, current, "packetsReceived")
    lost = _counter_delta(previous, current, "packetsLost")
    if delta is _RESET or lost is _RESET:
        coverage.note(_COV_PACKET_LOSS, "not_observed", "counter_reset")
        return
    if delta is None or lost is None:
        return  # a missing member is unknown, never a fabricated zero
    denominator = delta + lost
    if denominator <= 0:
        return  # no packets in the interval: unknown, not a zero-loss claim
    measurements.append(
        _measurement(_PACKET_LOSS, lost / denominator, "1", at_ms, "measured", "packetsLost")
    )


def _emit_jitter_buffer(
    stat_id: str,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    coverage: _CoverageLedger,
    last_buffer_avg_ms: dict[str, float],
) -> bool:
    delay = _counter_delta(previous, current, "jitterBufferDelay")
    emitted = _counter_delta(previous, current, "jitterBufferEmittedCount")
    if delay is _RESET or emitted is _RESET:
        coverage.note(_COV_JITTER_BUFFER, "not_observed", "counter_reset")
        return False
    if delay is None or emitted is None or emitted <= 0:
        return False
    average_ms = (delay / emitted) * _SECONDS_TO_MS
    measurements.append(
        _measurement(_JITTER_BUFFER_DELAY, average_ms, "ms", at_ms, "measured", "jitterBufferDelay")
    )
    previous_avg = last_buffer_avg_ms.get(stat_id)
    grew = previous_avg is not None and average_ms > previous_avg + _GROWTH_EPSILON_MS
    last_buffer_avg_ms[stat_id] = average_ms
    return grew


def _emit_concealment(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    coverage: _CoverageLedger,
) -> None:
    concealed = _counter_delta(previous, current, "concealedSamples")
    total = _counter_delta(previous, current, "totalSamplesReceived")
    if concealed is _RESET or total is _RESET:
        coverage.note(_COV_CONCEALMENT, "not_observed", "counter_reset")
        return
    if concealed is None or total is None or total <= 0:
        return
    ratio = min(1.0, concealed / total)
    measurements.append(
        _measurement(_CONCEALMENT, ratio, "1", at_ms, "measured", "concealedSamples")
    )


def _emit_instants(
    stats: Mapping[str, Mapping[str, Any]],
    at_ms: float,
    measurements: list[EngineMeasurement],
) -> None:
    for _stat_id, stat in sorted(_audio_inbound(stats).items()):
        jitter_seconds = _number(stat, "jitter")
        if jitter_seconds is not None:
            measurements.append(
                _measurement(
                    _JITTER, jitter_seconds * _SECONDS_TO_MS, "ms", at_ms, "measured", "jitter"
                )
            )
    rtt_seconds = _round_trip_seconds(stats)
    if rtt_seconds is not None:
        measurements.append(
            _measurement(
                _ROUND_TRIP_TIME,
                rtt_seconds * _SECONDS_TO_MS,
                "ms",
                at_ms,
                "measured",
                "roundTripTime",
            )
        )


# -- transport state machines --------------------------------------------------


def _connection_state(stats: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Normalized ICE/DTLS state, or a selected candidate-pair state, or None."""

    for stat in _by_type(stats, "transport"):
        for key in ("iceState", "dtlsState", "connectionState"):
            value = _string(stat, key)
            if value is not None:
                return value.lower()
    selected = _selected_pair(stats)
    if selected is not None:
        value = _string(selected, "state")
        if value is not None:
            return value.lower()
    return None


def _selected_route(
    stats: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """Return ``(selected_candidate_pair_id, local_network_type)`` (either may be None)."""

    pair_id: str | None = None
    network_type: str | None = None
    for stat in _by_type(stats, "transport"):
        pair_id = _string(stat, "selectedCandidatePairId") or pair_id
    selected = _selected_pair(stats)
    if selected is not None:
        pair_id = pair_id or _string(selected, "id") or _string(selected, "candidatePairId")
        local_id = _string(selected, "localCandidateId")
        if local_id is not None:
            local = stats.get(local_id)
            if isinstance(local, Mapping):
                network_type = _string(local, "networkType")
    if network_type is None:
        for stat in _by_type(stats, "local-candidate"):
            network_type = _string(stat, "networkType") or network_type
    return pair_id, network_type


def _selected_pair(stats: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The nominated/selected candidate pair, chosen deterministically."""

    pairs = sorted(
        ((stat_id, stat) for stat_id, stat in stats.items() if _type(stat) == "candidate-pair"),
        key=lambda item: item[0],
    )
    for _stat_id, stat in pairs:
        if _bool(stat, "selected") or _bool(stat, "nominated"):
            return stat
    return pairs[0][1] if pairs else None


def _route_changed(
    previous: tuple[str | None, str | None],
    current: tuple[str | None, str | None],
) -> bool:
    prev_pair, prev_net = previous
    curr_pair, curr_net = current
    pair_changed = prev_pair is not None and curr_pair is not None and prev_pair != curr_pair
    net_changed = prev_net is not None and curr_net is not None and prev_net != curr_net
    return pair_changed or net_changed


def _merge_route(
    previous: tuple[str | None, str | None] | None,
    current: tuple[str | None, str | None],
) -> tuple[str | None, str | None]:
    """Carry forward last-known route components so a transient gap is not a change."""

    if previous is None:
        return current
    prev_pair, prev_net = previous
    curr_pair, curr_net = current
    return (curr_pair if curr_pair is not None else prev_pair, curr_net or prev_net)


def _round_trip_seconds(stats: Mapping[str, Mapping[str, Any]]) -> float | None:
    for stat in _by_type(stats, "remote-inbound-rtp"):
        value = _number(stat, "roundTripTime")
        if value is not None:
            return value
    selected = _selected_pair(stats)
    if selected is not None:
        for key in ("currentRoundTripTime", "roundTripTime"):
            value = _number(selected, key)
            if value is not None:
                return value
    return None


# -- primitives ----------------------------------------------------------------


class _Reset:
    """Sentinel: a cumulative counter that moved backwards (a reset)."""


_RESET = _Reset()


def _counter_delta(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    key: str,
) -> float | None | _Reset:
    """Delta of a cumulative counter: None if a member is absent, _RESET if it fell."""

    before = _number(previous, key)
    after = _number(current, key)
    if before is None or after is None:
        return None
    if after < before:
        return _RESET
    return after - before


def _measurement(
    name: str,
    value: float,
    unit: str,
    at_ms: float,
    confidence: str,
    source_field: str,
) -> EngineMeasurement:
    return EngineMeasurement(
        name=name,
        value=value,
        unit=unit,
        at_ms=at_ms,
        source=_SOURCE,
        confidence=confidence,
        quality_kind=_QUALITY_KIND,
        source_field=source_field,
        basis="webrtc_stats_delta",
    )


def _transport_event(name: str, at_ms: float, source_field: str) -> EngineEvent:
    return EngineEvent(
        name=name,
        at_ms=at_ms,
        participant="user",
        source=_SOURCE,
        confidence="measured",
        source_field=source_field,
    )


def _normalize_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
) -> list[tuple[float, Mapping[str, Mapping[str, Any]]]]:
    """Validate and order snapshots, dropping malformed entries (fail-open)."""

    normalized: list[tuple[float, Mapping[str, Mapping[str, Any]]]] = []
    if not isinstance(snapshots, Sequence) or isinstance(snapshots, (str, bytes)):
        return normalized
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        timestamp = snapshot.get("timestamp_ms")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            continue
        if not math.isfinite(timestamp):
            continue
        stats = snapshot.get("stats")
        if not isinstance(stats, Mapping):
            continue
        clean = {
            stat_id: stat
            for stat_id, stat in stats.items()
            if isinstance(stat_id, str) and isinstance(stat, Mapping)
        }
        normalized.append((float(timestamp), clean))
    return normalized


def _audio_inbound(
    stats: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Inbound-rtp stats for the audio stream (or, absent kind hints, all inbound)."""

    result: dict[str, Mapping[str, Any]] = {}
    for stat_id, stat in stats.items():
        if _type(stat) != "inbound-rtp":
            continue
        kind = _string(stat, "kind") or _string(stat, "mediaType")
        if kind is None or kind == "audio":
            result[stat_id] = stat
    return result


def _by_type(stats: Mapping[str, Mapping[str, Any]], stat_type: str) -> list[Mapping[str, Any]]:
    return [stat for _stat_id, stat in sorted(stats.items()) if _type(stat) == stat_type]


def _type(stat: Mapping[str, Any]) -> str | None:
    return _string(stat, "type")


def _number(stat: Mapping[str, Any], key: str) -> float | None:
    """A finite numeric member, or None when absent -- a missing member is not zero."""

    value = stat.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def _string(stat: Mapping[str, Any], key: str) -> str | None:
    value = stat.get(key)
    return value if isinstance(value, str) and value else None


def _bool(stat: Mapping[str, Any], key: str) -> bool | None:
    value = stat.get(key)
    return value if isinstance(value, bool) else None
