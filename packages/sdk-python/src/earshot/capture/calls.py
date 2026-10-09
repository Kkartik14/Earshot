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
an authorized seal over the live session, which produces a **provisional** artifact
because no close was observed. Nothing on a timer, a tab close, or a TTL ever
finalizes a call -- that is Phase 2.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..checkpoint.framing import scan_frames
from ..checkpoint.records import (
    JournalFormatError,
    JournalOpen,
    JournalOperationOpen,
    JournalRecordEntry,
    decode_entry,
)
from ..contract import TimePoint
from ..engines.base import BrowserClockDomain
from ..engines.device import apply_audio_graph
from ..engines.webrtc import WebRtcCarry, apply_webrtc_stats
from ..observation import SourceClockReading
from ..pipeline import PipelineSession, TurnRecorder
from .durable import (
    CURRENT_CAPTURE_DIGEST_VERSION,
    DEFAULT_MAX_SEALED_REPLAY_LEDGERS,
    DIGEST_RING_SIZE,
    JOURNAL_SUFFIX,
    LEGACY_CAPTURE_DIGEST_VERSION,
    REJECTION_COVERAGE_CAPTURE_DIGEST_VERSION,
    SIDECAR_SUFFIX,
    WRITER_LOCK_NAME,
    CaptureSidecar,
    acquire_writer_lock,
    discard_rejected_first_drain,
    iter_sidecars,
    journal_path,
    max_fact_sequence,
    prune_sealed_sidecars,
    read_sidecar,
    recover_pending_sidecar,
    release_writer_lock,
    remove_sealed_journal,
    replay_coverage,
    sidecar_path,
    write_sidecar,
)
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

# The one end reason that finalizes a call. Everything else -- a stopped capture,
# a hidden or unloaded page -- abandons the call: the observer stopped watching,
# which is not the same as the call ending, so the artifact stays provisional
# forever. Only the application, the best-placed observer of a browser call's end,
# can declare ``call_ended``; the browser kernel never emits it from a lifecycle
# hook (``stop()`` / ``pagehide``).
END_CALL_ENDED = "call_ended"

# The browser-domain bounds of the call, and the same-clock-domain durations
# differenced between them. ``observed_call_duration`` (endCall - first observed
# coordinate) is emitted only when a close was observed; ``observed_capture_extent``
# (last - first observed sample) is how far the observer saw when it was not.
_CLIENT_CAPTURE_STARTED = "earshot.client.capture_started"
_CLIENT_CALL_ENDED = "earshot.client.call_ended"
_OBSERVED_CALL_DURATION = "client.observed_call_duration"
_OBSERVED_CAPTURE_EXTENT = "client.observed_capture_extent"
_DURATION_SOURCE_FIELD = "clock_domain_extent"
# Coverage the provenance of the close (or its absence) rides on.
_CALL_CLOSE_SIGNAL = "client.call_close"
_CALL_CLOSE_REASON = "client_declared"
_CALL_DURATION_SIGNAL = "client.call_duration"
_CALL_DURATION_UNOBSERVED_REASON = "close_not_observed"
# A call that ended cleanly finalizes as complete; one that lost or withheld any
# evidence finalizes as incomplete. The finalize status is the lever: the profile
# builder derives ``completeness`` from it, so a non-``completed`` status is what
# keeps a lossy-but-ended call from ever presenting as complete.
_STATUS_COMPLETE = "completed"
_STATUS_INCOMPLETE = "incomplete"

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
# The boundary a backend restart falls across: the WebRTC carry (the raw snapshot
# the delta differences against) is genuinely absent from the durable journal, so
# it is dropped rather than guessed, and the interval spanning the restart is
# declared lost -- exactly as a drain gap's carry loss is.
_STATS_CONTINUITY_RESTART_REASON = "carry_lost_on_restart"

# How many accepted-batch digests a call remembers, so a retried or reordered
# drain within this window is answered from content rather than re-applied. One
# checksum-sized slot per drain; bounded by construction.


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


class CaptureCallReplayExpiredError(CaptureError):
    """A sealed call's bounded replay ledger expired while its bundle ID stayed reserved."""


class CaptureJournalUnavailableError(CaptureError):
    """The durable call ledger could not be persisted before the journal."""


class _CaptureJournalTooLargeError(OSError):
    """A durable journal exceeds the configured recovery read bound."""


class CaptureJournalWriterConflictError(CaptureError):
    """Another process already owns this durable capture journal directory."""


class CaptureCallClosedError(CaptureError):
    """A drain arrived for a call an explicit ``endCall()`` already finalized."""


@dataclass(frozen=True)
class ResyncClaim:
    """A client's declaration that it permanently gave up on earlier drains."""

    missed_from: int
    missed_through: int
    reason: str


