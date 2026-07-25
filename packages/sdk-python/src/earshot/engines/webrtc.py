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
* the W3C ``media-playout`` (``RTCAudioPlayoutStats``) counters become
  ``playout_delay`` and ``synthesized_samples_ratio``; synthesized playout
  samples -- audio the output device had to invent because the render queue ran
  dry -- emit ``earshot.audio.render.stale``, which fires ``audio.stale_playback``.
  This is the render half of capture-to-render: it is measured at the playout
  device, not inferred from a transport counter.

Three disciplines hold throughout: a member absent from a snapshot is *unknown*
(no measurement, never a zero); a counter that moved backwards is a reset -- the
interval is dropped with a coverage note, never reported as negative; and a
snapshot that precedes its predecessor makes that step unobserved -- the interval
is dropped the same way, because a delta across it would be differenced over a
negative span. Every fact keeps the raw timestamp of the observation it came
from, so an out-of-order batch is never flattened onto one coordinate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..observation import ObservationSink
from .base import (
    BrowserClockDomain,
    EngineCoverage,
    EngineEvent,
    EngineMeasurement,
    _AppliedClock,
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
# Render-path scalars. ``processing_delay`` is the W3C ``totalProcessingDelay``
# averaged over the samples the jitter buffer emitted in the interval -- the
# packet-received-to-decoded time. (The per-frame ``totalDecodeTime`` counter is
# video-only in webrtc-stats, so audio decode time alone is never claimed here.)
_PROCESSING_DELAY = "processing_delay"
_PLAYOUT_DELAY = "playout_delay"
_SYNTHESIZED_RATIO = "synthesized_samples_ratio"

# Event names the transport-boundary rules consume.
_RECONNECTING = "earshot.transport.reconnecting"
_ROUTE_CHANGED = "earshot.transport.route_changed"
# The same governed stale-render event the device engine emits (see
# ``engines/device.py``); ``audio.stale_playback`` reads it by name.
_RENDER_STALE = "earshot.audio.render.stale"

# Coverage signals for dropped intervals (counter resets, and a batch whose
# readings move backwards -- an interval that cannot be differenced honestly).
_COV_SNAPSHOT_ORDER = "webrtc.snapshot_order"
_COV_PACKET_LOSS = "webrtc.packet_loss"
_COV_JITTER_BUFFER = "webrtc.jitter_buffer"
_COV_CONCEALMENT = "webrtc.concealment"
_COV_PROCESSING = "webrtc.processing_delay"
_COV_PLAYOUT = "webrtc.playout"

# Normalized ICE/DTLS connection states.
_DOWN_STATES = frozenset({"disconnected", "failed", "closed"})
_UP_STATES = frozenset({"connected", "completed", "succeeded"})

_SECONDS_TO_MS = 1000.0
# Below this the two jitter-buffer averages are equal within float noise.
_GROWTH_EPSILON_MS = 1e-6


@dataclass(frozen=True, slots=True)
class WebRtcCarry:
    """The cross-segment delta state that resumes :func:`analyze_webrtc_stats`.

    The engine is a *stateful delta* over consecutive snapshot pairs: loss, jitter
    buffer growth, concealment, processing and playout are each a difference
    between two genuinely observed snapshots, and reconnect / route detection are
    small state machines. When one continuous call is captured as several
    separately-drained batches, analysing each batch from a clean slate silently
    drops the interval that *spans* the boundary -- the loss / jitter /
    concealment / playout evidence between the last snapshot of one batch and the
    first of the next -- and a ``disconnected -> connected`` transition whose
    endpoints fall in different batches is never reported as a reconnect (the down
    state is forgotten at the boundary). This value is the exact, bounded state
    needed to *resume* the delta across a boundary so that folding a series
    through the carry in N pieces yields facts byte-identical to one single-shot
    call.

    It is a plain, serializable data value -- one snapshot plus a handful of
    scalars, bounded by construction and never the whole series. Each field is the
    last-observed value the next batch's first step reads:

    * ``prev_stats`` -- the previous snapshot's sanitized stats: the cumulative
      counters the loss / jitter-buffer / concealment / processing / playout
      deltas difference the next snapshot against. Without it the boundary
      interval is lost, which is the evidence-loss bug this carry fixes.
    * ``prev_state`` -- the last ICE / DTLS / candidate-pair connection state, so
      a ``down`` already seen is not re-reported as a fresh reconnect and an ``up``
      in the next batch closes the same one.
    * ``seen_down`` -- whether a ``down`` state is still outstanding (a recovery is
      pending). This is what makes a boundary-spanning reconnect observable at all.
    * ``prev_route`` -- the last-known ``(selected pair id, network type)`` so a
      route change straddling a boundary is neither missed nor fabricated.
    * ``last_buffer_avg_ms`` -- the per-stat jitter-buffer average, because
      *growth* is a trend comparison between consecutive intervals, not a level.
    * ``prev_ts_ms`` -- the last snapshot's raw timestamp, so the ordering check
      that refuses to difference a non-monotonic step still holds across the
      boundary instead of trusting the next batch's first snapshot blindly.
    * ``origin_ms`` -- the call's fixed time origin (the first batch's earliest
      reading). Every fact is stamped so ``origin_ms + at_ms`` reconstructs its raw
      browser timestamp; carrying the origin keeps a resumed batch's coordinates
      identical to the single-shot run instead of restarting the timeline at the
      batch's own first reading. Fold-equivalence is exact when the first batch
      carries the call's minimum timestamp (the real-world, time-ordered drain
      case); a later batch that arrives with an earlier reading than the fixed
      origin is still handled correctly within its own call but cannot move an
      origin the earlier batches already stamped against.
    """

    prev_stats: Mapping[str, Mapping[str, Any]] | None = None
    prev_state: str | None = None
    seen_down: bool = False
    prev_route: tuple[str | None, str | None] | None = None
    last_buffer_avg_ms: Mapping[str, float] = field(default_factory=dict)
    prev_ts_ms: float | None = None
    origin_ms: float | None = None


@dataclass(frozen=True, slots=True)
class WebRtcFacts:
    """Immutable, deterministic result of one ``getStats`` derivation.

    ``measurements``/``events``/``coverage`` are the governed facts. The booleans
    are convenience summaries of what the state machines observed, so a caller can
    assert on the derivation without re-scanning the recorded incident. ``carry``
    is the outbound cross-segment state (see :class:`WebRtcCarry`): thread it into
    the next :func:`analyze_webrtc_stats` call to resume a call captured as
    several drains. It is ``None`` only for an empty single-shot derivation.
    """

    measurements: tuple[EngineMeasurement, ...]
    events: tuple[EngineEvent, ...]
    coverage: tuple[EngineCoverage, ...]
    jitter_buffer_growth: bool = False
    reconnected: bool = False
    route_changed: bool = False
    clock: _AppliedClock | None = None
    carry: WebRtcCarry | None = None

    def apply(self, sink: ObservationSink) -> None:
        """Write every derived fact onto ``sink`` (coverage, then samples, events).

        When a browser clock domain was supplied to :func:`analyze_webrtc_stats`,
        the samples/events land in that domain at their raw browser timestamps.
        """

        apply_facts(sink, self.coverage, self.measurements, self.events, self.clock)


def analyze_webrtc_stats(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    clock_domain: BrowserClockDomain | None = None,
    carry: WebRtcCarry | None = None,
) -> WebRtcFacts:
    """Derive governed facts from ordered ``{timestamp_ms, stats}`` snapshots.

    ``stats`` maps a stat id to an ``RTCStats``-shaped dict carrying a ``type``
    (``inbound-rtp``, ``remote-inbound-rtp``, ``candidate-pair``, ``transport``,
    ``local-candidate``). Malformed snapshots are skipped (fail-open, no raise).

    ``clock_domain`` declares the browser clock the ``timestamp_ms`` values belong
    to. When supplied, applying the facts places them in that clock domain at their
    RAW browser timestamps (this batch's own origin is preserved), never rebased
    onto the server clock -- so the analyzer keeps browser and server time in
    separate domains and refuses cross-clock latency absent a ClockRelation.

    ``carry`` resumes the delta engine from a previous segment of the SAME call
    (see :class:`WebRtcCarry`). With no carry this is a single-shot derivation,
    byte-for-byte identical to calling with the whole series at once. With a carry
    the interval spanning the segment boundary is differenced exactly once -- at
    the later snapshot's raw coordinate -- so a call captured as N drains yields
    the same facts as one call. The outbound carry rides back on
    :attr:`WebRtcFacts.carry`.
    """

    normalized = _normalize_snapshots(snapshots)
    measurements: list[EngineMeasurement] = []
    events: list[EngineEvent] = []
    coverage = _CoverageLedger()
    if not normalized:
        # An empty (or fully malformed) segment derives nothing and leaves the
        # delta state untouched: hand the inbound carry straight back so folding
        # an empty piece is a genuine no-op. With no inbound carry this is the
        # historical empty result (``carry`` stays ``None``), so single-shot
        # behaviour is unchanged.
        return WebRtcFacts((), (), (), carry=carry)

    # Resume from a prior segment's carry when one was supplied. ``origin_ms`` is
    # the call's FIXED origin (the first batch's earliest reading), carried so a
    # resumed batch stamps facts at the same coordinates the single-shot run
    # would; ``prev_*`` / ``seen_down`` / ``last_buffer_avg_ms`` seed the delta and
    # the reconnect/route state machines so the boundary interval is differenced
    # rather than dropped. Absent a carry the batch's own origin is its EARLIEST
    # reading, so ``base_ms + at_ms`` stays the raw browser timestamp of every
    # observation even when the batch arrived out of order.
    resuming = carry is not None and carry.prev_stats is not None
    if resuming and carry.origin_ms is not None:
        base_ms = carry.origin_ms
    else:
        base_ms = min(ts_ms for ts_ms, _stats in normalized)
    last_buffer_avg_ms: dict[str, float] = dict(carry.last_buffer_avg_ms) if resuming else {}
    jitter_buffer_growth = False
    seen_down = carry.seen_down if resuming else False
    reconnected = False
    route_changed = False
    prev_ts_ms: float | None = carry.prev_ts_ms if resuming else None
    prev_state: str | None = carry.prev_state if resuming else None
    prev_route: tuple[str | None, str | None] | None = carry.prev_route if resuming else None
    prev_stats: Mapping[str, Mapping[str, Any]] | None = carry.prev_stats if resuming else None

    for ts_ms, stats in normalized:
        at_ms = ts_ms - base_ms
        # A reading that precedes its predecessor makes the STEP unobserved: a
        # delta across it would be differenced backwards, and a transition across
        # it would assert an order the batch does not establish. Same discipline
        # as a counter that moved backwards -- drop the interval, record the gap,
        # and carry on from this snapshot. Per-snapshot instants are unaffected:
        # an instantaneous reading needs no predecessor.
        ordered = prev_ts_ms is None or ts_ms >= prev_ts_ms
        if not ordered:
            coverage.note(_COV_SNAPSHOT_ORDER, "not_observed", "non_monotonic_snapshot")
            # Jitter-buffer *growth* is a comparison between consecutive intervals,
            # so it cannot reach across the discontinuity either. Forgetting the
            # last average makes the next interval report a level, not a trend.
            last_buffer_avg_ms.clear()

        # --- transport reconnect: an ICE/DTLS drop then recovery --------------
        state = _connection_state(stats)
        if state is not None:
            if not ordered:
                seen_down = state in _DOWN_STATES
            elif state in _DOWN_STATES:
                if prev_state not in _DOWN_STATES:
                    events.append(_transport_event(_RECONNECTING, at_ms, "iceState"))
                seen_down = True
            elif state in _UP_STATES and seen_down:
                reconnected = True
                seen_down = False
            prev_state = state

        # --- route change: a new selected pair or network type ----------------
        route = _selected_route(stats)
        if ordered:
            if prev_route is not None and _route_changed(prev_route, route):
                events.append(_transport_event(_ROUTE_CHANGED, at_ms, "selectedCandidatePairId"))
                route_changed = True
            prev_route = _merge_route(prev_route, route)
        else:
            prev_route = route

        # --- per-pair deltas + per-snapshot instants --------------------------
        if ordered and prev_stats is not None:
            grew = _emit_deltas(
                prev_stats, stats, at_ms, measurements, events, coverage, last_buffer_avg_ms
            )
            jitter_buffer_growth = jitter_buffer_growth or grew
        _emit_instants(stats, at_ms, measurements)
        prev_stats = stats
        prev_ts_ms = ts_ms

    return WebRtcFacts(
        measurements=tuple(measurements),
        events=tuple(events),
        coverage=coverage.as_tuple(),
        jitter_buffer_growth=jitter_buffer_growth,
        reconnected=reconnected,
        route_changed=route_changed,
        clock=None if clock_domain is None else _AppliedClock(clock_domain, base_ms),
        # The outbound carry captures exactly the state the next segment's first
        # step reads. ``origin_ms`` is the fixed call origin (unchanged when
        # resuming); ``last_buffer_avg_ms`` is copied so the carry never aliases
        # the loop's mutable working dict.
        carry=WebRtcCarry(
            prev_stats=prev_stats,
            prev_state=prev_state,
            seen_down=seen_down,
            prev_route=prev_route,
            last_buffer_avg_ms=dict(last_buffer_avg_ms),
            prev_ts_ms=prev_ts_ms,
            origin_ms=base_ms,
        ),
    )


def apply_webrtc_stats(
    sink: ObservationSink,
    snapshots: Sequence[Mapping[str, Any]],
    *,
    clock_domain: BrowserClockDomain | None = None,
    carry: WebRtcCarry | None = None,
) -> WebRtcFacts:
    """Derive and record ``getStats`` facts onto ``sink``; return the facts.

    ``carry`` resumes a call captured as several drains; the outbound carry rides
    back on the returned :attr:`WebRtcFacts.carry` so the caller can thread it into
    the next segment. Nothing here changes for a single-shot call (no carry in,
    the returned carry is simply ignored).
    """

    facts = analyze_webrtc_stats(snapshots, clock_domain=clock_domain, carry=carry)
    facts.apply(sink)
    return facts


# -- delta / instant emission --------------------------------------------------


def _emit_deltas(
    prev_stats: Mapping[str, Mapping[str, Any]],
    curr_stats: Mapping[str, Mapping[str, Any]],
    at_ms: float,
    measurements: list[EngineMeasurement],
    events: list[EngineEvent],
    coverage: _CoverageLedger,
    last_buffer_avg_ms: dict[str, float],
) -> bool:
    """Emit loss/jitter-buffer/concealment/playout deltas for ids in both snapshots."""

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
        _emit_processing_delay(previous, current, at_ms, measurements, coverage)
    prev_playout = _audio_playout(prev_stats)
    curr_playout = _audio_playout(curr_stats)
    for stat_id in sorted(prev_playout.keys() & curr_playout.keys()):
        _emit_playout(
            prev_playout[stat_id], curr_playout[stat_id], at_ms, measurements, events, coverage
        )
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


def _emit_processing_delay(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    coverage: _CoverageLedger,
) -> None:
    """Average packet-received-to-decoded delay over the interval's emitted samples.

    ``totalProcessingDelay`` is a cumulative sum of per-sample delays, so the only
    honest scalar is (delta of the sum) / (delta of the samples it accumulated
    over). ``jitterBufferEmittedCount`` is that sample count for audio -- the same
    denominator the jitter-buffer average already uses.
    """

    processing = _counter_delta(previous, current, "totalProcessingDelay")
    emitted = _counter_delta(previous, current, "jitterBufferEmittedCount")
    if processing is _RESET or emitted is _RESET:
        coverage.note(_COV_PROCESSING, "not_observed", "counter_reset")
        return
    if processing is None or emitted is None or emitted <= 0:
        return  # a missing member is unknown, never a fabricated zero
    measurements.append(
        _measurement(
            _PROCESSING_DELAY,
            (processing / emitted) * _SECONDS_TO_MS,
            "ms",
            at_ms,
            "measured",
            "totalProcessingDelay",
        )
    )


def _emit_playout(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    at_ms: float,
    measurements: list[EngineMeasurement],
    events: list[EngineEvent],
    coverage: _CoverageLedger,
) -> None:
    """Derive playout delay and synthesized-sample facts from ``RTCAudioPlayoutStats``.

    ``totalPlayoutDelay`` accumulates one delay per played-out sample, so the
    interval average is (delta of the sum) / (delta of ``totalSamplesCount``).
    ``synthesizedSamplesDuration`` grows only when the playout device had to
    invent audio because the render queue ran dry -- an OBSERVED render underrun,
    so it emits the same governed stale-render event a device-reported under-run
    does. Absent members stay unknown; a counter that fell is coverage, not a
    negative delta.
    """

    delay = _counter_delta(previous, current, "totalPlayoutDelay")
    samples = _counter_delta(previous, current, "totalSamplesCount")
    if delay is _RESET or samples is _RESET:
        coverage.note(_COV_PLAYOUT, "not_observed", "counter_reset")
    elif delay is not None and samples is not None and samples > 0:
        measurements.append(
            _measurement(
                _PLAYOUT_DELAY,
                (delay / samples) * _SECONDS_TO_MS,
                "ms",
                at_ms,
                "measured",
                "totalPlayoutDelay",
            )
        )

    synthesized = _counter_delta(previous, current, "synthesizedSamplesDuration")
    total = _counter_delta(previous, current, "totalSamplesDuration")
    if synthesized is _RESET or total is _RESET:
        coverage.note(_COV_PLAYOUT, "not_observed", "counter_reset")
        return
    if synthesized is None or total is None or total <= 0:
        return
    measurements.append(
        _measurement(
            _SYNTHESIZED_RATIO,
            min(1.0, synthesized / total),
            "1",
            at_ms,
            "measured",
            "synthesizedSamplesDuration",
        )
    )
    if synthesized > 0:
        events.append(
            EngineEvent(
                name=_RENDER_STALE,
                at_ms=at_ms,
                # The synthesized audio stands in for the AGENT's rendered speech.
                participant="agent",
                source=_SOURCE,
                confidence="measured",
                source_field="synthesizedSamplesDuration",
            )
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
    """Return ``(selected_candidate_pair_id, local_network_type)`` for the ACTIVE pair.

    The network type is read ONLY from the selected pair's own local candidate --
    never from an arbitrary local-candidate stat -- so an unrelated candidate
    appearing or changing its ``networkType`` is not misread as a route change.
    When no pair is actually selected, the route's network type is unknown.
    """

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
    return pair_id, network_type


def _selected_pair(stats: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The transport's SELECTED candidate pair, resolved honestly (no arbitrary guess).

    Resolution order:
      1. the pair the transport names via ``selectedCandidatePairId``;
      2. failing that, a pair that marks itself ``selected``/``nominated``
         (deterministic by stat id).
    When neither exists the active pair is *unknown* and this returns ``None`` --
    it never falls back to an arbitrary pair, which would attribute a route/RTT to
    a candidate the ICE agent never selected.
    """

    selected_id: str | None = None
    for stat in _by_type(stats, "transport"):
        selected_id = _string(stat, "selectedCandidatePairId") or selected_id
    if selected_id is not None:
        pair = stats.get(selected_id)
        if isinstance(pair, Mapping) and _type(pair) == "candidate-pair":
            return pair
    pairs = sorted(
        ((stat_id, stat) for stat_id, stat in stats.items() if _type(stat) == "candidate-pair"),
        key=lambda item: item[0],
    )
    for _stat_id, stat in pairs:
        if _bool(stat, "selected") or _bool(stat, "nominated"):
            return stat
    return None


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
) -> float | _Reset | None:
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


def _audio_playout(
    stats: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """``media-playout`` (``RTCAudioPlayoutStats``) entries for the audio sink.

    ``kind`` is ``audio`` for every playout stat the spec defines; an entry that
    names some other kind is a future/foreign stat and is left alone rather than
    guessed at.
    """

    result: dict[str, Mapping[str, Any]] = {}
    for stat_id, stat in stats.items():
        if _type(stat) != "media-playout":
            continue
        kind = _string(stat, "kind")
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
