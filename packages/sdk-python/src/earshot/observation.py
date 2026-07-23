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
events, coverage and omissions should be expressible without dragging a pipeline
session behind it, and should be testable by handing the engine a sink that just
appends to a list.

``TurnRecorder`` satisfies this protocol structurally: it is *not* required to
inherit from it, and nothing here is enforced at runtime. That is intentional --
the protocol is a description of an existing seam, not a new base class, so it can
be introduced without changing a single recorded fact.

The verbs are the governed fact kinds, and only those:

* :meth:`record_measurement` -- a scalar with a unit, an evidence source, and a
  confidence. The one way a number enters an incident.
* :meth:`record_event` -- a point observation on a boundary (transport, device,
  render, speech). The one way an instant enters an incident.
* :meth:`record_coverage` -- an explicit *unknown*. A source that could not observe
  a signal says so here rather than emitting a fabricated zero.
* :meth:`record_omission` -- the privacy ledger. A source that saw a value but
  deliberately discarded it (transcript text, audio payload) records the discard
  without retaining the value.
* :meth:`register_clock_domain` -- a source whose timestamps are not on the server
  clock declares its own domain, so its facts stay honestly incomparable until a
  calibration exists.

Stage/operation authoring (``record_stage``) is deliberately *absent*. A stage
mints an operation id and advances the pipeline's turn cursor; it is turn
bookkeeping, not an observation, and a collector that only reports facts must not
be forced to model it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .contract import ClockDomain
from .privacy import CaptureClass


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
        browser_clock_domain_id: str | None = None,
        browser_monotonic_ms: float | None = None,
        browser_uncertainty_nano: int | None = None,
        browser_wall_origin_nano: int | None = None,
    ) -> None:
        """Author a scalar in its native unit, with its own evidence qualifiers.

        The ``browser_*`` arguments carry a foreign-clock reading: when
        ``browser_clock_domain_id`` is set the sample belongs to that domain at its
        RAW monotonic timestamp and must not be rebased onto the server clock.
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
        browser_clock_domain_id: str | None = None,
        browser_monotonic_ms: float | None = None,
        browser_uncertainty_nano: int | None = None,
        browser_wall_origin_nano: int | None = None,
    ) -> None:
        """Author a point observation, with the same foreign-clock discipline."""

    def record_coverage(
        self,
        signal: str,
        availability: str,
        reason: str | None = None,
    ) -> None:
        """Ledger what this source could or could not observe (session scope)."""

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


__all__ = ["ObservationSink"]
