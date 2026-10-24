"""Analysis jobs in the catalog.

``AnalysisStore`` is the catalog half of the analysis job: storing a recording
queues its job in the same transaction, the job is leased, fenced and retried
through the generic leased-job helper (``leased_jobs``), its result is written only
under its claim's lease, and a server start requeues what a previous process or an
older algorithm left. These tests drive it on a real catalog of the dialect under
test, with the real recordings store, a real blob directory and the store's
injected clock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from earshot.blobstore import LocalBlobStore, put_stream
from earshot.clock import ManualClock
from earshot.signals import (
    ALGORITHM_VERSION,
    ANALYSIS_CRASHED,
    ANALYSIS_DECODE_FAILED,
    ANALYSIS_LEASE_EXPIRED,
    ANALYSIS_TIMEOUT,
    SIGNALS_EXTRA_REQUIRED,
)
from earshot.storage_analysis import OUTPUT_RESERVE_GRACE_SECONDS, AnalysisStore
from earshot.storage_call_purge import CallPurger
from earshot.storage_calls import CallStore
from earshot.storage_recordings import RecordingManifest, RecordingStore
from store_support import catalog, catalog_rows, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
MINUTE = 60 * SECOND
START = 1_800_000_000 * SECOND
BODY = b"RIFF" * 256


def _open(tmp_path: Path, blobs=None) -> tuple[RecordingStore, AnalysisStore, ManualClock]:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    blobs = LocalBlobStore(tmp_path / "blobs") if blobs is None else blobs
    recordings = RecordingStore(store, calls=CallStore(store), blobs=blobs)
    return recordings, AnalysisStore(store), clock


def _stored(
    recordings: RecordingStore,
    recording_id: str,
    *,
    speaker: str = "user",
    duration_ms: int | None = 32_486,
    started_at: int = START,
    channel_speakers: tuple[str, ...] | None = None,
    session_key: str = "call-1",
    body: bytes = BODY,
):
    recordings.create(
        "default",
        RecordingManifest(
            recording_id=recording_id,
            session_key=session_key,
            speaker=speaker,
            content_type="audio/wav",
            started_at_unix_nano=str(started_at),
            channel_speakers=channel_speakers,
        ),
    )
    attempt = recordings.begin_upload("default", recording_id)
    try:
        sha256, size = put_stream(recordings.blobs, attempt.blob_key, [body])
        return recordings.finish_upload(
            "default",
            recording_id,
            attempt.blob_key,
            sha256=sha256,
            size=size,
            duration_ms=duration_ms,
        )
    finally:
        recordings.end_upload("default", attempt.blob_key)


def _job(path: Path, recording_id: str) -> tuple:
    [row] = catalog_rows(
        path,
        "SELECT state, attempts, lease_until_unix_nano, lease_token, next_attempt_unix_nano, "
        "error_code, algorithm_version, peaks_blob_key, rendition_blob_key "
        "FROM audio_analysis WHERE recording_id = ?",
        (recording_id,),
    )
    return row


def _journal(path: Path) -> dict[str, tuple]:
    return {
        key: (queued, attempts)
        for key, queued, attempts in catalog_rows(
            path, "SELECT blob_key, queued_at_unix_nano, attempts FROM pending_blob_deletions"
        )
    }


def _projection_seq(path: Path) -> int:
    [(seq,)] = catalog_rows(path, "SELECT projection_seq FROM calls")
    return int(seq)


# --- scheduling ---------------------------------------------------------------------------------


def test_storing_a_recording_queues_its_analysis_in_the_same_transaction(tmp_path) -> None:
    recordings, _, _ = _open(tmp_path)
    recordings.create(
        "default",
        RecordingManifest(
            recording_id="call-1.agent",
            session_key="call-1",
            speaker="agent",
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )

    _stored(recordings, "call-1.user")

    # The pending recording has no job: storing it queues one.
    assert catalog_rows(tmp_path, "SELECT recording_id FROM audio_analysis") == [("call-1.user",)]
    assert _job(tmp_path, "call-1.user") == (
        "pending", 0, None, 0, None, None, 0, None, None
    )  # fmt: skip
    # A losing attempt (the recording is stored already) queues nothing more.
    attempt = recordings.begin_upload("default", "call-1.agent")
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [BODY])
    recordings.end_upload("default", attempt.blob_key)
    for _ in range(2):
        recordings.finish_upload(
            "default", "call-1.agent", attempt.blob_key, sha256=sha256, size=size, duration_ms=1
        )
    assert catalog_rows(tmp_path, "SELECT count(*) FROM audio_analysis") == [(2,)]


# --- claims, leases and retries ------------------------------------------------------------------


def test_a_claim_leases_the_job_for_its_timeout_plus_a_margin(tmp_path) -> None:
    recordings, analyses, clock = _open(tmp_path)
    stored = _stored(recordings, "call-1.user", duration_ms=32_486)

    claim = analyses.claim()

    assert claim is not None
    assert (claim.project_id, claim.recording_id, claim.call_id) == (
        "default",
        "call-1.user",
        stored.call_id,
    )
    assert (claim.recording.blob_key, claim.speakers, claim.rendition) == (
        stored.blob_key,
        ("user",),
        None,
    )
    timeout = 30 + 0.25 * 32.486
    assert claim.timeout_seconds == pytest.approx(timeout)
    lease_until = START + round((timeout + 60) * SECOND)
    assert _job(tmp_path, "call-1.user")[:4] == ("running", 0, lease_until, claim.lease.token)
    assert claim.lease.token > 0
    assert analyses.claim() is None  # leased

    clock.advance(lease_until - START + 1)  # its holder never answered: the lease expires
    assert analyses.claim() is None  # a failed attempt, put off like any other
    clock.advance(MINUTE)
    again = analyses.claim()

    assert again is not None and again.lease.attempts == 1
    assert again.lease.token != claim.lease.token


def test_a_recording_of_unknown_duration_gets_a_15_minute_timeout(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    _stored(recordings, "call-1.user", duration_ms=None)

    claim = analyses.claim()

    assert claim is not None and claim.timeout_seconds == 15 * 60
    assert claim.lease.lease_until_unix_nano == START + 16 * MINUTE


def test_a_duration_the_recording_cannot_hold_gets_the_unknown_duration_timeout(
    tmp_path,
) -> None:
    recordings, analyses, _ = _open(tmp_path)
    # A FLAC STREAMINFO can declare 2^36 - 1 samples at 8 kHz (99 days) in 42 bytes.
    _stored(recordings, "call-1.user", duration_ms=8_589_934_591)

    claim = analyses.claim()

    assert claim is not None and claim.timeout_seconds == 15 * 60
    assert claim.lease.lease_until_unix_nano == START + 16 * MINUTE


def test_a_long_recording_s_timeout_and_lease_are_capped_at_an_hour(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    _stored(recordings, "call-1.user", duration_ms=10 * 3600 * 1000, body=bytes(64 * 1024))

    claim = analyses.claim()

    assert claim is not None and claim.timeout_seconds == 3600
    assert claim.lease.lease_until_unix_nano == START + 61 * MINUTE


def test_failed_attempts_back_off_1_4_16_minutes_then_fail_for_good(tmp_path) -> None:
    recordings, analyses, clock = _open(tmp_path)
    _stored(recordings, "call-1.user")
    seq = _projection_seq(tmp_path)

    for delay_minutes, code in (
        (1, ANALYSIS_CRASHED),
        (4, ANALYSIS_TIMEOUT),
        (16, ANALYSIS_CRASHED),
    ):
        claim = analyses.claim()
        assert claim is not None
        assert analyses.fail(claim, code, terminal=False) == "pending"
        state, _, _, _, next_attempt, error_code, *_ = _job(tmp_path, "call-1.user")
        assert (state, error_code) == ("pending", code)
        assert next_attempt == clock.unix_nano() + delay_minutes * MINUTE
        clock.advance(delay_minutes * MINUTE - 1)
        assert analyses.claim() is None  # not before its backoff
        clock.advance(1)
    assert _projection_seq(tmp_path) == seq  # nothing a projection reads changed yet

    last = analyses.claim()
    assert last is not None and last.lease.attempts == 3
    assert analyses.fail(last, ANALYSIS_TIMEOUT, terminal=False) == "failed"

    state, attempts, lease_until, _, next_attempt, error_code, version, *_ = _job(
        tmp_path, "call-1.user"
    )
    assert (state, attempts, lease_until, next_attempt, error_code, version) == (
        "failed", 4, None, None, ANALYSIS_TIMEOUT, ALGORITHM_VERSION
    )  # fmt: skip
    assert analyses.claim() is None
    assert _projection_seq(tmp_path) == seq + 1  # the job's outcome is final


def test_a_lease_that_runs_out_is_a_failed_attempt_and_the_fourth_ends_the_job(
    tmp_path,
) -> None:
    """A holder that never writes an outcome (a store it cannot reach, a process that
    died without a restart) still ends its job, within the retry policy."""

    recordings, analyses, clock = _open(tmp_path)
    _stored(recordings, "call-1.user")
    seq = _projection_seq(tmp_path)

    for attempt, delay_minutes in enumerate((1, 4, 16)):
        claim = analyses.claim()
        assert claim is not None and claim.lease.attempts == attempt
        clock.advance(claim.lease.lease_until_unix_nano - clock.unix_nano() + 1)
        assert analyses.claim() is None  # counted and put off, not run again at once
        state, attempts, _, _, next_attempt, error_code, *_ = _job(tmp_path, "call-1.user")
        assert (state, attempts, error_code) == ("pending", attempt + 1, ANALYSIS_LEASE_EXPIRED)
        assert next_attempt == clock.unix_nano() + delay_minutes * MINUTE
        clock.advance(delay_minutes * MINUTE)
    assert _projection_seq(tmp_path) == seq
    last = analyses.claim()
    assert last is not None
    clock.advance(last.lease.lease_until_unix_nano - clock.unix_nano() + 1)

    assert analyses.claim() is None
    assert _job(tmp_path, "call-1.user")[:2] == ("failed", 4)
    assert _job(tmp_path, "call-1.user")[5:7] == (ANALYSIS_LEASE_EXPIRED, ALGORITHM_VERSION)
    assert _projection_seq(tmp_path) == seq + 1


def test_a_decode_error_is_terminal_unavailable_with_its_code(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    _stored(recordings, "call-1.user")
    claim = analyses.claim()
    assert claim is not None

    assert analyses.fail(claim, ANALYSIS_DECODE_FAILED, terminal=True) == "unavailable"

    assert _job(tmp_path, "call-1.user")[:7] == (
        "unavailable", 0, None, claim.lease.token, None, ANALYSIS_DECODE_FAILED, ALGORITHM_VERSION
    )  # fmt: skip
    assert analyses.claim() is None


# --- results --------------------------------------------------------------------------------------


def test_a_result_is_written_only_under_its_claims_lease(tmp_path) -> None:
    recordings, analyses, clock = _open(tmp_path)
    _stored(recordings, "call-1.user")
    seq = _projection_seq(tmp_path)
    stale = analyses.claim()
    assert stale is not None
    clock.advance(stale.lease.lease_until_unix_nano - START + 1)
    assert analyses.claim() is None  # the expired lease is counted and put off
    clock.advance(MINUTE)
    current = analyses.claim()
    assert current is not None
    stale_keys = analyses.output_keys(stale)
    current_keys = analyses.output_keys(current)

    # The stale holder may not even reserve its outputs; the current one may.
    assert not analyses.reserve_outputs(stale, stale_keys)
    assert analyses.reserve_outputs(current, current_keys)
    held = _journal(tmp_path)[current_keys.peaks]
    assert held[0] > current.lease.lease_until_unix_nano  # not drained while the lease holds

    result = {"algorithm_version": ALGORITHM_VERSION, "channels": []}
    assert analyses.store_result(current, current_keys, result, rendition=False)

    assert _job(tmp_path, "call-1.user")[:9] == (
        "stored", 1, None, current.lease.token, None, None, ALGORITHM_VERSION,
        current_keys.peaks, None,
    )  # fmt: skip
    [(stored_json,)] = catalog_rows(tmp_path, "SELECT result_json FROM audio_analysis")
    assert json.loads(stored_json) == result
    assert _journal(tmp_path) == {}  # the row owns its peaks now
    assert _projection_seq(tmp_path) == seq + 1


def test_a_claim_on_a_purged_job_cannot_write_to_the_job_that_replaces_it(tmp_path) -> None:
    """A recording ID can come back after a purge (in another call, say): its new job
    row starts afresh, and the purged job's claim must not match the new one's."""

    recordings, analyses, _ = _open(tmp_path)
    purged = _stored(recordings, "r1")
    stale = analyses.claim()
    assert stale is not None
    CallPurger(recordings.store, calls=recordings.calls, recordings=recordings).purge_call(
        "default", purged.call_id
    )
    _stored(recordings, "r1", session_key="call-2")
    fresh = analyses.claim()
    assert fresh is not None

    assert analyses.fail(stale, ANALYSIS_DECODE_FAILED, terminal=True) is None
    assert analyses.fail(stale, ANALYSIS_CRASHED, terminal=False) is None
    assert not analyses.reserve_outputs(stale, analyses.output_keys(stale))
    lease = fresh.lease
    assert _job(tmp_path, "r1")[:4] == ("running", 0, lease.lease_until_unix_nano, lease.token)


def test_a_result_whose_lease_was_lost_queues_its_blobs_for_deletion(tmp_path) -> None:
    recordings, analyses, clock = _open(tmp_path)
    _stored(recordings, "call-1.user")
    claim = analyses.claim()
    assert claim is not None
    keys = analyses.output_keys(claim)
    assert analyses.reserve_outputs(claim, keys)
    with catalog(tmp_path) as connection:  # what a purge's cascade does to the job
        connection.execute("DELETE FROM recordings")

    assert not analyses.store_result(claim, keys, {"channels": []}, rendition=False)

    assert _journal(tmp_path) == {keys.peaks: (clock.unix_nano(), 0)}  # due at once
    assert catalog_rows(tmp_path, "SELECT count(*) FROM audio_analysis") == [(0,)]


def test_outputs_written_after_the_drain_took_their_reservation_are_deleted(
    tmp_path, blob_backend
) -> None:
    """A job that outlives its lease and the reservation's grace (a very slow spool
    or put) finds its reserved keys drained; what it writes after that is journaled
    again and deleted."""

    recordings, analyses, clock = _open(tmp_path, blob_backend.store)
    _stored(recordings, "call-1.user")
    claim = analyses.claim()
    assert claim is not None
    keys = analyses.output_keys(claim)
    assert analyses.reserve_outputs(claim, keys)
    clock.advance(claim.lease.lease_until_unix_nano - START + OUTPUT_RESERVE_GRACE_SECONDS * SECOND)
    recordings.drain_blob_deletions()
    assert _journal(tmp_path) == {}
    put_stream(recordings.blobs, keys.peaks, [b"peaks written by a slow put"])

    assert not analyses.store_result(claim, keys, {"channels": []}, rendition=False)
    assert _journal(tmp_path) == {keys.peaks: (clock.unix_nano(), 0)}
    recordings.drain_blob_deletions()

    assert [key for key in blob_backend.objects() if key.startswith("analysis/")] == []
    assert _journal(tmp_path) == {}


def test_a_replaced_result_queues_the_blobs_it_replaces(tmp_path) -> None:
    recordings, analyses, clock = _open(tmp_path)
    _stored(recordings, "call-1.user")
    first = analyses.claim()
    assert first is not None
    old = analyses.output_keys(first)
    analyses.reserve_outputs(first, old)
    analyses.store_result(first, old, {"channels": []}, rendition=False)
    with catalog(tmp_path) as connection:  # an older algorithm computed it
        connection.execute("UPDATE audio_analysis SET algorithm_version = 0")
    assert analyses.requeue(extra_available=True) == 1
    second = analyses.claim()
    assert second is not None
    new = analyses.output_keys(second)
    analyses.reserve_outputs(second, new)

    assert analyses.store_result(second, new, {"channels": []}, rendition=False)

    assert _job(tmp_path, "call-1.user")[7] == new.peaks
    assert _journal(tmp_path) == {old.peaks: (clock.unix_nano(), 0)}


# --- requeue at startup ---------------------------------------------------------------------------


def test_a_start_requeues_left_leases_old_algorithms_and_missing_extras(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    for name in ("running", "old", "extra", "decode", "current"):
        _stored(recordings, f"call-1.{name}", session_key=f"key-{name}")
    with catalog(tmp_path) as connection:
        for name, state, version, code in (
            ("running", "running", 0, None),
            ("old", "stored", ALGORITHM_VERSION - 1, None),
            ("extra", "unavailable", ALGORITHM_VERSION, SIGNALS_EXTRA_REQUIRED),
            ("decode", "unavailable", ALGORITHM_VERSION, ANALYSIS_DECODE_FAILED),
            ("current", "stored", ALGORITHM_VERSION, None),
        ):
            connection.execute(
                "UPDATE audio_analysis SET state = ?, algorithm_version = ?, error_code = ?, "
                "lease_until_unix_nano = ?, attempts = 2 WHERE recording_id = ?",
                (state, version, code, START + MINUTE if state == "running" else None,
                 f"call-1.{name}"),
            )  # fmt: skip

    def states() -> dict[str, str]:
        return dict(catalog_rows(tmp_path, "SELECT recording_id, state FROM audio_analysis"))

    assert analyses.release_leases() == 1
    # Without the extra, its rows stay as they are; the old result is requeued.
    assert analyses.requeue(extra_available=False, limit=10) == 1
    assert analyses.requeue(extra_available=False, limit=10) == 0
    # With it, in bounded batches.
    assert analyses.requeue(extra_available=True, limit=1) == 1
    assert analyses.requeue(extra_available=True, limit=1) == 0

    assert states() == {
        "call-1.running": "pending",
        "call-1.old": "pending",
        "call-1.extra": "pending",
        "call-1.decode": "unavailable",
        "call-1.current": "stored",
    }
    requeued = catalog_rows(
        tmp_path,
        "SELECT attempts, error_code, next_attempt_unix_nano FROM audio_analysis "
        "WHERE recording_id IN ('call-1.old', 'call-1.extra')",
    )
    assert requeued == [(0, None, None), (0, None, None)]


# --- mono pairs on one timeline -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_changes", "paired"),
    [
        ({}, True),
        ({"duration_ms": 32_486 + 20}, True),
        ({"duration_ms": 32_486 + 21}, False),  # not one timeline: durations differ
        ({"started_at": START + 1}, False),  # not one timeline: another start
        ({"duration_ms": None}, False),  # unknown duration: not provably one timeline
    ],
)
def test_a_mono_pair_on_one_timeline_is_claimed_for_a_stereo_rendition(
    tmp_path, agent_changes, paired
) -> None:
    recordings, analyses, _ = _open(tmp_path)
    user = _stored(recordings, "call-1.user", speaker="user")
    agent = _stored(recordings, "call-1.agent", speaker="agent", **agent_changes)

    claims = [analyses.claim(), analyses.claim()]

    assert all(claim is not None for claim in claims)
    renditions = [claim.rendition for claim in claims if claim.rendition is not None]
    if not paired:
        assert renditions == []
        return
    # Each job of the pair could make it; both plan it until one has stored it.
    for rendition in renditions:
        assert rendition.owner_recording_id == "call-1.user"
        assert [source.blob_key for source in rendition.sources] == [user.blob_key, agent.blob_key]
        assert rendition.speakers == ("user", "agent")
    assert len(renditions) == 2


def test_a_pair_gets_one_rendition_held_by_its_user_recording(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    _stored(recordings, "call-1.user", speaker="user")
    _stored(recordings, "call-1.agent", speaker="agent")
    first = analyses.claim()
    assert first is not None and first.rendition is not None
    keys = analyses.output_keys(first)
    analyses.reserve_outputs(first, keys)
    assert analyses.store_result(first, keys, {"channels": []}, rendition=True)

    second = analyses.claim()

    assert second is not None and second.rendition is None  # made already
    rows = dict(
        catalog_rows(tmp_path, "SELECT recording_id, rendition_blob_key FROM audio_analysis")
    )
    assert rows == {"call-1.user": keys.rendition, "call-1.agent": None}


def test_a_stereo_recording_is_one_timeline_already(tmp_path) -> None:
    recordings, analyses, _ = _open(tmp_path)
    _stored(recordings, "call-1.mixed", speaker="mixed", channel_speakers=("user", "agent"))

    claim = analyses.claim()

    assert claim is not None
    assert (claim.speakers, claim.rendition) == (("user", "agent"), None)
