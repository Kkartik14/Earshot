"""Live tail of an open conversation: the journal, fanned out, and nothing more.

An incident is an immutable artifact. A *live* session is not one, and this
module exists to make that difference impossible to lose. It streams exactly the
facts the checkpoint journal already holds — admitted, sanitized, in admission
order — and it refuses to compute anything on top of them.

Three properties are load-bearing:

* **Append-only, never edited.** A later frame can add information (the completed
  ``operation`` for an earlier ``operation_open``) but never rewrites an earlier
  one. There is no patch, retraction, or update on the wire, because there is
  none in the artifact either.
* **Unknown is stated, never implied.** Everything that cannot be known before
  close — session status and end, turn membership, interruption classification,
  the privacy manifest, whether truncation will happen — is enumerated on the
  ``open`` event and carried as explicit nulls elsewhere. A subscriber that
  renders a live session as complete has to ignore the wire to do it.
* **No analysis, ever.** ``DerivedAnalysis`` binds to ``input_sha256``; there is
  no digest for a session still being written, so any p50/p95, diagnosis, or
  turn metric computed here would be a claim no artifact attests. The listing
  endpoint says so rather than silently omitting it.

Backpressure is lossless by construction. Every buffer is bounded; when a
subscriber falls behind, the server closes that connection with ``overflow``
instead of dropping events, and the durable journal replays on reconnect.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoint.framing import CHECKSUM_SIZE, HEADER_SIZE, scan_frames
from .checkpoint.reader import JournalReader, JournalUnreadableError, iter_journals
from .checkpoint.records import (
    JOURNAL_FORMAT_VERSION,
    JournalEntry,
    JournalExhausted,
    JournalFinalize,
    JournalFormatError,
    JournalLimitEntry,
    JournalOpen,
    JournalOperationOpen,
    JournalRecordEntry,
    decode_entry,
)
from .checkpoint.writer import DEFAULT_MAX_FRAME_BYTES
from .storage import DEFAULT_PROJECT_ID

SOURCE_JOURNAL = "journal"
SOURCE_CHECKPOINT = "checkpoint"

STATE_LIVE = "live"
STATE_STALE = "stale"
STATE_FINALIZED = "finalized"
STATE_ABANDONED = "abandoned"

EVENT_OPEN = "open"
EVENT_RECORD = "record"
EVENT_LIMIT = "limit"
EVENT_OPERATION_OPEN = "operation_open"
EVENT_EXHAUSTED = "exhausted"
EVENT_FINALIZE = "finalize"
EVENT_REPLAY_TRUNCATED = "replay_truncated"
EVENT_OVERFLOW = "overflow"
EVENT_RESET = "reset"
EVENT_END = "end"
# A named event rather than the conventional ``:`` comment: browsers never
# dispatch comments, so a comment keepalive keeps a proxy happy while leaving the
# client unable to tell "quiet session" from "dead connection". A heartbeat that
# carries its own as-of sequence answers that question honestly.
EVENT_HEARTBEAT = "heartbeat"

END_JOURNAL_REMOVED = "journal_removed"
END_FINAL_ARTIFACT_STORED = "final_artifact_stored"
END_SEALED = "sealed"
END_SESSION_EXPIRED = "session_expired"
END_SESSION_SUPERSEDED = "session_superseded"
END_SERVER_STOPPING = "server_stopping"

# What a live view is structurally unable to know. Sent on the ``open`` event so
# a client cannot mistake an absent value for a measured one, and asserted by
# tests so the list cannot quietly shrink.
UNKNOWN_UNTIL_CLOSE = (
    "session_status",
    "session_ended_at",
    "session_duration",
    "manifest_finality",
    "manifest_completeness",
    "privacy_manifest",
    "turn_membership",
    "turn_metrics",
    "interruption_classification",
    "derived_analysis",
    "diagnoses",
)

# Why the listing carries no verdicts. Stated rather than omitted: "analysis did
# not run" and "analysis found nothing" are different claims.
LIVE_LIMITATIONS = (
    "a live session is not an incident: it is never listed under /v1/incidents "
    "and has no immutable artifact until one is ingested or an operator seals it",
    "no analysis, diagnosis, or turn metric is derived from a live session, "
    "because derived analysis binds to the digest of a finished artifact",
    "every value not yet observed is absent rather than zero, and every "
    "operation without an observed end is reported as such",
)


class LiveError(Exception):
    """Base class for refusals the live surface makes on purpose."""


class SessionNotLiveError(LiveError):
    """No live session with this identity is visible to this caller."""


class TailCapacityError(LiveError):
    """The server is already carrying as many tail connections as it will."""


class LiveCapacityError(LiveError):
    """A live-session quota for this project is already fully used."""


class CheckpointSequenceError(LiveError):
    """An uploaded batch does not continue the sequence the server holds."""

    def __init__(self, message: str, *, expected_sequence: int) -> None:
        super().__init__(message)
        self.expected_sequence = expected_sequence


class CheckpointFramesInvalidError(LiveError):
    """An uploaded batch is not an intact run of plaintext journal frames."""


class SessionNotSealableError(LiveError):
    """This live session cannot be materialized into an artifact."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """One server-sent event: a name, an optional journal slot, and a payload.

    ``sequence`` is zero for control events (``reset``, ``replay_truncated``,
    ``overflow``, ``end``). Those are deliberately sent without an SSE ``id:``
    line, so a control event never advances the client's ``Last-Event-ID`` past a
    journal position it has not actually received.
    """

    name: str
    journal_id: str
    sequence: int
    payload: str

    @property
    def event_id(self) -> str | None:
        return f"{self.journal_id}:{self.sequence}" if self.sequence > 0 else None

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


def make_event(name: str, journal_id: str, sequence: int, data: dict[str, Any]) -> LiveEvent:
    return LiveEvent(name=name, journal_id=journal_id, sequence=sequence, payload=_json(data))


def render_sse(event: LiveEvent) -> str:
    """Render one event in the ``text/event-stream`` wire format."""

    lines = []
    identifier = event.event_id
    if identifier is not None:
        lines.append(f"id: {identifier}")
    lines.append(f"event: {event.name}")
    # ``payload`` is compact JSON, so it can never contain a newline and can
    # never be split across data lines.
    lines.append(f"data: {event.payload}")
    return "\n".join(lines) + "\n\n"


@dataclass(frozen=True, slots=True)
class LiveSessionSummary:
    session_id: str
    bundle_id: str
    journal_id: str
    source: str
    state: str
    last_sequence: int
    available_from_sequence: int
    last_append_unix_nano: str
    close_observed: bool
    journal_complete: bool
    sealable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "bundle_id": self.bundle_id,
            "journal_id": self.journal_id,
            "source": self.source,
            "state": self.state,
            "last_sequence": self.last_sequence,
            "available_from_sequence": self.available_from_sequence,
            "last_append_unix_nano": self.last_append_unix_nano,
            "close_observed": self.close_observed,
            "journal_complete": self.journal_complete,
            "sealable": self.sealable,
        }


@dataclass(frozen=True)
class LiveConfig:
    """Every bound the live surface runs under. All of them are enforced.

    The replay window (``max_records_per_session``) and the per-connection queue
    (``max_queue_records`` / ``max_queue_bytes``) are separate on purpose: the
    first is how far back a *new* subscriber may start, the second is how far
    behind an *existing* one may fall before the server closes it.
    """

    poll_interval_ms: int = 200
    stale_after_seconds: float = 30.0
    session_ttl_seconds: float = 900.0
    heartbeat_seconds: float = 15.0
    max_sessions: int = 32
    max_sessions_per_project: int = 16
    max_records_per_session: int = 10_000
    max_replay_bytes: int = 8 * 1024 * 1024
    max_seal_bytes: int = 8 * 1024 * 1024
    max_connections: int = 8
    max_subscribers_per_session: int = 4
    max_queue_records: int = 512
    max_queue_bytes: int = 1024 * 1024
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        for name in (
            "poll_interval_ms",
            "max_sessions",
            "max_sessions_per_project",
            "max_records_per_session",
            "max_replay_bytes",
            "max_seal_bytes",
            "max_connections",
            "max_subscribers_per_session",
            "max_queue_records",
            "max_queue_bytes",
            "max_frame_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"live {name} must be a positive integer")
        for name in ("stale_after_seconds", "session_ttl_seconds", "heartbeat_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"live {name} must be positive")


@dataclass(frozen=True, slots=True)
class AcceptedCheckpoint:
    journal_id: str
    accepted_through: int
    accepted_records: int
    state: str
    sealable: bool


class Subscription:
    """One tail connection's view of one journal.

    The backlog — everything the subscriber asked to replay — is handed over as a
    list rather than pushed through the queue, because replaying the whole
    retained window would otherwise trip the queue bound the moment it started.
    The bounded queue governs only how far behind a subscriber may fall *after*
    it has joined, which is the condition that actually needs an answer.
    """

    def __init__(
        self,
        registry: LiveSessionRegistry,
        session: _LiveSession,
        *,
        backlog: Sequence[LiveEvent],
        resume_from: int,
        config: LiveConfig,
    ) -> None:
        self._registry = registry
        self._session = session
        self._config = config
        self._backlog = list(backlog)
        self._queue: deque[LiveEvent] = deque()
        self._queued_bytes = 0
        self._lock = threading.Lock()
        self._overflowed = False
        self._end_reason: str | None = None
        self._closed = False
        self._loop: Any = None
        self._wakeup: Any = None
        self.session_id = session.session_id
        self.journal_id = session.journal_id
        self.last_delivered_sequence = resume_from

    # ------------------------------------------------------------- producer

    def offer(self, events: Sequence[LiveEvent]) -> None:
        """Queue events for this connection, or mark it as fallen behind.

        Called from the poller thread or from an ingest request, never from the
        serving coroutine.
        """

        with self._lock:
            if self._overflowed or self._closed:
                return
            for event in events:
                if (
                    len(self._queue) >= self._config.max_queue_records
                    or self._queued_bytes + event.size_bytes > self._config.max_queue_bytes
                ):
                    # Close rather than drop. The journal still holds every
                    # record, so a reconnect with Last-Event-ID recovers them.
                    self._overflowed = True
                    break
                self._queue.append(event)
                self._queued_bytes += event.size_bytes
        self._wake()

    def finish(self, reason: str) -> None:
        """Tell this connection the session is over, and why."""

        with self._lock:
            if self._end_reason is None:
                self._end_reason = reason
        self._wake()

    def _wake(self) -> None:
        loop, wakeup = self._loop, self._wakeup
        if loop is None or wakeup is None:
            return
        # A torn-down loop means the connection is already gone; there is
        # nothing left to wake and nothing to report.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(wakeup.set)

    # ------------------------------------------------------------- consumer

    def attach(self, loop: Any, wakeup: Any) -> None:
        """Bind the asyncio primitives the serving coroutine waits on."""

        with self._lock:
            self._loop = loop
            self._wakeup = wakeup

    def drain(self) -> list[LiveEvent]:
        """Take everything currently available, backlog first."""

        batch: list[LiveEvent] = []
        if self._backlog:
            batch = self._backlog
            self._backlog = []
        with self._lock:
            while self._queue:
                event = self._queue.popleft()
                self._queued_bytes -= event.size_bytes
                batch.append(event)
        for event in reversed(batch):
            if event.sequence > 0:
                self.last_delivered_sequence = event.sequence
                break
        return batch

    def terminal(self) -> list[LiveEvent]:
        """The event that ends this stream, once everything else has gone out."""

        with self._lock:
            overflowed = self._overflowed
            end_reason = self._end_reason
            pending = bool(self._queue)
        if pending or self._backlog:
            return []
        if overflowed:
            return [
                make_event(
                    EVENT_OVERFLOW,
                    self.journal_id,
                    0,
                    {
                        "reason": "subscriber_fell_behind",
                        "last_sequence": self.last_delivered_sequence,
                        "resume_with": f"{self.journal_id}:{self.last_delivered_sequence}",
                        "note": (
                            "nothing was dropped; the durable journal still holds every "
                            "record, so reconnect with Last-Event-ID to catch up"
                        ),
                    },
                )
            ]
        if end_reason is not None:
            return [
                make_event(
                    EVENT_END,
                    self.journal_id,
                    0,
                    {
                        "reason": end_reason,
                        "last_sequence": self.last_delivered_sequence,
                        "close_observed": self._session.close_observed,
                    },
                )
            ]
        return []

    @property
    def finished(self) -> bool:
        if self._backlog:
            return False
        with self._lock:
            if self._queue:
                return False
            return self._overflowed or self._end_reason is not None

    def close(self) -> None:
        """Release this connection's slot. Idempotent."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._registry.release(self)