@dataclass(frozen=True)
class CaptureEnd:
    """The client's declaration, on its final drain, that the call is over.

    ``reason`` is ``call_ended`` for an explicit application-observed close (the
    only thing that finalizes) or one of the abandon reasons (``capture_stopped``,
    ``page_hidden``, ``page_unloaded``) for a lifecycle flush that stopped the
    observer without ending the call. ``timestamp_ms`` is the raw browser
    coordinate of the declaration, in the call's clock domain -- never fabricated,
    never the last snapshot's time and never a server clock reading.
    """

    reason: str
    timestamp_ms: float

    @property
    def finalizes(self) -> bool:
        return self.reason == END_CALL_ENDED


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
    end: CaptureEnd | None = None

    @property
    def call_key(self) -> str:
        return derive_call_key(
            project_id=self.project_id,
            capture_version=self.capture_version,
            session_id=self.session_id,
            clock_domain_id=self.clock_domain_id,
            wall_origin_ms=self.clock_wall_origin_ms,
        )

    def call_metadata_digest(self) -> str:
        """Hash the call-level trace and clock metadata without storing it twice."""

        material = json.dumps(
            [self.trace_id, self.span_id, float(self.clock_uncertainty_ms)],
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def digest(self, *, version: int = CURRENT_CAPTURE_DIGEST_VERSION) -> str:
        """A stable digest of this drain's governed content.

        Two byte-identical raw batches sanitize to the same content and so to the
        same digest -- a genuine retry. A forged batch claiming a taken slot has
        different content and a different digest, and is refused. Version 1 is
        retained for older persisted retries; version 2 binds rejection coverage;
        version 3 also binds the drain's trace/span and clock uncertainty metadata.
        """

        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version
            not in {
                LEGACY_CAPTURE_DIGEST_VERSION,
                REJECTION_COVERAGE_CAPTURE_DIGEST_VERSION,
                CURRENT_CAPTURE_DIGEST_VERSION,
            }
        ):
            raise ValueError("unsupported capture drain digest version")
        content: dict[str, object] = {
            "sequence": self.drain_sequence,
            "snapshots": list(self.snapshots),
            "device_events": list(self.device_events),
            "coverage": [list(note) for note in self.coverage],
            "resync": (
                None
                if self.resync is None
                else [self.resync.missed_from, self.resync.missed_through, self.resync.reason]
            ),
            "end": (None if self.end is None else [self.end.reason, self.end.timestamp_ms]),
        }
        if version >= REJECTION_COVERAGE_CAPTURE_DIGEST_VERSION:
            content["rejection_coverage"] = [list(note) for note in self.rejection_coverage]
        if version >= CURRENT_CAPTURE_DIGEST_VERSION:
            content["call_metadata_digest"] = self.call_metadata_digest()
        material = json.dumps(
            content,
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
    # True only for the drain whose explicit ``endCall()`` finalized the call. The
    # sealed artifact is then final; every other drain leaves it provisional.
    finalized: bool = False


class CaptureCall:
    """One continuous call: its recorder, its turn, its carry, its sequencing."""

    def __init__(
        self,
        drain: CaptureDrain,
        *,
        journal_dir: Path | None = None,
        correlation_key_id: str | None = None,
        call_metadata: tuple[str | None, str | None, float] | None = None,
        call_metadata_bound: bool = True,
    ) -> None:
        self.call_key = drain.call_key
        self.project_id = drain.project_id
        self._correlation_key_id = correlation_key_id
        self._call_metadata_digest = drain.call_metadata_digest()
        self._call_metadata_bound = call_metadata_bound
        # Where this call's durable journal and drain-sequencing ledger live, so an
        # in-flight call survives a backend restart. ``None`` keeps the call purely
        # in memory (its pre-durability behaviour).
        self._journal_dir = None if journal_dir is None else Path(journal_dir)
        self._durable_path = (
            None if self._journal_dir is None else journal_path(self._journal_dir, self.call_key)
        )
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
        # Declare the browser clock domain up front, so the call's end coordinate
        # (``session.ended_at``) references a domain the artifact always carries --
        # even for a call whose only content was its ``endCall()``.
        self.turn.register_clock_domain(self._domain.to_contract())
        self.carry: WebRtcCarry | None = None
        self._applied = 0
        self._digests: OrderedDict[int, str] = OrderedDict()
        self._digest_versions: OrderedDict[int, int] = OrderedDict()
        self._outcomes: OrderedDict[int, DrainOutcome] = OrderedDict()
        self._identity_declared = False
        # The browser-domain span the call actually observed: the earliest and
        # latest raw sample coordinates seen across every drain. Both are genuine
        # observed readings; neither is ever fabricated or defaulted to zero.
        self._first_observed_ms: float | None = None
        self._last_observed_ms: float | None = None
        # Whether any evidence was lost or withheld on the call. It decides the
        # completeness of a finalized artifact: any loss -> incomplete.
        self._lossy = False
        # Set once an explicit ``endCall()`` finalized the call. A later drain is
        # then refused rather than appended onto a closed journal.
        self._finalized = False
        # True only for a call reconstructed from disk after a backend restart. The
        # first drain that continues such a call declares the carry lost across the
        # restart, because the raw snapshot the boundary delta needs was never in
        # the journal and must not be fabricated.
        self._restarted = False
        self._retry_sequence: int | None = None
        self._retry_digest: str | None = None
        self._retry_digest_version: int | None = None
        self._intent_pending = False

    @property
    def finalized(self) -> bool:
        return self._finalized

    @classmethod
    def rebuild(
        cls,
        drain: CaptureDrain,
        sidecar: CaptureSidecar,
        journal_path_on_disk: Path,
        *,
        journal_dir: Path,
        finalized: bool,
        correlation_key_id: str | None = None,
    ) -> CaptureCall:
        """Reconstruct a call from its durable ledger so a restart is resumable.

        The recorder, its turn and its clock domain are rebuilt fresh from the
        continuing drain (which carries the same stable clock-domain identity), and
        the fresh header and clock-domain frames it re-emits are discarded because
        they are already durable and already in the rebuilt live session. Three
        pieces are then restored so a continuing drain resumes cleanly rather than
        getting an ``unknown session``:

        * the drain-sequencing ledger (applied sequence + digest ring) from the
          sidecar, so the next in-sequence drain is accepted, a resent one replays,
          and a forged one at a taken slot still conflicts;
        * the recorder's fact-id counter, read from the journal so a post-restart
          fact never collides with one already on disk;
        * the recorder's coverage list, replayed from the journal so a later
          in-place coverage supersession rewrites the right slot at seal time.

        The WebRTC carry is deliberately **not** restored: the raw snapshot it
        differences against is not in the journal, so it is dropped and the first
        continuing drain declares ``carry_lost_on_restart`` rather than inventing an
        interval. ``finalized`` comes from the durable journal (its finalize frame),
        never fabricated, so a rebuilt call is provisional unless a close really was
        observed and written before the restart.
        """

        call = cls(
            drain,
            journal_dir=journal_dir,
            correlation_key_id=correlation_key_id,
            call_metadata_bound=sidecar.call_metadata_bound,
        )
        # Discard the fresh recorder's re-emitted header and clock-domain entries:
        # both are already durable and in the rebuilt live session, so appending
        # them again would duplicate frames.
        call.writer.take_new()
        call._applied = sidecar.applied
        call._digests = OrderedDict(sorted(sidecar.digests.items()))
        call._digest_versions = OrderedDict(sorted(sidecar.digest_versions.items()))
        call._first_observed_ms = sidecar.first_observed_ms
        call._last_observed_ms = sidecar.last_observed_ms
        call._lossy = sidecar.lossy
        call._finalized = finalized
        call._retry_sequence = sidecar.retry_sequence
        call._retry_digest = sidecar.retry_digest
        call._retry_digest_version = sidecar.retry_digest_version
        call._identity_declared = True
        call.carry = None
        call._restarted = True
        call.turn._sequence = max_fact_sequence(journal_path_on_disk)
        call.session.recorder._coverage = replay_coverage(journal_path_on_disk) or []
        return call

    @classmethod
    def rebuild_finalized_ledger(
        cls,
        drain: CaptureDrain,
        sidecar: CaptureSidecar,
        *,
        journal_dir: Path,
        correlation_key_id: str | None = None,
    ) -> CaptureCall:
        """Restore the closed-call replay fence after sealing removed its journal."""

        if (
            not sidecar.sealed
            or not sidecar.finalized
            or sidecar.applied < 1
            or sidecar.retry_sequence is not None
        ):
            raise CaptureJournalUnavailableError(
                "the capture journal is missing without a complete finalized ledger"
            )
        call = cls(
            drain,
            journal_dir=journal_dir,
            correlation_key_id=correlation_key_id,
            call_metadata_bound=sidecar.call_metadata_bound,
        )
        call.writer.take_new()
        call._applied = sidecar.applied
        call._digests = OrderedDict(sorted(sidecar.digests.items()))
        call._digest_versions = OrderedDict(sorted(sidecar.digest_versions.items()))
        call._first_observed_ms = sidecar.first_observed_ms
        call._last_observed_ms = sidecar.last_observed_ms
        call._lossy = sidecar.lossy
        call._finalized = True
        call._retry_digest_version = sidecar.retry_digest_version
        call._identity_declared = True
        call.turn._sequence = sidecar.turn_sequence
        return call

    # -- sequencing ------------------------------------------------------------

    def _classify(self, drain: CaptureDrain) -> str:
        """Decide what to do with this drain, or refuse it. Caller holds the lock.

        Returns ``"apply"``, ``"apply_gap"`` (a declared loss), or ``"replay"``;
        raises a sequencing refusal otherwise.
        """

        sequence = drain.drain_sequence
        if not _matches_call_metadata(
            drain,
            bound=self._call_metadata_bound,
            digest=self._call_metadata_digest,
        ):
            raise CaptureSequenceConflictError(
                "a drain changes this call's trace or clock metadata",
                sequence=sequence,
            )
        if sequence <= self._applied:
            recorded = self._digests.get(sequence)
            digest_version = self._digest_versions.get(sequence, LEGACY_CAPTURE_DIGEST_VERSION)
            if recorded is not None and recorded == drain.digest(version=digest_version):
                return "replay"
            raise CaptureSequenceConflictError(
                "a drain rewrites a sequence this call already resolved",
                sequence=sequence,
            )
        if self._retry_sequence is not None and (
            sequence != self._retry_sequence or self._retry_digest != self._digest(drain)
        ):
            raise CaptureSequenceConflictError(
                "a new drain differs from the interrupted request",
                sequence=sequence,
            )
        if sequence == self._applied + 1:
            return "apply"
        # sequence > applied + 1: a gap. Only a declared loss covering exactly the
        # missing range may cross it; anything else is a gap the client must resync.
        resync = drain.resync
        if (
            resync is not None
            and resync.missed_through == sequence - 1
            and resync.missed_from <= self._applied + 1
        ):
            # The client can lose the response to a drain that the server already
            # committed. Accept an overlapping claim and clip its applied prefix
            # below; the server's durable sequence is authoritative.
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

    def _digest_version_for(self, drain: CaptureDrain) -> int:
        if self._retry_sequence == drain.drain_sequence and self._retry_digest_version is not None:
            return self._retry_digest_version
        return CURRENT_CAPTURE_DIGEST_VERSION

    def _digest(self, drain: CaptureDrain) -> str:
        return drain.digest(version=self._digest_version_for(drain))

    def _remember_digest(self, sequence: int, digest: str, version: int) -> None:
        self._digests[sequence] = digest
        self._digest_versions[sequence] = version
        while len(self._digests) > DIGEST_RING_SIZE:
            expired_sequence, _ = self._digests.popitem(last=False)
            self._digest_versions.pop(expired_sequence, None)

    def _remember_outcome(self, sequence: int, outcome: DrainOutcome) -> None:
        self._outcomes[sequence] = outcome
        while len(self._outcomes) > DIGEST_RING_SIZE:
            self._outcomes.popitem(last=False)

    def _persist_sidecar(self, sidecar: CaptureSidecar | None = None) -> None:
        """Write the sequencing ledger durably, or do nothing for memory-only calls."""

        if self._journal_dir is None:
            return
        if sidecar is None:
            sidecar = CaptureSidecar(
                project_id=self.project_id,
                call_key=self.call_key,
                applied=self._applied,
                digests=dict(self._digests),
                digest_versions=dict(self._digest_versions),
                turn_sequence=self.turn._sequence,
                first_observed_ms=self._first_observed_ms,
                last_observed_ms=self._last_observed_ms,
                lossy=self._lossy,
                finalized=self._finalized,
                correlation_key_id=self._correlation_key_id,
                call_metadata_bound=self._call_metadata_bound,
                call_metadata_digest=(
                    self._call_metadata_digest if self._call_metadata_bound else None
                ),
                retry_sequence=self._retry_sequence,
                retry_digest=self._retry_digest,
                retry_digest_version=self._retry_digest_version,
            )
        write_sidecar(self._journal_dir, sidecar)

    def _journal_size_before_drain(self) -> int:
        if self._durable_path is None:
            return 0
        try:
            return self._durable_path.stat().st_size
        except FileNotFoundError:
            return 0

    # -- projection ------------------------------------------------------------

    def process(self, drain: CaptureDrain, live: LiveSessionRegistry) -> DrainOutcome:
        """Sequence, project, journal and append one drain. Holds the call lock."""

        with self.lock:
            decision = self._classify(drain)
            # A retry of an already-applied drain -- including the endCall drain
            # itself -- is an idempotent no-op that echoes the stored outcome, even
            # after the call finalized. Only a genuinely NEW drain is refused once
            # the call was ended: the journal is closed and accepts no more facts.
            if decision == "replay":
                if drain.drain_sequence in self._outcomes:
                    return replace(self._outcomes[drain.drain_sequence], replay=True)
                # A replay of a drain applied before a restart: its outcome was not
                # persisted, so echo the call's current live state rather than a
                # stored one. It is still a replay -- nothing is re-journaled.
                return self._replay_outcome(live)
            if self._finalized:
                raise CaptureCallClosedError(
                    "this call was ended by an explicit endCall() and accepts no more drains"
                )

            # The pre-append sidecar stores the previous committed state plus an
            # intent. If a process dies at any point before the committed sidecar
            # is durable, startup truncates the journal to this byte offset and
            # requires the identical drain digest again.
            previous_observed = (self._first_observed_ms, self._last_observed_ms)
            previous_lossy = self._lossy
            previous_digests = self._digests.copy()
            previous_digest_versions = self._digest_versions.copy()
            previous_applied = self._applied
            previous_finalized = self._finalized
            previous_retry = (
                self._retry_sequence,
                self._retry_digest,
                self._retry_digest_version,
            )
            previous_turn_sequence = self.turn._sequence
            digest_version = self._digest_version_for(drain)
            digest = drain.digest(version=digest_version)
            journal_size = self._journal_size_before_drain()
            if self._journal_dir is not None:
                intent = CaptureSidecar(
                    project_id=self.project_id,
                    call_key=self.call_key,
                    applied=self._applied,
                    digests=dict(previous_digests),
                    digest_versions=dict(previous_digest_versions),
                    turn_sequence=previous_turn_sequence,
                    first_observed_ms=previous_observed[0],
                    last_observed_ms=previous_observed[1],
                    lossy=previous_lossy,
                    finalized=self._finalized,
                    correlation_key_id=self._correlation_key_id,
                    call_metadata_bound=self._call_metadata_bound,
                    call_metadata_digest=(
                        self._call_metadata_digest if self._call_metadata_bound else None
                    ),
                    pending_sequence=drain.drain_sequence,
                    pending_digest=digest,
                    pending_journal_size=journal_size,
                    pending_digest_version=digest_version,
                    retry_sequence=self._retry_sequence,
                    retry_digest=self._retry_digest,
                    retry_digest_version=self._retry_digest_version,
                )
                try:
                    self._persist_sidecar(intent)
                except OSError as error:
                    raise CaptureJournalUnavailableError(
                        "durable capture ownership metadata could not be persisted"
                    ) from error
                self._intent_pending = True

            # Settle the observed span and the loss flag for the projected drain.
            self._observe_samples(drain)
            if decision == "apply_gap" or self._restarted or _drain_declares_loss(drain):
                self._lossy = True

            if not self._identity_declared:
                self.turn.record_coverage(_IDENTITY_SIGNAL, "partial", _IDENTITY_REASON)
                self._identity_declared = True

            if self._restarted:
                # The interval spanning the restart cannot be differenced: the raw
                # snapshot the carry needs was never in the journal. Declare it
                # lost rather than estimate it, exactly as a drain gap does.
                self.turn.record_coverage(
                    _STATS_CONTINUITY_SIGNAL, "partial", _STATS_CONTINUITY_RESTART_REASON
                )
                self.carry = None
                self._restarted = False

            if decision == "apply_gap":
                assert drain.resync is not None
                first_missing = max(drain.resync.missed_from, self._applied + 1)
                lost = max(0, drain.resync.missed_through - first_missing + 1)
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
                # honestly, so the carry is dropped rather than resumed. A lost
                # drain is lost evidence: a call that ends after one is incomplete.
                self.carry = None

            self._journal_facts(drain)
            # An observed end rides its final drain: ``call_ended`` finalizes the
            # journal at the observed coordinate; an abandon reason records how far
            # the observer saw and keeps the call provisional. This is the only
            # place a finalize frame is ever written for a capture call.
            finalize = drain.end is not None and drain.end.finalizes
            if drain.end is not None:
                self._record_end(drain.end)
            entries = self.writer.take_new()

            # Prepare the committed ledger before the durable append reaches the
            # live registry. The registry invokes this callback after fsyncing the
            # frames and before updating list/SSE memory, so observers never see a
            # drain whose commit marker can still fail.
            self._applied = drain.drain_sequence
            self._remember_digest(drain.drain_sequence, digest, digest_version)
            self._finalized = finalize
            self._retry_sequence = None
            self._retry_digest = None
            self._retry_digest_version = None

            def commit_capture_ledger() -> None:
                if self._journal_dir is not None:
                    self._persist_sidecar()
                    self._intent_pending = False

            try:
                accepted = live.accept_records(
                    self.call_key,
                    entries,
                    project_id=self.project_id,
                    recovery_method=RECOVERY_METHOD,
                    recovery_reason=RECOVERY_REASON_SEALED,
                    durable_path=self._durable_path,
                    before_publish=(
                        commit_capture_ledger if self._journal_dir is not None else None
                    ),
                )
            except OSError as error:
                self._first_observed_ms, self._last_observed_ms = previous_observed
                self._lossy = previous_lossy
                self._digests = previous_digests
                self._digest_versions = previous_digest_versions
                self._applied = previous_applied
                self._finalized = previous_finalized
                (
                    self._retry_sequence,
                    self._retry_digest,
                    self._retry_digest_version,
                ) = previous_retry
                raise CaptureJournalUnavailableError(
                    "the durable capture journal could not append this drain"
                ) from error
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
                finalized=finalize,
            )
            self._remember_outcome(drain.drain_sequence, outcome)
            return outcome

    def _replay_outcome(self, live: LiveSessionRegistry) -> DrainOutcome:
        """Echo the call's current live state for a pre-restart drain's replay.

        A drain applied before a restart has no stored outcome to echo, but it is
        still a replay: its frames are already on disk, so nothing is re-journaled.
        The echo reports the live session as it stands now.
        """

        try:
            summary = live.summary(self.call_key, project_id=self.project_id)
            journal_id, accepted_through, state, sealable = (
                summary.journal_id,
                summary.last_sequence,
                summary.state,
                summary.sealable,
            )
        except Exception:  # pragma: no cover - the session exists whenever a call does
            journal_id = self.writer.status().journal_id or ""
            accepted_through = self._applied
            state = "finalized" if self._finalized else "live"
            sealable = False
        return DrainOutcome(
            call_id=self.call_key,
            journal_id=journal_id,
            accepted_through=accepted_through,
            accepted_records=0,
            state=state,
            sealable=sealable,
            replay=True,
            accepted_snapshots=0,
            accepted_device_events=0,
            accepted_coverage=0,
        )

    def _journal_facts(self, drain: CaptureDrain) -> None:
        """Record this drain's governed facts onto the persistent turn.

        The observed span and the loss flag are already settled by the caller
        before this journals anything, so the durable ledger it persisted reflects
        exactly what these facts will say.
        """

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

    def _observe_samples(self, drain: CaptureDrain) -> None:
        """Widen the observed browser-domain span to this drain's raw coordinates.

        Every sample's ``timestamp_ms`` is a raw browser reading in the call's
        clock domain. The earliest and latest across the whole call are the honest
        endpoints of the observed capture extent -- and the first is the coordinate
        the observed call duration is differenced from.
        """

        for sample in (*drain.snapshots, *drain.device_events):
            ts = sample.get("timestamp_ms")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            reading = float(ts)
            if self._first_observed_ms is None or reading < self._first_observed_ms:
                self._first_observed_ms = reading
            if self._last_observed_ms is None or reading > self._last_observed_ms:
                self._last_observed_ms = reading

    # -- end of call -----------------------------------------------------------

    def _record_end(self, end: CaptureEnd) -> None:
        """Handle a declared end: finalize on ``call_ended``, ledger the extent else.

        A ``call_ended`` is an observed close: it records the two browser-domain
        call bounds and the same-domain duration between them, notes who observed
        the close, and writes the journal's finalize frame at the observed end
        coordinate so the sealed artifact is final. Any other reason abandons the
        call -- the observer stopped, the call did not end -- so it records only how
        far the observer saw (the capture extent) and that the close was not
        observed, and writes no finalize frame, leaving the call provisional.
        """

        if end.finalizes:
            first = self._first_observed_ms
            if first is not None:
                self.turn.record_event(
                    _CLIENT_CAPTURE_STARTED,
                    at_ms=0.0,
                    source="browser",
                    confidence="inferred",
                    source_field=_DURATION_SOURCE_FIELD,
                    source_clock=self._browser_reading(first),
                )
            self.turn.record_event(
                _CLIENT_CALL_ENDED,
                at_ms=0.0,
                source="browser",
                confidence="inferred",
                source_field=_DURATION_SOURCE_FIELD,
                source_clock=self._browser_reading(end.timestamp_ms),
            )
            if first is not None and end.timestamp_ms >= first:
                self.turn.record_measurement(
                    _OBSERVED_CALL_DURATION,
                    end.timestamp_ms - first,
                    unit="ms",
                    source="browser",
                    confidence="inferred",
                    source_field=_DURATION_SOURCE_FIELD,
                    source_clock=self._browser_reading(end.timestamp_ms),
                )
            self.turn.record_coverage(_CALL_CLOSE_SIGNAL, "available", _CALL_CLOSE_REASON)
            status = _STATUS_COMPLETE if not self._lossy else _STATUS_INCOMPLETE
            self.session.recorder.finalize_journal(
                ended=self._browser_point(end.timestamp_ms),
                status=status,
            )
            return

        # An abandon: how far the observer saw, and that the close was not observed.
        first = self._first_observed_ms
        last = self._last_observed_ms
        if first is not None and last is not None and last >= first:
            self.turn.record_measurement(
                _OBSERVED_CAPTURE_EXTENT,
                last - first,
                unit="ms",
                source="browser",
                confidence="inferred",
                source_field=_DURATION_SOURCE_FIELD,
                source_clock=self._browser_reading(last),
            )
        self.turn.record_coverage(
            _CALL_DURATION_SIGNAL, "partial", _CALL_DURATION_UNOBSERVED_REASON
        )

    def _browser_reading(self, monotonic_ms: float) -> SourceClockReading:
        """A source-clock reading in the call's browser domain at ``monotonic_ms``."""

        return SourceClockReading(
            clock_domain_id=self._domain.clock_domain_id,
            monotonic_ms=monotonic_ms,
            uncertainty_nano=self._domain.uncertainty_nano,
            wall_origin_nano=self._domain.wall_origin_unix_nano,
        )

    def _browser_point(self, monotonic_ms: float) -> TimePoint:
        """The call's observed end coordinate as a browser-domain ``TimePoint``.

        Identical in shape to the coordinate every browser fact carries: the raw
        monotonic reading is domain-local, a browser-wall ``source_time_unix_nano``
        is derived only when the clock's wall origin is known, and nothing is ever
        rebased onto the server clock. This is exactly the endCall's observed
        coordinate -- never the last snapshot, never a timeout, never fabricated.
        """

        monotonic_nano = int(monotonic_ms * _NANOS_PER_MS)
        wall_origin = self._domain.wall_origin_unix_nano
        return TimePoint(
            source_time_unix_nano=(
                None if wall_origin is None else str(wall_origin + monotonic_nano)
            ),
            monotonic_time_nano=str(monotonic_nano),
            clock_domain_id=self._domain.clock_domain_id,
            uncertainty_nano=str(int(self._domain.uncertainty_nano)),
        )


