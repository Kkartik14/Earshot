"""FR-6: the closer claims a due call, materializes its next revision, and finishes it.

``Closer.claim()``, ``materialize()`` and ``finish()`` are the three steps the
server's maintenance loop composes (``close_next``); tests call them one by one,
so an interleaving (a span arriving between claim and finish) is real and needs
no barrier. Every test runs on a real catalog (both dialects), synthetic
Pipecat-shaped OTLP requests replayed through ``CallStore.record_spans`` as the
receiver does, and the store's injected clock (§7 seam 1).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

from earshot.blobstore import LocalBlobStore
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_recordings import RecordingStore
from otlp_support import make_request, make_span, string_attribute
from store_support import catalog, catalog_rows, make_store
from synthetic_otlp import SYNTHETIC_SESSION_KEY, synthetic_requests

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
LONG_QUIET = TIMINGS.long_quiet_nano
END_GRACE = TIMINGS.end_grace_nano
LATE_DEBOUNCE = TIMINGS.late_debounce_nano
LEASE = 60 * SECOND
# The seven synthetic requests arrive 5 s apart: the end marker (request 7)
# at START + 30 s, so the call is due END_GRACE later.
END_SIGNAL = START + 30 * SECOND
DUE = END_SIGNAL + END_GRACE


@dataclass
class World:
    path: Path
    clock: ManualClock
    store: IncidentStore
    calls: CallStore
    recordings: RecordingStore
    closer: object


def _world(tmp_path: Path) -> World:
    from earshot.storage_closer import Closer

    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    closer = Closer(store, calls=calls, recordings=recordings)
    return World(tmp_path, clock, store, calls, recordings, closer)


def _replay(world: World, requests=None) -> str:
    """Record synthetic requests 5 s apart and return the call's ID."""

    call_ids: set[str] = set()
    for request in synthetic_requests() if requests is None else requests:
        call_ids.update(world.calls.record_spans("default", spans_from_request(request)).call_ids)
        world.clock.advance(5 * SECOND)
    [call_id] = call_ids
    return call_id


def _at(world: World, when: int) -> None:
    world.clock.advance(when - world.clock.wall)


def _call(world: World, call_id: str) -> dict:
    columns = (
        "state",
        "dirty",
        "pending_revision",
        "latest_revision",
        "lease_until_unix_nano",
        "lease_token",
        "materialize_attempts",
        "materialize_error",
        "close_due_unix_nano",
        "span_count",
        "recording_count",
        "evidence_after_latest_revision",
    )
    [row] = catalog_rows(
        world.path, f"SELECT {', '.join(columns)} FROM calls WHERE call_id = ?", (call_id,)
    )
    return dict(zip(columns, row, strict=True))


def _revisions(world: World, call_id: str) -> list[tuple]:
    return catalog_rows(
        world.path,
        "SELECT revision, bundle_id, close_reason, finality, span_count, recording_count "
        "FROM call_revisions WHERE call_id = ? ORDER BY revision",
        (call_id,),
    )


def _incidents(world: World) -> list[tuple]:
    return catalog_rows(
        world.path, "SELECT bundle_id, session_id, finality FROM incidents ORDER BY bundle_id"
    )


def _bundle(world: World, bundle_id: str):
    from earshot.codec import decode_incident_protobuf

    return decode_incident_protobuf(world.store.get_artifact(bundle_id)[1])


def _late_span(world: World, span: int, *, name: str = "llm") -> None:
    """One more span of the call's trace (unkeyed: it joins through its trace)."""

    trace = int.from_bytes(spans_from_request(synthetic_requests()[0])[0].span.trace_id, "big")
    world.calls.record_spans(
        "default",
        spans_from_request(make_request([make_span(trace, span, name=name, start=END_SIGNAL)])),
    )


# --- claim (AC6.2) -------------------------------------------------------------------------


def test_a_call_is_claimed_once_it_is_due_and_the_claim_snapshots_it(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)

    _at(world, DUE - 1)
    assert world.closer.claim() is None
    _at(world, DUE)
    claim = world.closer.claim()

    assert (claim.call_id, claim.revision, claim.close_reason, claim.final) == (
        call_id,
        1,
        "end_signal",
        True,
    )
    assert len(claim.span_keys) == 17
    assert claim.recordings == ()
    call = _call(world, call_id)
    assert (call["dirty"], call["pending_revision"], call["lease_token"]) == (0, 1, 1)
    assert call["lease_until_unix_nano"] == DUE + LEASE
    assert call["state"] == "open"


