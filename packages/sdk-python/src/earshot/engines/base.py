"""Shared, deterministic fact primitives for server-side diagnostic engines.

A diagnostic engine turns raw browser telemetry (W3C ``RTCStatsReport``
snapshots, Web Audio / device-lifecycle events) into *governed facts*: quality
measurements, point events, and coverage notes. The engine itself never touches
a recorder -- it returns an immutable ``EngineFacts`` value that a caller applies
to a :class:`~earshot.pipeline.TurnRecorder`. This keeps the derivation pure and
byte-for-byte deterministic (and therefore trivially testable without a browser),
while reusing the exact same authoring seams the raw provider adapters use.

Two disciplines are load-bearing across every engine and are enforced here by
construction rather than convention:

* **Missing member is not zero.** A stat or field absent from a snapshot yields
  no measurement for that interval. The engine says *unknown* (optionally via an
  :class:`EngineCoverage` note); it never emits a fabricated ``0``.
* **A non-monotonic counter is dropped, not negated.** A cumulative counter that
  moved backwards (a reset) produces no negative delta -- the interval is dropped
  and recorded as coverage.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..contract import ClockDomain
from ..pipeline import TurnRecorder

# Default reading uncertainty (1ms) for a browser monotonic clock: browsers
# coarsen ``performance.now()``, so browser-derived facts carry this forward as
# their ``uncertainty_nano`` rather than pretending the readings are exact.
_DEFAULT_BROWSER_UNCERTAINTY_NANO = 1_000_000


@dataclass(frozen=True, slots=True)
class BrowserClockDomain:
    """The browser's own clock, declared as a distinct clock domain.

    Browser-derived facts are recorded in THIS domain with their raw browser
    timestamps as ``monotonic_time_nano`` -- never rebased onto the server clock.
    Because no calibration (:class:`~earshot.contract.ClockRelation`) to the
    server clock exists by default, the analyzer then honestly refuses cross-clock
    latency instead of presenting false comparability. ``clock_domain_id`` is the
    stable opaque id the browser recorder minted for its session, so batches
    drained separately share one continuous browser timeline.

    ``wall_origin_unix_nano`` is the Unix-epoch wall time the monotonic origin
    corresponds to (the browser's ``performance.timeOrigin``). When known, each
    fact ALSO carries a browser-wall ``source_time_unix_nano`` -- the only thing a
    declared ``ClockRelation`` can align to the server clock. The monotonic value
    stays domain-local (never aligned across domains); absent a relation the wall
    value is still in a foreign domain, so cross-clock latency stays unavailable.
    """

    clock_domain_id: str
    kind: str = "browser_monotonic"
    observer: str = "browser"
    uncertainty_nano: int = _DEFAULT_BROWSER_UNCERTAINTY_NANO
    wall_origin_unix_nano: int | None = None

    def to_contract(self) -> ClockDomain:
        """The governed :class:`ClockDomain` to declare in the incident profile."""

        return ClockDomain(
            clock_domain_id=self.clock_domain_id,
            kind=self.kind,
            observer=self.observer,
            monotonic_origin_nano="0",
            wall_origin_unix_nano=(
                None if self.wall_origin_unix_nano is None else str(int(self.wall_origin_unix_nano))
            ),
            uncertainty_nano=str(int(self.uncertainty_nano)),
            synchronization_method="browser_uncalibrated",
        )


@dataclass(frozen=True, slots=True)
class _AppliedClock:
    """Binds a browser clock domain to one batch's origin (its first raw timestamp).

    A fact's turn-relative ``at_ms`` plus ``origin_ms`` reconstructs the RAW
    browser timestamp of the observation (``origin_ms + at_ms == ts_ms``), which
    is recorded as ``monotonic_time_nano`` in :attr:`domain`. Preserving each
    batch's own origin is what stops periodic drains from restarting the timeline
    and stops two batches with different origins from colliding at offset zero.
    """

    domain: BrowserClockDomain
    origin_ms: float


@dataclass(frozen=True, slots=True)
class EngineMeasurement:
    """A governed scalar derived from raw telemetry, ready for the recorder.

    ``value`` is already in ``unit`` (an interval ratio in ``1``, a latency in
    ``ms`` or native ``s``); the engine performed any unit conversion so the fact
    is faithful and self-describing. ``at_ms`` is a non-negative turn-relative
    offset. ``source`` is the open evidence source (``webrtc_stats``, ``app``);
    ``confidence`` is ``measured``/``estimated``/``inferred``.
    """

    name: str
    value: float
    unit: str
    at_ms: float
    source: str
    confidence: str
    quality_kind: str
    source_field: str
    basis: str | None = None

    def apply(self, turn: TurnRecorder, clock: _AppliedClock | None = None) -> None:
        turn.record_measurement(
            self.name,
            self.value,
            unit=self.unit,
            source=self.source,
            confidence=self.confidence,
            source_field=self.source_field,
            basis=self.basis,
            at_ms=self.at_ms,
            quality_kind=self.quality_kind,
            browser_clock_domain_id=None if clock is None else clock.domain.clock_domain_id,
            browser_monotonic_ms=None if clock is None else clock.origin_ms + self.at_ms,
            browser_uncertainty_nano=None if clock is None else clock.domain.uncertainty_nano,
            browser_wall_origin_nano=None if clock is None else clock.domain.wall_origin_unix_nano,
        )


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """A governed point event (transport/device/render boundary) at ``at_ms``."""

    name: str
    at_ms: float
    participant: str | None
    source: str
    confidence: str
    source_field: str

    def apply(self, turn: TurnRecorder, clock: _AppliedClock | None = None) -> None:
        turn.record_event(
            self.name,
            at_ms=self.at_ms,
            participant=self.participant,
            source=self.source,
            confidence=self.confidence,
            source_field=self.source_field,
            browser_clock_domain_id=None if clock is None else clock.domain.clock_domain_id,
            browser_monotonic_ms=None if clock is None else clock.origin_ms + self.at_ms,
            browser_uncertainty_nano=None if clock is None else clock.domain.uncertainty_nano,
            browser_wall_origin_nano=None if clock is None else clock.domain.wall_origin_unix_nano,
        )


@dataclass(frozen=True, slots=True)
class EngineCoverage:
    """An explicit *unknown*: a signal the engine could not derive for a window."""

    signal: str
    availability: str
    reason: str

    def apply(self, turn: TurnRecorder) -> None:
        turn.record_coverage(self.signal, self.availability, self.reason)


def apply_facts(
    turn: TurnRecorder,
    coverage: Iterable[EngineCoverage],
    measurements: Iterable[EngineMeasurement],
    events: Iterable[EngineEvent],
    clock: _AppliedClock | None = None,
) -> None:
    """Write coverage, then measurements, then events, in a stable order.

    When ``clock`` is supplied the measurements/events are placed in the browser
    clock domain (declared once here) at their raw browser timestamps, rather than
    on the server clock. Coverage notes are session-scoped and carry no timestamp.
    """

    if clock is not None:
        turn.register_clock_domain(clock.domain.to_contract())
    for note in coverage:
        note.apply(turn)
    for measurement in measurements:
        measurement.apply(turn, clock)
    for event in events:
        event.apply(turn, clock)


@dataclass(frozen=True, slots=True)
class _CoverageLedger:
    """De-duplicating coverage accumulator (one note per signal, first-wins)."""

    _notes: dict[str, EngineCoverage] = field(default_factory=dict)

    def note(self, signal: str, availability: str, reason: str) -> None:
        self._notes.setdefault(signal, EngineCoverage(signal, availability, reason))

    def as_tuple(self) -> tuple[EngineCoverage, ...]:
        return tuple(self._notes[signal] for signal in sorted(self._notes))