def _namespace_client_coverage(signal: str) -> str:
    """Keep a client's coverage under its own prefix so it cannot mask a server one."""

    return signal if signal.startswith("browser.") else f"browser.{signal}"


def _drain_declares_loss(drain: CaptureDrain) -> bool:
    """Whether this drain admits it lost evidence, before any of it is journaled.

    A client coverage note that counted observations it dropped, or a server
    allowlist that withheld non-governed members, is loss rather than mere
    structure: a call that ends after one can never present as clean. Computed up
    front so the durable ledger persisted before journaling already records it.
    """

    if any(dropped is not None and dropped > 0 for _, _, _, dropped in drain.coverage):
        return True
    return any(count > 0 for _, _, count in drain.rejection_coverage)


def _matches_call_metadata(
    drain: CaptureDrain,
    *,
    bound: bool,
    digest: str | None,
) -> bool:
    """Compare stable call metadata when the sidecar can prove its first values."""

    return not bound or drain.call_metadata_digest() == digest


def _capture_journal_entries(
    journal_bytes: bytes,
    *,
    call_key: str,
    max_frame_bytes: int,
) -> list[object]:
    """Decode a complete durable call journal before any live replay or append."""

    scan = scan_frames(journal_bytes, max_body_bytes=max_frame_bytes)
    if not scan.frames or scan.torn_tail_bytes or scan.stop_reason is not None:
        raise ValueError("capture journal is not a complete frame sequence")
    try:
        entries = [decode_entry(frame.body) for frame in scan.frames]
    except JournalFormatError as error:
        raise ValueError("capture journal contains an unreadable entry") from error

    header = entries[0]
    expected_journal_id = hashlib.sha256(
        b"earshot.capture.journal:" + call_key.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:32]
    if (
        not isinstance(header, JournalOpen)
        or header.session_id != call_key
        or header.bundle_id != call_key
        or header.journal_id != expected_journal_id
    ):
        raise ValueError("capture journal header does not match its call")
    return entries


