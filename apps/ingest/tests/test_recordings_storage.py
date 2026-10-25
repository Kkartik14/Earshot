"""Recordings in the catalog (catalog and upload lifecycle).

``RecordingStore`` is the storage half of the recordings API: it creates a
recording and its call, journals every upload attempt, commits the winning
attempt, sweeps uploads that never arrive, and deletes unreferenced blobs after
commit. These tests drive it directly on a real catalog (both dialects) and a real
blob store (local disk, and MinIO where the backend matters), with the store's
injected clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path

import pytest

from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.maintenance import MaintenanceLoop
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_recordings import (
    CallPurgedError,
    PresetForbidsMediaError,
    RecordingConflictError,
    RecordingManifest,
    RecordingNotFoundError,
    RecordingStore,
    blob_deletion_job,
    pending_deadline,
    recording_upload_sweep_job,
)
from otlp_support import make_request, make_span, string_attribute
from store_support import catalog, catalog_rows, make_store
from synthetic_otlp import SYNTHETIC_SESSION_KEY, synthetic_requests

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
HOLD = int(TIMINGS.upload_hold_seconds * SECOND)
LONG_QUIET = TIMINGS.long_quiet_nano
END_GRACE = TIMINGS.end_grace_nano
LATE_DEBOUNCE = TIMINGS.late_debounce_nano
BODY = b"RIFF-not-parsed-here" * 100


def manifest(recording_id: str = "call-1.user", **changes) -> RecordingManifest:
    fields = {
        "recording_id": recording_id,
        "session_key": "call-1",
        "speaker": "user",
        "content_type": "audio/wav",
        "started_at_unix_nano": str(START),
        "label": None,
        **changes,
    }
    return RecordingManifest(**fields)


def _open(tmp_path: Path, blobs=None):
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    blobs = blobs if blobs is not None else LocalBlobStore(tmp_path / "blobs")
    return RecordingStore(store, calls=calls, blobs=blobs), calls, clock


def _call(path: Path, call_id: str) -> tuple:
    return catalog_rows(
        path,
        "SELECT key_source, session_key, dirty, close_due_unix_nano, first_evidence_unix_nano, "
        "last_evidence_unix_nano, recording_count, span_count, state FROM calls "
        "WHERE call_id = ?",
        (call_id,),
    )[0]


def _journal(path: Path) -> list[tuple]:
    return catalog_rows(
        path, "SELECT project_id, blob_key, attempts FROM pending_blob_deletions ORDER BY blob_key"
    )


def _states(path: Path) -> dict[str, str]:
    return dict(catalog_rows(path, "SELECT recording_id, state FROM recordings"))


def _upload(recordings: RecordingStore, recording_id: str = "call-1.user", body: bytes = BODY):
    """One whole attempt, as the API runs it: begin, stream, commit."""

    attempt = recordings.begin_upload("default", recording_id)
    try:
        sha256, size = put_stream(recordings.blobs, attempt.blob_key, [body])
        return attempt, recordings.finish_upload(
            "default", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
    finally:
        recordings.end_upload("default", attempt.blob_key)


def _closer(recordings: RecordingStore):
    """The closer over the same store: the hold is its claim's."""

    from earshot.storage_closer import Closer

    return Closer(recordings.store, calls=recordings.calls, recordings=recordings)


def _spans(session_key: str, trace: int, span: int):
    attributes = [string_attribute("earshot.session.id", session_key)]
    return spans_from_request(make_request([make_span(trace, span, attributes=attributes)]))


# --- create ---------------------------------------------------------------------------------


def test_a_recording_for_a_new_key_creates_its_call_keyed_by_the_recording(tmp_path) -> None:
    recordings, calls, _ = _open(tmp_path)

    recording, created = recordings.create("default", manifest())

    assert created
    assert (recording.recording_id, recording.state, recording.size) == (
        "call-1.user",
        "pending",
        None,
    )
    assert recording.call_id == calls.resolve_call("default", "call-1")
    # This call is not due before LONG_QUIET without spans or an end marker.
    assert _call(tmp_path, recording.call_id) == (
        "recording", "call-1", 1, START + LONG_QUIET, START, START, 0, 0, "open"
    )  # fmt: skip
    assert _journal(tmp_path) == []