def test_a_leased_call_is_claimed_again_only_once_its_lease_has_expired(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    first = world.closer.claim()

    _at(world, DUE + LEASE)
    assert world.closer.claim() is None
    _at(world, DUE + LEASE + 1)
    again = world.closer.claim()

    # The pending revision keeps the call claimable without new evidence (L3).
    assert (again.call_id, again.revision) == (call_id, first.revision)
    assert again.lease_token == first.lease_token + 1


def test_a_call_of_a_project_being_deleted_does_not_stall_the_closer(tmp_path) -> None:
    world = _world(tmp_path)
    world.store.create_project("leaving", display_name="Leaving")
    request = make_request([make_span(5, 1, attributes=[string_attribute("session.id", "a")])])
    world.calls.record_spans("leaving", spans_from_request(request))
    call_id = _replay(world)
    world.store.begin_project_deletion("leaving")
    _at(world, END_SIGNAL + LONG_QUIET)

    outcome = world.closer.close_next()

    assert (outcome.call_id, outcome.result) == (call_id, "revision")
    assert world.closer.close_next() is None


# --- finish (AC6.3) ------------------------------------------------------------------------


def test_finishing_stores_the_revision_and_closes_the_call_in_one_transaction(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)

    claim = world.closer.claim()
    world.closer.finish(world.closer.materialize(claim))

    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 17, 0)]
    assert _incidents(world) == [(f"{call_id}.r1", call_id, "final")]
    call = _call(world, call_id)
    assert (call["state"], call["latest_revision"], call["pending_revision"]) == ("closed", 1, None)
    assert (call["lease_until_unix_nano"], call["materialize_attempts"], call["dirty"]) == (
        None,
        0,
        0,
    )
    assert world.closer.claim() is None


def test_a_finish_whose_lease_was_taken_over_commits_nothing(tmp_path) -> None:
    from earshot.storage_closer import StaleClaimError

    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    stale = world.closer.materialize(world.closer.claim())
    _at(world, DUE + LEASE + 1)
    current = world.closer.materialize(world.closer.claim())

    with pytest.raises(StaleClaimError):
        world.closer.finish(stale)
    assert _incidents(world) == []
    assert _revisions(world, call_id) == []
    assert _call(world, call_id)["pending_revision"] == 1

    world.closer.finish(current)
    assert [row[0] for row in _revisions(world, call_id)] == [1]


def test_a_span_arriving_between_claim_and_finish_makes_the_next_revision(tmp_path) -> None:
    # AC6.8: claim, a real span for the call, then materialize + finish.
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    claim = world.closer.claim()

    _late_span(world, 900)
    world.closer.finish(world.closer.materialize(claim))

    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 17, 0)]
    call = _call(world, call_id)
    assert (call["dirty"], call["span_count"], call["state"]) == (1, 18, "closed")
    late = (900).to_bytes(8, "big").hex()
    first = _bundle(world, f"{call_id}.r1")
    assert late not in {operation.span_id for operation in first.profile.operations}

    _at(world, DUE + END_GRACE)
    world.closer.close_next()

    assert _revisions(world, call_id)[1] == (2, f"{call_id}.r2", "late_data", "final", 18, 0)
    second = _bundle(world, f"{call_id}.r2")
    assert late in {operation.span_id for operation in second.profile.operations}
    assert _call(world, call_id)["dirty"] == 0


# --- late data, the hold and recordings (FR-6 close rules, AC9.2) ----------------------------


def test_late_data_after_close_waits_out_the_debounce_then_makes_the_next_revision(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    world.closer.close_next()

    late = DUE + 100 * SECOND
    _at(world, late)
    _late_span(world, 901)
    _at(world, late + LATE_DEBOUNCE - 1)
    assert world.closer.close_next() is None
    _at(world, late + LATE_DEBOUNCE)
    outcome = world.closer.close_next()

    assert (outcome.revision, outcome.result) == (2, "revision")
    assert [row[:4] for row in _revisions(world, call_id)] == [
        (1, f"{call_id}.r1", "end_signal", "final"),
        (2, f"{call_id}.r2", "late_data", "final"),
    ]


def _manifest(speaker: str, started_at: int = START):
    from earshot.storage_recordings import RecordingManifest

    return RecordingManifest(
        recording_id=f"conv-1.{speaker}",
        session_key=SYNTHETIC_SESSION_KEY,
        speaker=speaker,
        content_type="audio/wav",
        started_at_unix_nano=str(started_at),
    )


def _store_recording(world: World, speaker: str, body: bytes = b"RIFF" + b"\x00" * 60) -> None:
    from earshot.blobstore import put_stream

    attempt = world.recordings.begin_upload("default", f"conv-1.{speaker}")
    try:
        sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [body])
        world.recordings.finish_upload(
            "default",
            f"conv-1.{speaker}",
            attempt.blob_key,
            sha256=sha256,
            size=size,
            duration_ms=None,
        )
    finally:
        world.recordings.end_upload("default", attempt.blob_key)


def test_a_recording_stored_after_close_is_in_the_next_revision_after_the_debounce(
    tmp_path,
) -> None:
    # AC9.2's after-close half.
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    world.closer.close_next()

    stored_at = DUE + 50 * SECOND
    _at(world, stored_at)
    world.recordings.create("default", _manifest("user"))
    _store_recording(world, "user")
    _at(world, stored_at + LATE_DEBOUNCE - 1)
    assert world.closer.close_next() is None
    _at(world, stored_at + LATE_DEBOUNCE)
    world.closer.close_next()

    assert _revisions(world, call_id)[1] == (2, f"{call_id}.r2", "late_data", "final", 17, 1)
    refs = _bundle(world, f"{call_id}.r2").profile.media_refs
    assert [ref.media_id for ref in refs] == ["recording-conv-1.user"]


