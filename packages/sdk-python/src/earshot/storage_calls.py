"""Calls in the catalog: OTLP spans grouped by trace into calls keyed by session.

A ``CallStore`` is part of an ``IncidentStore``: it runs on that store's database,
write locks (``_mutation``), clock and correlation key. The OTLP receiver builds
one per server; tests build one per store.

**Traces bind to calls.** Every span belongs to its trace (``call_traces``). The
first span of a trace that names a session key (``call_capture.resolve_session_key``)
binds the whole trace to that key's call; the first binding wins, and a later span
naming another key counts as a ``key_disagreement`` and still joins the bound call.
Spans of a trace no span has named yet are stored **parked** (the trace row has no
call); when a later request names the trace, its parked spans join the call.

**Call identity.** ``call_id = "call_" + HMAC("call-session-v1", project \\0 key \\0
generation)[:32]``. ``call_key_generations`` holds a key's generation (0 without a
row); a call purge increments it, so a purged call is never rebuilt. While a live
``call_tombstones`` row exists for the key, or for a new span's trace (a purge
tombstones the purged call's traces too), its evidence is dropped and counted
(``purged_call_evidence_dropped``).

**Parked-trace collection** (``collect_parked_traces``, scheduled by the server).
A parked trace is due ``LONG_QUIET`` after its last span arrived. A due trace with a
framework session span becomes a call keyed by its trace (``key_source =
'trace_id'``); any other due trace is deleted and counted
(``parked_traces_discarded``).

**Close rules**, recomputed on every evidence write: with an end signal,
``close_due = now + END_GRACE``; otherwise ``now + LONG_QUIET``; for a closed call
receiving late evidence, the later of ``now + LATE_DEBOUNCE`` and that rule. A call
with ``REVISION_CAP`` revisions is revised no more, except for its first end-signal
close (``UNCAPPED_SQL``); evidence past the cap is stored, keeps the call dirty, and
sets ``evidence_after_latest_revision`` (counted once as ``revision_cap_reached``).
A batch whose only spans for a call are tail spans (``call_capture.TAIL_SPANS``:
LiveKit's job runner after ``session_close``) is not evidence: the spans are stored
and counted, and the call's dirty flag, due time and last evidence are left alone,
unless a tail end marker gives the call its first end signal.
The closer (``storage_closer``) turns due calls into revisions.

**Counters.** Only rare events are persisted (``otlp_counters``); nothing is written
per request for accepted spans. The gauges (``parked_traces``, ``open_calls``) are
counted when asked.

**Lock order** is sorted trace rows, then sorted call rows (``lock_order``); every
statement of one batch follows it.

**Set-based writes.** One request is a fixed number of statements, however many
traces and calls its spans belong to: each read covers the whole batch (one
statement per IN-list chunk) and each write is one ``executemany``. A worker's
``BatchSpanProcessor`` mixes the spans of every call it serves into one export,
and the write lock is held for the whole batch.

**Transcript lines.** Each span's line (``call_materializer.transcript_text_json``)
is stored with it, from the preset-filtered span, so reading a call's transcript
never reads its spans' payloads.

Importing this module needs ``opentelemetry-proto`` only to build a resource
payload, which happens when spans are recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .call_capture import (
    CAPTURE_PRESET_FULL,
    OtlpSpan,
    RequestMemo,
    SessionKey,
    apply_capture_preset,
    is_end_marker,
    is_session_span,
    is_tail_span,
    payload_digest,
    resolve_session_key,
    resource_payload,
    span_payload,
    valid_session_key,
)
from .call_materializer import transcript_text_json
from .crash_points import crash_point
from .maintenance import MAINTENANCE_LOCK_BUDGET_SECONDS
from .server_config import CALL_TIMING_RANGE, valid_call_timing
from .storage import StorageBusyError, StorageError

if TYPE_CHECKING:
    from .maintenance import MaintenanceJob
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection

_SECOND = 1_000_000_000
_SESSION_CALL_NAMESPACE = "call-session-v1"
_SESSION_KEY_NAMESPACE = "call-session-key-v1"
_TRACE_CALL_NAMESPACE = "call-trace-v1"
_TRACE_KEY_NAMESPACE = "call-trace-key-v1"
TRACE_KEY_SOURCE = "trace_id"
RECORDING_KEY_SOURCE = "recording"
# The persisted rare-event counters; the gauges are derived.
OTLP_COUNTERS = (
    "rejected_spans",
    "span_conflicts",
    "parked_traces_discarded",
    "key_disagreements",
    "revision_cap_reached",
    "purged_call_evidence_dropped",
)
PARKED_TRACE_BATCH_SIZE = 100
# A call is revised at most this many times, plus its first end-signal close.
REVISION_CAP = 16
# The cap rule on a ``calls`` row aliased ``c``: true while the call may be revised.
UNCAPPED_SQL = (
    f"(c.latest_revision IS NULL OR c.latest_revision < {REVISION_CAP} "
    "OR (c.end_signal_unix_nano IS NOT NULL AND NOT EXISTS (SELECT 1 FROM call_revisions r "
    "WHERE r.project_id = c.project_id AND r.call_id = c.call_id AND r.finality = 'final')))"
)
# Bound parameters per IN list: under SQLite's oldest default limit (999).
_IN_LIST_SIZE = 500


def call_spans_sql(columns: str, *, condition: str = "", order_by: str = "") -> str:
    """``SELECT columns`` over one call's stored spans (``s``, through its traces ``t``);
    the parameters are ``(project_id, call_id)``.

    The one shape of every read of a call's spans. ``CROSS JOIN`` fixes SQLite's join
    order (its left table is always the outer loop): the call's traces by
    ``call_traces_call_idx``, then each trace's spans by key, so the cost follows the
    call. A plain join, with no ``sqlite_stat1`` to go on, walked every span of the
    project and probed the call's traces once per span (312 ms a read, and as long a hold of the
    claim's write lock, at 1M spans).
    Postgres reads ``CROSS JOIN … WHERE`` as the same inner join and plans it freely
    (it has no ``CROSS JOIN … ON``).
    """

    return (
        f"SELECT {columns} FROM call_traces t CROSS JOIN spans s "
        "WHERE t.project_id = ? AND t.call_id = ? "
        "AND s.project_id = t.project_id AND s.trace_id = t.trace_id"
        + (f" AND {condition}" if condition else "")
        + (f" ORDER BY {order_by}" if order_by else "")
    )


@dataclass(frozen=True, slots=True)
class CallTimings:
    """The call lifecycle's durations (``EARSHOT_CALL_*_SECONDS``)."""

    end_grace_seconds: float = 10.0
    long_quiet_seconds: float = 300.0
    late_debounce_seconds: float = 30.0
    upload_hold_seconds: float = 600.0

    def __post_init__(self) -> None:
        for name in (
            "end_grace_seconds",
            "long_quiet_seconds",
            "late_debounce_seconds",
            "upload_hold_seconds",
        ):
            if not valid_call_timing(getattr(self, name)):
                raise ValueError(f"{name} must be {CALL_TIMING_RANGE}")

    @property
    def end_grace_nano(self) -> int:
        return int(self.end_grace_seconds * _SECOND)

    @property
    def long_quiet_nano(self) -> int:
        return int(self.long_quiet_seconds * _SECOND)

    @property
    def late_debounce_nano(self) -> int:
        return int(self.late_debounce_seconds * _SECOND)

    @property
    def upload_hold_nano(self) -> int:
        return int(self.upload_hold_seconds * _SECOND)