def _legacy_call_metadata_from_journal(
    journal_bytes: bytes,
    *,
    call_key: str,
    clock_domain_id: str,
    max_frame_bytes: int,
) -> tuple[int, tuple[str | None, str | None] | None]:
    """Recover metadata an older sidecar did not bind, from its bounded journal.

    Clock uncertainty is always in the call's declared clock-domain record. Trace
    context is present on captured event and operation records when those facts
    were emitted. A call with no such record has no durable trace evidence, so its
    first accepted post-migration drain establishes that context.
    """

    try:
        entries = _capture_journal_entries(
            journal_bytes,
            call_key=call_key,
            max_frame_bytes=max_frame_bytes,
        )
    except ValueError as error:
        raise ValueError(f"legacy {error}") from error

    uncertainties: set[int] = set()
    trace_pairs: set[tuple[str | None, str | None]] = set()
    for entry in entries[1:]:
        if isinstance(entry, JournalRecordEntry):
            value = entry.value or {}
            if entry.kind == "clock_domain" and value.get("clock_domain_id") == clock_domain_id:
                uncertainty = value.get("uncertainty_nano")
                if isinstance(uncertainty, str) and uncertainty.isdecimal():
                    uncertainties.add(int(uncertainty))
                elif isinstance(uncertainty, int) and not isinstance(uncertainty, bool):
                    uncertainties.add(uncertainty)
                else:
                    raise ValueError("legacy capture clock uncertainty is unavailable")
            if entry.kind in {"event", "operation"} and ("trace_id" in value or "span_id" in value):
                trace_id = value.get("trace_id")
                span_id = value.get("span_id")
                if (trace_id is None) != (span_id is None) or (
                    trace_id is not None
                    and (not isinstance(trace_id, str) or not isinstance(span_id, str))
                ):
                    raise ValueError("legacy capture trace context is invalid")
                trace_pairs.add((trace_id, span_id))
        elif isinstance(entry, JournalOperationOpen):
            trace_id = entry.trace_id
            span_id = entry.span_id
            if (trace_id is None) != (span_id is None):
                raise ValueError("legacy capture trace context is invalid")
            trace_pairs.add((trace_id, span_id))

    if len(uncertainties) != 1 or len(trace_pairs) > 1:
        raise ValueError("legacy capture call metadata is ambiguous")
    return uncertainties.pop(), next(iter(trace_pairs)) if trace_pairs else None


def _legacy_metadata_matches_durable_values(
    drain: CaptureDrain,
    *,
    uncertainty_nano: int,
    trace_pair: tuple[str | None, str | None] | None,
) -> bool:
    """Check the durable precision available for an unbound legacy call."""

    uncertainty_matches = int(drain.clock_uncertainty_ms * _NANOS_PER_MS) == uncertainty_nano
    trace_matches = trace_pair is None or (drain.trace_id, drain.span_id) == trace_pair
    return uncertainty_matches and trace_matches


