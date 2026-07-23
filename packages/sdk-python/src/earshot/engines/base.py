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

from ..pipeline import TurnRecorder


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

    def apply(self, turn: TurnRecorder) -> None:
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

    def apply(self, turn: TurnRecorder) -> None:
        turn.record_event(
            self.name,
            at_ms=self.at_ms,
            participant=self.participant,
            source=self.source,
            confidence=self.confidence,
            source_field=self.source_field,
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
) -> None:
    """Write coverage, then measurements, then events, in a stable order."""

    for note in coverage:
        note.apply(turn)
    for measurement in measurements:
        measurement.apply(turn)
    for event in events:
        event.apply(turn)


@dataclass(frozen=True, slots=True)
class _CoverageLedger:
    """De-duplicating coverage accumulator (one note per signal, first-wins)."""

    _notes: dict[str, EngineCoverage] = field(default_factory=dict)

    def note(self, signal: str, availability: str, reason: str) -> None:
        self._notes.setdefault(signal, EngineCoverage(signal, availability, reason))

    def as_tuple(self) -> tuple[EngineCoverage, ...]:
        return tuple(self._notes[signal] for signal in sorted(self._notes))
