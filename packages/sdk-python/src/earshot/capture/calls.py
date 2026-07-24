"""One continuous browser call, accumulated from many drains.

``captureVersion: 2`` stops treating each browser telemetry drain as its own
incident and treats the whole call as one growing, journal-backed artifact. This
module is where that happens. A :class:`CaptureCall` holds, per call, exactly the
state that makes the accumulation honest and bounded:

* a real recorder (a :class:`~earshot.pipeline.PipelineSession`) that projects a
  sanitized drain into governed facts and journals every one of them to an
  in-memory :class:`~earshot.checkpoint.server_writer.ServerJournalWriter` -- so
  the facts, their evidence, their clock domain and their privacy governance are
  built by exactly the code every other artifact uses, never a second
  approximation of it;
* one persistent turn, so a fact's synthetic id comes from one monotonic counter
  across every drain rather than restarting each batch;
* the :class:`~earshot.engines.webrtc.WebRtcCarry`, threaded across drains so the
  loss / jitter / concealment / reconnect interval that *spans* a drain boundary
  is recovered instead of silently dropped;
* the drain sequencing state -- the highest applied drain and a bounded ring of
  the sanitized-batch digests it accepted -- which makes a re-sent drain resolve
  to the evidence it already produced and a forged drain at a taken slot a
  refusal.

The recorder is never closed here. Phase 1 only *accumulates*; materialization is
the existing operator seal over the live session, which produces a **provisional**
artifact because no close was observed. Nothing on a timer, a tab close, or a TTL
ever finalizes a call -- that is Phase 2.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..engines.base import BrowserClockDomain
from ..engines.device import apply_audio_graph
from ..engines.webrtc import WebRtcCarry, apply_webrtc_stats
from ..pipeline import PipelineSession, TurnRecorder
from .identity import call_key as derive_call_key

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing live at module load
    from ..live import LiveSessionRegistry

_NANOS_PER_MS = 1_000_000

# The producer identity and framework a browser capture is authored under -- the
# same ones the single-slice v1 path uses, so a capture call assembles through
# the same profile builder and adapter surface.
_CAPTURE_PRODUCER = "earshot.capture_api"
_CAPTURE_FRAMEWORK = "browser_capture"
_CAPTURE_TURN_ID = "browser-capture"
_CAPTURE_CLOCK_KIND = "browser_monotonic"

# How a mid-call seal names its reconstruction. Distinct from v1's
# ``browser_capture_batch`` (a single slice) so a reader can tell one drain from a
# continuous, journal-backed call.
RECOVERY_METHOD = "browser_capture_journal"
RECOVERY_REASON_SEALED = "capture_sealed_before_call_end"

# The call-identity coverage: within a tenant the continuity rests on the
# client's own stable ids, and the artifact says so rather than pretending the
# identity is server-attested.
_IDENTITY_SIGNAL = "capture.call_identity"
_IDENTITY_REASON = "client_declared_identity"

# The loss ledger a declared drain gap writes: how many drains were lost, and
# that the boundary delta across the gap was dropped rather than estimated.
_DRAIN_SEQUENCE_SIGNAL = "capture.drain_sequence"
_STATS_CONTINUITY_SIGNAL = "capture.stats_continuity"
_STATS_CONTINUITY_REASON = "carry_invalidated_by_drain_loss"

# How many accepted-batch digests a call remembers, so a retried or reordered
# drain within this window is answered from content rather than re-applied. One
# checksum-sized slot per drain; bounded by construction.
_DIGEST_RING = 64


class CaptureError(Exception):
    """Base class for refusals the continuous-capture surface makes on purpose."""


class CaptureSequenceGapError(CaptureError):
    """A drain skips ahead of the sequence the call holds, with no declared loss."""

    def __init__(self, message: str, *, expected_sequence: int) -> None:
        super().__init__(message)
        self.expected_sequence = expected_sequence


class CaptureSequenceConflictError(CaptureError):
    """A drain rewrites, or forges, a sequence slot the call already resolved."""

    def __init__(self, message: str, *, sequence: int) -> None:
        super().__init__(message)
        self.sequence = sequence


class CaptureCallCapacityError(CaptureError):
    """This project is already carrying as many continuous calls as it will."""


@dataclass(frozen=True)
class ResyncClaim:
    """A client's declaration that it permanently gave up on earlier drains."""

    missed_from: int
    missed_through: int
    reason: str


