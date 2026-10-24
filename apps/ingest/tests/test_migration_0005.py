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
- on a catalog that already holds revised calls, the ``first_revision`` mark of each
  span and stored recording is its call's latest revision, except evidence stored
  after that revision was built, which stays unmarked so the next revision marks it;
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
from otlp_support import make_request, make_span, string_attribute
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


def _close_calls(recordings: RecordingStore, clock: ManualClock) -> None:
    closer = Closer(recordings.store, calls=recordings.calls, recordings=recordings)
    clock.advance(TIMINGS.long_quiet_nano + TIMINGS.end_grace_nano)
    while closer.close_next() is not None:
        pass


def test_an_upgrade_marks_revised_evidence_with_its_revision_and_leaves_later_evidence_unmarked(
    tmp_path,
) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    recordings = _recordings(tmp_path, clock)
    calls = recordings.calls
    for item in captured_requests():
        calls.record_spans("default", spans_from_request(item))
        clock.advance(5 * SECOND)
    _create(recordings, "call-1.user", "user")
    _store(recordings, "call-1.user")
    _close_calls(recordings, clock)
    [(call_id, revision)] = catalog_rows(tmp_path, "SELECT call_id, latest_revision FROM calls")
    assert revision == 1
    # After that revision was built: a late span of the call's trace, and the agent's
    # track. The call is dirty; the revision does not hold either.
    clock.advance(100 * SECOND)
    trace = int.from_bytes(spans_from_request(captured_requests()[0])[0].span.trace_id, "big")
    calls.record_spans(
        "default",
        spans_from_request(make_request([make_span(trace, 901, name="llm", start=START)])),
    )
    _create(recordings, "call-1.agent", "agent")
    _store(recordings, "call-1.agent")
    _create(recordings, "call-1.mixed", "mixed")  # never stored
    # Another call that never closed: nothing of it is in a revision.
    attributes = [string_attribute("earshot.session.id", "unrevised")]
    calls.record_spans(
        "default", spans_from_request(make_request([make_span(77, 1, attributes=attributes)]))
    )
    recordings.store.close()
    with catalog(tmp_path) as connection:  # back to 0004_calls, rows kept
        drop_projection_objects(connection)
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0004_calls",))

    make_store(tmp_path).close()

    [(built,)] = catalog_rows(
        tmp_path,
        "SELECT created_unix_nano FROM call_revisions WHERE call_id = ? AND revision = 1",
        (call_id,),
    )
    spans = catalog_rows(
        tmp_path,
        "SELECT s.first_revision, s.received_unix_nano > ?, t.call_id FROM spans s "
        "JOIN call_traces t ON t.project_id = s.project_id AND t.trace_id = s.trace_id",
        (built,),
    )
    assert len(spans) > 2
    assert {(mark, later) for mark, later, owner in spans if owner == call_id} == {
        (1, False),  # in the revision
        (None, True),  # the late span
    }
    assert [mark for mark, _, owner in spans if owner != call_id] == [None]  # never revised
    assert catalog_rows(
        tmp_path, "SELECT recording_id, state, first_revision FROM recordings ORDER BY 1"
    ) == [
        ("call-1.agent", "stored", None),
        ("call-1.mixed", "pending", None),
        ("call-1.user", "stored", 1),
    ]
    # The next revision marks what was left.
    clock.advance(TIMINGS.upload_hold_nano + TIMINGS.late_debounce_nano)  # the hold is over
    recordings = _recordings(tmp_path, clock)
    assert Closer(recordings.store, calls=recordings.calls, recordings=recordings).close_next()
    assert catalog_rows(
        tmp_path,
        "SELECT recording_id, first_revision FROM recordings WHERE state = 'stored' ORDER BY 1",
    ) == [("call-1.agent", 2), ("call-1.user", 1)]
    assert catalog_rows(
        tmp_path,
        "SELECT count(*) FROM spans s JOIN call_traces t ON t.project_id = s.project_id "
        "AND t.trace_id = s.trace_id WHERE t.call_id = ? AND s.first_revision IS NULL",
        (call_id,),
    ) == [(0,)]


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