@pytest.mark.parametrize(
    ("started_at", "valid"),
    [
        ("1", True),
        (str(2**63 - 1), True),
        ("0", False),
        ("01", False),
        ("-1", False),
        (str(2**63), False),
        (str(2**64 - 1), False),
        ("1e3", False),
        (" 1", False),
        ("1\n", False),
        ("٣", False),
        ("1" * 25, False),
    ],
)
def test_the_store_itself_refuses_a_start_outside_the_catalogs_time_range(
    tmp_path, started_at, valid
) -> None:
    # The store enforces the time range too, so alternate entry points cannot poison a call.
    recordings, _, _ = _open(tmp_path)

    if valid:
        recordings.create("default", manifest(started_at_unix_nano=started_at))
    else:
        with pytest.raises(ValueError, match="start"):
            recordings.create("default", manifest(started_at_unix_nano=started_at))

    assert catalog_rows(tmp_path, "SELECT started_at_unix_nano FROM recordings") == (
        [(started_at,)] if valid else []
    )


def test_spans_and_recordings_of_one_key_share_one_call_whichever_comes_first(tmp_path) -> None:
    recordings, calls, clock = _open(tmp_path)
    calls.record_spans("default", _spans("call-1", 1, 1))
    clock.advance(SECOND)
    joined, _ = recordings.create("default", manifest())
    recorded_first, _ = recordings.create("default", manifest("call-2.user", session_key="call-2"))
    clock.advance(SECOND)
    calls.record_spans("default", _spans("call-2", 2, 1))

    assert joined.call_id == calls.resolve_call("default", "call-1")
    assert _call(tmp_path, joined.call_id)[0] == "earshot.session.id"
    assert recorded_first.call_id == calls.resolve_call("default", "call-2")
    assert _call(tmp_path, recorded_first.call_id)[0] == "recording"  # first binding wins
    assert _call(tmp_path, recorded_first.call_id)[7] == 1
    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(2,)]


def test_the_same_manifest_is_idempotent_and_any_difference_conflicts(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    first, _ = recordings.create("default", manifest(label="left"))
    clock.advance(SECOND)

    again, created = recordings.create("default", manifest(label="left"))

    assert not created and again == first
    for change in (
        {"session_key": "call-9"},
        {"speaker": "agent"},
        {"content_type": "audio/ogg"},
        {"started_at_unix_nano": str(START + 1)},
        {"label": None},
        {"label": "right"},
    ):
        with pytest.raises(RecordingConflictError):
            recordings.create("default", manifest(**{"label": "left", **change}))
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(1,)]


def test_a_recording_is_scoped_to_its_project(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.store.create_project("support", display_name="Support")

    mine, _ = recordings.create("default", manifest())
    theirs, created = recordings.create("support", manifest(speaker="agent"))

    assert created and mine.call_id != theirs.call_id
    assert recordings.get("support", "call-1.user").speaker == "agent"
    assert recordings.get("default", "call-1.user").speaker == "user"
    assert recordings.get("support", "call-2.user") is None
    with pytest.raises(RecordingNotFoundError):
        recordings.begin_upload("support", "call-2.user")


def test_metadata_only_refuses_recordings_and_writes_nothing(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.store.set_capture_preset("default", "metadata_only")

    with pytest.raises(PresetForbidsMediaError):
        recordings.create("default", manifest())

    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(0,)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(0,)]


def test_metadata_only_refuses_an_upload_to_a_recording_created_before_the_switch(
    tmp_path,
) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.create("default", manifest())
    recordings.store.set_capture_preset("default", "metadata_only")

    with pytest.raises(PresetForbidsMediaError):
        recordings.begin_upload("default", "call-1.user")
    assert _journal(tmp_path) == []


def test_a_purged_key_refuses_a_recording_and_counts_the_dropped_evidence(tmp_path) -> None:
    recordings, calls, _ = _open(tmp_path)
    fingerprint = calls.key_fingerprint("default", "call-1")
    with catalog(tmp_path) as connection:
        connection.execute(
            "INSERT INTO call_tombstones(project_id, key_fingerprint, generation, "
            "expires_at_unix_nano) VALUES ('default', ?, 0, ?)",
            (fingerprint, START + SECOND),
        )

    with pytest.raises(CallPurgedError):
        recordings.create("default", manifest())

    assert calls.otlp_stats("default")["purged_call_evidence_dropped"] == 1
    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(0,)]


# --- upload attempts --------------------------------------------------------------------------