class CaptureCallRegistry:
    """Every continuous browser call this server is currently accumulating.

    Keyed by ``(project_id, call_key)``. The call key folds in the authenticated
    project and client identity; the registry key independently enforces
    project-scoped lookup. Each project is bounded to a fixed number of
    concurrent calls, refused with :class:`CaptureCallCapacityError`, so one
    tenant cannot spend the whole server's capture budget. Durable calls also
    share a server-wide bound, so dormant journals cannot accumulate beyond the
    configured live-session count after repeated process restarts.
    """

    def __init__(
        self,
        live: LiveSessionRegistry,
        *,
        journal_dir: Path | str | None = None,
        max_calls_per_project: int = 16,
        max_durable_calls: int | None = None,
        max_sealed_replay_ledgers: int = DEFAULT_MAX_SEALED_REPLAY_LEDGERS,
        correlation_key_id: str | None = None,
    ) -> None:
        self._live = live
        # When set, every call journals its facts and its drain-sequencing ledger
        # here, so an in-flight call survives a backend restart. ``None`` keeps
        # calls purely in memory (a restart then drops them, as it did before).
        self._journal_dir = (
            None if journal_dir is None else Path(journal_dir).expanduser().resolve()
        )
        self._writer_lock_fd: int | None = None
        self._writer_lock_error: OSError | None = None
        self._correlation_key_id = correlation_key_id
        self._correlation_state_error: OSError | None = None
        self._hosted_inventory_error: OSError | None = None
        self._hosted_inventory_directory_identity: tuple[int, int] | None = None
        self._hosted_inventory_failed_identity: tuple[int, int] | None = None
        if self._journal_dir is not None:
            try:
                self._writer_lock_fd = acquire_writer_lock(self._journal_dir, create=False)
            except BlockingIOError as error:
                raise CaptureJournalWriterConflictError(str(error)) from error
            except OSError as error:
                # Keep unrelated API operations available so project deletion can
                # remain pending until its capture journals are safely removable.
                # Durable capture itself is fenced in drain() below.
                self._writer_lock_error = error
        self._max_per_project = max_calls_per_project
        self._max_durable_calls = (
            live.config.max_sessions if max_durable_calls is None else max_durable_calls
        )
        if (
            isinstance(self._max_durable_calls, bool)
            or not isinstance(self._max_durable_calls, int)
            or self._max_durable_calls < 1
        ):
            raise ValueError("max_durable_calls must be a positive integer")
        if (
            isinstance(max_sealed_replay_ledgers, bool)
            or not isinstance(max_sealed_replay_ledgers, int)
            or max_sealed_replay_ledgers < 1
        ):
            raise ValueError("max_sealed_replay_ledgers must be a positive integer")
        self._max_sealed_replay_ledgers = max_sealed_replay_ledgers
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], CaptureCall] = {}
        # Unfinalized durable calls remain capacity owners while they are
        # dormant after expiry or restart, even though they are absent from the
        # live registry and the in-memory call map.
        self._durable_call_keys: set[tuple[str, str]] = set()
        # Durable storage has one server-wide budget. Reservations cover the gap
        # between capacity admission and the first sidecar write; storage keys
        # keep dormant and unsealed calls counted after a process restart.
        self._durable_storage_keys: set[tuple[str, str]] = set()
        self._durable_reserved_keys: set[tuple[str, str]] = set()
        self._journal_limit_refusals: OrderedDict[tuple[str, str], tuple[int, str, int]] = (
            OrderedDict()
        )
        self._drain_locks: dict[tuple[str, str], tuple[threading.Lock, int]] = {}
        self._project_is_active: Callable[[str], bool] | None = None
        self._expiry_handler = self.expire_session
        if self._journal_dir is not None:
            self._live.set_capture_expiry_handler(self._expiry_handler)

    @property
    def durable_configured(self) -> bool:
        """Whether accepted calls can survive a process restart."""

        return self._journal_dir is not None

    @property
    def durable_available(self) -> bool:
        """Whether the configured journal and its correlation identity are usable."""

        if self._journal_dir is None or not self._ensure_writer_lock():
            return False
        if self._hosted_inventory_error is not None:
            self._refresh_hosted_inventory(force=True)
        return self._correlation_state_error is None

    def has_call_ownership(self, project_id: str, call_key: str) -> bool:
        """Whether in-memory or durable state still owns this call identity."""

        key = (project_id, call_key)
        with self._lock:
            if (
                key in self._calls
                or key in self._durable_storage_keys
                or key in self._durable_reserved_keys
            ):
                return True
        if self._journal_dir is None:
            return False
        try:
            sidecar_path(self._journal_dir, call_key).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # Let the normal durable recovery path turn uncertain ownership into
            # a retryable error; treating it as absent could reuse the call ID.
            return True
        return True

    def close(self) -> None:
        """Release this process's exclusive journal-directory lease."""

        if self._journal_dir is not None:
            self._live.set_capture_expiry_handler(None)
        descriptor = self._writer_lock_fd
        self._writer_lock_fd = None
        if descriptor is not None:
            with contextlib.suppress(OSError):
                release_writer_lock(descriptor)

    def _ensure_writer_lock(self) -> bool:
        """Acquire the configured lease, retrying after storage is repaired."""

        if self._journal_dir is None:
            return True
        if self._writer_lock_fd is not None:
            try:
                directory_stat = self._journal_dir.lstat()
                lock_stat = (self._journal_dir / WRITER_LOCK_NAME).lstat()
                descriptor_stat = os.fstat(self._writer_lock_fd)
            except OSError as error:
                self._writer_lock_error = error
                self.close()
                return False
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or not stat.S_ISREG(lock_stat.st_mode)
                or (lock_stat.st_dev, lock_stat.st_ino)
                != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            ):
                self._writer_lock_error = OSError(
                    "capture journal directory no longer matches its writer lease"
                )
                self.close()
                return False
            self._writer_lock_error = None
            self._refresh_hosted_inventory()
            return True
        try:
            self._writer_lock_fd = acquire_writer_lock(self._journal_dir, create=False)
        except OSError as error:
            self._writer_lock_error = error
            return False
        self._writer_lock_error = None
        self._refresh_hosted_inventory()
        return True

    def _refresh_hosted_inventory(self, *, force: bool = False) -> None:
        """Recheck ownership when the configured journal directory changes."""

        if self._journal_dir is None or self._correlation_key_id is None:
            return
        try:
            directory_stat = self._journal_dir.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise OSError("hosted capture journal path is not a real directory")
            identity = (directory_stat.st_dev, directory_stat.st_ino)
        except OSError as error:
            self._hosted_inventory_error = error
            self._correlation_state_error = error
            self._hosted_inventory_directory_identity = None
            self._hosted_inventory_failed_identity = None
            return
        if not force and identity == self._hosted_inventory_directory_identity:
            return
        if not force and identity == self._hosted_inventory_failed_identity:
            return
        try:
            self._validate_hosted_journal_inventory()
        except OSError as error:
            self._hosted_inventory_error = error
            self._correlation_state_error = error
            self._hosted_inventory_directory_identity = None
            self._hosted_inventory_failed_identity = identity
            return
        prior_inventory_error = self._hosted_inventory_error
        self._hosted_inventory_error = None
        if self._correlation_state_error is prior_inventory_error:
            self._correlation_state_error = None
        self._hosted_inventory_directory_identity = identity
        self._hosted_inventory_failed_identity = None

    def _fence_hosted_inventory(self, error: OSError) -> None:
        """Fence new hosted writes when one call's durable ownership is uncertain."""

        if self._correlation_key_id is None:
            return
        self._hosted_inventory_error = error
        self._correlation_state_error = error
        self._hosted_inventory_directory_identity = None
        self._hosted_inventory_failed_identity = None

    def _retain_drain_lock(self, key: tuple[str, str]) -> threading.Lock:
        """Keep a per-call transaction lock alive through waiters and recovery."""

        with self._lock:
            entry = self._drain_locks.get(key)
            lock, users = (threading.Lock(), 0) if entry is None else entry
            self._drain_locks[key] = (lock, users + 1)
            return lock

    def _release_drain_lock(self, key: tuple[str, str], lock: threading.Lock) -> None:
        """Drop an idle per-call lock without racing a queued drain."""

        with self._lock:
            entry = self._drain_locks.get(key)
            if entry is None or entry[0] is not lock:
                return
            if entry[1] == 1:
                self._drain_locks.pop(key)
            else:
                self._drain_locks[key] = (lock, entry[1] - 1)

    def _remember_journal_limit_refusal(
        self, key: tuple[str, str], drain: CaptureDrain, *, digest_version: int
    ) -> None:
        """Cache a permanent retry result for an active, already-full call."""

        with self._lock:
            self._journal_limit_refusals[key] = (
                drain.drain_sequence,
                drain.digest(version=digest_version),
                digest_version,
            )
            self._journal_limit_refusals.move_to_end(key)
            while len(self._journal_limit_refusals) > self._live.config.max_sessions:
                self._journal_limit_refusals.popitem(last=False)

    def _refuse_cached_journal_limit(self, drain: CaptureDrain) -> None:
        """Avoid rereading a full journal for an exact retry already refused."""

        from ..live import LiveCaptureJournalLimitError

        key = (drain.project_id, drain.call_key)
        with self._lock:
            refusal = self._journal_limit_refusals.get(key)
        if refusal is None:
            return
        if not self._live.contains(drain.call_key, project_id=drain.project_id):
            with self._lock:
                if self._journal_limit_refusals.get(key) == refusal:
                    refusal = self._journal_limit_refusals.get(key)
                    if refusal is not None and drain.drain_sequence >= refusal[0]:
                        self._journal_limit_refusals.pop(key, None)
            return
        sequence, digest, digest_version = refusal
        if drain.drain_sequence != sequence:
            return
        with self._lock:
            if self._journal_limit_refusals.get(key) == refusal:
                self._journal_limit_refusals.move_to_end(key)
        if drain.digest(version=digest_version) == digest:
            raise LiveCaptureJournalLimitError(
                "capture journal would exceed its configured byte limit"
            )
        with self._lock:
            if self._journal_limit_refusals.get(key) == refusal:
                self._journal_limit_refusals.pop(key, None)

    def _validate_hosted_journal_inventory(self) -> None:
        """Reject hosted recovery when durable capture ownership is incomplete."""

        if self._journal_dir is None or self._correlation_key_id is None:
            return
        try:
            entries = tuple(self._journal_dir.iterdir())
        except OSError as error:
            raise OSError("hosted capture journal inventory cannot be read") from error

        sidecars: dict[str, CaptureSidecar] = {}
        journal_stems: set[str] = set()
        for path in entries:
            name = path.name
            if name == WRITER_LOCK_NAME:
                continue
            if name.endswith(f"{SIDECAR_SUFFIX}.tmp"):
                raise OSError("hosted capture journal contains an unresolved sidecar write")
            if name.endswith(SIDECAR_SUFFIX):
                try:
                    file_stat = path.lstat()
                except OSError as error:
                    raise OSError("hosted capture sidecar cannot be inspected") from error
                if not stat.S_ISREG(file_stat.st_mode):
                    raise OSError("hosted capture sidecar is not a regular file")
                sidecar = read_sidecar(path)
                if (
                    sidecar is None
                    or sidecar.correlation_key_id != self._correlation_key_id
                    or sidecar_path(self._journal_dir, sidecar.call_key) != path
                ):
                    raise OSError("hosted capture sidecar identity cannot be proven")
                sidecars[name.removesuffix(SIDECAR_SUFFIX)] = sidecar
                continue
            if name.endswith(JOURNAL_SUFFIX):
                try:
                    file_stat = path.lstat()
                except OSError as error:
                    raise OSError("hosted capture journal cannot be inspected") from error
                if not stat.S_ISREG(file_stat.st_mode):
                    raise OSError("hosted capture journal is not a regular file")
                journal_stems.add(name.removesuffix(JOURNAL_SUFFIX))

        if journal_stems.difference(sidecars):
            raise OSError("hosted capture journal has no attributable sidecar")
        for stem, sidecar in sidecars.items():
            if stem in journal_stems or sidecar.sealed:
                continue
            if sidecar.applied > 0 or (
                sidecar.pending_journal_size is not None and sidecar.pending_journal_size > 0
            ):
                raise OSError("hosted capture sidecar refers to a missing journal")

    def _read_journal(self, path: Path) -> bytes:
        """Read one regular capture journal without exceeding the configured cap."""

        maximum = self._live.config.max_capture_journal_bytes
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("durable capture journal is not a regular file")
            if metadata.st_size > maximum:
                raise _CaptureJournalTooLargeError(
                    "durable capture journal exceeds its configured byte limit"
                )

            chunks: list[bytes] = []
            total = 0
            while total <= maximum:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > maximum:
                raise _CaptureJournalTooLargeError(
                    "durable capture journal exceeds its configured byte limit"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def __del__(self) -> None:
        # Application lifespan is the normal release path. This is a fallback for
        # direct registry users that discard an instance without a close call.
        self.close()

    def rebuild_from_disk(
        self,
        *,
        project_is_active: Callable[[str], bool] | None = None,
    ) -> None:
        """Restore active calls and count expired calls without reattaching them.

        Reads the durable ledgers written before the process died and replays each
        call's journal back into a live session, so an in-flight call is tailable,
        sealable, and -- crucially -- **provisional** immediately after a restart,
        never fabricating a close. Calls already expired before shutdown stay
        dormant and count against their project's call budget; an exact retry
        reattaches one lazily without reviving stale work at startup.
        """

        from ..live import LiveError

        if self._journal_dir is None or not self._ensure_writer_lock():
            return
        self._project_is_active = project_is_active
        if self._correlation_key_id is not None:
            self._refresh_hosted_inventory(force=True)
            if self._hosted_inventory_error is not None:
                return
            # Retry any previously interrupted sealed-journal cleanup below.
            self._correlation_state_error = None
        with self._lock:
            self._durable_call_keys.clear()
            self._durable_storage_keys.clear()
            self._durable_reserved_keys.clear()
        for ledger in iter_sidecars(self._journal_dir):
            sidecar = read_sidecar(ledger)
            if sidecar is None:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = OSError(
                        "a hosted capture ledger is unreadable; correlation cannot be proven"
                    )
                continue
            key = (sidecar.project_id, sidecar.call_key)
            journal = journal_path(self._journal_dir, sidecar.call_key)
            if not sidecar.sealed:
                with self._lock:
                    self._durable_storage_keys.add(key)
            else:
                try:
                    journal.lstat()
                except FileNotFoundError:
                    pass
                except OSError:
                    with self._lock:
                        self._durable_storage_keys.add(key)
                else:
                    with self._lock:
                        self._durable_storage_keys.add(key)
            if sidecar.correlation_key_id != self._correlation_key_id:
                self._correlation_state_error = OSError(
                    "capture ledger uses a different instance correlation key"
                )
                continue
            if sidecar.sealed:
                # The durable seal marker prevents resurrection. A later startup
                # or project deletion can retry physical cleanup.
                try:
                    remove_sealed_journal(self._journal_dir, sidecar.call_key)
                except OSError as error:
                    if self._correlation_key_id is not None:
                        self._correlation_state_error = error
                else:
                    with self._lock:
                        self._durable_storage_keys.discard(key)
                with self._lock:
                    self._durable_call_keys.discard(key)
                continue
            if project_is_active is not None and not project_is_active(sidecar.project_id):
                continue
            if not sidecar.finalized:
                with self._lock:
                    self._durable_call_keys.add(key)
            try:
                sidecar = recover_pending_sidecar(self._journal_dir, sidecar)
            except OSError:
                # Keep the project attributable and fenced from a fresh call. Its
                # next retry can report the unavailable journal without exposing
                # an uncommitted prefix as accepted evidence.
                continue
            try:
                frames = self._read_journal(journal)
            except _CaptureJournalTooLargeError as error:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = error
                # Keep the attributable files intact so project deletion can
                # finish; lazy recovery will return a retryable unavailable error.
                continue
            except OSError:
                # The first interrupted drain has no prior journal and keeps its
                # digest as an exact-retry fence. Applied state without a journal
                # is corruption: preserve the ledger for deletion accounting and
                # refuse to replace it with a new call.
                if sidecar.applied == 0 and sidecar.retry_sequence is None:
                    with contextlib.suppress(OSError):
                        ledger.unlink()
                continue
            try:
                _capture_journal_entries(
                    frames,
                    call_key=sidecar.call_key,
                    max_frame_bytes=self._live.config.max_frame_bytes,
                )
            except ValueError:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = OSError(
                        "a hosted capture journal cannot be replayed safely"
                    )
                continue
            if sidecar.expired:
                # Keep the durable call available for its exact retry without
                # consuming live-session capacity merely because the server
                # restarted. _rebuild_call reattaches it lazily on that retry.
                continue
            try:
                self._live.rebuild_capture_session(
                    sidecar.call_key,
                    frames,
                    journal,
                    project_id=sidecar.project_id,
                    recovery_method=RECOVERY_METHOD,
                    recovery_reason=RECOVERY_REASON_SEALED,
                )
            except LiveError:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = OSError(
                        "a hosted capture journal cannot be replayed safely"
                    )
        try:
            with self._lock:
                prune_sealed_sidecars(
                    self._journal_dir,
                    max_count=self._max_sealed_replay_ledgers,
                    protected_call_keys=frozenset(key[1] for key in self._drain_locks),
                )
        except OSError as error:
            if self._correlation_key_id is not None:
                self._correlation_state_error = error

    def drop_project(self, project_id: str) -> bool:
        """Remove this project's in-memory calls and owned durable call journals."""

        with self._lock:
            for key in tuple(self._calls):
                if key[0] == project_id:
                    self._calls.pop(key, None)
            for key in tuple(self._journal_limit_refusals):
                if key[0] == project_id:
                    self._journal_limit_refusals.pop(key, None)
        if self._journal_dir is None:
            return True
        if not self._ensure_writer_lock():
            return False
        try:
            directory_stat = self._journal_dir.lstat()
        except FileNotFoundError:
            # The configured volume may have been removed or unmounted. The open
            # file descriptor cannot prove that the named durable store is empty.
            return False
        except OSError:
            return False
        if not stat.S_ISDIR(directory_stat.st_mode):
            return False

        complete = True
        try:
            with os.scandir(self._journal_dir) as entries:
                paths = tuple(self._journal_dir / entry.name for entry in entries)
        except OSError:
            return False
        ledgers = tuple(path for path in paths if path.name.endswith(SIDECAR_SUFFIX))
        journals = tuple(path for path in paths if path.name.endswith(JOURNAL_SUFFIX))
        temporary_ledgers = tuple(
            path for path in paths if path.name.endswith(f"{SIDECAR_SUFFIX}.tmp")
        )
        ledger_stems = {ledger.name.removesuffix(SIDECAR_SUFFIX) for ledger in ledgers}
        for temporary in temporary_ledgers:
            sidecar = read_sidecar(temporary)
            if sidecar is None:
                complete = False
                continue
            expected_temporary_name = (
                sidecar_path(self._journal_dir, sidecar.call_key).name + ".tmp"
            )
            if temporary.name != expected_temporary_name:
                complete = False
                continue
            ledger_stems.add(temporary.name.removesuffix(f"{SIDECAR_SUFFIX}.tmp"))
            if sidecar.project_id != project_id:
                continue
            try:
                journal_path(self._journal_dir, sidecar.call_key).unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
            except OSError:
                complete = False
        for ledger in ledgers:
            sidecar = read_sidecar(ledger)
            if sidecar is None:
                complete = False
                continue
            if sidecar_path(self._journal_dir, sidecar.call_key) != ledger:
                complete = False
                continue
            if sidecar.project_id != project_id:
                continue
            journal = journal_path(self._journal_dir, sidecar.call_key)
            try:
                journal.unlink(missing_ok=True)
                ledger.unlink(missing_ok=True)
            except OSError:
                complete = False
        for journal in journals:
            if journal.name.removesuffix(JOURNAL_SUFFIX) not in ledger_stems:
                complete = False
        try:
            descriptor = os.open(self._journal_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            complete = False
        if complete and self._correlation_state_error is not None:
            self.rebuild_from_disk(project_is_active=self._project_is_active)
        if complete:
            with self._lock:
                self._durable_call_keys = {
                    key for key in self._durable_call_keys if key[0] != project_id
                }
                self._durable_storage_keys = {
                    key for key in self._durable_storage_keys if key[0] != project_id
                }
                self._durable_reserved_keys = {
                    key for key in self._durable_reserved_keys if key[0] != project_id
                }
        return complete

    def drain(
        self,
        drain: CaptureDrain,
        *,
        bundle_identity_exists: Callable[[str, str], bool] | None = None,
    ) -> DrainOutcome:
        """Accept one drain of one call. Thread-safe; drains of a call serialize."""

        key = (drain.project_id, drain.call_key)
        transaction_lock = self._retain_drain_lock(key)
        try:
            with transaction_lock:
                if (
                    bundle_identity_exists is not None
                    and not self.has_call_ownership(drain.project_id, drain.call_key)
                    and bundle_identity_exists(drain.project_id, drain.call_key)
                ):
                    raise CaptureCallReplayExpiredError(
                        "the retained replay window for this completed capture call has expired"
                    )
                return self._drain_serialized(drain)
        finally:
            self._release_drain_lock(key, transaction_lock)

    def _drain_serialized(self, drain: CaptureDrain) -> DrainOutcome:
        """Run lookup, append, and recovery as one same-call transaction."""

        from ..live import LiveCaptureJournalLimitError

        self._refuse_cached_journal_limit(drain)
        if self._correlation_state_error is not None:
            raise CaptureJournalUnavailableError(
                "capture journal identity cannot be safely resumed"
            ) from self._correlation_state_error
        if not self._ensure_writer_lock():
            raise CaptureJournalUnavailableError(
                "the durable capture directory could not be exclusively opened"
            ) from self._writer_lock_error
        call = self._call_for(drain)
        try:
            outcome = call.process(drain, self._live)
            if self._journal_dir is not None:
                with self._lock:
                    key = (drain.project_id, drain.call_key)
                    # A successful replacement resolved the sequence that may
                    # previously have been refused for size. The cached 413 must
                    # no longer mask the now-authoritative sequence conflict.
                    self._journal_limit_refusals.pop(key, None)
                    self._durable_reserved_keys.discard(key)
                    self._durable_storage_keys.add(key)
                    if call.finalized:
                        self._durable_call_keys.discard(key)
                    else:
                        self._durable_call_keys.add(key)
            return outcome
        except Exception as error:
            if self._journal_dir is not None:
                ledger = sidecar_path(self._journal_dir, drain.call_key)
                sidecar = read_sidecar(ledger)
                persisted_sidecar = sidecar
                key = (drain.project_id, drain.call_key)
                pending = sidecar is not None and sidecar.pending_sequence is not None
                committed_before_failure = (
                    sidecar is not None
                    and sidecar.pending_sequence is None
                    and sidecar.applied == drain.drain_sequence
                    and sidecar.digests.get(drain.drain_sequence) == call._digest(drain)
                )
                if call._intent_pending or pending or committed_before_failure:
                    preserve_committed_prefix = False
                    discarded_first_drain = False
                    try:
                        if pending:
                            assert sidecar is not None
                            recovered = recover_pending_sidecar(self._journal_dir, sidecar)
                            persisted_sidecar = recovered
                            if isinstance(error, LiveCaptureJournalLimitError):
                                if recovered.applied == 0:
                                    discard_rejected_first_drain(self._journal_dir, recovered)
                                    persisted_sidecar = None
                                    discarded_first_drain = True
                                else:
                                    # A 413 proves this append never reached the
                                    # live session. Keep the accepted prefix and
                                    # let the client replace this unapplied body;
                                    # only ambiguous interrupted writes require an
                                    # exact digest retry.
                                    cleared_retry = replace(
                                        recovered,
                                        retry_sequence=None,
                                        retry_digest=None,
                                        retry_digest_version=None,
                                    )
                                    write_sidecar(self._journal_dir, cleared_retry)
                                    persisted_sidecar = cleared_retry
                                    preserve_committed_prefix = self._live.contains(
                                        drain.call_key, project_id=drain.project_id
                                    )
                                    if preserve_committed_prefix:
                                        self._remember_journal_limit_refusal(
                                            key,
                                            drain,
                                            digest_version=call._digest_version_for(drain),
                                        )
                    except OSError as recovery_error:
                        if isinstance(error, LiveCaptureJournalLimitError):
                            raise CaptureJournalUnavailableError(
                                "the rejected capture drain could not be safely released"
                            ) from recovery_error
                        raise
                    finally:
                        if not preserve_committed_prefix:
                            self._live.rollback_capture_session(
                                drain.call_key,
                                project_id=drain.project_id,
                                durable_path=journal_path(self._journal_dir, drain.call_key),
                            )
                        with self._lock:
                            self._durable_reserved_keys.discard(key)
                            if discarded_first_drain:
                                self._durable_storage_keys.discard(key)
                                self._durable_call_keys.discard(key)
                            elif persisted_sidecar is not None and not persisted_sidecar.sealed:
                                self._durable_storage_keys.add(key)
                                if persisted_sidecar.finalized:
                                    self._durable_call_keys.discard(key)
                                else:
                                    self._durable_call_keys.add(key)
                            else:
                                self._durable_storage_keys.discard(key)
                                self._durable_call_keys.discard(key)
                            if self._calls.get(key) is call:
                                self._calls.pop(key, None)
                else:
                    with self._lock:
                        self._durable_reserved_keys.discard(key)
                        if sidecar is None:
                            self._durable_storage_keys.discard(key)
                            self._durable_call_keys.discard(key)
                        elif sidecar.sealed:
                            self._durable_call_keys.discard(key)
                        else:
                            self._durable_storage_keys.add(key)
                            if sidecar.finalized:
                                self._durable_call_keys.discard(key)
                            else:
                                self._durable_call_keys.add(key)
            raise

    def _call_for(self, drain: CaptureDrain) -> CaptureCall:
        key = (drain.project_id, drain.call_key)
        with self._lock:
            call = self._calls.get(key)
            if call is not None:
                # A finalized call is kept so a drain arriving after its explicit
                # end is refused rather than silently starting a second call under
                # the same key. A still-live call appends. Its expired durable
                # ledger is rebuilt below for an exact retry; a missing ledger
                # (memory-only or superseded) starts afresh.
                if call.finalized or self._live.contains(
                    drain.call_key, project_id=drain.project_id
                ):
                    return call
                self._calls.pop(key, None)

            # Not in memory. If a durable ledger survived a restart, rebuild the
            # call from it so a continuing drain resumes cleanly instead of a fresh
            # call that would refuse its in-sequence drain as a gap.
            try:
                rebuilt = self._rebuild_call(drain)
            except CaptureJournalUnavailableError:
                if self._correlation_state_error is None:
                    self._fence_hosted_inventory(
                        OSError("hosted capture ownership could not be safely recovered")
                    )
                raise
            if rebuilt is not None:
                self._calls[key] = rebuilt
                return rebuilt
            if self._journal_dir is not None:
                durable_keys = self._durable_storage_keys | self._durable_reserved_keys
                if key not in durable_keys and len(durable_keys) >= self._max_durable_calls:
                    raise CaptureCallCapacityError(
                        "the server is carrying as many durable capture calls as it will"
                    )
            self._prune_finalized(drain.project_id)
            owned_keys = {key for key in self._calls if key[0] == drain.project_id}
            owned_keys.update(key for key in self._durable_call_keys if key[0] == drain.project_id)
            owned = len(owned_keys)
            if owned >= self._max_per_project:
                raise CaptureCallCapacityError(
                    "this project is carrying as many continuous calls as it will"
                )
            call = CaptureCall(
                drain,
                journal_dir=self._journal_dir,
                correlation_key_id=self._correlation_key_id,
            )
            self._calls[key] = call
            if self._journal_dir is not None:
                self._durable_reserved_keys.add(key)
            return call

    def mark_sealed(self, project_id: str, call_key: str) -> None:
        """Persist that a finalized call's artifact was accepted before unlinking it."""

        if self._journal_dir is None:
            return
        key = (project_id, call_key)
        transaction_lock = self._retain_drain_lock(key)
        try:
            with transaction_lock:
                self._mark_sealed_serialized(project_id, call_key)
        finally:
            self._release_drain_lock(key, transaction_lock)

    def _mark_sealed_serialized(self, project_id: str, call_key: str) -> None:
        """Commit and compact one finalized call while its drain lock is held."""

        if not self._ensure_writer_lock():
            raise CaptureJournalUnavailableError(
                "the durable capture directory could not be exclusively opened"
            ) from self._writer_lock_error
        ledger = sidecar_path(self._journal_dir, call_key)
        try:
            ledger_stat = ledger.lstat()
        except OSError as error:
            raise CaptureJournalUnavailableError(
                "the finalized capture ledger could not be inspected"
            ) from error
        if not stat.S_ISREG(ledger_stat.st_mode):
            raise CaptureJournalUnavailableError(
                "the finalized capture ledger is not a regular file"
            )
        sidecar = read_sidecar(ledger)
        if (
            sidecar is None
            or sidecar.call_key != call_key
            or sidecar.project_id != project_id
            or not sidecar.finalized
            or sidecar.pending_sequence is not None
            or sidecar.retry_sequence is not None
        ):
            raise CaptureJournalUnavailableError(
                "the finalized capture ledger is not in a sealable state"
            )
        if not sidecar.sealed:
            try:
                write_sidecar(
                    self._journal_dir,
                    replace(sidecar, sealed=True, expired=False),
                )
            except OSError as error:
                raise CaptureJournalUnavailableError(
                    "the sealed capture acknowledgment could not be persisted"
                ) from error
        try:
            remove_sealed_journal(self._journal_dir, call_key)
        except OSError as error:
            raise CaptureJournalUnavailableError(
                "the sealed capture journal could not be removed"
            ) from error
        with self._lock:
            self._calls.pop((project_id, call_key), None)
            self._durable_call_keys.discard((project_id, call_key))
            self._durable_storage_keys.discard((project_id, call_key))
            self._durable_reserved_keys.discard((project_id, call_key))
            try:
                prune_sealed_sidecars(
                    self._journal_dir,
                    max_count=self._max_sealed_replay_ledgers,
                    protected_call_keys=frozenset(
                        key[1] for key in self._drain_locks if key != (project_id, call_key)
                    ),
                )
            except OSError as error:
                raise CaptureJournalUnavailableError(
                    "sealed capture replay retention could not be enforced"
                ) from error

    def expire_session(self, project_id: str, call_key: str) -> bool:
        """Persist a call's dormant state before the live registry drops it."""

        from ..live import END_SESSION_EXPIRED, STATE_ABANDONED, SessionNotLiveError

        key = (project_id, call_key)
        transaction_lock = self._retain_drain_lock(key)
        try:
            with transaction_lock:
                try:
                    summary = self._live.summary(call_key, project_id=project_id)
                except SessionNotLiveError:
                    return True
                if summary.state != STATE_ABANDONED:
                    return True
                if not self._ensure_writer_lock():
                    raise OSError("capture ownership cannot be persisted before expiry")
                assert self._journal_dir is not None
                ledger = sidecar_path(self._journal_dir, call_key)
                try:
                    metadata = ledger.lstat()
                except OSError as error:
                    raise OSError("expired capture ownership ledger is unavailable") from error
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError("expired capture ownership ledger is not a regular file")
                sidecar = read_sidecar(ledger)
                if (
                    sidecar is None
                    or sidecar.project_id != project_id
                    or sidecar.call_key != call_key
                    or sidecar.correlation_key_id != self._correlation_key_id
                    or sidecar.finalized
                    or sidecar.sealed
                ):
                    raise OSError("expired capture ownership cannot be proven")
                sidecar = recover_pending_sidecar(self._journal_dir, sidecar)
                if not sidecar.expired:
                    write_sidecar(self._journal_dir, replace(sidecar, expired=True))
                self._live.drop_session(
                    call_key,
                    reason=END_SESSION_EXPIRED,
                    project_id=project_id,
                )
                with self._lock:
                    self._durable_call_keys.add(key)
                return True
        finally:
            self._release_drain_lock(key, transaction_lock)

    def _rebuild_call(self, drain: CaptureDrain) -> CaptureCall | None:
        """Reconstruct a call from its durable ledger, or ``None`` if there is none.

        Caller holds the registry lock. The live session is rebuilt from disk if a
        restart (or an expiry) dropped it, and whether the call is already finalized
        is read from the journal's finalize frame -- never fabricated -- so a drain
        after a durably-observed close is still refused.
        """

        if self._journal_dir is None:
            return None
        from ..live import LiveError, SessionNotLiveError

        ledger = sidecar_path(self._journal_dir, drain.call_key)
        journal = journal_path(self._journal_dir, drain.call_key)
        temporary_ledger = ledger.with_name(ledger.name + ".tmp")
        try:
            ledger_stat = ledger.lstat()
            ledger_exists = True
        except FileNotFoundError:
            ledger_stat = None
            ledger_exists = False
        except OSError as error:
            raise CaptureJournalUnavailableError(
                "the capture ownership ledger could not be inspected"
            ) from error
        if ledger_stat is not None and not stat.S_ISREG(ledger_stat.st_mode):
            raise CaptureJournalUnavailableError(
                "the capture ownership ledger is not a regular file"
            )
        sidecar = read_sidecar(ledger) if ledger_exists else None
        if sidecar is None:
            for path in (journal, temporary_ledger):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise CaptureJournalUnavailableError(
                        "the capture ownership files could not be inspected"
                    ) from error
                raise CaptureJournalUnavailableError(
                    "durable capture files exist without a valid ownership ledger"
                )
            if ledger_exists:
                raise CaptureJournalUnavailableError(
                    "the capture ownership ledger is unreadable or corrupt"
                )
            return None
        if sidecar.call_key != drain.call_key or sidecar.project_id != drain.project_id:
            raise CaptureJournalUnavailableError(
                "the capture ownership ledger does not match this project and call"
            )
        if sidecar.correlation_key_id != self._correlation_key_id:
            self._correlation_state_error = OSError(
                "capture ledger uses a different instance correlation key"
            )
            raise CaptureJournalUnavailableError(
                "capture journal identity cannot be safely resumed"
            ) from self._correlation_state_error
        if sidecar.sealed:
            try:
                journal.lstat()
            except FileNotFoundError:
                self._durable_storage_keys.discard((sidecar.project_id, sidecar.call_key))
            except OSError:
                self._durable_storage_keys.add((sidecar.project_id, sidecar.call_key))
            else:
                self._durable_storage_keys.add((sidecar.project_id, sidecar.call_key))
            self._durable_call_keys.discard((sidecar.project_id, sidecar.call_key))
        else:
            self._durable_storage_keys.add((sidecar.project_id, sidecar.call_key))
            if not sidecar.finalized:
                self._durable_call_keys.add((sidecar.project_id, sidecar.call_key))
        self._preflight_sidecar_drain(drain, sidecar)
        if sidecar.sealed:
            if not sidecar.call_metadata_bound:
                # A sealed ledger has no journal from which to recover its old
                # metadata. Replays cannot change the completed artifact, so pin
                # the first verified replay's values for all later retries.
                sidecar = replace(
                    sidecar,
                    call_metadata_bound=True,
                    call_metadata_digest=drain.call_metadata_digest(),
                )
                try:
                    write_sidecar(self._journal_dir, sidecar)
                except OSError as error:
                    raise CaptureJournalUnavailableError(
                        "legacy capture metadata could not be migrated"
                    ) from error
            return CaptureCall.rebuild_finalized_ledger(
                drain,
                sidecar,
                journal_dir=self._journal_dir,
                correlation_key_id=self._correlation_key_id,
            )
        try:
            sidecar = recover_pending_sidecar(self._journal_dir, sidecar)
        except OSError as error:
            raise CaptureJournalUnavailableError(
                "the pending capture journal transaction could not be recovered"
            ) from error
        if sidecar.applied == 0 and sidecar.retry_sequence is not None:
            # No accepted journal facts exist yet. Preserve the legacy sequence
            # fence, and bind metadata from the first drain that is actually
            # accepted after this restart.
            call = CaptureCall(
                drain,
                journal_dir=self._journal_dir,
                correlation_key_id=self._correlation_key_id,
                call_metadata_bound=True,
            )
            call._retry_sequence = sidecar.retry_sequence
            call._retry_digest = sidecar.retry_digest
            call._retry_digest_version = sidecar.retry_digest_version
            return call
        try:
            frames = self._read_journal(journal)
        except _CaptureJournalTooLargeError as error:
            raise CaptureJournalUnavailableError(
                "the durable capture journal exceeds its configured byte limit"
            ) from error
        except FileNotFoundError as error:
            if sidecar.applied > 0:
                raise CaptureJournalUnavailableError(
                    "the committed capture journal is missing"
                ) from error
            if sidecar.retry_sequence is None:
                return None
            call = CaptureCall(
                drain,
                journal_dir=self._journal_dir,
                correlation_key_id=self._correlation_key_id,
                call_metadata_bound=sidecar.call_metadata_bound,
            )
            call._retry_sequence = sidecar.retry_sequence
            call._retry_digest = sidecar.retry_digest
            call._retry_digest_version = sidecar.retry_digest_version
            return call
        except OSError as error:
            raise CaptureJournalUnavailableError("the capture journal could not be read") from error
        try:
            _capture_journal_entries(
                frames,
                call_key=sidecar.call_key,
                max_frame_bytes=self._live.config.max_frame_bytes,
            )
        except ValueError as error:
            if self._correlation_key_id is not None:
                self._correlation_state_error = OSError(
                    "a hosted capture journal cannot be replayed safely"
                )
            raise CaptureJournalUnavailableError(
                "the durable capture journal cannot be replayed because it is incomplete or corrupt"
            ) from error
        if not sidecar.call_metadata_bound:
            try:
                uncertainty_nano, trace_pair = _legacy_call_metadata_from_journal(
                    frames,
                    call_key=drain.call_key,
                    clock_domain_id=drain.clock_domain_id,
                    max_frame_bytes=self._live.config.max_frame_bytes,
                )
            except ValueError as error:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = OSError(
                        "a hosted capture journal cannot be replayed safely"
                    )
                raise CaptureJournalUnavailableError(
                    "the capture journal cannot be replayed or its legacy metadata recovered"
                ) from error
            if not _legacy_metadata_matches_durable_values(
                drain,
                uncertainty_nano=uncertainty_nano,
                trace_pair=trace_pair,
            ):
                raise CaptureSequenceConflictError(
                    "a drain changes this call's trace or clock metadata",
                    sequence=drain.drain_sequence,
                )
            sidecar = replace(
                sidecar,
                call_metadata_bound=True,
                call_metadata_digest=drain.call_metadata_digest(),
            )
            try:
                write_sidecar(self._journal_dir, sidecar)
            except OSError as error:
                raise CaptureJournalUnavailableError(
                    "legacy capture metadata could not be migrated"
                ) from error
        if not self._live.contains(drain.call_key, project_id=drain.project_id):
            try:
                self._live.rebuild_capture_session(
                    drain.call_key,
                    frames,
                    journal,
                    project_id=drain.project_id,
                    recovery_method=RECOVERY_METHOD,
                    recovery_reason=RECOVERY_REASON_SEALED,
                )
            except LiveError as error:
                if self._correlation_key_id is not None:
                    self._correlation_state_error = OSError(
                        "a hosted capture journal cannot be replayed safely"
                    )
                raise CaptureJournalUnavailableError(
                    "the durable capture journal cannot be replayed"
                ) from error
        try:
            finalized = self._live.summary(
                drain.call_key, project_id=drain.project_id
            ).close_observed
        except SessionNotLiveError as error:
            if self._correlation_key_id is not None:
                self._correlation_state_error = OSError(
                    "a hosted capture journal has no rebuilt live session"
                )
            raise CaptureJournalUnavailableError(
                "the durable capture journal has no rebuilt live session"
            ) from error
        return CaptureCall.rebuild(
            drain,
            sidecar,
            journal,
            journal_dir=self._journal_dir,
            finalized=finalized,
            correlation_key_id=self._correlation_key_id,
        )

    @staticmethod
    def _preflight_sidecar_drain(drain: CaptureDrain, sidecar: CaptureSidecar) -> None:
        """Refuse changed retries before rebuilding or publishing a live session."""

        sequence = drain.drain_sequence
        if not _matches_call_metadata(
            drain,
            bound=sidecar.call_metadata_bound,
            digest=sidecar.call_metadata_digest,
        ):
            raise CaptureSequenceConflictError(
                "a drain changes this call's trace or clock metadata",
                sequence=sequence,
            )

        if sequence <= sidecar.applied:
            digest = sidecar.digests.get(sequence)
            digest_version = sidecar.digest_versions.get(sequence, LEGACY_CAPTURE_DIGEST_VERSION)
            if digest is None or digest != drain.digest(version=digest_version):
                raise CaptureSequenceConflictError(
                    "a drain rewrites a sequence this call already resolved",
                    sequence=sequence,
                )

        retry_sequence = sidecar.retry_sequence or sidecar.pending_sequence
        retry_digest = sidecar.retry_digest or sidecar.pending_digest
        retry_digest_version = sidecar.retry_digest_version or sidecar.pending_digest_version
        if retry_sequence is not None and (
            sequence != retry_sequence
            or retry_digest is None
            or retry_digest_version is None
            or drain.digest(version=retry_digest_version) != retry_digest
        ):
            raise CaptureSequenceConflictError(
                "a new drain differs from the interrupted request",
                sequence=sequence,
            )

    def _prune_finalized(self, project_id: str) -> None:
        """Reclaim finalized calls from memory after their live session is gone.

        A finalized call is retained only long enough to refuse an in-flight late
        drain. Once its live session is gone there is nothing left to append to, so
        it no longer counts against the project's concurrent-call budget. Its
        durable ledger remains as the replay fence and project-deletion record.
        Caller holds the registry lock.
        """

        dead = [
            key
            for key, call in self._calls.items()
            if key[0] == project_id
            and call.finalized
            and not self._live.contains(key[1], project_id=project_id)
        ]
        for key in dead:
            self._calls.pop(key, None)


__all__ = [
    "END_CALL_ENDED",
    "RECOVERY_METHOD",
    "RECOVERY_REASON_SEALED",
    "CaptureCall",
    "CaptureCallCapacityError",
    "CaptureCallClosedError",
    "CaptureCallRegistry",
    "CaptureDrain",
    "CaptureEnd",
    "CaptureError",
    "CaptureJournalUnavailableError",
    "CaptureJournalWriterConflictError",
    "CaptureSequenceConflictError",
    "CaptureSequenceGapError",
    "DrainOutcome",
    "ResyncClaim",
]