class _LiveSession:
    """One journal being followed, plus everyone following it."""

    def __init__(
        self,
        *,
        session_id: str,
        project_id: str,
        source: str,
        header: JournalOpen,
        open_event: LiveEvent,
        config: LiveConfig,
        path: Path | None,
        retain_frames: bool,
    ) -> None:
        self.session_id = session_id
        self.project_id = project_id
        self.source = source
        self.path = path
        self.config = config
        self.journal_id = header.journal_id
        self.bundle_id = header.bundle_id
        self.open_event = open_event
        self.events: deque[LiveEvent] = deque()
        self.retained_bytes = 0
        self.last_sequence = 0
        self.close_observed = False
        self.journal_complete = True
        self.last_append_unix_nano = 0
        self.subscribers: set[Subscription] = set()
        # Only remotely uploaded frames are retained for sealing. A locally
        # tailed journal is already a file on disk; buffering it again would
        # double the storage for no gain.
        self.frames: bytearray | None = bytearray() if retain_frames else None
        self.frames_complete = retain_frames

    def append(self, event: LiveEvent) -> None:
        self.events.append(event)
        self.retained_bytes += event.size_bytes
        if event.sequence > self.last_sequence:
            self.last_sequence = event.sequence
        while len(self.events) > 1 and (
            len(self.events) > self.config.max_records_per_session
            or self.retained_bytes > self.config.max_replay_bytes
        ):
            dropped = self.events.popleft()
            self.retained_bytes -= dropped.size_bytes

    @property
    def available_from_sequence(self) -> int:
        return self.events[0].sequence if self.events else self.last_sequence + 1

    def state(self, now: float) -> str:
        if self.close_observed:
            return STATE_FINALIZED
        age = now - self.last_append_unix_nano / 1e9
        if age >= self.config.session_ttl_seconds:
            return STATE_ABANDONED
        if age >= self.config.stale_after_seconds:
            return STATE_STALE
        return STATE_LIVE

    @property
    def sealable(self) -> bool:
        if self.source == SOURCE_JOURNAL:
            return self.path is not None
        return self.frames is not None and self.frames_complete

    def summary(self, now: float) -> LiveSessionSummary:
        return LiveSessionSummary(
            session_id=self.session_id,
            bundle_id=self.bundle_id,
            journal_id=self.journal_id,
            source=self.source,
            state=self.state(now),
            last_sequence=self.last_sequence,
            available_from_sequence=self.available_from_sequence,
            last_append_unix_nano=str(self.last_append_unix_nano),
            close_observed=self.close_observed,
            journal_complete=self.journal_complete,
            sealable=self.sealable,
        )


