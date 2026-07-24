"""The authoring seam every capture source writes governed facts through.

A *capture source* is anything that observed a voice session and can say so: the
server pipeline itself, a provider stream adapter, a deterministic diagnostic
engine over browser telemetry, and -- in future -- a browser, native, or backend
collector. All of them need exactly one thing from earshot: somewhere to author
governed facts. :class:`ObservationSink` is that somewhere, and it is deliberately
the *only* thing a capture source is allowed to know about the recorder.

Depending on the concrete :class:`~earshot.pipeline.TurnRecorder` instead would
couple every capture source to the server pipeline's turn bookkeeping (turn ids,
stage cursors, operation minting, session lifecycle) -- none of which a browser or
native collector has or needs. A collector that can only produce measurements,
events, operations it observed, coverage and omissions should be expressible
without dragging a pipeline session behind it, and should be testable by handing
the engine a sink that just appends to a list.

``TurnRecorder`` satisfies this protocol structurally: it is *not* required to
inherit from it, and nothing here is enforced at runtime. That is intentional --
the protocol is a description of an existing seam, not a new base class, so it can
be introduced without changing a single recorded fact.

The verbs are the governed fact kinds, and only those:

* :meth:`record_measurement` -- a scalar with a unit, an evidence source, and a
  confidence. The one way a number enters an incident.
* :meth:`record_event` -- a point observation on a boundary (transport, device,
  render, speech). The one way an instant enters an incident.
* :meth:`record_operation` -- an operation the source *observed*, under an id it
  supplies. The one way a stage/span enters an incident from a source that already
  identified it (see below).
* :meth:`record_coverage` -- an explicit *unknown*. A source that could not observe
  a signal says so here rather than emitting a fabricated zero.
* :meth:`record_omission` -- the privacy ledger. A source that saw a value but
  deliberately discarded it (transcript text, audio payload) records the discard
  without retaining the value.
* :meth:`register_clock_domain` -- a source whose timestamps are not on the server
  clock declares its own domain, so its facts stay honestly incomparable until a
  calibration exists.

A foreign clock reading, once, everywhere. A source in a non-server clock domain
attaches a single :class:`SourceClockReading` to any timed verb -- there are no
capture-source-specific keyword clumps on the universal verbs. The reading is
source-agnostic: it carries the domain, the raw monotonic value, and its
uncertainty/wall origin, and nothing about *what kind* of source produced it.

*Observing* an operation vs. *minting* one. ``record_operation`` records an
operation the source already identified: it takes the operation id the source
chose (or derived deterministically) and never advances a turn cursor. That is a
real observation and belongs on the seam. What stays off the seam is the
pipeline's ``record_stage``, which *mints* an id from the turn cursor and advances
it -- that is turn bookkeeping a fact-only collector neither has nor should be
forced to model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .contract import ClockDomain
from .privacy import CaptureClass


@dataclass(frozen=True, slots=True)
class SourceClockReading:
    """A capture source's own-clock reading for one observed fact.

    A capture source whose timestamps are not on the server clock -- a browser, a
    native collector, a backend that timed the work itself -- attaches this to a
    timed verb so its fact stays in the clock domain it declared, at the RAW
    monotonic value it observed, and is never rebased onto the server clock. It is
    the single value object that replaced a clump of ``browser_*`` keyword
    parameters on the authoring verbs: it names no capture source, so any source in
    a foreign clock domain supplies it the same way.

    * ``clock_domain_id`` -- the domain the reading belongs to. It must have been
      declared first via :meth:`ObservationSink.register_clock_domain`, so no fact
      ever references an undeclared clock.
    * ``monotonic_ms`` -- the raw, domain-local monotonic reading in milliseconds.
      It is never aligned across domains; it is stored as the fact's
      ``monotonic_time_nano`` verbatim.
    * ``uncertainty_nano`` -- the reading's own uncertainty (browsers coarsen
      ``performance.now``), or ``None`` to claim none.
    * ``wall_origin_nano`` -- the Unix-epoch wall time the monotonic origin maps to
      (a browser's ``performance.timeOrigin``), when known. It is the sole
      component a declared ``ClockRelation`` can later align to the server clock;
      absent a relation the derived wall value is still in the foreign domain, so
      cross-clock latency stays honestly unavailable.
    """

    clock_domain_id: str
    monotonic_ms: float
    uncertainty_nano: int | None = None
    wall_origin_nano: int | None = None


class ObservationSink(Protocol):
    """Where a capture source authors governed facts.

    Every method is keyword-rich on purpose: the evidence qualifiers (``source``,
    ``confidence``, ``source_field``) are not optional metadata, they are what
    makes a recorded number a fact rather than a claim. A sink implementation may
    validate and reject, but it must never infer them.
    """

    def record_measurement(
        self,
        name: str,
        value: float,
        *,
        unit: str,
        operation_id: str | None = None,
        source: str,
        confidence: str,
        source_field: str | None = None,
        basis: str | None = None,
        at_ms: float | None = None,
        quality_kind: str = "provider_metric",
        attributes: Mapping[str, Any] | None = None,
        source_clock: SourceClockReading | None = None,
    ) -> None:
        """Author a scalar in its native unit, with its own evidence qualifiers.

        When ``source_clock`` is supplied the sample belongs to that declared clock
        domain at its RAW monotonic reading and must not be rebased onto the server
        clock.
        """

    def record_event(
        self,
        name: str,
        *,
        at_ms: float,
        participant: str | None = None,
        source: str = "app",
        confidence: str = "estimated",
        source_field: str = "pipeline.event",
        attributes: Mapping[str, Any] | None = None,
        source_clock: SourceClockReading | None = None,
    ) -> None:
        """Author a point observation, with the same foreign-clock discipline."""

    def record_operation(
        self,
        operation_id: str,
        operation_name: str,
        *,
        status: str = "ok",
        at_ms: float,
        ended_at_ms: float | None = None,
        participant: str | None = None,
        source: str = "app",
        confidence: str = "inferred",
        source_field: str = "pipeline.operation",
        attributes: Mapping[str, Any] | None = None,
        source_clock: SourceClockReading | None = None,
    ) -> None:
        """Author an operation this source *observed*, under an id it supplies.

        Unlike the pipeline's ``record_stage`` -- which MINTS an operation id from
        the turn cursor and advances that cursor -- this verb records an operation
        the source already identified, with the id it chose (or derived
        deterministically), and never advances a server turn cursor a browser or
        native collector does not have. ``at_ms`` is the observed start offset and
        ``ended_at_ms`` an optional end; when ``source_clock`` is supplied the
        operation is placed in that declared clock domain at its raw reading,
        exactly as :meth:`record_event` places a foreign-clock event, so an
        observed operation stays honestly incomparable to a server operation until
        a calibration exists.
        """

    def record_coverage(
        self,
        signal: str,
        availability: str,
        reason: str | None = None,
        *,
        dropped_count: int | None = None,
    ) -> None:
        """Ledger what this source could or could not observe (session scope).

        ``dropped_count`` is how many observations the source counted itself
        losing in this window. A source that can count its own loss (a bounded
        buffer that overflowed) says so here; one that cannot leaves it ``None``,
        which claims strictly less than ``0``.
        """

    def record_omission(
        self,
        field_name: str,
        *,
        capture_class: str | CaptureClass,
        reason: str = "adapter_payload_omitted",
    ) -> None:
        """Ledger a field this source saw and deliberately discarded."""

    def register_clock_domain(self, domain: ClockDomain) -> None:
        """Declare a clock domain this source's timestamps belong to."""


__all__ = ["ObservationSink", "SourceClockReading"]