def test_a_pending_recording_holds_a_call_that_runs_far_longer_than_the_hold(tmp_path) -> None:
    # The hold runs from the call's latest evidence (WS6's pending_deadline), not from
    # the recording's POST: a 20-minute call still gets its recordings in revision 1.
    world = _world(tmp_path)
    world.recordings.create("default", _manifest("user"))
    world.recordings.create("default", _manifest("agent"))
    requests = synthetic_requests()
    _replay(world, requests[:6])
    for minute in range(1, 20):
        _at(world, START + minute * 60 * SECOND)
        _late_span(world, 1000 + minute)  # the call goes on: a new span every minute
        world.recordings.fail_stale_uploads()
        assert world.closer.close_next() is None
    ended = START + 20 * 60 * SECOND
    _at(world, ended)
    call_id = _replay(world, requests[6:])  # the end marker
    _at(world, ended + END_GRACE)
    assert world.closer.close_next() is None  # due, but held: the uploads are pending

    _at(world, ended + 2 * 60 * SECOND)
    world.recordings.fail_stale_uploads()
    _store_recording(world, "user")
    _store_recording(world, "agent")
    assert world.closer.close_next() is None  # the upload is evidence: due END_GRACE later
    _at(world, ended + 2 * 60 * SECOND + END_GRACE)
    world.closer.close_next()

    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 36, 2)]


def test_an_upload_still_streaming_holds_its_call_past_the_deadline_into_revision_one(
    tmp_path,
) -> None:
    # Tribunal round 2, Linus N-1 / Lamport LAM2-5: the docs promise that a streaming
    # PUT holds its call, but the claim ignored uploads in flight, so the call closed
    # as revision 1 without its audio while the audio was still arriving.
    from earshot.blobstore import put_stream

    world = _world(tmp_path)
    call_id = _replay(world)
    world.recordings.create("default", _manifest("user"))
    attempt = world.recordings.begin_upload("default", "conv-1.user")  # the PUT streams
    _at(world, DUE + TIMINGS.upload_hold_nano + LONG_QUIET)  # long past due and deadline

    while_streaming = world.closer.claim()
    swept = world.recordings.fail_stale_uploads()
    sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [b"RIFF" + bytes(60)])
    world.recordings.finish_upload(
        "default", "conv-1.user", attempt.blob_key, sha256=sha256, size=size, duration_ms=None
    )
    world.recordings.end_upload("default", attempt.blob_key)
    _at(world, world.clock.wall + END_GRACE)  # the stored recording is evidence
    closed = world.closer.close_next()

    assert (while_streaming, swept) == (None, 0)
    assert (closed.result, closed.revision) == ("revision", 1)
    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 17, 1)]


def test_a_call_whose_only_recording_failed_closes_without_a_revision(tmp_path) -> None:
    world = _world(tmp_path)
    created, _ = world.recordings.create("default", _manifest("user"))
    hold_ends = START + TIMINGS.upload_hold_nano

    _at(world, START + LONG_QUIET)
    assert world.closer.close_next() is None  # due, but held by the pending upload
    _at(world, hold_ends + 1)
    assert world.recordings.fail_stale_uploads() == 1
    outcome = world.closer.close_next()

    assert outcome.result == "closed_without_revision"
    call = _call(world, created.call_id)
    assert (call["state"], call["latest_revision"], call["pending_revision"]) == (
        "closed",
        None,
        None,
    )
    assert _revisions(world, created.call_id) == []
    assert _incidents(world) == []
    assert world.closer.close_next() is None


# --- failure and integrity (AC6.4, AC6.5) --------------------------------------------------


def _with_mixed_classes(requests):
    """Synthetic requests with one malformed stt span: a transcript and an LLM output on one
    record, which the recorder refuses (one sensitive capture class per record)."""

    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    if span.name == "stt":
                        span.attributes.append(
                            string_attribute("gen_ai.output.messages", "not an stt field")
                        )
                        return requests
    raise AssertionError("the capture has an stt span")


