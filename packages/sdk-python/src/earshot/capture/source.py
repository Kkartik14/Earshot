"""Feed browser capture batches into a recorder the application already owns.

This is topology **T1** of the continuous-incident design: a *server-owned call*.
The application already runs the earshot SDK server-side for a voice call -- it
records the STT / LLM / TTS stages and the generated-audio boundaries on its own
:class:`~earshot.pipeline.PipelineSession`, in the server clock domain. The
browser drained its own WebRTC / audio-graph telemetry to that same backend. A
:class:`BrowserCaptureSource` authors the browser telemetry **into the app's own
recorder**, so the browser evidence and the server/model/TTS/render evidence land
in ONE :class:`~earshot.contract.IncidentBundle` -- not two artifacts a consumer
must later reconcile. This needs no merge semantics precisely because both sets of
facts are authored into the same recorder from the start (the design's rejected
topology T3 is a *server-side merge of two independently recorded artifacts*; this
is not that).

The projection is the exact one the standalone HTTP capture path performs:

* **Sanitize.** Every batch is re-run through the server's own allowlist
  (:mod:`earshot.capture.sanitize`) before an engine sees it. The client is not a
  trust boundary; skipping this would make T1 a privacy-allowlist bypass. The SDK
  path drops byte-for-byte the same hostile members the HTTP path drops, because
  it calls the same allowlist primitives.
* **Derive.** The sanitized batch is projected into governed facts by the same
  deterministic engines (:func:`~earshot.engines.webrtc.apply_webrtc_stats`,
  :func:`~earshot.engines.device.apply_audio_graph`), threading the
  :class:`~earshot.engines.webrtc.WebRtcCarry` across successive batches of the
  one call so the loss / jitter / concealment / reconnect interval that *spans* a
  batch boundary is recovered instead of silently dropped.
* **Author.** Those facts are written through the
  :class:`~earshot.observation.ObservationSink` seam into the app's recorder, in a
  declared browser :class:`~earshot.contract.ClockDomain` at the raw browser
  timestamps -- never rebased onto the server clock. The recorder's own
  server/model/TTS/render facts stay in the server clock domain.

The result is one bundle with two clock domains coexisting honestly. Because no
:class:`~earshot.contract.ClockRelation` between the two clocks exists unless the
application declares a real calibration, the analyzer keeps refusing cross-clock
latency between a browser render event and a server event -- it stays
``unavailable`` / ``cross_clock_domain``, never fabricated. When the app declares a
real calibration, the existing alignment path makes it honestly *estimated*.

A browser fact carries its own domain coordinate (a :class:`SourceClockReading`),
so it never advances the server-clock turn extent: the clock-domain discipline the
earlier phases established holds unchanged here.

Ordering is the engines' decision, reused rather than re-made: a snapshot that
precedes the carry's last reading makes that step *unobserved* -- the interval is
dropped and a ``webrtc.snapshot_order`` coverage note is written -- exactly as a
non-monotonic snapshot within a single batch is handled. This source does not add
a second, contradictory ordering rule; sequencing/idempotency for the drained HTTP
transport is the separate concern of :mod:`earshot.capture.calls`.

Usage (T1)::

    from earshot.capture import BrowserCaptureSource

    source = BrowserCaptureSource()            # locks the call's browser clock
    # ... the app records its server-side STT/LLM/TTS on ``session`` turns ...
    with session.turn("browser-capture") as turn:
        for batch in drained_browser_batches:  # in the order received
            source.apply(turn, batch)          # turn satisfies ObservationSink
    incident = session.close()                 # ONE bundle, two clock domains
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..engines.base import BrowserClockDomain
from ..engines.device import DeviceFacts, apply_audio_graph
from ..engines.webrtc import WebRtcCarry, WebRtcFacts, apply_webrtc_stats
from ..observation import ObservationSink
from .calls import _namespace_client_coverage
from .sanitize import sanitize_device_events, sanitize_snapshots

_NANOS_PER_MS = 1_000_000
_CAPTURE_CLOCK_KIND = "browser_monotonic"

# The rejection-coverage signals and reasons the server allowlist writes when it
# withholds a non-governed stat, member or event. Identical to what the HTTP
# ``/v1/capture`` path records, so a bundle assembled from the SDK path carries the
# same privacy ledger as one assembled from a drain.
_REJECTION_COVERAGE = (
    ("stats", "capture.stats", "non_governed_stat_dropped"),
    ("stat_members", "capture.stat_members", "non_governed_member_dropped"),
    ("device_events", "capture.device_events", "non_governed_event_dropped"),
    ("device_members", "capture.device_event_members", "non_governed_member_dropped"),
)


@dataclass(frozen=True, slots=True)
class BrowserCaptureReport:
    """What one applied browser batch resolved to, for the caller to inspect.

    ``accepted_*`` count the sanitized facts fed to the engines; ``dropped_*``
    count what the server allowlist refused (and surfaced as coverage). ``webrtc``
    / ``device`` are the immutable engine results, so a caller can assert on the
    derivation -- including :attr:`WebRtcFacts.reconnected` for a boundary-spanning
    reconnect -- without re-scanning the recorded incident.
    """

    accepted_snapshots: int
    accepted_device_events: int
    dropped_stats: int
    dropped_stat_members: int
    dropped_device_events: int
    dropped_device_members: int
    webrtc: WebRtcFacts
    device: DeviceFacts


class BrowserCaptureSource:
    """Authors one browser call's drained batches into an app-owned recorder.

    Construct one per call. The browser clock domain is either supplied up front
    or locked from the first batch's ``clockDomain``; a later batch that declares a
    *different* clock-domain id is refused rather than spliced onto this call's
    timeline (a reload mints a new domain, so it is honestly a new call). The
    WebRTC delta carry is held here and threaded across every :meth:`apply`, so a
    call captured as many batches yields the facts a single-shot derivation would.

    The source is not thread-safe: drive one call's batches from one thread, in the
    order they were received. Concurrency across *different* calls is fine -- each
    call has its own source.
    """

    def __init__(self, *, clock_domain: BrowserClockDomain | None = None) -> None:
        self._clock_domain = clock_domain
        self._carry: WebRtcCarry | None = None

    @property
    def clock_domain(self) -> BrowserClockDomain | None:
        """The browser clock this call's facts are recorded in, once known."""

        return self._clock_domain

    @property
    def carry(self) -> WebRtcCarry | None:
        """The cross-batch WebRTC delta state threaded into the next batch."""

        return self._carry

    def apply(self, sink: ObservationSink, batch: Mapping[str, Any]) -> BrowserCaptureReport:
        """Sanitize, project and author one browser batch onto ``sink``.

        ``batch`` is a parsed browser capture payload (or the subset carrying
        ``snapshots`` / ``deviceEvents`` / ``coverage`` / ``clockDomain``). ``sink``
        is the app's recorder -- typically a :class:`~earshot.pipeline.TurnRecorder`
        from ``session.turn(...)``. The browser clock domain is declared on the sink
        (once, by the engines) and every derived fact is placed in it at its raw
        browser timestamp.
        """

        domain = self._resolve_clock_domain(batch)

        snapshots, dropped_stats, dropped_members = sanitize_snapshots(
            _mappings(batch.get("snapshots"))
        )
        events, dropped_events, dropped_event_members = sanitize_device_events(
            _mappings(_device_events(batch))
        )

        # Author the client's own coverage first (namespaced under ``browser.`` so
        # it can never mask a server-derived note), then the server allowlist's
        # refusals -- exactly the order and signals the standalone path records.
        self._author_client_coverage(sink, batch.get("coverage"))
        self._author_rejection_coverage(
            sink,
            {
                "stats": dropped_stats,
                "stat_members": dropped_members,
                "device_events": dropped_events,
                "device_members": dropped_event_members,
            },
        )

        # Resume the WebRTC delta from the prior batch of this call; the engine
        # declares the clock domain on the sink and stamps every fact at its raw
        # browser coordinate. The outbound carry threads into the next batch.
        webrtc = apply_webrtc_stats(sink, snapshots, clock_domain=domain, carry=self._carry)
        self._carry = webrtc.carry
        device = apply_audio_graph(sink, events, clock_domain=domain)

        return BrowserCaptureReport(
            accepted_snapshots=len(snapshots),
            accepted_device_events=len(events),
            dropped_stats=dropped_stats,
            dropped_stat_members=dropped_members,
            dropped_device_events=dropped_events,
            dropped_device_members=dropped_event_members,
            webrtc=webrtc,
            device=device,
        )

    def _resolve_clock_domain(self, batch: Mapping[str, Any]) -> BrowserClockDomain:
        """Lock this call's browser clock, or reject a batch from a different one."""

        declared = _clock_domain_from_batch(batch.get("clockDomain"))
        if self._clock_domain is None:
            if declared is None:
                raise ValueError(
                    "a browser capture batch must declare a clockDomain (or the source "
                    "must be constructed with clock_domain=...)"
                )
            self._clock_domain = declared
            return declared
        if declared is not None and declared.clock_domain_id != self._clock_domain.clock_domain_id:
            # A different clock-domain id is a different continuous browser timeline
            # (a page reload mints a fresh one). Splicing it onto this call would
            # fabricate continuity across two calls, so refuse instead.
            raise ValueError(
                "this browser capture batch belongs to a different clock domain "
                f"({declared.clock_domain_id!r} != {self._clock_domain.clock_domain_id!r}); "
                "use a separate BrowserCaptureSource for a separate call"
            )
        return self._clock_domain

    def _author_client_coverage(self, sink: ObservationSink, coverage: Any) -> None:
        """Record the browser's own coverage claims, under their own namespace."""

        for note in _mappings(coverage):
            signal = note.get("signal")
            availability = note.get("availability")
            if not isinstance(signal, str) or not isinstance(availability, str):
                continue  # a malformed note is not a claim; let the well-formed ones through
            reason = note.get("reason")
            dropped = note.get("droppedCount")
            if dropped is None:
                dropped = note.get("dropped_count")
            countable = isinstance(dropped, int) and not isinstance(dropped, bool)
            counted = dropped if countable else None
            sink.record_coverage(
                _namespace_client_coverage(signal),
                availability,
                reason if isinstance(reason, str) else None,
                dropped_count=counted,
            )

    @staticmethod
    def _author_rejection_coverage(sink: ObservationSink, counts: Mapping[str, int]) -> None:
        """Ledger what the server allowlist withheld, as the HTTP path does."""

        for key, signal, reason in _REJECTION_COVERAGE:
            if counts[key] > 0:
                sink.record_coverage(signal, "partial", reason)