@dataclass(frozen=True, slots=True)
class SpanBatchResult:
    """What one ``record_spans`` call did."""

    stored_spans: int = 0
    duplicate_spans: int = 0
    span_conflicts: int = 0
    key_disagreements: int = 0
    purged_call_evidence_dropped: int = 0
    parked_spans: int = 0
    # Calls that received new evidence, sorted.
    call_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParkedTraceCollection:
    """What one ``collect_parked_traces`` call did."""

    promoted: int = 0
    discarded: int = 0
    purged_call_evidence_dropped: int = 0


def lock_order(*, traces: Iterable[bytes], calls: Iterable[str]) -> tuple[tuple[str, Any], ...]:
    """The order a batch touches rows in: sorted trace rows, then sorted call rows.

    Every writer taking row locks in one global order cannot deadlock against
    another; writers are serialized anyway (``K_write``, ``.store.lock``).
    """

    return (
        *(("trace", trace) for trace in sorted(set(traces))),
        *(("call", call) for call in sorted(set(calls))),
    )


@dataclass(slots=True)
class _Prepared:
    """One span as it will be stored, plus what it says about its call."""

    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    name: str
    scope_name: str
    start: int
    end: int
    end_marker: bool
    session_span: bool
    tail: bool
    key: SessionKey | None
    payload: bytes
    digest: bytes
    resource_digest: bytes
    resource_payload: bytes
    text_json: str | None


@dataclass(slots=True)
class _CallTarget:
    call_id: str
    key_fingerprint: str
    created: bool = False
    key: SessionKey | None = None
    generation: int = 0
    first_evidence: int = 0
    new_spans: int = 0
    # Of ``new_spans``, the tail spans (``call_capture.TAIL_SPANS``).
    tail_spans: int = 0
    end_marker: bool = False


@dataclass(slots=True)
class _Trace:
    """One trace of a batch: its spans, its stored row's state, and its call."""

    spans: list[_Prepared]
    exists: bool
    bound: bool
    first_received: int | None
    target: _CallTarget | None = None
    dropped: bool = False


@dataclass(frozen=True, slots=True)
class _Evidence:
    """What one write adds to a call."""

    spans: int = 0
    recordings: int = 0
    end_marker: bool = False
    # Of ``spans``, the tail spans, which are not evidence on their own.
    tail_spans: int = 0


@dataclass(slots=True)
class _Counts:
    stored: int = 0
    duplicates: int = 0
    conflicts: int = 0
    disagreements: int = 0
    dropped: int = 0
    parked: int = 0
    targets: dict[str, _CallTarget] = field(default_factory=dict)


def _prepare(span: OtlpSpan, preset: str, memo: RequestMemo) -> _Prepared:
    """One span as its rows store it. ``memo`` is the request's: the preset's copy of
    a shared resource and scope, its bytes and their digest are made once."""

    raw = span.span
    if len(raw.trace_id) != 16 or not any(raw.trace_id):
        raise ValueError("a span needs a 16-byte, non-zero trace ID")
    if len(raw.span_id) != 8 or not any(raw.span_id):
        raise ValueError("a span needs an 8-byte, non-zero span ID")
    if raw.parent_span_id and len(raw.parent_span_id) != 8:
        raise ValueError("a parent span ID has 8 bytes")
    kept = apply_capture_preset(preset, span, memo=memo)
    payload = span_payload(kept.span)
    resource = resource_payload(kept, memo=memo)
    return _Prepared(
        trace_id=bytes(raw.trace_id),
        span_id=bytes(raw.span_id),
        parent_span_id=bytes(raw.parent_span_id) or None,
        name=raw.name,
        scope_name=span.scope.name,
        start=raw.start_time_unix_nano,
        end=raw.end_time_unix_nano,
        end_marker=is_end_marker(span),
        session_span=is_session_span(span),
        tail=is_tail_span(span),
        key=resolve_session_key(span, memo=memo),
        payload=payload,
        digest=payload_digest(payload),
        resource_digest=memo.get(payload_digest, resource),
        resource_payload=resource,
        # From the stored (preset-filtered) span: under metadata_only it has no text.
        text_json=transcript_text_json(kept),
    )