@dataclass(frozen=True)
class CaptureDrain:
    """One sanitized drain, ready to be sequenced and projected.

    The identity components (``project_id`` .. ``clock_wall_origin_ms``) derive
    the call key; the rest is the drain's own governed content. ``snapshots`` and
    ``device_events`` are already through the server allowlist; ``coverage`` is
    the client's own coverage claims; ``rejection_coverage`` is what the allowlist
    refused, as ``(signal, reason, count)``.
    """

    project_id: str
    session_id: str
    capture_version: int
    clock_domain_id: str
    clock_uncertainty_ms: float
    clock_wall_origin_ms: float | None
    trace_id: str | None
    span_id: str | None
    drain_sequence: int
    snapshots: Sequence[dict[str, Any]]
    device_events: Sequence[dict[str, Any]]
    coverage: Sequence[tuple[str, str, str, int | None]]
    rejection_coverage: Sequence[tuple[str, str, int]]
    resync: ResyncClaim | None

    @property
    def call_key(self) -> str:
        return derive_call_key(
            project_id=self.project_id,
            capture_version=self.capture_version,
            session_id=self.session_id,
            clock_domain_id=self.clock_domain_id,
            wall_origin_ms=self.clock_wall_origin_ms,
        )

    def digest(self) -> str:
        """A stable digest of this drain's governed content.

        Two byte-identical raw batches sanitize to the same content and so to the
        same digest -- a genuine retry. A forged batch claiming a taken slot has
        different content and a different digest, and is refused.
        """

        material = json.dumps(
            {
                "sequence": self.drain_sequence,
                "snapshots": list(self.snapshots),
                "device_events": list(self.device_events),
                "coverage": [list(note) for note in self.coverage],
                "resync": (
                    None
                    if self.resync is None
                    else [self.resync.missed_from, self.resync.missed_through, self.resync.reason]
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DrainOutcome:
    """What one drain resolved to, echoed back to the client."""

    call_id: str
    journal_id: str
    accepted_through: int
    accepted_records: int
    state: str
    sealable: bool
    replay: bool
    accepted_snapshots: int
    accepted_device_events: int
    accepted_coverage: int


class CaptureCall:
    """One continuous call: its recorder, its turn, its carry, its sequencing."""

    def __init__(self, drain: CaptureDrain) -> None:
        self.call_key = drain.call_key
        self.project_id = drain.project_id
        self.lock = threading.Lock()
        self._domain = BrowserClockDomain(
            clock_domain_id=drain.clock_domain_id,
            kind=_CAPTURE_CLOCK_KIND,
            observer="browser",
            uncertainty_nano=int(drain.clock_uncertainty_ms * _NANOS_PER_MS),
            wall_origin_unix_nano=(
                None
                if drain.clock_wall_origin_ms is None
                else int(drain.clock_wall_origin_ms * _NANOS_PER_MS)
            ),
        )
        # The in-memory journal the recorder writes to, and the recorder itself.
        # The session id is the call key: the call's stable, project-scoped
        # identity, so the header, the live session and the artifact all agree.
        from ..checkpoint.server_writer import ServerJournalWriter

        self.writer = ServerJournalWriter()
        self.session = PipelineSession(
            session_id=self.call_key,
            bundle_id=self.call_key,
            framework=_CAPTURE_FRAMEWORK,
            producer_name=_CAPTURE_PRODUCER,
            trace_id=drain.trace_id,
            span_id=drain.span_id,
            checkpoint=self.writer,
        )
        # One persistent turn, so a fact's synthetic id runs off one monotonic
        # counter across every drain. This is the ``turn()`` bookkeeping done once
        # and never exited, because a browser fact lands in its own clock domain
        # and never advances the server-clock turn extent.
        self.session._turn_origins_nano.append(self.session._cursor_nano)
        self.session._turn_ids.add(_CAPTURE_TURN_ID)
        self.turn = TurnRecorder(self.session, _CAPTURE_TURN_ID, 0)
        self.carry: WebRtcCarry | None = None
        self._applied = 0
        self._digests: OrderedDict[int, str] = OrderedDict()
        self._outcomes: OrderedDict[int, DrainOutcome] = OrderedDict()
        self._identity_declared = False

    # -- sequencing ------------------------------------------------------------

    def _classify(self, drain: CaptureDrain) -> str:
        """Decide what to do with this drain, or refuse it. Caller holds the lock.

        Returns ``"apply"``, ``"apply_gap"`` (a declared loss), or ``"replay"``;
        raises a sequencing refusal otherwise.
        """

        sequence = drain.drain_sequence
        if sequence == self._applied + 1:
            return "apply"
        if sequence <= self._applied:
            recorded = self._digests.get(sequence)
            if recorded is not None and recorded == drain.digest():
                return "replay"
            raise CaptureSequenceConflictError(
                "a drain rewrites a sequence this call already resolved",
                sequence=sequence,
            )
        # sequence > applied + 1: a gap. Only a declared loss covering exactly the
        # missing range may cross it; anything else is a gap the client must resync.
        resync = drain.resync
        if (
            resync is not None
            and resync.missed_from == self._applied + 1
            and resync.missed_through == sequence - 1
        ):
            return "apply_gap"
        if resync is not None and resync.missed_from <= self._applied:
            raise CaptureSequenceConflictError(
                "a resync claims a range already applied",
                sequence=resync.missed_from,
            )
        raise CaptureSequenceGapError(
            "a drain skips ahead with no declared loss",
            expected_sequence=self._applied + 1,
        )

    def _remember(self, sequence: int, digest: str, outcome: DrainOutcome) -> None:
        self._digests[sequence] = digest
        self._outcomes[sequence] = outcome
        while len(self._digests) > _DIGEST_RING:
            self._digests.popitem(last=False)
        while len(self._outcomes) > _DIGEST_RING:
            self._outcomes.popitem(last=False)

    # -- projection ------------------------------------------------------------

    def process(self, drain: CaptureDrain, live: LiveSessionRegistry) -> DrainOutcome:
        """Sequence, project, journal and append one drain. Holds the call lock."""

        with self.lock:
            decision = self._classify(drain)
            if decision == "replay":
                return replace(self._outcomes[drain.drain_sequence], replay=True)

            if not self._identity_declared:
                self.turn.record_coverage(_IDENTITY_SIGNAL, "partial", _IDENTITY_REASON)
                self._identity_declared = True

            if decision == "apply_gap":
                assert drain.resync is not None
                lost = drain.resync.missed_through - drain.resync.missed_from + 1
                self.turn.record_coverage(
                    _DRAIN_SEQUENCE_SIGNAL,
                    "partial",
                    drain.resync.reason,
                    dropped_count=lost,
                )
                self.turn.record_coverage(
                    _STATS_CONTINUITY_SIGNAL, "partial", _STATS_CONTINUITY_REASON
                )
                # The delta across the lost boundary cannot be differenced
                # honestly, so the carry is dropped rather than resumed.
                self.carry = None

            self._project(drain)
            entries = self.writer.take_new()
            accepted = live.accept_records(
                self.call_key,
                entries,
                project_id=self.project_id,
                recovery_method=RECOVERY_METHOD,
                recovery_reason=RECOVERY_REASON_SEALED,
            )
            outcome = DrainOutcome(
                call_id=self.call_key,
                journal_id=accepted.journal_id,
                accepted_through=accepted.accepted_through,
                accepted_records=accepted.accepted_records,
                state=accepted.state,
                sealable=accepted.sealable,
                replay=False,
                accepted_snapshots=len(drain.snapshots),
                accepted_device_events=len(drain.device_events),
                accepted_coverage=len(drain.coverage),
            )
            self._applied = drain.drain_sequence
            self._remember(drain.drain_sequence, drain.digest(), outcome)
            return outcome

    def _project(self, drain: CaptureDrain) -> None:
        """Record this drain's governed facts onto the persistent turn."""

        for signal, availability, reason, dropped in drain.coverage:
            self.turn.record_coverage(
                _namespace_client_coverage(signal),
                availability,
                reason,
                dropped_count=dropped,
            )
        for signal, reason, count in drain.rejection_coverage:
            if count > 0:
                self.turn.record_coverage(signal, "partial", reason)
        facts = apply_webrtc_stats(
            self.turn, drain.snapshots, clock_domain=self._domain, carry=self.carry
        )
        self.carry = facts.carry
        apply_audio_graph(self.turn, drain.device_events, clock_domain=self._domain)


def _namespace_client_coverage(signal: str) -> str:
    """Keep a client's coverage under its own prefix so it cannot mask a server one."""

    return signal if signal.startswith("browser.") else f"browser.{signal}"


class CaptureCallRegistry:
    """Every continuous browser call this server is currently accumulating.

    Keyed by ``(project_id, call_key)``. The call key already folds in the
    authenticated project, so this keying only makes lookup total; isolation is a
    property of the key itself. Each project is bounded to a fixed number of
    concurrent calls, refused with :class:`CaptureCallCapacityError`, so one
    tenant cannot spend the whole server's capture budget.
    """

    def __init__(
        self,
        live: LiveSessionRegistry,
        *,
        max_calls_per_project: int = 16,
    ) -> None:
        self._live = live
        self._max_per_project = max_calls_per_project
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], CaptureCall] = {}

    def drain(self, drain: CaptureDrain) -> DrainOutcome:
        """Accept one drain of one call. Thread-safe; drains of a call serialize."""

        call = self._call_for(drain)
        return call.process(drain, self._live)

    def _call_for(self, drain: CaptureDrain) -> CaptureCall:
        key = (drain.project_id, drain.call_key)
        with self._lock:
            call = self._calls.get(key)
            if call is not None and self._live.contains(
                drain.call_key, project_id=drain.project_id
            ):
                return call
            if call is not None:
                # The live session behind this call expired or was superseded;
                # its accumulation is gone, so start the call afresh rather than
                # appending onto a session that no longer exists.
                self._calls.pop(key, None)
            owned = sum(1 for project, _ in self._calls if project == drain.project_id)
            if owned >= self._max_per_project:
                raise CaptureCallCapacityError(
                    "this project is carrying as many continuous calls as it will"
                )
            call = CaptureCall(drain)
            self._calls[key] = call
            return call


__all__ = [
    "RECOVERY_METHOD",
    "RECOVERY_REASON_SEALED",
    "CaptureCall",
    "CaptureCallCapacityError",
    "CaptureCallRegistry",
    "CaptureDrain",
    "CaptureError",
    "CaptureSequenceConflictError",
    "CaptureSequenceGapError",
    "DrainOutcome",
    "ResyncClaim",
]