def test_a_failing_materialization_backs_off_exponentially_with_a_stable_code(
    tmp_path, caplog
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world, _with_mixed_classes(synthetic_requests()))
    _at(world, DUE)

    outcome = world.closer.close_next()

    assert (outcome.result, outcome.error) == ("failed", "EARSHOT_MATERIALIZE_INVALID_EVIDENCE")
    call = _call(world, call_id)
    assert (call["materialize_attempts"], call["dirty"], call["pending_revision"]) == (1, 1, None)
    assert call["close_due_unix_nano"] == DUE + 2 * SECOND
    assert call["materialize_error"] == "EARSHOT_MATERIALIZE_INVALID_EVIDENCE"
    assert "not an stt field" not in caplog.text

    attempts = [DUE]
    for tick in range(1, LONG_QUIET // SECOND + 1):
        _at(world, DUE + tick * SECOND)
        if world.closer.close_next() is not None:
            attempts.append(world.clock.wall)

    assert len(attempts) <= 9
    assert [(later - earlier) // SECOND for earlier, later in pairwise(attempts)] == [
        2,
        4,
        8,
        16,
        32,
        64,
        128,
    ]
    assert _call(world, call_id)["materialize_attempts"] == len(attempts)
    assert _revisions(world, call_id) == []


def test_a_revision_row_for_a_still_pending_revision_is_an_integrity_error_set_aside(
    tmp_path, caplog
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    world.closer.close_next()
    # A state AC6.3 makes unreachable: revision 1 is stored and still pending.
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE calls SET pending_revision = 1 WHERE call_id = ?", (call_id,))
        connection.commit()

    assert world.closer.close_next() is None

    assert "EARSHOT_CALL_INTEGRITY" in caplog.text
    assert world.closer.integrity_errors == 1
    call = _call(world, call_id)
    assert (call["pending_revision"], call["materialize_error"]) == (1, "EARSHOT_CALL_INTEGRITY")
    assert [row[0] for row in _revisions(world, call_id)] == [1]
    _late_span(world, 902)
    _at(world, DUE + LEASE + LONG_QUIET)
    assert world.closer.close_next() is None  # set aside, never repaired
    assert world.closer.integrity_errors == 1


def test_a_revision_id_taken_by_another_incident_fails_with_a_conflict_code(tmp_path) -> None:
    from incident_factory import make_valid_bundle

    world = _world(tmp_path)
    call_id = _replay(world)
    world.store.ingest(make_valid_bundle(bundle_id=f"{call_id}.r1"))  # posted by a client
    _at(world, DUE)

    outcome = world.closer.close_next()

    assert (outcome.result, outcome.error) == ("failed", "EARSHOT_MATERIALIZE_CONFLICT")
    assert _revisions(world, call_id) == []
    assert _call(world, call_id)["materialize_error"] == "EARSHOT_MATERIALIZE_CONFLICT"


def test_a_snapshot_whose_spans_are_gone_is_not_materialized(tmp_path) -> None:
    from earshot.storage_closer import SnapshotGoneError

    world = _world(tmp_path)
    _replay(world)
    _at(world, DUE)
    claim = world.closer.claim()
    with catalog(tmp_path) as connection:  # as a purge racing the closer would
        connection.execute("DELETE FROM spans WHERE name = 'tts'")
        connection.commit()

    with pytest.raises(SnapshotGoneError):
        world.closer.materialize(claim)


def _closer_with(world: World, materializer):
    from earshot.storage_closer import Closer

    return Closer(
        world.store, calls=world.calls, recordings=world.recordings, materializer=materializer
    )


def test_a_revision_built_in_the_materializer_process_is_the_one_built_in_process(
    tmp_path,
) -> None:
    from earshot.storage_closer import MaterializerProcess

    world = _world(tmp_path)
    call_id = _replay(world)
    world.recordings.create("default", _manifest("user"))
    _store_recording(world, "user")
    _at(world, DUE + LONG_QUIET)
    claim = world.closer.claim()

    in_process = world.closer.materialize(claim).prepared
    process = MaterializerProcess()
    try:
        isolated = _closer_with(world, process).materialize(claim).prepared
    finally:
        process.close()

    assert in_process.bundle_id == f"{call_id}.r1"
    assert isolated == in_process  # canonical bytes, projected rows and catalog columns
    assert process.pid is None


def test_a_materializer_process_that_dies_is_a_failed_attempt_and_is_replaced(
    tmp_path, monkeypatch, caplog
) -> None:
    from earshot.storage_closer import MaterializerProcess

    world = _world(tmp_path)
    call_id = _replay(world)
    closer = _closer_with(world, MaterializerProcess())
    # The child inherits the crash point; this process never reaches it.
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_CRASH_AT", "during_materialize")
    try:
        closer.start()
        _at(world, DUE)
        failed = closer.close_next()

        call = _call(world, call_id)
        monkeypatch.delenv("EARSHOT_TEST_CRASH_AT")
        _at(world, DUE + 2 * SECOND)
        replaced = closer.close_next()
    finally:
        closer.close()

    assert (failed.result, failed.error) == ("failed", "EARSHOT_MATERIALIZE_WORKER_FAILED")
    assert (call["materialize_attempts"], call["dirty"], call["pending_revision"]) == (1, 1, None)
    assert call["close_due_unix_nano"] == DUE + 2 * SECOND
    assert "(MaterializerWorkerError)" in caplog.text
    assert (replaced.result, replaced.revision) == ("revision", 1)
    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 17, 0)]
    assert _call(world, call_id)["materialize_attempts"] == 0