def _device_events(batch: Mapping[str, Any]) -> Any:
    """The batch's device events under either the wire (``deviceEvents``) or snake key."""

    events = batch.get("deviceEvents")
    return batch.get("device_events") if events is None else events


def _mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield only the mapping items of ``value``; tolerate ``None`` and stray members.

    A browser payload is untrusted structure, so a non-list or a non-mapping entry
    is skipped rather than raised on -- the same fail-open discipline the engines
    already apply to malformed snapshots.
    """

    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return
    for item in value:
        if isinstance(item, Mapping):
            yield item


def _clock_domain_from_batch(declared: Any) -> BrowserClockDomain | None:
    """Map a payload ``clockDomain`` mapping to a :class:`BrowserClockDomain`.

    Mirrors the standalone capture path's construction so the SDK path records
    browser facts in an identically-shaped domain. ``uncertaintyMs`` defaults to
    the browser-monotonic reading uncertainty when the payload omits it; a wall
    origin is carried only when the client knew its ``performance.timeOrigin``.
    """

    if not isinstance(declared, Mapping):
        return None
    clock_domain_id = declared.get("id")
    if not isinstance(clock_domain_id, str) or not clock_domain_id:
        return None
    uncertainty_ms = declared.get("uncertaintyMs")
    wall_origin_ms = declared.get("wallOriginMs")
    fields: dict[str, Any] = {
        "clock_domain_id": clock_domain_id,
        "kind": _CAPTURE_CLOCK_KIND,
        "observer": "browser",
    }
    if isinstance(uncertainty_ms, (int, float)) and not isinstance(uncertainty_ms, bool):
        fields["uncertainty_nano"] = int(float(uncertainty_ms) * _NANOS_PER_MS)
    if isinstance(wall_origin_ms, (int, float)) and not isinstance(wall_origin_ms, bool):
        fields["wall_origin_unix_nano"] = int(float(wall_origin_ms) * _NANOS_PER_MS)
    return BrowserClockDomain(**fields)


__all__ = ["BrowserCaptureReport", "BrowserCaptureSource"]