def _batch_result(counts: _Counts) -> SpanBatchResult:
    return SpanBatchResult(
        stored_spans=counts.stored,
        duplicate_spans=counts.duplicates,
        span_conflicts=counts.conflicts,
        key_disagreements=counts.disagreements,
        purged_call_evidence_dropped=counts.dropped,
        parked_spans=counts.parked,
        call_ids=tuple(sorted(call for call, target in counts.targets.items() if target.new_spans)),
    )


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _rows_in(
    connection: DatabaseConnection,
    select: str,
    project_id: str,
    column: str,
    values: Collection[Any],
    *,
    tail: str = "",
    tail_parameters: tuple[Any, ...] = (),
) -> list[Any]:
    """``select`` over the project's rows whose ``column`` is one of ``values``: one
    statement per ``_IN_LIST_SIZE`` values (sorted), whatever the batch's size, never
    server-prepared (``execute_unprepared``)."""

    rows: list[Any] = []
    for chunk in _chunks(sorted(set(values)), _IN_LIST_SIZE):
        marks = ", ".join("?" * len(chunk))
        rows.extend(
            connection.execute_unprepared(
                f"{select} WHERE project_id = ? AND {column} IN ({marks}){tail}",
                (project_id, *chunk, *tail_parameters),
            ).fetchall()
        )
    return rows


def _span_row(project_id: str, span: _Prepared, now: int) -> tuple[Any, ...]:
    return (
        project_id,
        span.trace_id,
        span.span_id,
        span.parent_span_id,
        span.name,
        span.scope_name,
        span.start,
        span.end,
        1 if span.end_marker else 0,
        span.resource_digest,
        span.digest,
        now,
        span.text_json,
        span.payload,
    )