@dataclass
class _TrackedFile:
    """What the poller remembers about one journal file between scans."""

    size: int = 0
    mtime_ns: int = 0
    last_sequence: int = 0
    session_id: str | None = None


class LiveSessionRegistry:
    """Every live session the server can currently see, and its subscribers.

    Two sources feed it and both land in the same buffer: a checkpoint directory
    this process can read (Phase 3a) and checkpoint frames uploaded over HTTP
    (Phase 3b). A live buffer is never an incident. It leaves this registry only
    by expiring, by being superseded when the real artifact is ingested, or
    through an explicit operator seal — never automatically, because the server
    cannot tell "crashed" from "slow" and must not manufacture artifacts nobody
    produced.
    """

    def __init__(
        self,
        *,
        journal_dir: Path | str | None = None,
        key: bytes | None = None,
        config: LiveConfig | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
        clock: Any = time.time,
    ) -> None:
        self.config = config or LiveConfig()
        self.journal_dir = None if journal_dir is None else Path(journal_dir)
        self._key = key
        self._project_id = project_id
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, _LiveSession] = {}
        # Structurally mutated only by the polling thread, so a scan can iterate
        # it without racing a request handler.
        self._tracked: dict[Path, _TrackedFile] = {}
        self._connections = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Begin following the configured checkpoint directory, if any."""

        if self.journal_dir is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="earshot-live-tail", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            for subscriber in list(session.subscribers):
                subscriber.finish(END_SERVER_STOPPING)

    def _run(self) -> None:
        interval = self.config.poll_interval_ms / 1000.0
        while not self._stop.wait(interval):
            try:
                self.refresh()
                self.expire()
            except Exception:  # pragma: no cover - the poller must never die
                continue

    # -------------------------------------------------------------- reading

    def refresh(self) -> None:
        """Scan the checkpoint directory once and publish whatever is new.

        Each changed journal is replayed in full through :class:`JournalReader`
        rather than incrementally from a saved offset. That costs a re-read per
        poll and buys the property that matters: the frame chain, its checksums
        and — when configured — its authentication are revalidated end to end
        every time, by exactly the code the assembler uses. A voice session's
        journal is bounded, so the cost is bounded with it.
        """

        if self.journal_dir is None or not self.journal_dir.is_dir():
            return
        seen: set[Path] = set()
        for path in iter_journals(self.journal_dir):
            seen.add(path)
            try:
                status = path.stat()
            except OSError:
                continue
            tracked = self._tracked.setdefault(path, _TrackedFile())
            if tracked.size == status.st_size and tracked.mtime_ns == status.st_mtime_ns:
                continue
            tracked.size = status.st_size
            tracked.mtime_ns = status.st_mtime_ns
            try:
                replay = JournalReader(path, key=self._key).read()
            except (JournalUnreadableError, OSError):
                # An unreadable header is not evidence of anything; it is simply
                # not followable. It is never deleted, truncated, or repaired.
                continue
            self._publish_replay(path, tracked, replay.header, replay.entries)
        for path in [known for known in self._tracked if known not in seen]:
            tracked = self._tracked.pop(path)
            if tracked.session_id is not None:
                self.drop_session(tracked.session_id, reason=END_JOURNAL_REMOVED)

    def _publish_replay(
        self,
        path: Path,
        tracked: _TrackedFile,
        header: JournalOpen,
        entries: Sequence[JournalEntry],
    ) -> None:
        now_nano = int(self._clock() * 1e9)
        with self._lock:
            session = self._sessions.get(header.session_id)
            if session is not None and session.journal_id != header.journal_id:
                # A new journal for a session we were already following. Tell
                # every subscriber to discard its state rather than splicing two
                # sessions into one client-side timeline.
                self._reset_session(session, header.journal_id)
                session = None
                tracked.last_sequence = 0
            if session is None:
                if tracked.last_sequence > 0:
                    # This journal was already followed and its session was
                    # dropped (sealed, superseded, expired). Do not resurrect it.
                    return
                session = self._register(
                    session_id=header.session_id,
                    project_id=self._project_id,
                    source=SOURCE_JOURNAL,
                    header=header,
                    path=path,
                    retain_frames=False,
                )
                tracked.last_sequence = 1
                tracked.session_id = header.session_id
                session.last_append_unix_nano = now_nano
                self._deliver(session, [session.open_event])
            batch: list[LiveEvent] = []
            sequence = 1
            for entry in entries:
                sequence += 1
                if sequence <= tracked.last_sequence:
                    continue
                batch.append(_entry_event(entry, header.journal_id, sequence))
                _absorb(session, entry)
            if not batch:
                return
            tracked.last_sequence = sequence
            session.last_append_unix_nano = now_nano
            self._deliver(session, batch)

    # --------------------------------------------------------------- upload

    def accept_frames(
        self,
        session_id: str,
        payload: bytes,
        *,
        project_id: str,
    ) -> AcceptedCheckpoint:
        """Accept a contiguous run of plaintext journal frames over HTTP.

        Frames are verified the way the reader verifies them — separator, length
        bound, CRC, strict sequence contiguity — and a batch with a torn tail is
        refused whole. A torn tail is meaningful at the end of a crashed file; in
        an upload it only means a malformed request, and accepting a prefix would
        let a client decide where the server's evidence stops.

        An encrypted journal cannot be uploaded: the server has no key, so the
        header would not decode and the batch is refused. Remote live tailing is
        therefore an explicit choice to let the backend read these frames.
        """

        if len(payload) < HEADER_SIZE:
            raise CheckpointFramesInvalidError("checkpoint batch is shorter than one frame")
        first_sequence = int.from_bytes(payload[1:5], "big")
        now_nano = int(self._clock() * 1e9)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.project_id != project_id:
                # Never confirm that another project holds this session id.
                raise SessionNotLiveError("no live session with this identity")
            if first_sequence != 1:
                if session is None:
                    raise CheckpointSequenceError(
                        "no live session to continue", expected_sequence=1
                    )
                if first_sequence > session.last_sequence + 1:
                    raise CheckpointSequenceError(
                        "checkpoint batch skips a sequence",
                        expected_sequence=session.last_sequence + 1,
                    )
            scan = scan_frames(
                payload,
                max_body_bytes=self.config.max_frame_bytes,
                first_sequence=first_sequence,
            )
            if scan.torn_tail_bytes or not scan.frames:
                raise CheckpointFramesInvalidError("checkpoint batch is not an intact frame run")
            decoded: list[tuple[int, JournalEntry]] = []
            for frame in scan.frames:
                try:
                    decoded.append((frame.sequence, decode_entry(frame.body)))
                except JournalFormatError as error:
                    raise CheckpointFramesInvalidError(
                        "checkpoint batch holds a frame that is not a journal entry"
                    ) from error
            header: JournalOpen | None = None
            if first_sequence == 1:
                candidate = decoded[0][1]
                if not isinstance(candidate, JournalOpen):
                    raise CheckpointFramesInvalidError("checkpoint batch has no journal header")
                if candidate.journal_format_version != JOURNAL_FORMAT_VERSION:
                    raise CheckpointFramesInvalidError("unsupported journal format version")
                if candidate.session_id != session_id:
                    raise CheckpointFramesInvalidError(
                        "checkpoint header declares a different session"
                    )
                header = candidate
                if session is not None and session.journal_id != header.journal_id:
                    self._reset_session(session, header.journal_id)
                    session = None
            if session is None:
                if header is None:  # pragma: no cover - guarded above
                    raise CheckpointFramesInvalidError("checkpoint batch has no journal header")
                self._enforce_quota(project_id)
                session = self._register(
                    session_id=session_id,
                    project_id=project_id,
                    source=SOURCE_CHECKPOINT,
                    header=header,
                    path=None,
                    retain_frames=True,
                )
                session.last_append_unix_nano = now_nano
                self._deliver(session, [session.open_event])
                retained_through = 0
            else:
                retained_through = session.last_sequence
            batch: list[LiveEvent] = []
            offset = 0
            for sequence, entry in decoded:
                frame_end = _frame_end(payload, offset)
                if sequence > retained_through:
                    self._retain_frame(session, payload[offset:frame_end])
                    if sequence > 1:
                        batch.append(_entry_event(entry, session.journal_id, sequence))
                        _absorb(session, entry)
                offset = frame_end
            if batch:
                session.last_append_unix_nano = now_nano
                self._deliver(session, batch)
            return AcceptedCheckpoint(
                journal_id=session.journal_id,
                accepted_through=session.last_sequence,
                accepted_records=len(batch),
                state=session.state(self._clock()),
                sealable=session.sealable,
            )

    def _retain_frame(self, session: _LiveSession, frame: bytes) -> None:
        """Keep the raw frame so an operator can seal this session later.

        Retention stops at the cap and says so through ``frames_complete``. A
        prefix of a journal would assemble into an artifact that looks whole and
        is silently short, so a session that outgrew the cap simply stops being
        sealable instead.
        """

        frames = session.frames
        if frames is None:
            return
        if len(frames) + len(frame) > self.config.max_seal_bytes:
            session.frames_complete = False
            session.frames = None
            return
        frames.extend(frame)

    def _enforce_quota(self, project_id: str) -> None:
        if len(self._sessions) >= self.config.max_sessions:
            raise LiveCapacityError("the server is holding as many live sessions as it will")
        owned = sum(1 for item in self._sessions.values() if item.project_id == project_id)
        if owned >= self.config.max_sessions_per_project:
            raise LiveCapacityError("this project is holding as many live sessions as it will")

    # ------------------------------------------------------------- registry

    def _register(
        self,
        *,
        session_id: str,
        project_id: str,
        source: str,
        header: JournalOpen,
        path: Path | None,
        retain_frames: bool,
    ) -> _LiveSession:
        open_event = make_event(EVENT_OPEN, header.journal_id, 1, _open_payload(header, source))
        session = _LiveSession(
            session_id=session_id,
            project_id=project_id,
            source=source,
            header=header,
            open_event=open_event,
            config=self.config,
            path=path,
            retain_frames=retain_frames,
        )
        session.last_append_unix_nano = int(self._clock() * 1e9)
        self._sessions[session_id] = session
        return session

    def _reset_session(self, session: _LiveSession, journal_id: str) -> None:
        event = make_event(
            EVENT_RESET,
            session.journal_id,
            0,
            {
                "reason": "journal_identity_changed",
                "previous_journal_id": session.journal_id,
                "journal_id": journal_id,
                "note": "discard everything received for the previous journal",
            },
        )
        for subscriber in list(session.subscribers):
            subscriber.offer([event])
            subscriber.finish(END_SESSION_SUPERSEDED)
        session.subscribers.clear()
        self._sessions.pop(session.session_id, None)

    def _deliver(self, session: _LiveSession, events: Sequence[LiveEvent]) -> None:
        for event in events:
            session.append(event)
        for subscriber in list(session.subscribers):
            subscriber.offer(events)

    def drop_session(
        self,
        session_id: str,
        *,
        reason: str,
        project_id: str | None = None,
    ) -> bool:
        """Forget a live buffer, because its artifact exists or it expired."""

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            if project_id is not None and session.project_id != project_id:
                return False
            self._sessions.pop(session_id, None)
            subscribers = list(session.subscribers)
            session.subscribers.clear()
            for tracked in self._tracked.values():
                if tracked.session_id == session_id:
                    tracked.session_id = None
        for subscriber in subscribers:
            subscriber.finish(reason)
        return True

    def expire(self) -> None:
        now = self._clock()
        with self._lock:
            stale = [
                session.session_id
                for session in self._sessions.values()
                if session.state(now) == STATE_ABANDONED
            ]
        for session_id in stale:
            self.drop_session(session_id, reason=END_SESSION_EXPIRED)

    def sessions(self, *, project_id: str) -> tuple[LiveSessionSummary, ...]:
        now = self._clock()
        with self._lock:
            return tuple(
                session.summary(now)
                for session in sorted(self._sessions.values(), key=lambda item: item.session_id)
                if session.project_id == project_id
            )

    def summary(self, session_id: str, *, project_id: str) -> LiveSessionSummary:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.project_id != project_id:
                raise SessionNotLiveError("no live session with this identity")
            return session.summary(self._clock())

    # ------------------------------------------------------------ subscribe

    def subscribe(
        self,
        session_id: str,
        *,
        project_id: str,
        from_spec: str = "start",
        last_event_id: str | None = None,
    ) -> Subscription:
        """Open one tail connection, resuming where the client left off.

        ``Last-Event-ID`` wins over ``from`` because it is the client's own
        record of what it actually received. When it names a different journal
        the server emits ``reset`` before anything else, so two sessions can
        never be spliced into one client-side timeline.
        """

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.project_id != project_id:
                raise SessionNotLiveError("no live session with this identity")
            if self._connections >= self.config.max_connections:
                raise TailCapacityError("the server is carrying as many tails as it will")
            if len(session.subscribers) >= self.config.max_subscribers_per_session:
                raise TailCapacityError("this session already has as many tails as it will")

            preamble: list[LiveEvent] = []
            resumed = False
            start = 2
            if last_event_id:
                journal_id, _, raw = last_event_id.partition(":")
                if journal_id == session.journal_id and raw.isdigit() and int(raw) >= 1:
                    start = int(raw) + 1
                    resumed = True
                else:
                    preamble.append(
                        make_event(
                            EVENT_RESET,
                            session.journal_id,
                            0,
                            {
                                "reason": "journal_identity_changed",
                                "previous_journal_id": journal_id or None,
                                "journal_id": session.journal_id,
                                "note": "discard everything received for the previous journal",
                            },
                        )
                    )
            elif from_spec == "live":
                start = session.last_sequence + 1
            elif from_spec.isdigit():
                start = max(2, int(from_spec))

            if not resumed:
                # Nothing after the header is interpretable without it, so a
                # fresh connection always receives it first.
                preamble.append(session.open_event)
            available_from = max(session.available_from_sequence, 2)
            ceiling = session.last_sequence + 1
            first_shown = min(max(start, available_from), ceiling)
            withheld = (
                max(0, min(available_from, ceiling) - start) if resumed else max(0, first_shown - 2)
            )
            if withheld > 0:
                preamble.append(
                    make_event(
                        EVENT_REPLAY_TRUNCATED,
                        session.journal_id,
                        0,
                        {
                            "reason": (
                                "requested_live_only"
                                if from_spec == "live" and not resumed
                                else "replay_window_exceeded"
                            ),
                            "requested_from_sequence": start,
                            "available_from_sequence": first_shown,
                            "withheld_records": withheld,
                            "note": "earlier facts exist in this session and are not shown here",
                        },
                    )
                )
            backlog = [event for event in session.events if event.sequence >= max(start, 2)]
            subscription = Subscription(
                self,
                session,
                backlog=[*preamble, *backlog],
                resume_from=max(1, start - 1),
                config=self.config,
            )
            session.subscribers.add(subscription)
            self._connections += 1
            return subscription

    def release(self, subscription: Subscription) -> None:
        with self._lock:
            session = self._sessions.get(subscription.session_id)
            if session is not None:
                session.subscribers.discard(subscription)
            self._connections = max(0, self._connections - 1)

    # ----------------------------------------------------------------- seal

    def seal_source(self, session_id: str, *, project_id: str) -> tuple[str, Path | bytes]:
        """What an explicit seal would read, or why it cannot be done.

        Sealing is the only path from a live buffer to an artifact, and it is
        always an operator action. The server never seals on its own: it cannot
        distinguish a crashed producer from a slow one, and guessing would
        manufacture an artifact nobody produced.
        """

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.project_id != project_id:
                raise SessionNotLiveError("no live session with this identity")
            if session.source == SOURCE_JOURNAL:
                if session.path is None:  # pragma: no cover - journals always have a path
                    raise SessionNotSealableError("this session has no readable journal")
                return SOURCE_JOURNAL, session.path
            if session.frames is None or not session.frames_complete:
                raise SessionNotSealableError("this session outgrew its retained frame window")
            return SOURCE_CHECKPOINT, bytes(session.frames)


def _frame_end(payload: bytes, offset: int) -> int:
    length = int.from_bytes(payload[offset + 5 : offset + HEADER_SIZE], "big")
    return offset + HEADER_SIZE + length + CHECKSUM_SIZE


def _absorb(session: _LiveSession, entry: JournalEntry) -> None:
    if isinstance(entry, JournalFinalize):
        session.close_observed = True
        session.journal_complete = session.journal_complete and entry.journal_complete
    elif isinstance(entry, JournalExhausted):
        session.journal_complete = False


def _open_payload(header: JournalOpen, source: str) -> dict[str, Any]:
    return {
        "journal_id": header.journal_id,
        "journal_format_version": header.journal_format_version,
        "session_id": header.session_id,
        "bundle_id": header.bundle_id,
        "source": source,
        "producer": {"name": header.producer_name, "version": header.producer_version},
        "clock_domain_id": header.clock_domain_id,
        "started_at": {
            "wall_time_unix_nano": header.started_wall,
            "monotonic_time_nano": header.started_mono,
            "clock_domain_id": header.clock_domain_id,
        },
        "capture_policy": {
            "policy_id": header.policy_id,
            "policy_version": header.policy_version,
            "enabled_classes": list(header.enabled_classes),
        },
        "recorder_limits": {
            "max_records": header.max_records,
            "max_capture_bytes": header.max_capture_bytes,
            "max_raw_otlp_bytes": header.max_raw_otlp_bytes,
            "max_value_bytes": header.max_value_bytes,
        },
        # The two assertions that keep a live view from reading as a final one.
        "in_progress": True,
        "unknown_until_close": list(UNKNOWN_UNTIL_CLOSE),
    }


def _record_payload(entry: JournalRecordEntry) -> dict[str, Any]:
    value = entry.value
    if entry.kind == "raw_otlp" and value is not None:
        encoded = value.get("payload_base64")
        value = {key: item for key, item in value.items() if key != "payload_base64"}
        # The opaque OTLP blob stays in the journal and out of the fan-out: it is
        # unbounded relative to every other record and a live subscriber cannot
        # interpret it anyway. Its identity and digest still travel, so its
        # presence is never hidden.
        value["payload_withheld"] = True
        value["payload_base64_length"] = len(encoded) if isinstance(encoded, str) else 0
    return {
        "kind": entry.kind,
        "value": value,
        "supersedes_index": entry.replaces_index,
        "omissions": [
            {
                "field_key_sha256": omission.field_key_sha256,
                "capture_class": omission.capture_class,
                "reason": omission.reason,
            }
            for omission in entry.omissions
        ],
        "retained_classes": list(entry.retained_classes),
    }


def _entry_event(entry: JournalEntry, journal_id: str, sequence: int) -> LiveEvent:
    """Project one journal entry onto the wire, verbatim and without inference."""

    if isinstance(entry, JournalRecordEntry):
        return make_event(EVENT_RECORD, journal_id, sequence, _record_payload(entry))
    if isinstance(entry, JournalOperationOpen):
        return make_event(
            EVENT_OPERATION_OPEN,
            journal_id,
            sequence,
            {
                "operation_id": entry.operation_id,
                "operation_name": entry.operation_name,
                "started_at": entry.started_at,
                "participant_id": entry.participant_id,
                "stream_id": entry.stream_id,
                "turn_id": entry.turn_id,
                "trace_id": entry.trace_id,
                "span_id": entry.span_id,
                "parent_span_id": entry.parent_span_id,
                "parent_scope": entry.parent_scope,
                # A distinct event kind carrying explicit nulls: an operation
                # whose end was never observed cannot be rendered as one that
                # completed, and has no duration to extrapolate.
                "status": "unknown",
                "ended_at": None,
                "duration_nano": None,
                "end_observed": False,
            },
        )
    if isinstance(entry, JournalLimitEntry):
        return make_event(
            EVENT_LIMIT,
            journal_id,
            sequence,
            {
                "reason": entry.reason,
                "kind": entry.kind,
                "capture_class": entry.capture_class,
                "estimated_bytes": entry.estimated_bytes,
                "whole_record": entry.whole_record,
                "freeze": entry.freeze,
            },
        )
    if isinstance(entry, JournalExhausted):
        return make_event(
            EVENT_EXHAUSTED,
            journal_id,
            sequence,
            {
                "reason": entry.reason,
                "journal_complete": False,
                "note": "the journal reached its cap; later facts are not recorded",
            },
        )
    if isinstance(entry, JournalFinalize):
        return make_event(
            EVENT_FINALIZE,
            journal_id,
            sequence,
            {
                "status": entry.status,
                "ended_at": entry.ended,
                "journal_complete": entry.journal_complete,
                "first_limit_reason": entry.first_limit_reason,
                "truncated_records": entry.truncated_records,
                # The recorder closed. The artifact is a separate thing that has
                # to arrive through ingest; this stream never carries it.
                "artifact_available": False,
                "note": (
                    "the recorder closed; the immutable artifact arrives separately "
                    "through /v1/incidents"
                ),
            },
        )
    if isinstance(entry, JournalOpen):  # pragma: no cover - a header never repeats
        return make_event(EVENT_OPEN, journal_id, sequence, _open_payload(entry, SOURCE_JOURNAL))
    raise CheckpointFramesInvalidError("unsupported journal entry kind")


__all__ = [
    "END_FINAL_ARTIFACT_STORED",
    "END_JOURNAL_REMOVED",
    "END_SEALED",
    "END_SERVER_STOPPING",
    "END_SESSION_EXPIRED",
    "END_SESSION_SUPERSEDED",
    "EVENT_END",
    "EVENT_EXHAUSTED",
    "EVENT_FINALIZE",
    "EVENT_HEARTBEAT",
    "EVENT_LIMIT",
    "EVENT_OPEN",
    "EVENT_OPERATION_OPEN",
    "EVENT_OVERFLOW",
    "EVENT_RECORD",
    "EVENT_REPLAY_TRUNCATED",
    "EVENT_RESET",
    "LIVE_LIMITATIONS",
    "SOURCE_CHECKPOINT",
    "SOURCE_JOURNAL",
    "STATE_ABANDONED",
    "STATE_FINALIZED",
    "STATE_LIVE",
    "STATE_STALE",
    "UNKNOWN_UNTIL_CLOSE",
    "AcceptedCheckpoint",
    "CheckpointFramesInvalidError",
    "CheckpointSequenceError",
    "LiveCapacityError",
    "LiveConfig",
    "LiveError",
    "LiveEvent",
    "LiveSessionRegistry",
    "LiveSessionSummary",
    "SessionNotLiveError",
    "SessionNotSealableError",
    "Subscription",
    "TailCapacityError",
    "make_event",
    "render_sse",
]