@pytest.mark.parametrize("test_mode", ["1", "0"], ids=["fault", "inert"])
def test_an_error_after_a_revision_is_stored_reports_the_revision_not_a_failure(
    tmp_path, monkeypatch, caplog, test_mode
) -> None:
    """Tribunal round 3, Linus R3-4: when a step after the finish's commit raised (a
    failed ``chmod``, a lost commit answer), ``fail`` rightly recorded nothing, but
    ``close_next`` still logged ``EARSHOT_MATERIALIZE_FAILED`` and returned
    ``failed`` for a stored revision. The declared fault point ``after_catalog_commit``
    raises there; without test mode it does nothing."""

    from earshot.crash_points import fault_point

    world = _world(tmp_path)
    call_id = _replay(world)
    monkeypatch.setenv("EARSHOT_TEST_MODE", test_mode)
    monkeypatch.setenv("EARSHOT_TEST_FAULT_AT", "after_catalog_commit")
    _at(world, DUE)

    outcome = world.closer.close_next()
    _at(world, DUE + LONG_QUIET)  # past any backoff

    assert (outcome.result, outcome.error, outcome.revision) == ("revision", None, 1)
    assert "EARSHOT_MATERIALIZE_FAILED" not in caplog.text
    assert ("EARSHOT_REVISION_STORED_DESPITE_ERROR" in caplog.text) is (test_mode == "1")
    assert _revisions(world, call_id) == [(1, f"{call_id}.r1", "end_signal", "final", 17, 0)]
    assert (_call(world, call_id)["dirty"], _call(world, call_id)["materialize_error"]) == (0, None)
    assert world.closer.close_next() is None
    with pytest.raises(ValueError, match="undeclared fault point"):
        fault_point("before_anything")


# Run in a child Python: its descriptor table is filled to its soft limit but for
# the two that the materializer's own pipe takes, so ``Process.start()`` fails with
# the host's real EMFILE.
_OUT_OF_DESCRIPTORS = """
import logging, os, resource, sys
import multiprocessing.popen_spawn_posix, multiprocessing.resource_tracker
from earshot.storage_closer import MaterializerProcess, MaterializerWorkerError
logging.basicConfig(stream=sys.stderr, format="%(message)s")
materializer = MaterializerProcess(start_timeout_s=10)
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
held = []
try:
    while True:
        held.append(os.open(os.devnull, os.O_RDONLY))
except OSError:
    pass
os.close(held.pop(0)); os.close(held.pop(0))
try:
    materializer.start()
except MaterializerWorkerError as error:
    print("refused:", error)
"""


def test_a_materializer_that_cannot_be_started_logs_why(tmp_path) -> None:
    """Tribunal round 3, Linus R3-3: when ``Process.start()`` itself failed (no file
    descriptors or processes left), the start was refused with no cause, and a hint
    about ``__main__`` guards that sends an operator the wrong way. Its type and errno
    are logged; its message is not, since an OSError's can quote paths."""

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _OUT_OF_DESCRIPTORS],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert result.stdout.startswith("refused: EARSHOT_MATERIALIZER_START_FAILED"), result
    assert "EARSHOT_MATERIALIZER_START_FAILED" in result.stderr
    assert "OSError, errno 24" in result.stderr, result.stderr
    assert "__main__" not in result.stderr
    assert "Too many open files" not in result.stderr + result.stdout


STALL = "materializer_reading_snapshot"


def _padded(world: World) -> None:
    """A 4 MiB span on the call's trace: its snapshot fills any pipe buffer, so a
    child that stops reading blocks the parent's send."""

    trace = int.from_bytes(spans_from_request(synthetic_requests()[0])[0].span.trace_id, "big")
    padding = [string_attribute("padding", "x" * (4 << 20))]
    world.calls.record_spans(
        "default",
        spans_from_request(make_request([make_span(trace, 950, attributes=padding)])),
    )


def _within(seconds: float, function, *arguments):
    """``function(*arguments)`` in a thread: ``(result or exception, elapsed s)``, or
    ``None`` if it has not returned within ``seconds``."""

    import threading

    outcome: list = []

    def run() -> None:
        started = time.monotonic()
        try:
            outcome.append(function(*arguments))
        except BaseException as error:
            outcome.append(error)
        outcome.append(time.monotonic() - started)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return None if thread.is_alive() else (outcome[0], outcome[1])


def _kill_child(process) -> None:
    import os
    import signal

    if process.pid is not None:
        os.kill(process.pid, signal.SIGKILL)


def test_the_stall_point_is_inert_without_test_mode_even_in_the_real_child(
    tmp_path, monkeypatch
) -> None:
    from earshot.crash_points import stall_point
    from earshot.storage_closer import MaterializerProcess

    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", STALL)
    for test_mode in (None, "0", "true"):
        if test_mode is None:
            monkeypatch.delenv("EARSHOT_TEST_MODE", raising=False)
        else:
            monkeypatch.setenv("EARSHOT_TEST_MODE", test_mode)
        assert _within(5, stall_point, STALL)[0] is None  # returns at once
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", "another_step")
    assert _within(5, stall_point, STALL)[0] is None
    with pytest.raises(ValueError, match="undeclared stall point"):
        stall_point("undeclared")

    # The spawned child inherits the environment: named, but not in test mode, it builds.
    monkeypatch.delenv("EARSHOT_TEST_MODE")
    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", STALL)
    world = _world(tmp_path)
    call_id = _replay(world)
    _padded(world)
    closer = _closer_with(world, MaterializerProcess(timeout_s=60.0))
    try:
        closer.start()
        _at(world, world.clock.wall + LONG_QUIET)  # due, the padding span included
        outcome = closer.close_next()
    finally:
        closer.close()

    assert (outcome.result, outcome.revision) == ("revision", 1)
    assert [row[0] for row in _revisions(world, call_id)] == [1]