def test_an_attempt_is_journaled_under_a_server_key_until_it_commits(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    recording, _ = recordings.create("default", manifest("Call.ID-1"))
    clock.advance(SECOND)

    attempt = recordings.begin_upload("default", "Call.ID-1")
    journal = _journal(tmp_path)
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [BODY])
    stored = recordings.finish_upload(
        "default", "Call.ID-1", attempt.blob_key, sha256=sha256, size=size, duration_ms=1500
    )
    recordings.end_upload("default", attempt.blob_key)

    prefix, _, attempt_id = attempt.blob_key.rpartition("/")
    assert prefix == "recordings/default" and "call" not in attempt_id.lower()
    assert journal == [("default", attempt.blob_key, 0)]
    assert _journal(tmp_path) == []
    assert stored is not None
    assert (stored.state, stored.sha256, stored.size, stored.duration_ms) == (
        "stored",
        hashlib.sha256(BODY).hexdigest(),
        len(BODY),
        1500,
    )
    assert stored.stored_unix_nano == START + SECOND
    assert recordings.get("default", "Call.ID-1") == stored
    assert stored.call_id == recording.call_id


def test_of_two_attempts_the_first_commit_wins_and_the_loser_removes_its_object(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.create("default", manifest())
    first = recordings.begin_upload("default", "call-1.user")
    second = recordings.begin_upload("default", "call-1.user")
    winner = put_stream(recordings.blobs, first.blob_key, [b"first body"])
    loser = put_stream(recordings.blobs, second.blob_key, [b"second body"])

    won = recordings.finish_upload(
        "default", "call-1.user", first.blob_key, sha256=winner[0], size=winner[1], duration_ms=None
    )
    lost = recordings.finish_upload(
        "default", "call-1.user", second.blob_key, sha256=loser[0], size=loser[1], duration_ms=None
    )
    recordings.discard_upload("default", second.blob_key)
    for attempt in (first, second):
        recordings.end_upload("default", attempt.blob_key)

    assert won is not None and won.sha256 == winner[0]
    assert lost is None
    assert recordings.blobs.head(first.blob_key) is not None
    assert recordings.blobs.head(second.blob_key) is None
    assert _journal(tmp_path) == []
    assert recordings.get("default", "call-1.user").blob_key == first.blob_key


def test_an_upload_to_a_stored_or_failed_recording_starts_no_attempt(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    recordings.create("default", manifest())
    recordings.create("default", manifest("call-1.agent", speaker="agent"))
    _upload(recordings)
    clock.advance(HOLD + 1)
    assert recordings.fail_stale_uploads() == 1

    verify = recordings.begin_upload("default", "call-1.user")

    assert verify.blob_key is None and verify.recording.state == "stored"
    with pytest.raises(RecordingConflictError):
        recordings.begin_upload("default", "call-1.agent")
    with pytest.raises(RecordingConflictError):
        recordings.create("default", manifest("call-1.agent", speaker="agent"))
    with pytest.raises(RecordingNotFoundError):
        recordings.begin_upload("default", "call-1.mixed")
    assert _journal(tmp_path) == []


# --- Stored recording evidence ----------------------------------------------------


def test_a_stored_recording_marks_an_open_call_dirty_and_due_a_quiet_period_later(
    tmp_path,
) -> None:
    recordings, _, clock = _open(tmp_path)
    recording, _ = recordings.create("default", manifest())
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE calls SET dirty = 0")
    clock.advance(40 * SECOND)

    _upload(recordings)

    now = START + 40 * SECOND
    assert _call(tmp_path, recording.call_id)[2:7] == (1, now + LONG_QUIET, START, now, 1)


def test_a_stored_recording_on_an_ended_call_is_due_after_the_end_grace(tmp_path) -> None:
    recordings, calls, clock = _open(tmp_path)
    for request in synthetic_requests():  # the synthetic call: its conversation span ends it
        calls.record_spans("default", spans_from_request(request))
    recording, _ = recordings.create("default", manifest(session_key=SYNTHETIC_SESSION_KEY))
    clock.advance(5 * SECOND)

    _upload(recordings)

    now = START + 5 * SECOND
    assert _call(tmp_path, recording.call_id)[2:7] == (1, now + END_GRACE, START, now, 1)


def test_a_recording_stored_after_close_dirties_the_call_after_the_late_debounce(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    recording, _ = recordings.create("default", manifest())
    recordings.create("default", manifest("call-1.agent", speaker="agent"))
    with catalog(tmp_path) as connection:  # the closed call after a revision
        connection.execute(
            "UPDATE calls SET state = 'closed', dirty = 0, close_due_unix_nano = NULL, "
            "end_signal_unix_nano = ?",
            (START,),
        )
    clock.advance(SECOND)

    _upload(recordings)
    ended = _call(tmp_path, recording.call_id)
    _upload(recordings, "call-1.agent")

    now = START + SECOND
    # END_GRACE (10 s) < LATE_DEBOUNCE (30 s): the debounce wins for a closed call.
    assert ended[2:4] == (1, now + LATE_DEBOUNCE)
    assert _call(tmp_path, recording.call_id)[6] == 2


# --- The pending deadline, hold, and sweep --------------------------------


def test_the_pending_deadline_is_the_latest_of_creation_evidence_and_end_plus_the_hold() -> None:
    assert pending_deadline(10, 5, None, hold_nano=100) == 110
    assert pending_deadline(10, 50, None, hold_nano=100) == 150
    assert pending_deadline(10, 50, 70, hold_nano=100) == 170


def test_a_pending_recording_is_failed_once_its_deadline_has_passed(tmp_path) -> None:
    # The hold side of the same deadline is the closer's (``test_closer.py``).
    recordings, _, clock = _open(tmp_path)
    recordings.create("default", manifest())
    states = []

    for step in (HOLD - 1, 1, 1):
        clock.advance(step)
        recordings.fail_stale_uploads()
        states.append(_states(tmp_path)["call-1.user"])

    assert states == ["pending", "pending", "failed"]


def test_a_call_still_receiving_evidence_keeps_its_pending_recording_past_the_hold(
    tmp_path,
) -> None:
    recordings, calls, clock = _open(tmp_path)
    recordings.create("default", manifest())
    last = START
    span = 0

    while last < START + 2 * HOLD:  # a call much longer than UPLOAD_HOLD_CAP
        clock.advance(30 * SECOND)
        last += 30 * SECOND
        span += 1
        calls.record_spans("default", _spans("call-1", 1, span))
        assert recordings.fail_stale_uploads() == 0

    clock.advance(HOLD)
    assert recordings.fail_stale_uploads() == 0
    clock.advance(1)
    assert recordings.fail_stale_uploads() == 1
    assert _states(tmp_path) == {"call-1.user": "failed"}


def test_an_upload_still_streaming_past_the_deadline_is_not_failed_and_stores(tmp_path) -> None:
    """The deadline cannot expire while an upload attempt is streaming."""

    recordings, _, clock = _open(tmp_path)
    recording, _ = recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")  # the PUT starts streaming
    clock.advance(HOLD + 60 * SECOND)  # a slow uplink: still streaming past the deadline

    failed = recordings.fail_stale_uploads()
    uploading = recordings.uploading_calls()
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [BODY])
    stored = recordings.finish_upload(
        "default", "call-1.user", attempt.blob_key, sha256=sha256, size=size, duration_ms=None
    )
    recordings.end_upload("default", attempt.blob_key)

    assert failed == 0
    assert uploading == frozenset({("default", recording.call_id)})  # the closer's hold
    assert recordings.uploading_calls() == frozenset()
    assert stored is not None and stored.state == "stored"
    assert _states(tmp_path) == {"call-1.user": "stored"}


def test_a_recording_past_its_deadline_fails_once_its_last_attempt_ends(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    closer = _closer(recordings)
    recording, _ = recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")
    clock.advance(HOLD + 1)  # past the call's due time and the recording's deadline
    while_streaming = (recordings.fail_stale_uploads(), closer.claim())

    recordings.discard_upload("default", attempt.blob_key)  # the client went away
    recordings.end_upload("default", attempt.blob_key)

    assert while_streaming == (0, None)
    assert recordings.fail_stale_uploads() == 1
    assert closer.claim().call_id == recording.call_id
    assert _states(tmp_path) == {"call-1.user": "failed"}


def test_discard_keeps_the_object_of_a_recording_that_did_store(tmp_path) -> None:
    """The PUT handler may discard after an ambiguous commit. A stored recording's
    object must remain available."""

    recordings, _, _ = _open(tmp_path)
    recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [BODY])
    recordings.finish_upload(
        "default", "call-1.user", attempt.blob_key, sha256=sha256, size=size, duration_ms=None
    )  # committed, but its result never reached the handler

    recordings.discard_upload("default", attempt.blob_key)
    recordings.end_upload("default", attempt.blob_key)

    assert _states(tmp_path) == {"call-1.user": "stored"}
    assert recordings.blobs.head(attempt.blob_key) is not None
    assert _journal(tmp_path) == []


def test_a_stored_recording_and_a_failed_one_hold_nothing(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    stored, _ = recordings.create("default", manifest())
    failed, _ = recordings.create("default", manifest("call-2.user", session_key="call-2"))
    _upload(recordings)
    clock.advance(HOLD + 1)
    recordings.fail_stale_uploads()
    closer = _closer(recordings)

    claimed = {closer.claim().call_id, closer.claim().call_id}

    assert claimed == {stored.call_id, failed.call_id}


def test_the_sweep_and_the_deletion_jobs_run_on_the_maintenance_loops_clock(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")
    put_stream(recordings.blobs, attempt.blob_key, [BODY])  # the process dies before commit
    restarted = RecordingStore(recordings.store, calls=recordings.calls, blobs=recordings.blobs)
    clock = ManualClock(wall=START + HOLD)
    loop = MaintenanceLoop(clock=clock)
    loop.add_job("recording-upload-sweep", recording_upload_sweep_job(restarted))
    loop.add_job("blob-deletions", blob_deletion_job(restarted))

    asyncio.run(loop.run_once())
    at_deadline = (_states(tmp_path)["call-1.user"], recordings.blobs.head(attempt.blob_key))
    clock.advance(1)
    asyncio.run(loop.run_once())

    assert at_deadline[0] == "pending"  # the orphaned attempt object is already gone
    assert at_deadline[1] is None
    assert _states(tmp_path) == {"call-1.user": "failed"}
    assert _journal(tmp_path) == []


# --- blob deletions after commit ----------------------------------------------------------------


def test_an_attempt_orphaned_by_a_crash_is_deleted_once_no_upload_owns_it(
    tmp_path, blob_backend
) -> None:
    recordings, _, _ = _open(tmp_path, blob_backend.store)
    recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")
    put_stream(blob_backend.store, attempt.blob_key, [BODY])

    in_flight = recordings.drain_blob_deletions()
    restarted = RecordingStore(recordings.store, calls=recordings.calls, blobs=blob_backend.store)
    after_restart = restarted.drain_blob_deletions()

    assert (in_flight.deleted, in_flight.failed) == (0, 0)
    assert (after_restart.deleted, after_restart.failed) == (1, 0)
    assert blob_backend.store.head(attempt.blob_key) is None
    assert blob_backend.objects() == []
    assert _journal(tmp_path) == []
    assert _states(tmp_path) == {"call-1.user": "pending"}


@pytest.mark.sqlite_only
def test_a_failed_deletion_is_retried_with_backoff_until_it_succeeds(tmp_path) -> None:
    recordings, _, clock = _open(tmp_path)
    recordings.create("default", manifest())
    attempt = recordings.begin_upload("default", "call-1.user")
    put_stream(recordings.blobs, attempt.blob_key, [BODY])
    recordings.end_upload("default", attempt.blob_key)
    directory = tmp_path / "blobs" / "recordings" / "default"
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)  # a real filesystem refusal
    try:
        refused = recordings.drain_blob_deletions()
    finally:
        directory.chmod(stat.S_IRWXU)
    too_soon = recordings.drain_blob_deletions()
    clock.advance(2 * SECOND)
    retried = recordings.drain_blob_deletions()

    assert (refused.deleted, refused.failed) == (0, 1)
    assert (too_soon.deleted, too_soon.failed) == (0, 0)
    assert (retried.deleted, retried.failed) == (1, 0)
    assert _journal(tmp_path) == []
    assert recordings.blobs.head(attempt.blob_key) is None


def test_a_journaled_key_a_stored_recording_still_uses_is_never_deleted(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.create("default", manifest())
    attempt, stored = _upload(recordings)
    with catalog(tmp_path) as connection:
        connection.execute(
            "INSERT INTO pending_blob_deletions(project_id, blob_key, queued_at_unix_nano, "
            "attempts) VALUES ('default', ?, ?, 0)",
            (attempt.blob_key, START),
        )

    result = recordings.drain_blob_deletions()

    assert (result.deleted, result.failed) == (0, 0)
    assert recordings.blobs.head(attempt.blob_key) is not None
    assert _journal(tmp_path) == []
    assert stored is not None
