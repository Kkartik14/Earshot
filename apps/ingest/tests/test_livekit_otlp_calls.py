"""AC8.2 and the job tail: a real LiveKit 1.8 call through the call catalog and the closer.

LiveKit 1.8.2+ runs a call as a job, and the job runner ends more spans after
``session_close`` (the call's end marker): ``agent_session``, ``on_session_end``,
``session_end_upload``, ``room_disconnect``, ``job_shutdown``, ``job_entrypoint`` (the
trace root) and, when the host registers one, ``shutdown_callback``. They are
stored with the call, but on their own they never make it dirty, move its close or
count as its evidence, so a tail exported after the call closed never makes a
``late_data`` revision. ``agent_session`` still ends a call that has no end signal
yet (LiveKit 1.6 and 1.7 have no ``session_close``). Every test replays the real
capture (``fixtures/captured/livekit-otlp-1.8``) through ``CallStore.record_spans`` as
the receiver does, on the store's injected clock, on both dialects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from captured_livekit_otlp import LIVEKIT_SESSION_KEY, livekit_requests
from earshot.blobstore import LocalBlobStore
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_recordings import RecordingStore
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
END_GRACE = TIMINGS.end_grace_nano
LONG_QUIET = TIMINGS.long_quiet_nano
LATE_DEBOUNCE = TIMINGS.late_debounce_nano
JOB_TAIL = (
    "agent_session",
    "on_session_end",
    "session_end_upload",
    "room_disconnect",
    "job_shutdown",
    "job_entrypoint",
)


@dataclass
class World:
    path: Path
    clock: ManualClock
    store: IncidentStore
    calls: CallStore
    closer: object


def _world(tmp_path: Path) -> World:
    from earshot.storage_closer import Closer

    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    return World(tmp_path, clock, store, calls, Closer(store, calls=calls, recordings=recordings))


def _only(request, names, *, keep: bool):
    """The request with only the spans named in ``names`` (``keep``) or without them."""

    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            spans = list(scope_spans.spans)
            del scope_spans.spans[:]
            scope_spans.spans.extend(span for span in spans if (span.name in names) is keep)
    return request


def _record(world: World, request) -> set[str]:
    result = world.calls.record_spans("default", spans_from_request(request))
    return set(result.call_ids)


def _replay(world: World, requests) -> str:
    """Record the requests 5 s apart, as the real exporter sent them; the call's ID."""

    call_ids: set[str] = set()
    for request in requests:
        call_ids |= _record(world, request)
        world.clock.advance(5 * SECOND)
    [call_id] = call_ids
    return call_id


def _call(world: World, call_id: str) -> dict:
    columns = (
        "state",
        "dirty",
        "latest_revision",
        "span_count",
        "last_evidence_unix_nano",
        "end_signal_unix_nano",
        "close_due_unix_nano",
    )
    [row] = catalog_rows(
        world.path, f"SELECT {', '.join(columns)} FROM calls WHERE call_id = ?", (call_id,)
    )
    return dict(zip(columns, row, strict=True))


def _revisions(world: World, call_id: str) -> list[tuple]:
    return catalog_rows(
        world.path,
        "SELECT revision, close_reason, finality, span_count FROM call_revisions "
        "WHERE call_id = ? ORDER BY revision",
        (call_id,),
    )


def test_the_real_call_ends_at_session_close_in_one_final_revision_of_every_span(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world, livekit_requests())
    ended = _call(world, call_id)["end_signal_unix_nano"]

    assert call_id == world.calls.call_id("default", LIVEKIT_SESSION_KEY, 0)
    assert ended == START + 30 * SECOND  # request 7 carries session_close
    world.clock.advance(ended + END_GRACE - world.clock.wall)
    world.closer.close_next()

    assert _revisions(world, call_id) == [(1, "end_signal", "final", 58)]
    assert world.closer.claim() is None


def test_a_job_tail_exported_after_the_close_is_stored_but_never_reopens_the_call(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    *early, last = livekit_requests()
    call_id = _replay(world, [*early, _only(last, JOB_TAIL, keep=False)])
    world.clock.advance(END_GRACE)
    world.closer.close_next()
    closed = _call(world, call_id)
    assert (closed["state"], closed["dirty"], closed["span_count"]) == ("closed", 0, 52)

    world.clock.advance(100 * SECOND)
    assert _record(world, _only(livekit_requests()[-1], JOB_TAIL, keep=True)) == {call_id}

    after = _call(world, call_id)
    assert after == {**closed, "span_count": 58}
    world.clock.advance(LATE_DEBOUNCE + LONG_QUIET)
    assert world.closer.close_next() is None
    assert _revisions(world, call_id) == [(1, "end_signal", "final", 52)]


def test_a_job_tail_span_before_the_end_does_not_move_an_open_calls_close(tmp_path) -> None:
    world = _world(tmp_path)
    requests = livekit_requests()
    call_id = _replay(world, requests[:3])
    before = _call(world, call_id)

    _record(world, _only(livekit_requests()[-1], ("job_shutdown",), keep=True))

    assert _call(world, call_id) == {**before, "span_count": before["span_count"] + 1}


def test_without_session_close_the_agent_session_span_still_ends_the_call(tmp_path) -> None:
    """LiveKit 1.6 and 1.7 end the session span at close and have no ``session_close``."""

    world = _world(tmp_path)
    *early, last = livekit_requests()
    call_id = _replay(world, [*early, _only(last, ("session_close",), keep=False)])

    call = _call(world, call_id)
    ended = START + 30 * SECOND
    assert (call["end_signal_unix_nano"], call["close_due_unix_nano"]) == (
        ended,
        ended + END_GRACE,
    )


def test_an_agent_session_span_after_session_close_does_not_extend_the_call(tmp_path) -> None:
    world = _world(tmp_path)
    *early, last = livekit_requests()
    call_id = _replay(world, [*early, _only(last, ("agent_session",), keep=False)])
    before = _call(world, call_id)

    world.clock.advance(1 * SECOND)
    _record(world, _only(livekit_requests()[-1], ("agent_session",), keep=True))

    assert _call(world, call_id) == {**before, "span_count": 58}