def test_a_materializer_child_that_stops_reading_is_killed_at_the_deadline_and_replaced(
    tmp_path, monkeypatch, caplog
) -> None:
    # Lamport LAM2-2: the build's deadline bounded only the wait for an answer, not the
    # sends before it. A child that is alive but not reading (stopped, frozen,
    # swapping) blocked the closer in ``send_bytes`` for good once the pipe was full.
    from earshot.storage_closer import MaterializerProcess

    world = _world(tmp_path)
    call_id = _replay(world)
    _padded(world)
    process = MaterializerProcess(timeout_s=2.0)
    closer = _closer_with(world, process)
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")  # the spawned child inherits these
    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", STALL)
    try:
        closer.start()
        _at(world, world.clock.wall + LONG_QUIET)  # due, the padding span included
        finished = _within(30, closer.close_next)
        if finished is None:
            _kill_child(process)  # unblock the stuck send so the test can clean up
        call = _call(world, call_id)
        monkeypatch.delenv("EARSHOT_TEST_STALL_AT")
        _at(world, world.clock.wall + 2 * SECOND)  # the first failure's backoff
        replaced = closer.close_next() if finished is not None else None
    finally:
        closer.close()

    assert finished is not None, "close_next did not return within 30 s"
    failed, seconds = finished
    assert (failed.result, failed.error) == ("failed", "EARSHOT_MATERIALIZE_WORKER_FAILED")
    assert 1.5 < seconds < 15
    assert (call["materialize_attempts"], call["dirty"], call["pending_revision"]) == (1, 1, None)
    assert "(MaterializerWorkerError)" in caplog.text
    assert (replaced.result, replaced.revision) == ("revision", 1)


def test_closing_the_materializer_never_waits_on_a_child_that_stopped_reading(
    monkeypatch,
) -> None:
    # Lamport LAM2-2: ``close`` (the server's shutdown) waited for the build lock, so
    # one stalled child made a graceful stop need SIGKILL.
    from earshot.storage_closer import (
        MaterializerClosedError,
        MaterializerProcess,
        RevisionInput,
    )

    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", STALL)
    process = MaterializerProcess(close_grace_s=1.0)  # the 600 s build deadline stands
    process.start()
    child = process.pid
    snapshot = RevisionInput("default", "call", 1, False, "full", (), {})
    spans = [(b"d" * 32, b"x" * (1 << 20)) for _ in range(16)]  # 16 MiB: past any pipe
    building = _within_background(process.build, snapshot, iter(spans))
    time.sleep(1.0)  # the build is now blocked sending to the stalled child

    closed = _within(15, process.close)
    if closed is None:
        _kill_child(process)
    built = building.result(30)

    assert closed is not None, "close did not return within 15 s"
    assert closed[1] < 6.0
    assert isinstance(built, MaterializerClosedError)
    assert not _running(child)
    assert process.pid is None


def _within_background(function, *arguments):
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1)

    def run():
        try:
            return function(*arguments)
        except BaseException as error:
            return error

    future = executor.submit(run)
    executor.shutdown(wait=False)
    return future