class CallStore:
    """Call storage operations on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore, *, timings: CallTimings | None = None) -> None:
        self.store = store
        self.timings = timings or CallTimings()

    # --- identity ---------------------------------------------------------------------

    def key_fingerprint(self, project_id: str, session_key: str) -> str:
        """The HMAC that stands for a session key in ``call_key_generations`` and
        ``call_tombstones`` (project scoped, so keys never link across projects)."""

        return self.store.fingerprint(_SESSION_KEY_NAMESPACE, f"{project_id}\x00{session_key}")

    def call_id(self, project_id: str, session_key: str, generation: int) -> str:
        value = f"{project_id}\x00{session_key}\x00{generation}"
        return "call_" + self.store.fingerprint(_SESSION_CALL_NAMESPACE, value)[:32]

    def trace_key_fingerprint(self, project_id: str, trace_id: bytes) -> str:
        return self.store.fingerprint(_TRACE_KEY_NAMESPACE, f"{project_id}\x00{trace_id.hex()}")

    def trace_call_id(self, project_id: str, trace_id: bytes, generation: int) -> str:
        value = f"{project_id}\x00{trace_id.hex()}\x00{generation}"
        return "call_" + self.store.fingerprint(_TRACE_CALL_NAMESPACE, value)[:32]

    @staticmethod
    def _generation(connection: DatabaseConnection, project_id: str, fingerprint: str) -> int:
        row = connection.execute(
            "SELECT generation FROM call_key_generations "
            "WHERE project_id = ? AND key_fingerprint = ?",
            (project_id, fingerprint),
        ).fetchone()
        return 0 if row is None else int(row["generation"])

    @staticmethod
    def _tombstoned(
        connection: DatabaseConnection, project_id: str, fingerprint: str, now: int
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM call_tombstones WHERE project_id = ? AND key_fingerprint = ? "
                "AND expires_at_unix_nano > ? LIMIT 1",
                (project_id, fingerprint, now),
            ).fetchone()
            is not None
        )

    def resolve_call(self, project_id: str, session_key: str) -> str | None:
        """The call a session key names now (its current generation), or ``None``.

        A read: it takes no write lock. An invalid key, a key no evidence has
        named, and another project's key all give ``None``.
        """

        key = valid_session_key(session_key)
        if key is None:
            return None
        fingerprint = self.key_fingerprint(project_id, key)
        with self.store._connect() as connection:
            call_id = self.call_id(
                project_id, key, self._generation(connection, project_id, fingerprint)
            )
            row = connection.execute(
                "SELECT 1 FROM calls WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ).fetchone()
        return None if row is None else call_id

    # --- recording spans --------------------------------------------------------------

    def _read_preset(self, connection: DatabaseConnection, project_id: str) -> str:
        self.store._require_active_project(connection, project_id)
        row = connection.execute(
            "SELECT capture_preset FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return str(row["capture_preset"])

    def record_spans(
        self,
        project_id: str,
        spans: Sequence[OtlpSpan],
        *,
        rejected_spans: int = 0,
        budget_s: float | None = None,
    ) -> SpanBatchResult:
        """Store one request's accepted spans in one transaction.

        Each span is filtered by the project's capture preset, then stored under
        its trace; traces are bound to calls, parked, or (for a purged key)
        dropped; calls that received new evidence are marked dirty with their due
        time recomputed. An identical re-send stores nothing and touches no call;
        a different payload for a stored span keeps the first and counts a
        ``span_conflict``. ``rejected_spans`` (spans the receiver refused) is
        counted in the same transaction.
        """

        if rejected_spans < 0:
            raise ValueError("rejected_spans cannot be negative")
        # Filter and serialize before taking the write lock; redo it under the lock
        # only if the preset changed in between.
        with self.store._connect() as connection:
            preset = self._read_preset(connection, project_id)
        memo = RequestMemo()
        prepared = [_prepare(span, preset, memo) for span in spans]
        with (
            self.store._mutation("record_spans", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            current = self._read_preset(connection, project_id)
            if current != preset:
                memo = RequestMemo()
                prepared = [_prepare(span, current, memo) for span in spans]
            now = self.store._clock.unix_nano()
            counts = self._record(connection, project_id, prepared, current, now)
            self._bump_counters(
                connection,
                project_id,
                rejected_spans=rejected_spans,
                span_conflicts=counts.conflicts,
                key_disagreements=counts.disagreements,
                purged_call_evidence_dropped=counts.dropped,
            )
            crash_point("before_otlp_commit")
            connection.commit()
        return _batch_result(counts)

    def record_spans_in(
        self,
        connection: DatabaseConnection,
        project_id: str,
        spans: Sequence[OtlpSpan],
        *,
        now: int,
    ) -> SpanBatchResult:
        """``record_spans`` inside the caller's write transaction, under the preset
        read there: for a writer whose spans must commit together with its own rows
        (a connector delivery's recordings and receipt), or not at all."""

        preset = self._read_preset(connection, project_id)
        memo = RequestMemo()
        counts = self._record(
            connection, project_id, [_prepare(span, preset, memo) for span in spans], preset, now
        )
        self._bump_counters(
            connection,
            project_id,
            span_conflicts=counts.conflicts,
            key_disagreements=counts.disagreements,
            purged_call_evidence_dropped=counts.dropped,
        )
        return _batch_result(counts)

    def _record(
        self,
        connection: DatabaseConnection,
        project_id: str,
        prepared: list[_Prepared],
        preset: str,
        now: int,
    ) -> _Counts:
        """Write one batch with a fixed number of statements, whatever its traces.

        Every read is one statement per IN-list chunk over the whole batch (trace
        rows, tombstones, key generations, calls, parked spans, stored spans), and
        every write one ``executemany``; the rows are touched in ``lock_order``.
        """

        counts = _Counts()
        by_trace: dict[bytes, list[_Prepared]] = {}
        for span in prepared:
            by_trace.setdefault(span.trace_id, []).append(span)
        trace_ids = sorted(by_trace)

        # Lock order, traces first: lock the stored trace rows, then decide where each
        # trace's spans go (its bound call, the call its first key names, parked,
        # dropped).
        stored_traces = self._lock_traces(connection, project_id, trace_ids)
        traces: dict[bytes, _Trace] = {}
        for trace_id in trace_ids:
            row = stored_traces.get(trace_id)
            traces[trace_id] = _Trace(
                spans=by_trace[trace_id],
                exists=row is not None,
                bound=row is not None and row["call_id"] is not None,
                first_received=None if row is None else int(row["first_span_received_unix_nano"]),
            )
        self._resolve_targets(connection, project_id, traces, stored_traces, now)
        for trace_id in trace_ids:
            trace = traces[trace_id]
            if trace.dropped:
                counts.dropped += len(trace.spans)
                if trace.exists:
                    counts.dropped += self._delete_trace(connection, project_id, trace_id)
                del traces[trace_id]
                continue
            if trace.target is not None:
                trace.target = counts.targets.setdefault(trace.target.call_id, trace.target)
                if trace.target.created and trace.exists and not trace.bound:
                    # A new call's first evidence is its earliest parked span.
                    trace.target.first_evidence = min(
                        trace.target.first_evidence, trace.first_received
                    )

        connection.executemany(
            "INSERT INTO call_traces(project_id, trace_id, call_id, has_session_span, "
            "first_span_received_unix_nano, last_span_received_unix_nano) "
            "VALUES (?, ?, NULL, 0, ?, ?)",
            [
                (project_id, trace_id, now, now)
                for trace_id in sorted(traces)
                if not traces[trace_id].exists
            ],
        )
        # Then calls: new ones exist before a trace row refers to them.
        self._insert_calls(
            connection,
            project_id,
            [counts.targets[call_id] for call_id in sorted(counts.targets)],
            preset,
        )

        resources = {
            span.resource_digest: span.resource_payload
            for trace in traces.values()
            for span in trace.spans
        }
        connection.executemany(
            "INSERT INTO otlp_resources(project_id, resource_digest, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id, resource_digest) DO NOTHING",
            [(project_id, digest, resources[digest]) for digest in sorted(resources)],
        )
        self._store_spans(connection, project_id, traces, counts, now)

        # Calls again, in order: the ones that received evidence.
        self._add_evidence(
            connection,
            project_id,
            {
                call_id: _Evidence(
                    spans=target.new_spans,
                    end_marker=target.end_marker,
                    tail_spans=target.tail_spans,
                )
                for call_id, target in counts.targets.items()
                if target.new_spans
            },
            now,
        )
        return counts

    def _resolve_targets(
        self,
        connection: DatabaseConnection,
        project_id: str,
        traces: dict[bytes, _Trace],
        stored: dict[bytes, Any],
        now: int,
    ) -> None:
        """Give each trace the call its spans go to (``target``; ``None`` while it
        is parked), or mark it ``dropped``: a new trace of a purged call, or a trace
        whose first key is purged. Reads the batch's tombstones, generations and
        calls with one statement each (per chunk)."""

        trace_fingerprints = {
            trace_id: self.trace_key_fingerprint(project_id, trace_id)
            for trace_id, trace in traces.items()
            if not trace.exists
        }
        keys: dict[bytes, SessionKey] = {}
        for trace_id, trace in traces.items():
            key = (
                None
                if trace.bound
                else next((span.key for span in trace.spans if span.key is not None), None)
            )
            if key is not None:
                keys[trace_id] = key
        key_fingerprints = {
            key.value: self.key_fingerprint(project_id, key.value) for key in keys.values()
        }
        tombstoned = {
            row["key_fingerprint"]
            for row in _rows_in(
                connection,
                "SELECT key_fingerprint FROM call_tombstones",
                project_id,
                "key_fingerprint",
                {*trace_fingerprints.values(), *key_fingerprints.values()},
                tail=" AND expires_at_unix_nano > ?",
                tail_parameters=(now,),
            )
        }
        generations = {
            row["key_fingerprint"]: int(row["generation"])
            for row in _rows_in(
                connection,
                "SELECT key_fingerprint, generation FROM call_key_generations",
                project_id,
                "key_fingerprint",
                set(key_fingerprints.values()) - tombstoned,
            )
        }
        named: dict[bytes, _CallTarget] = {}
        for trace_id, trace in traces.items():
            if trace.bound:
                continue
            if not trace.exists and trace_fingerprints[trace_id] in tombstoned:
                # A trace of a purged call: its late spans are the purged call's evidence.
                trace.dropped = True
                continue
            key = keys.get(trace_id)
            if key is None:
                continue  # parked
            fingerprint = key_fingerprints[key.value]
            if fingerprint in tombstoned:
                trace.dropped = True
                continue
            generation = generations.get(fingerprint, 0)
            named[trace_id] = _CallTarget(
                self.call_id(project_id, key.value, generation),
                fingerprint,
                key=key,
                generation=generation,
                first_evidence=now,
            )
        calls = {
            row["call_id"]: row["key_fingerprint"]
            for row in _rows_in(
                connection,
                "SELECT call_id, key_fingerprint FROM calls",
                project_id,
                "call_id",
                {
                    *(stored[trace_id]["call_id"] for trace_id, t in traces.items() if t.bound),
                    *(target.call_id for target in named.values()),
                },
            )
        }
        for trace_id, trace in traces.items():
            if trace.bound:
                call_id = stored[trace_id]["call_id"]
                trace.target = _CallTarget(call_id, calls[call_id])
            elif trace_id in named:
                trace.target = named[trace_id]
                trace.target.created = trace.target.call_id not in calls

    @staticmethod
    def _insert_calls(
        connection: DatabaseConnection,
        project_id: str,
        targets: Sequence[_CallTarget],
        preset: str,
        *,
        close_due: int | None = None,
    ) -> None:
        """Create the new session-keyed calls among ``targets``. Spans then give them
        their evidence (``_add_evidence``); a call a recording creates is due at
        ``close_due``."""

        rows = []
        for target in targets:
            if not target.created:
                continue
            key = target.key
            if key is None:  # pragma: no cover - only keyed targets are created here
                raise ValueError("a new call needs the key that named it")
            rows.append(
                (
                    project_id,
                    target.call_id,
                    key.value if preset == CAPTURE_PRESET_FULL else None,
                    target.key_fingerprint,
                    key.source,
                    target.generation,
                    target.first_evidence,
                    target.first_evidence,
                    close_due,
                    0 if close_due is None else 1,
                    target.first_evidence,
                )
            )
        connection.executemany(
            "INSERT INTO calls(project_id, call_id, session_key, key_fingerprint, key_source, "
            "generation, state, first_evidence_unix_nano, last_evidence_unix_nano, "
            "close_due_unix_nano, dirty, created_unix_nano) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            rows,
        )

    def recording_call(
        self, connection: DatabaseConnection, project_id: str, session_key: str, now: int
    ) -> str | None:
        """The call a recording's (valid, trimmed) session key names, created if needed.

        A new call is keyed by the recording (``key_source = 'recording'``) and,
        with no spans and no end marker yet, is due ``LONG_QUIET`` from now.
        ``None`` while a live tombstone covers the key: its evidence is
        refused. Runs inside the caller's write transaction.
        """

        fingerprint = self.key_fingerprint(project_id, session_key)
        if self._tombstoned(connection, project_id, fingerprint, now):
            return None
        generation = self._generation(connection, project_id, fingerprint)
        call_id = self.call_id(project_id, session_key, generation)
        exists = connection.execute(
            "SELECT 1 FROM calls WHERE project_id = ? AND call_id = ?", (project_id, call_id)
        ).fetchone()
        if exists is None:
            target = _CallTarget(
                call_id,
                fingerprint,
                created=True,
                key=SessionKey(session_key, RECORDING_KEY_SOURCE),
                generation=generation,
                first_evidence=now,
            )
            self._insert_calls(
                connection,
                project_id,
                [target],
                CAPTURE_PRESET_FULL,
                close_due=self._close_due(state="open", ended=False, now=now),
            )
        return call_id

    def add_recording_evidence(
        self, connection: DatabaseConnection, project_id: str, call_id: str, now: int
    ) -> None:
        """A recording of ``call_id`` was stored at ``now``: count it and mark the call
        dirty under the close rules. Runs inside the caller's write transaction."""

        self._add_evidence(connection, project_id, {call_id: _Evidence(recordings=1)}, now)

    @staticmethod
    def _lock_traces(
        connection: DatabaseConnection, project_id: str, trace_ids: Sequence[bytes]
    ) -> dict[bytes, Any]:
        """Read (and on Postgres lock, in sorted order) the stored rows of ``trace_ids``."""

        rows: dict[bytes, Any] = {}
        for chunk in _chunks(trace_ids, _IN_LIST_SIZE):
            marks = ", ".join("?" * len(chunk))
            select = (
                "SELECT trace_id, call_id, first_span_received_unix_nano FROM call_traces "
                f"WHERE project_id = ? AND trace_id IN ({marks}) ORDER BY trace_id"
            )
            for row in connection.execute_unprepared(
                connection.dialect.sql(
                    "lock-call-traces",
                    # SQLite's write transaction already holds the whole database.
                    sqlite=select,
                    postgresql=select + " FOR UPDATE",
                ),
                (project_id, *chunk),
            ).fetchall():
                rows[bytes(row["trace_id"])] = row
        return rows

    def _store_spans(
        self,
        connection: DatabaseConnection,
        project_id: str,
        traces: dict[bytes, _Trace],
        counts: _Counts,
        now: int,
    ) -> None:
        """Insert the batch's new spans, bind or park their traces, and credit calls.

        A span already stored (or repeated earlier in the batch) is not inserted:
        the same payload and resource count as a duplicate, anything else as a
        ``span_conflict``, and the first write is kept. The stored spans and the
        parked spans of joining traces are read once per chunk and the new spans
        inserted with one ``executemany``; the write lock (and the trace rows'
        locks) keep those reads current.
        """

        stored = self._stored_span_digests(
            connection,
            project_id,
            [
                (trace_id, span.span_id)
                for trace_id, trace in traces.items()
                if trace.exists
                for span in trace.spans
            ],
        )
        joining = {
            trace_id
            for trace_id, trace in traces.items()
            if trace.target is not None and trace.exists and not trace.bound
        }
        parked = {
            bytes(row["trace_id"]): (int(row["spans"]), bool(row["ended"]))
            for row in _rows_in(
                connection,
                "SELECT trace_id, count(*) AS spans, max(end_marker) AS ended FROM spans",
                project_id,
                "trace_id",
                joining,
                tail=" GROUP BY trace_id",
            )
        }
        fingerprints: dict[str, str] = {}
        rows: list[tuple[Any, ...]] = []
        trace_updates: list[tuple[Any, ...]] = []
        for trace_id in sorted(traces):
            trace = traces[trace_id]
            target = trace.target
            if trace_id in joining:
                spans, ended = parked.get(trace_id, (0, False))
                target.new_spans += spans
                target.end_marker = target.end_marker or ended
            new_spans = session_span = 0
            for span in sorted(trace.spans, key=lambda item: item.span_id):
                first = stored.get((trace_id, span.span_id))
                if first is not None:
                    if first == (span.digest, span.resource_digest):
                        counts.duplicates += 1
                    else:
                        counts.conflicts += 1
                    continue
                stored[(trace_id, span.span_id)] = (span.digest, span.resource_digest)
                rows.append(_span_row(project_id, span, now))
                new_spans += 1
                session_span = session_span or span.session_span
                if target is None:
                    continue
                target.tail_spans += span.tail
                target.end_marker = target.end_marker or span.end_marker
                if span.key is not None:
                    fingerprint = fingerprints.get(span.key.value)
                    if fingerprint is None:
                        fingerprint = self.key_fingerprint(project_id, span.key.value)
                        fingerprints[span.key.value] = fingerprint
                    if fingerprint != target.key_fingerprint:
                        counts.disagreements += 1
            counts.stored += new_spans
            if target is None:
                counts.parked += new_spans
            else:
                target.new_spans += new_spans
            if new_spans or trace_id in joining:
                trace_updates.append(
                    (
                        None if target is None else target.call_id,
                        now,
                        1 if session_span else 0,
                        project_id,
                        trace_id,
                    )
                )
        inserted = connection.executemany(
            "INSERT INTO spans(project_id, trace_id, span_id, parent_span_id, name, "
            "scope_name, start_unix_nano, end_unix_nano, end_marker, resource_digest, "
            "digest, received_unix_nano, text_json, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, trace_id, span_id) DO NOTHING",
            rows,
        ).rowcount
        if rows and inserted != len(rows):
            # Nothing else writes spans while this transaction holds the write lock.
            raise StorageError("stored spans changed under the store write lock")
        connection.executemany(
            "UPDATE call_traces SET call_id = ?, last_span_received_unix_nano = ?, "
            "has_session_span = CASE WHEN ? = 1 THEN 1 ELSE has_session_span END "
            "WHERE project_id = ? AND trace_id = ?",
            trace_updates,
        )

    @staticmethod
    def _stored_span_digests(
        connection: DatabaseConnection, project_id: str, keys: Sequence[tuple[bytes, bytes]]
    ) -> dict[tuple[bytes, bytes], tuple[bytes, bytes]]:
        """``(trace, span) -> (digest, resource_digest)`` of the listed spans already
        stored: one primary-key lookup per span, ``_IN_LIST_SIZE // 2`` spans per
        statement (two parameters each)."""

        stored: dict[tuple[bytes, bytes], tuple[bytes, bytes]] = {}
        for chunk in _chunks(sorted(set(keys)), _IN_LIST_SIZE // 2):
            pairs = ", ".join("(?, ?)" for _ in chunk)
            for row in connection.execute_unprepared(
                "SELECT trace_id, span_id, digest, resource_digest FROM spans "
                f"WHERE project_id = ? AND (trace_id, span_id) IN (VALUES {pairs})",
                (project_id, *(value for key in chunk for value in key)),
            ).fetchall():
                stored[(bytes(row["trace_id"]), bytes(row["span_id"]))] = (
                    bytes(row["digest"]),
                    bytes(row["resource_digest"]),
                )
        return stored

    def _close_due(self, *, state: str, ended: bool, now: int) -> int:
        """The due time after evidence arrives at ``now``."""

        due = now + (self.timings.end_grace_nano if ended else self.timings.long_quiet_nano)
        if state == "closed":
            return max(now + self.timings.late_debounce_nano, due)
        return due

    def _add_evidence(
        self,
        connection: DatabaseConnection,
        project_id: str,
        evidence: dict[str, _Evidence],
        now: int,
    ) -> None:
        """New evidence for calls at ``now``: count it, mark each call dirty and
        recompute its due time, whatever the evidence is. One read and one
        ``executemany`` for the batch, in call order."""

        if not evidence:
            return
        calls = {
            row["call_id"]: row
            for row in _rows_in(
                connection,
                "SELECT call_id, state, end_signal_unix_nano, latest_revision FROM calls",
                project_id,
                "call_id",
                evidence,
            )
        }
        updates = []
        counted_only = []
        capped = []
        for call_id in sorted(evidence):
            call, item = calls[call_id], evidence[call_id]
            end_signal = call["end_signal_unix_nano"]
            gives_end_signal = end_signal is None and item.end_marker
            if not (item.spans - item.tail_spans or item.recordings or gives_end_signal):
                counted_only.append((item.spans, project_id, call_id))
                continue
            if gives_end_signal:
                end_signal = now
            updates.append(
                (
                    now,
                    item.spans,
                    item.recordings,
                    end_signal,
                    self._close_due(state=call["state"], ended=end_signal is not None, now=now),
                    project_id,
                    call_id,
                )
            )
            latest = call["latest_revision"]
            if latest is not None and latest >= REVISION_CAP:
                capped.append(call_id)
        connection.executemany(
            "UPDATE calls SET last_evidence_unix_nano = ?, dirty = 1, "
            "span_count = span_count + ?, recording_count = recording_count + ?, "
            "end_signal_unix_nano = ?, close_due_unix_nano = ? "
            "WHERE project_id = ? AND call_id = ?",
            updates,
        )
        connection.executemany(
            "UPDATE calls SET span_count = span_count + ? WHERE project_id = ? AND call_id = ?",
            counted_only,
        )
        for call_id in capped:
            self.note_evidence_past_cap(connection, project_id, call_id)

    def note_evidence_past_cap(
        self, connection: DatabaseConnection, project_id: str, call_id: str
    ) -> None:
        """A dirty call at the revision cap holds evidence no revision will show:
        flag it (``evidence_after_latest_revision``) and, the first time a call is
        flagged, count it (``revision_cap_reached``: once per call, though a
        call's first end-signal close past the cap clears the flag and later
        evidence sets it again). Runs inside the caller's write transaction."""

        flagged = connection.execute(
            "UPDATE calls SET evidence_after_latest_revision = 1 "
            "WHERE project_id = ? AND call_id = ? AND dirty = 1 "
            "AND evidence_after_latest_revision = 0 AND EXISTS (SELECT 1 FROM calls c "
            "WHERE c.project_id = calls.project_id AND c.call_id = calls.call_id "
            f"AND NOT {UNCAPPED_SQL})",
            (project_id, call_id),
        ).rowcount
        if not flagged:
            return
        first = connection.execute(
            "UPDATE calls SET revision_cap_counted = 1 "
            "WHERE project_id = ? AND call_id = ? AND revision_cap_counted = 0",
            (project_id, call_id),
        ).rowcount
        if first:
            self._bump_counters(connection, project_id, revision_cap_reached=1)

    def _delete_trace(
        self, connection: DatabaseConnection, project_id: str, trace_id: bytes
    ) -> int:
        """Delete a parked trace and its spans; return how many spans it had."""

        digests = [
            bytes(row["resource_digest"])
            for row in connection.execute(
                "SELECT DISTINCT resource_digest FROM spans WHERE project_id = ? AND trace_id = ?",
                (project_id, trace_id),
            ).fetchall()
        ]
        deleted = connection.execute(
            "DELETE FROM spans WHERE project_id = ? AND trace_id = ?", (project_id, trace_id)
        ).rowcount
        connection.execute(
            "DELETE FROM call_traces WHERE project_id = ? AND trace_id = ?", (project_id, trace_id)
        )
        self._delete_unreferenced_resources(connection, project_id, digests)
        return deleted

    @staticmethod
    def _delete_unreferenced_resources(
        connection: DatabaseConnection, project_id: str, digests: Iterable[bytes]
    ) -> None:
        connection.executemany(
            "DELETE FROM otlp_resources WHERE project_id = ? AND resource_digest = ? "
            "AND NOT EXISTS (SELECT 1 FROM spans WHERE spans.project_id = ? "
            "AND spans.resource_digest = ?)",
            [(project_id, digest, project_id, digest) for digest in sorted(set(digests))],
        )

    @staticmethod
    def _bump_counters(connection: DatabaseConnection, project_id: str, **increments: int) -> None:
        rows = [(project_id, name, value) for name, value in sorted(increments.items()) if value]
        if rows:
            connection.executemany(
                "INSERT INTO otlp_counters(project_id, name, value) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id, name) DO UPDATE "
                "SET value = otlp_counters.value + excluded.value",
                rows,
            )

    # --- parked traces --------------------------------------------------------------------

    def collect_parked_traces(
        self, *, limit: int = PARKED_TRACE_BATCH_SIZE, budget_s: float | None = None
    ) -> ParkedTraceCollection:
        """Resolve up to ``limit`` parked traces whose quiet deadline has passed.

        The deadline is ``last_span_received + LONG_QUIET``, so a trace that keeps
        receiving spans is never collected. Oldest deadlines go first.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        promoted = discarded = dropped = 0
        # A read first: an idle collection takes no write lock.
        with self.store._connect() as connection:
            if not self._due_parked_traces(connection, self.store._clock.unix_nano(), 1):
                return ParkedTraceCollection()
        with (
            self.store._mutation("collect_parked_traces", budget_s=budget_s),
            self.store._connect() as connection,
        ):
            connection.begin_write()
            now = self.store._clock.unix_nano()
            due = self._due_parked_traces(connection, now, limit)
            counted: dict[str, dict[str, int]] = {}
            for row in due:
                project_id, trace_id = row["project_id"], bytes(row["trace_id"])
                project_counts = counted.setdefault(project_id, {})
                if not row["has_session_span"]:
                    self._delete_trace(connection, project_id, trace_id)
                    discarded += 1
                    project_counts["parked_traces_discarded"] = (
                        project_counts.get("parked_traces_discarded", 0) + 1
                    )
                    continue
                fingerprint = self.trace_key_fingerprint(project_id, trace_id)
                if self._tombstoned(connection, project_id, fingerprint, now):
                    spans = self._delete_trace(connection, project_id, trace_id)
                    dropped += spans
                    project_counts["purged_call_evidence_dropped"] = (
                        project_counts.get("purged_call_evidence_dropped", 0) + spans
                    )
                    continue
                self._promote(connection, project_id, trace_id, fingerprint, row)
                promoted += 1
            for project_id in sorted(counted):
                self._bump_counters(connection, project_id, **counted[project_id])
            connection.commit()
        return ParkedTraceCollection(
            promoted=promoted, discarded=discarded, purged_call_evidence_dropped=dropped
        )

    def _due_parked_traces(self, connection: DatabaseConnection, now: int, limit: int) -> list[Any]:
        return connection.execute(
            "SELECT project_id, trace_id, has_session_span, first_span_received_unix_nano, "
            "last_span_received_unix_nano FROM call_traces "
            "WHERE call_id IS NULL AND last_span_received_unix_nano <= ? "
            "ORDER BY last_span_received_unix_nano, project_id, trace_id LIMIT ?",
            (now - self.timings.long_quiet_nano, limit),
        ).fetchall()

    def _promote(
        self,
        connection: DatabaseConnection,
        project_id: str,
        trace_id: bytes,
        fingerprint: str,
        row: Any,
    ) -> None:
        """Make a quiet parked trace with a session span a call keyed by its trace.

        Its due time is computed as of its last evidence, as every evidence write
        computes it, so the call is due at once.
        """

        generation = self._generation(connection, project_id, fingerprint)
        call_id = self.trace_call_id(project_id, trace_id, generation)
        spans = connection.execute(
            "SELECT count(*) AS spans, min(CASE WHEN end_marker = 1 THEN received_unix_nano END) "
            "AS ended FROM spans WHERE project_id = ? AND trace_id = ?",
            (project_id, trace_id),
        ).fetchone()
        first = int(row["first_span_received_unix_nano"])
        last = int(row["last_span_received_unix_nano"])
        end_signal = None if spans["ended"] is None else int(spans["ended"])
        connection.execute(
            "INSERT INTO calls(project_id, call_id, session_key, key_fingerprint, key_source, "
            "generation, state, first_evidence_unix_nano, last_evidence_unix_nano, "
            "end_signal_unix_nano, close_due_unix_nano, dirty, span_count, created_unix_nano) "
            "VALUES (?, ?, NULL, ?, ?, ?, 'open', ?, ?, ?, ?, 1, ?, ?)",
            (
                project_id,
                call_id,
                fingerprint,
                TRACE_KEY_SOURCE,
                generation,
                first,
                last,
                end_signal,
                self._close_due(state="open", ended=end_signal is not None, now=last),
                int(spans["spans"]),
                self.store._clock.unix_nano(),
            ),
        )
        connection.execute(
            "UPDATE call_traces SET call_id = ? WHERE project_id = ? AND trace_id = ?",
            (call_id, project_id, trace_id),
        )

    # --- statistics ---------------------------------------------------------------------

    def otlp_stats(self, project_id: str) -> dict[str, int]:
        """The storage half of the receiver counters: the persisted ones plus the derived gauges."""

        with self.store._connect() as connection:
            stats = dict.fromkeys(OTLP_COUNTERS, 0)
            for row in connection.execute(
                "SELECT name, value FROM otlp_counters WHERE project_id = ?", (project_id,)
            ).fetchall():
                stats[row["name"]] = int(row["value"])
            stats["parked_traces"] = int(
                connection.execute(
                    "SELECT count(*) FROM call_traces WHERE project_id = ? AND call_id IS NULL",
                    (project_id,),
                ).fetchone()[0]
            )
            stats["open_calls"] = int(
                connection.execute(
                    "SELECT count(*) FROM calls WHERE project_id = ? AND state = 'open'",
                    (project_id,),
                ).fetchone()[0]
            )
        return stats


def parked_trace_job(calls: CallStore) -> MaintenanceJob:
    """A maintenance job: resolve parked traces past their deadline.

    One bounded ``collect_parked_traces`` batch per run; a full batch asks to run
    again at once.
    """

    async def collect(_now_unix_nano: int) -> float | None:
        try:
            result = await asyncio.to_thread(
                calls.collect_parked_traces, budget_s=MAINTENANCE_LOCK_BUDGET_SECONDS
            )
        except StorageBusyError:
            return None
        return 0.0 if result.promoted + result.discarded >= PARKED_TRACE_BATCH_SIZE else None

    return collect
