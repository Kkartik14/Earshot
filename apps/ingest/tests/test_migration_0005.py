"""Revision ``0005_projections``: what an upgrade writes, and what its keys delete.

The catalog-parity module proves the schema (every table, column, CHECK and
foreign key, on fresh SQLite, upgraded SQLite and fresh Postgres). These tests
prove the revision's data step and the deletes its foreign keys make, on a real
catalog of the dialect under test, with the real recordings store, closer and
call purge:

- a catalog at ``0004_calls`` that holds stored recordings gains one pending
  ``audio_analysis`` row per stored recording when it upgrades (a recording that
  is still pending gets none: storing it queues its analysis), and starts every
  version counter at 0;
- purging a call deletes every row derived from it (its recordings' analyses,
  its projection, the projection's transcript lines and tags), through the keys'
  ``ON DELETE CASCADE``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from captured_otlp import CAPTURED_SESSION_KEY, captured_requests
from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_migrations import head_revision
from earshot.storage_recordings import RecordingManifest, RecordingStore
from store_support import catalog, catalog_rows, drop_projection_objects, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
DERIVED_TABLES = ("audio_analysis", "call_projections", "call_transcripts", "call_tags")


def _recordings(tmp_path: Path, clock: ManualClock) -> RecordingStore:
    store = make_store(tmp_path, clock=clock)
    return RecordingStore(store, calls=CallStore(store), blobs=LocalBlobStore(tmp_path / "blobs"))


def _create(recordings: RecordingStore, recording_id: str, speaker: str) -> None:
    recordings.create(
        "default",
        RecordingManifest(
            recording_id=recording_id,
            session_key=CAPTURED_SESSION_KEY,
            speaker=speaker,
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )


def _store(recordings: RecordingStore, recording_id: str) -> None:
    attempt = recordings.begin_upload("default", recording_id)
    try:
        sha256, size = put_stream(recordings.blobs, attempt.blob_key, [b"RIFF" * 64])
        recordings.finish_upload(
            "default", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
    finally:
        recordings.end_upload("default", attempt.blob_key)


def test_0005_is_a_revision_of_the_chain() -> None:
    from earshot.storage_migrations import _known_revisions

    assert "0005_projections" in _known_revisions()


def test_an_upgrade_queues_one_analysis_per_stored_recording(tmp_path) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    recordings = _recordings(tmp_path, clock)
    _create(recordings, "call-1.user", "user")
    _create(recordings, "call-1.agent", "agent")
    _store(recordings, "call-1.user")  # the agent's track never arrives
    recordings.store.close()
    with catalog(tmp_path) as connection:  # back to 0004_calls, rows kept
        drop_projection_objects(connection)
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0004_calls",))

    make_store(tmp_path).close()

    assert catalog_rows(tmp_path, "SELECT version_num FROM alembic_version") == [(head_revision(),)]
    assert catalog_rows(
        tmp_path,
        "SELECT project_id, recording_id, state, attempts, lease_until_unix_nano, lease_token, "
        "next_attempt_unix_nano, error_code, algorithm_version, result_json, peaks_blob_key, "
        "rendition_blob_key FROM audio_analysis",
    ) == [("default", "call-1.user", "pending", 0, None, 0, None, None, 0, None, None, None)]
    [(updated,)] = catalog_rows(tmp_path, "SELECT updated_unix_nano FROM audio_analysis")
    assert updated > 0
    assert catalog_rows(tmp_path, "SELECT projection_seq FROM calls") == [(0,)]
    assert catalog_rows(tmp_path, "SELECT price_version FROM projects") == [(0,)]
    assert catalog_rows(tmp_path, "SELECT singleton, version FROM builtin_price_version") == [
        (1, 0)
    ]
    assert catalog_rows(tmp_path, "SELECT channel_speakers_json FROM recordings") == [
        (None,),
        (None,),
    ]


def test_purging_a_call_deletes_every_row_derived_from_it(tmp_path) -> None:
    from earshot.storage_call_purge import CallPurger

    clock = ManualClock(wall=START, monotonic=START)
    recordings = _recordings(tmp_path, clock)
    calls = recordings.calls
    call_ids = set()
    for item in captured_requests():
        call_ids.update(calls.record_spans("default", spans_from_request(item)).call_ids)
        clock.advance(5 * SECOND)
    [call_id] = call_ids
    _create(recordings, f"{CAPTURED_SESSION_KEY}.user", "user")
    _store(recordings, f"{CAPTURED_SESSION_KEY}.user")
    closer = Closer(recordings.store, calls=calls, recordings=recordings)
    clock.advance(TIMINGS.long_quiet_nano + TIMINGS.end_grace_nano)
    while closer.close_next() is not None:
        pass
    [(revision,)] = catalog_rows(tmp_path, "SELECT latest_revision FROM calls")
    # Storing the recording queued its analysis; what the projection job writes:
    with catalog(tmp_path) as connection:
        connection.execute(
            "INSERT INTO call_projections(project_id, call_id, revision, source_seq, "
            "price_version, builtin_price_version, projection_version, input_digest, "
            "turn_count, measurable_turns, cost_unknown_items, analysis_state, "
            "updated_unix_nano, detail_json) "
            "VALUES ('default', ?, ?, 0, 0, 0, 1, ?, 3, 0, 2, 'pending', ?, '{}')",
            (call_id, revision, "0" * 64, START),
        )
        connection.execute(
            "INSERT INTO call_transcripts(project_id, call_id, line_no, speaker, start_ms, "
            "end_ms, timing, text) VALUES ('default', ?, 0, 'user', 1000, 2400, 'measured', "
            "'Seven thirty works for us.')",
            (call_id,),
        )
        connection.execute(
            "INSERT INTO call_tags(project_id, call_id, tag) VALUES ('default', ?, 'booking')",
            (call_id,),
        )
    assert [
        catalog_rows(tmp_path, f"SELECT count(*) FROM {table}")[0][0] for table in DERIVED_TABLES
    ] == [1, 1, 1, 1]

    CallPurger(recordings.store, calls=calls, recordings=recordings).purge_call("default", call_id)

    assert [
        catalog_rows(tmp_path, f"SELECT count(*) FROM {table}")[0][0] for table in DERIVED_TABLES
    ] == [0, 0, 0, 0]