def _running(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _DeletingTheProjectMidBuild:
    """The in-process materializer, while a project deletion begins: a real
    interleaving of the closer's build and its finish."""

    def __init__(self, store, project_id: str) -> None:
        from earshot.storage_closer import InProcessMaterializer

        self.store, self.project_id = store, project_id
        self.inner = InProcessMaterializer()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def build(self, snapshot, spans):
        prepared = self.inner.build(snapshot, spans)
        self.store.begin_project_deletion(self.project_id)
        return prepared


def test_whatever_else_a_finish_raises_is_a_failed_attempt_with_a_code(tmp_path) -> None:
    # Linus R-4, Lamport R1: an exception from finish other than a lost lease or a
    # taken revision ID backs off with a stable code, never an endless silent retry.
    world = _world(tmp_path)
    world.store.create_project("leaving", display_name="Leaving")
    request = make_request([make_span(5, 1, attributes=[string_attribute("session.id", "a")])])
    [call_id] = world.calls.record_spans("leaving", spans_from_request(request)).call_ids
    closer = _closer_with(world, _DeletingTheProjectMidBuild(world.store, "leaving"))
    _at(world, START + LONG_QUIET)

    outcome = closer.close_next()

    assert (outcome.result, outcome.error) == ("failed", "EARSHOT_MATERIALIZE_FINISH_FAILED")
    call = _call(world, call_id)
    assert (call["materialize_attempts"], call["pending_revision"], call["dirty"]) == (1, None, 1)
    assert call["materialize_error"] == "EARSHOT_MATERIALIZE_FINISH_FAILED"
    assert _revisions(world, call_id) == []


def test_a_failure_reported_after_its_finish_committed_changes_nothing(tmp_path) -> None:
    # Lamport LAM2-1: ``close_next`` reports any exception from ``finish`` as a failed
    # attempt. When the finish had committed (a post-commit step raised, or a commit
    # whose answer was lost), the call's revision is stored and its claim is over:
    # ``fail`` must not make it dirty again, which built a duplicate revision.
    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    claim = world.closer.claim()
    world.closer.finish(world.closer.materialize(claim))
    stored = _call(world, call_id)

    recorded = world.closer.fail(claim, "EARSHOT_MATERIALIZE_FINISH_FAILED")

    assert recorded is False
    assert _call(world, call_id) == stored
    assert (stored["dirty"], stored["pending_revision"], stored["latest_revision"]) == (0, None, 1)
    _at(world, DUE + LONG_QUIET)  # past any backoff the failure would have set
    assert world.closer.close_next() is None
    assert [row[0] for row in _revisions(world, call_id)] == [1]


def test_a_pending_recording_holds_its_call_until_its_deadline_and_not_a_nanosecond_more(
    tmp_path,
) -> None:
    # The claim's hold is ``storage_recordings.pending_deadline`` restated in SQL.
    from earshot.storage_recordings import pending_deadline

    world = _world(tmp_path)
    created, _ = world.recordings.create("default", _manifest("user"))
    deadline = pending_deadline(START, START, None, hold_nano=TIMINGS.upload_hold_nano)

    _at(world, deadline - 1)  # long past due (LONG_QUIET), one nanosecond short of it
    held = world.closer.claim()
    _at(world, deadline)
    free = world.closer.claim()

    assert held is None
    assert free.call_id == created.call_id


@pytest.mark.sqlite_only
def test_the_claims_candidate_reads_are_index_ranges_of_calls_never_a_scan(tmp_path) -> None:
    # Tribunal round 2 (found re-measuring Bellard N-1): the claim's one candidate
    # query OR-ed "dirty and due" with "pending revision", so SQLite walked every call
    # of the catalog, once a tick and again inside the claim's write lock. Each
    # candidate read must be a range of a ``calls`` index.
    from earshot.storage_closer import claim_candidate_queries

    world = _world(tmp_path)
    _replay(world)
    for other in range(30):  # open calls, not yet due
        attributes = [string_attribute("earshot.session.id", f"other-{other}")]
        request = make_request([make_span(500 + other, 1, attributes=attributes)])
        world.calls.record_spans("default", spans_from_request(request))
    now = DUE
    horizon = now - TIMINGS.upload_hold_nano

    plans = {}
    with catalog(tmp_path) as connection:
        for sql, parameters in claim_candidate_queries(now, horizon, limit=3):
            plans[sql] = [
                tuple(row)[-1]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
            ]

    scans = {sql: plan for sql, plan in plans.items() if "SCAN c" in plan}
    assert scans == {}
    for plan in plans.values():
        [calls_step] = [step for step in plan if step.startswith("SEARCH c ")]
        assert "calls_pending_revision_idx" in calls_step or "calls_close_due_idx" in calls_step
        assert not any("recordings_state_idx" in step for step in plan), plan


# --- the revision cap (AC6.7) --------------------------------------------------------------


def _keyed_spans(span: int, *, ended: bool = False):
    attributes = [string_attribute("earshot.session.id", "capped-call")]
    if ended:
        attributes.append(string_attribute("earshot.call.ended", "x"))
    request = make_request([make_span(77, span, attributes=attributes)])
    if ended:
        request.resource_spans[0].scope_spans[0].spans[0].attributes[-1].value.bool_value = True
    return spans_from_request(request)


def test_a_call_gets_at_most_sixteen_revisions_plus_its_first_end_signal_close(tmp_path) -> None:
    world = _world(tmp_path)
    [call_id] = world.calls.record_spans("default", _keyed_spans(1)).call_ids
    for revision in range(1, 17):
        world.clock.advance(LONG_QUIET)
        assert world.closer.close_next().revision == revision
        world.calls.record_spans("default", _keyed_spans(100 + revision))
    assert _call(world, call_id)["latest_revision"] == 16

    world.clock.advance(LONG_QUIET + LEASE)
    assert world.closer.close_next() is None  # capped: evidence stays, no revision
    call = _call(world, call_id)
    assert (call["evidence_after_latest_revision"], call["span_count"]) == (1, 17)
    assert world.calls.otlp_stats("default")["revision_cap_reached"] == 1

    world.calls.record_spans("default", _keyed_spans(200, ended=True))
    world.clock.advance(LATE_DEBOUNCE)  # a closed call: the late-data rule
    outcome = world.closer.close_next()

    assert outcome.revision == 17
    assert _revisions(world, call_id)[-1][:4] == (17, f"{call_id}.r17", "end_signal", "final")
    assert _call(world, call_id)["evidence_after_latest_revision"] == 0
    world.calls.record_spans("default", _keyed_spans(201))
    world.clock.advance(LONG_QUIET + LEASE)
    assert world.closer.close_next() is None
    assert len(_revisions(world, call_id)) == 17
    assert _call(world, call_id)["evidence_after_latest_revision"] == 1
    # Flagged again after revision 17, but a call is counted once (Bellard R-4).
    assert world.calls.otlp_stats("default")["revision_cap_reached"] == 1


# --- what reads revisions: retention, fleet metrics, the project summary -------------------


def _three_revisions(world: World) -> str:
    """The synthetic call closed by its end marker, then two late spans: r1, r2, r3."""

    call_id = _replay(world)
    _at(world, DUE)
    world.closer.close_next()
    for index in range(2):
        _late_span(world, 950 + index)
        world.clock.advance(LATE_DEBOUNCE)
        world.closer.close_next()
    assert [row[0] for row in _revisions(world, call_id)] == [1, 2, 3]
    return call_id


def test_the_retention_reaper_never_purges_a_call_revision_on_its_own(tmp_path) -> None:
    from incident_factory import make_valid_bundle

    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    world.closer.close_next()
    world.store.ingest(make_valid_bundle(bundle_id="not-a-revision"))
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE incidents SET expires_at_unix_nano = '1'")
        connection.commit()

    world.store.purge_expired(limit=100)

    # A revision goes only with its call (AC14.4); call_revisions never dangles.
    assert [row[0] for row in _incidents(world)] == [f"{call_id}.r1"]


def test_fleet_turn_metrics_count_only_the_latest_revision_of_a_call(tmp_path) -> None:
    from incident_factory import make_valid_bundle

    world = _world(tmp_path)
    call_id = _three_revisions(world)
    world.store.ingest(make_valid_bundle(bundle_id="not-a-call"))
    single = len(world.store.list_turn_facts(session_id="session-1"))

    fleet = world.store.summarize_turn_metric_fleet("turn_duration_ms")

    assert sum(group.turn_count for group in fleet.groups) == 3 + single
    assert (fleet.incident_count, fleet.withheld_incident_count) == (2, 0)
    facts = world.store.list_turn_facts(session_id=call_id)
    assert {fact.bundle_id for fact in facts} == {f"{call_id}.r3"}
    assert len(facts) == 3


def test_the_metrics_route_and_the_project_summary_show_a_call_once(tmp_path) -> None:
    from earshot.api import create_app
    from otlp_support import request, serve

    world = _world(tmp_path)
    call_id = _three_revisions(world)

    with serve(create_app(store=world.store)) as port:
        metrics = request(port, "GET", "/v1/metrics/turns?metric=turn_duration_ms").json()
        summary = request(port, "GET", "/v1/projects/default/summary").json()

    assert sum(group["turn_count"] for group in metrics["groups"]) == 3
    assert metrics["incident_count"] == 1
    assert [item["session_id"] for item in summary["items"]] == [call_id]


# --- the maintenance loop (AC6.1, AC4.4 scheduling) -----------------------------------------


def _loop(world: World):
    from earshot.maintenance import MaintenanceLoop
    from earshot.storage_calls import parked_trace_job
    from earshot.storage_closer import call_closer_job

    loop = MaintenanceLoop(clock=world.clock)
    loop.add_job("call-closer", call_closer_job(world.closer))
    loop.add_job("parked-trace-collection", parked_trace_job(world.calls))
    return loop


def test_the_maintenance_loop_closes_due_calls_and_collects_parked_traces(tmp_path) -> None:
    import asyncio

    world = _world(tmp_path)
    loop = _loop(world)
    call_id = _replay(world)
    stray = spans_from_request(make_request([make_span(99, 1, name="GET /health")]))
    world.calls.record_spans("default", stray)  # a trace no span ever keys
    stray_at = world.clock.wall

    asyncio.run(loop.run_once())
    assert (
        world.store.mutation_timing_stats()
        .keys()
        .isdisjoint({"claim_call", "collect_parked_traces"})
    )  # nothing due: no write lock taken
    _at(world, stray_at + LONG_QUIET)
    asyncio.run(loop.run_once())

    assert [row[0] for row in _revisions(world, call_id)] == [1]
    assert world.calls.otlp_stats("default")["parked_traces_discarded"] == 1


def test_a_lease_left_by_a_dead_process_is_released_when_the_loop_starts(tmp_path) -> None:
    import asyncio

    world = _world(tmp_path)
    call_id = _replay(world)
    _at(world, DUE)
    world.closer.claim()  # this "process" dies here: the call stays leased for 60 s

    asyncio.run(_loop(world).run_once())

    assert [row[0] for row in _revisions(world, call_id)] == [1]
    assert _call(world, call_id)["lease_token"] == 2
