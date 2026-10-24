"""FR-14: purging a call, purged keys, retention and project deletion for call evidence.

Every test runs on a real catalog (both dialects), synthetic Pipecat-shaped OTLP requests
recorded through ``CallStore.record_spans`` (the receiver's storage call), the
real closer, and a real blob store (local disk; MinIO where the backend matters),
with the store's injected clock (§7 seam 1). The clock is set after the wall
clock, so a served app's own closer and reapers never act on these calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage import IncidentPurgedError, IncidentStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import CallPurgedError, RecordingManifest, RecordingStore
from otlp_support import make_request, make_span, request, serve, string_attribute
from store_support import catalog, catalog_rows, current_dialect, make_store, stored_needles
from synthetic_otlp import SYNTHETIC_SESSION_KEY, synthetic_requests

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
END_GRACE = TIMINGS.end_grace_nano
LONG_QUIET = TIMINGS.long_quiet_nano
LATE_DEBOUNCE = TIMINGS.late_debounce_nano
DAY = 24 * 3600 * SECOND
# The tables FR-14 must empty for a purged call or a deleted project.
CALL_TABLES = (
    "calls",
    "call_traces",
    "spans",
    "otlp_resources",
    "call_revisions",
    "recordings",
    "pending_blob_deletions",
    "call_key_generations",
    "call_tombstones",
    "otlp_counters",
)
TRANSCRIPT_NEEDLES = [
    b"Seven thirty works for us.",  # an stt transcript (spans, revision bundle)
    b"See you tomorrow.",  # an llm output (spans, revision bundle)
    b"We look forward to welcoming you tomorrow.",  # a tts text (spans)
]


@dataclass
class World:
    path: Path
    clock: ManualClock
    store: IncidentStore
    calls: CallStore
    recordings: RecordingStore
    closer: Closer
    purger: object


def _world(tmp_path: Path, blobs=None) -> World:
    from earshot.storage_call_purge import CallPurger

    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    blobs = blobs if blobs is not None else LocalBlobStore(tmp_path / "blobs")
    recordings = RecordingStore(store, calls=calls, blobs=blobs)
    closer = Closer(store, calls=calls, recordings=recordings)
    purger = CallPurger(store, calls=calls, recordings=recordings)
    return World(tmp_path, clock, store, calls, recordings, closer, purger)


def _replay(world: World, requests=None, *, project_id: str = "default") -> str:
    """Record synthetic requests 5 s apart and return the call's ID."""

    call_ids: set[str] = set()
    for item in synthetic_requests() if requests is None else requests:
        call_ids.update(world.calls.record_spans(project_id, spans_from_request(item)).call_ids)
        world.clock.advance(5 * SECOND)
    [call_id] = call_ids
    return call_id


def _keyed_spans(key: str, trace: int, span: int = 1):
    attributes = [string_attribute("earshot.session.id", key)]
    return spans_from_request(make_request([make_span(trace, span, attributes=attributes)]))


def _keyed_call(world: World, key: str, trace: int, *, project_id: str = "default") -> str:
    [call_id] = world.calls.record_spans(project_id, _keyed_spans(key, trace)).call_ids
    return call_id


def _synthetic_trace() -> int:
    return int.from_bytes(spans_from_request(synthetic_requests()[0])[0].span.trace_id, "big")


def _store_recording(
    world: World, speaker: str = "user", *, key: str = SYNTHETIC_SESSION_KEY, project_id="default"
) -> str:
    """Create and upload one recording of ``key``'s call; its blob key."""

    recording_id = f"{key}.{speaker}"
    world.recordings.create(
        project_id,
        RecordingManifest(
            recording_id=recording_id,
            session_key=key,
            speaker=speaker,
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )
    attempt = world.recordings.begin_upload(project_id, recording_id)
    try:
        sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [b"RIFF" * 64])
        world.recordings.finish_upload(
            project_id, recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
    finally:
        world.recordings.end_upload(project_id, attempt.blob_key)
    return attempt.blob_key


def _close_all(world: World) -> None:
    """Advance past every due time and close every due call."""

    world.clock.advance(LONG_QUIET + END_GRACE)
    while world.closer.close_next() is not None:
        pass


def _counts(world: World, project_id: str = "default") -> dict[str, int]:
    return {
        table: catalog_rows(
            world.path, f"SELECT count(*) FROM {table} WHERE project_id = ?", (project_id,)
        )[0][0]
        for table in CALL_TABLES
    }


def _incident_ids(world: World) -> list[str]:
    return [row[0] for row in catalog_rows(world.path, "SELECT bundle_id FROM incidents")]


def _expire(world: World, bundle_id: str) -> None:
    """Give a revision incident a past retention deadline (Month 1 revisions carry no
    retention policy, so the catalog column is the only way to reach this state)."""

    with catalog(world.path) as connection:
        connection.execute(
            "UPDATE incidents SET expires_at_unix_nano = '1' WHERE bundle_id = ?", (bundle_id,)
        )
        connection.commit()


# --- AC14.1: purging a call ------------------------------------------------------------------


def test_a_call_purge_removes_the_call_its_evidence_and_its_revisions_at_once(tmp_path) -> None:
    from earshot.storage_call_purge import CallNotFoundError

    world = _world(tmp_path)
    call_id = _replay(world)
    blob_key = _store_recording(world)
    kept = _keyed_call(world, "kept-call", 77)
    _close_all(world)
    world.calls.record_spans(
        "default",
        spans_from_request(make_request([make_span(_synthetic_trace(), 950, start=START)])),
    )
    world.clock.advance(LATE_DEBOUNCE)
    world.closer.close_next()
    assert sorted(_incident_ids(world)) == sorted([f"{call_id}.r1", f"{call_id}.r2", f"{kept}.r1"])
    fingerprint = catalog_rows(
        tmp_path, "SELECT key_fingerprint FROM calls WHERE call_id = ?", (call_id,)
    )[0][0]
    before = _counts(world)
    scrubs = (
        catalog_rows(tmp_path, "SELECT purge_generation FROM storage_maintenance")[0][0]
        if current_dialect() == "sqlite"
        else None
    )
    purged_at = world.clock.wall

    world.purger.purge_call("default", call_id)

    after = _counts(world)
    # Only the kept call's rows remain: 1 call, 1 trace, 1 span, its 1 resource, 1 revision.
    assert {
        table: after[table] for table in ("calls", "call_traces", "spans", "call_revisions")
    } == {
        "calls": 1,
        "call_traces": 1,
        "spans": 1,
        "call_revisions": 1,
    }
    assert before["otlp_resources"] == 3 and after["otlp_resources"] == 1
    assert (after["recordings"], after["pending_blob_deletions"]) == (0, 0)
    assert world.recordings.blobs.head(blob_key) is None
    assert _incident_ids(world) == [f"{kept}.r1"]
    for revision in (1, 2):
        with pytest.raises(IncidentPurgedError):
            world.store.get_record(f"{call_id}.r{revision}")
    assert catalog_rows(
        tmp_path,
        "SELECT generation FROM call_key_generations WHERE key_fingerprint = ?",
        (fingerprint,),
    ) == [(1,)]
    assert catalog_rows(
        tmp_path,
        "SELECT generation, expires_at_unix_nano FROM call_tombstones WHERE key_fingerprint = ?",
        (fingerprint,),
    ) == [(0, purged_at + LONG_QUIET + DAY)]
    assert world.calls.resolve_call("default", SYNTHETIC_SESSION_KEY) is None
    if scrubs is not None:  # SQLite: the purge recorded a scrub and ran it
        [(recorded, scrubbed)] = catalog_rows(
            tmp_path, "SELECT purge_generation, scrubbed_generation FROM storage_maintenance"
        )
        assert recorded > scrubs and scrubbed == recorded
    with pytest.raises(CallNotFoundError):
        world.purger.purge_call("default", call_id)


def test_the_purge_route_answers_204_then_404_and_the_call_is_gone(tmp_path) -> None:
    from earshot.api import create_app

    world = _world(tmp_path)
    call_id = _replay(world)
    _close_all(world)
    world.store.create_project("other", display_name="Other")
    other = world.store.issue_api_key("other", label="other").credential

    with serve(create_app(store=world.store, blob_store=world.recordings.blobs)) as port:
        foreign = request(
            port, "DELETE", f"/v1/calls/{call_id}", headers={"Authorization": f"Bearer {other}"}
        )
        purged = request(port, "DELETE", f"/v1/calls/{call_id}")
        again = request(port, "DELETE", f"/v1/calls/{call_id}")
        detail = request(port, "GET", f"/v1/calls/{call_id}")
        listed = request(port, "GET", "/v1/calls").json()["items"]
        revision = request(port, "GET", f"/v1/incidents/{call_id}.r1")

    assert (foreign.status, foreign.problem_code()) == (404, "EARSHOT_CALL_NOT_FOUND")
    assert (purged.status, purged.body) == (204, b"")
    for reply in (again, detail):
        assert (reply.status, reply.problem_code()) == (404, "EARSHOT_CALL_NOT_FOUND")
    assert listed == []
    assert (revision.status, revision.problem_code()) == (410, "EARSHOT_INCIDENT_PURGED")


def _cli_store(tmp_path: Path, monkeypatch) -> list[str]:
    """The CLI's store arguments for this test's catalog; blobs in ``<data-dir>/blobs``."""

    from store_support import TEST_CORRELATION_KEY_BASE64, postgres_cluster

    monkeypatch.delenv("EARSHOT_BLOB_URL", raising=False)
    arguments = ["--data-dir", str(tmp_path)]
    if current_dialect() == "postgresql":
        monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
        arguments += ["--database-url", postgres_cluster().database_for(tmp_path)]
    return arguments


def test_the_cli_purges_a_call_of_a_project(tmp_path, monkeypatch, capsys) -> None:
    from earshot.cli import main

    world = _world(tmp_path)
    world.store.create_project("support", display_name="Support")
    call_id = _replay(world, project_id="support")
    blob_key = _store_recording(world, project_id="support")
    arguments = _cli_store(tmp_path, monkeypatch)

    assert main(["call", "purge", "--project", "support", call_id, *arguments]) == 0
    assert json.loads(capsys.readouterr().out) == {"call_id": call_id, "purged": True}
    assert main(["call", "purge", "--project", "support", call_id, *arguments]) == 2
    assert "EARSHOT_CALL_NOT_FOUND" in capsys.readouterr().err

    assert _counts(world, "support")["calls"] == 0
    assert world.recordings.blobs.head(blob_key) is None


# --- AC14.2: purged keys -----------------------------------------------------------------------


def test_a_purged_keys_evidence_is_dropped_until_its_tombstone_expires(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _close_all(world)
    world.purger.purge_call("default", call_id)
    expires = world.clock.wall + LONG_QUIET + DAY

    # Late evidence: the key in a new trace; an unkeyed span of the purged call's
    # trace; a recording of the key. Each is dropped and counted.
    keyed = world.calls.record_spans("default", _keyed_spans(SYNTHETIC_SESSION_KEY, 4242))
    unkeyed = world.calls.record_spans(
        "default", spans_from_request(make_request([make_span(_synthetic_trace(), 951)]))
    )
    with pytest.raises(CallPurgedError):
        _store_recording(world)
    world.clock.wall = expires - 1
    last_moment = world.calls.record_spans("default", _keyed_spans(SYNTHETIC_SESSION_KEY, 4243))

    assert (keyed.stored_spans, keyed.purged_call_evidence_dropped) == (0, 1)
    assert (unkeyed.stored_spans, unkeyed.purged_call_evidence_dropped) == (0, 1)
    assert (last_moment.stored_spans, last_moment.purged_call_evidence_dropped) == (0, 1)
    assert world.calls.otlp_stats("default")["purged_call_evidence_dropped"] == 4
    assert {table: _counts(world)[table] for table in ("calls", "call_traces", "spans")} == {
        "calls": 0,
        "call_traces": 0,
        "spans": 0,
    }

    # Once it expires, the same key starts a new call under the next generation;
    # the purged call and its tombstoned revisions are never rebuilt.
    world.clock.wall = expires
    renewed = _replay(world)
    _close_all(world)

    assert renewed == world.calls.call_id("default", SYNTHETIC_SESSION_KEY, 1) != call_id
    assert _incident_ids(world) == [f"{renewed}.r1"]
    assert world.calls.resolve_call("default", SYNTHETIC_SESSION_KEY) == renewed
    with pytest.raises(IncidentPurgedError):
        world.store.get_record(f"{call_id}.r1")


# --- AC14.3: a revision is purged only with its call -------------------------------------------


def test_purging_a_call_revision_as_an_incident_is_refused_with_its_call_id(
    tmp_path, monkeypatch, capsys
) -> None:
    from earshot.api import create_app
    from earshot.cli import main
    from earshot.storage import IncidentBelongsToCallError
    from incident_factory import make_valid_bundle

    world = _world(tmp_path)
    call_id = _replay(world)
    _close_all(world)
    world.store.ingest(make_valid_bundle(bundle_id="plain-incident"))

    with serve(create_app(store=world.store)) as port:
        refused = request(port, "DELETE", f"/v1/incidents/{call_id}.r1")
        plain = request(port, "DELETE", "/v1/incidents/plain-incident")
    with pytest.raises(IncidentBelongsToCallError) as raised:
        world.store.purge(f"{call_id}.r1")
    capsys.readouterr()
    assert main(["purge", f"{call_id}.r1", *_cli_store(tmp_path, monkeypatch)]) == 2
    assert capsys.readouterr().err == (
        "earshot: EARSHOT_INCIDENT_BELONGS_TO_CALL: the incident is a revision of a call; "
        f"purge the call: earshot call purge {call_id}\n"
    )

    assert refused.status == 409
    assert refused.json()["error"] == {
        "code": "EARSHOT_INCIDENT_BELONGS_TO_CALL",
        "message": "the incident is a revision of a call; purge the call",
        "call_id": call_id,
    }
    assert raised.value.call_id == call_id
    assert plain.status == 204
    assert _incident_ids(world) == [f"{call_id}.r1"]


# --- AC14.4: retention -------------------------------------------------------------------------


def _two_revisions(world: World) -> str:
    call_id = _replay(world)
    _close_all(world)
    world.calls.record_spans(
        "default",
        spans_from_request(make_request([make_span(_synthetic_trace(), 950, start=START)])),
    )
    world.clock.advance(LATE_DEBOUNCE)
    world.closer.close_next()
    return call_id


def test_an_expired_earlier_revision_is_never_expired_alone_not_even_by_a_read(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _two_revisions(world)
    _expire(world, f"{call_id}.r1")

    record = world.store.get_record(f"{call_id}.r1")
    world.store.set_external_reference(
        f"{call_id}.r1", namespace="host", record_type="call", external_id="ext-1"
    )
    references = world.store.get_external_references(f"{call_id}.r1")
    removed = world.store.delete_external_reference(
        f"{call_id}.r1", namespace="host", record_type="call"
    )
    world.store.purge_expired(limit=100)
    expired_calls = world.purger.purge_expired_calls()

    assert record.bundle_id == f"{call_id}.r1"
    assert [reference.external_id for reference in references] == ["ext-1"]
    assert removed is True
    assert expired_calls == 0
    assert sorted(_incident_ids(world)) == [f"{call_id}.r1", f"{call_id}.r2"]
    assert len(catalog_rows(tmp_path, "SELECT 1 FROM call_revisions")) == 2


def test_a_call_whose_latest_revision_expired_is_purged_whole_in_bounded_batches(
    tmp_path,
) -> None:
    from earshot.maintenance import MaintenanceLoop
    from earshot.storage_call_purge import call_retention_job

    world = _world(tmp_path)
    expiring = _replay(world)
    blob_key = _store_recording(world)
    other = _keyed_call(world, "other-call", 77)
    _close_all(world)
    still_open = _keyed_call(world, "open-call", 78)  # no revision: an open call never expires
    for call_id in (expiring, other):
        _expire(world, f"{call_id}.r1")
    loop = MaintenanceLoop(clock=world.clock)
    loop.add_job("call-retention", call_retention_job(world.purger, batch_size=1))

    first = world.purger.purge_expired_calls(limit=1)
    asyncio.run(loop.run_once())
    idle = world.purger.purge_expired_calls(limit=1)

    assert (first, idle) == (1, 0)
    assert [row[0] for row in catalog_rows(tmp_path, "SELECT call_id FROM calls")] == [still_open]
    assert _incident_ids(world) == []
    for call_id in (expiring, other):
        with pytest.raises(IncidentPurgedError):
            world.store.get_record(f"{call_id}.r1")
    counts = _counts(world)
    # A key tombstone per call, and one per trace of the call.
    assert (counts["call_key_generations"], counts["call_tombstones"]) == (2, 4)
    assert (counts["recordings"], counts["pending_blob_deletions"]) == (0, 0)
    assert world.recordings.blobs.head(blob_key) is None


def test_the_retention_job_collects_expired_call_and_trace_tombstones(tmp_path) -> None:
    """Tribunal round 1, LAM-4: an expired tombstone protects nothing (the key's
    generation was bumped at purge), so the retention job deletes it; a live one stays."""

    from earshot.maintenance import MaintenanceLoop
    from earshot.storage_call_purge import (
        TOMBSTONE_COLLECTION_INTERVAL_SECONDS,
        call_retention_job,
    )

    world = _world(tmp_path)
    first = _replay(world)
    _close_all(world)
    world.purger.purge_call("default", first)
    first_expires = world.clock.wall + LONG_QUIET + DAY
    world.clock.advance(3600 * SECOND)
    second = _keyed_call(world, "other-call", 77)
    _close_all(world)
    world.purger.purge_call("default", second)
    tombstones = catalog_rows(
        tmp_path, "SELECT expires_at_unix_nano FROM call_tombstones ORDER BY expires_at_unix_nano"
    )
    loop = MaintenanceLoop(clock=world.clock)
    loop.add_job("call-retention", call_retention_job(world.purger))

    world.clock.wall = first_expires - 1
    asyncio.run(loop.run_once())
    before_expiry = _counts(world)["call_tombstones"]
    world.clock.wall = first_expires
    at_expiry = world.purger.collect_expired_tombstones(limit=1)  # the boundary, one at a time
    world.clock.wall = first_expires + int(TOMBSTONE_COLLECTION_INTERVAL_SECONDS * SECOND)
    asyncio.run(loop.run_once())
    remaining = catalog_rows(tmp_path, "SELECT expires_at_unix_nano FROM call_tombstones")
    renewed = _replay(world)

    # The first call's key tombstone and its trace's; then the second call's two.
    assert [int(row[0]) for row in tombstones][:2] == [first_expires, first_expires]
    assert before_expiry == len(tombstones) == 4
    assert at_expiry == 1
    assert [int(row[0]) > first_expires for row in remaining] == [True, True]
    assert _counts(world)["call_key_generations"] == 2  # generations are never collected
    assert renewed == world.calls.call_id("default", SYNTHETIC_SESSION_KEY, 1) != first


# --- AC14.5: project deletion ------------------------------------------------------------------


def test_project_deletion_removes_every_call_row_and_blob_of_the_project(
    tmp_path, blob_backend
) -> None:
    world = _world(tmp_path, blob_backend.store)
    for project_id in ("tenant", "neighbour"):
        world.store.create_project(project_id, display_name=project_id)
        _replay(world, project_id=project_id)
        _store_recording(world, project_id=project_id)
    _close_all(world)
    # More of the tenant's state: a purged call (tombstone, generation, journal), a
    # call that is due but never closed (the closer skips a deleting project), a
    # parked trace, a pending recording, and a counter.
    purged = _keyed_call(world, "purged-call", 70, project_id="tenant")
    _store_recording(world, key="purged-call", project_id="tenant")
    world.purger.purge_call("tenant", purged)
    _keyed_call(world, "due-call", 71, project_id="tenant")
    world.calls.record_spans("tenant", spans_from_request(make_request([make_span(72, 1)])))
    world.recordings.create(
        "tenant",
        RecordingManifest(
            recording_id="pending.user",
            session_key="due-call",
            speaker="user",
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )
    world.calls.record_spans("tenant", [], rejected_spans=2)
    world.clock.advance(LONG_QUIET + END_GRACE)
    neighbour = _counts(world, "neighbour")
    tenant = _counts(world, "tenant")
    assert all(tenant[table] for table in CALL_TABLES if table != "pending_blob_deletions")
    assert len(blob_backend.objects()) == 2

    world.store.begin_project_deletion("tenant")
    assert world.closer.close_next() is None  # the due call stays unrevised
    finished = world.store.delete_project_data("tenant")
    drained = world.recordings.drain_blob_deletions(project_id="tenant")
    finished_after_drain = world.store.delete_project_data("tenant")

    assert (finished, drained.deleted, finished_after_drain) == (False, 1, True)
    assert _counts(world, "tenant") == dict.fromkeys(CALL_TABLES, 0)
    assert _counts(world, "neighbour") == neighbour
    assert len(blob_backend.objects()) == 1  # the neighbour's recording
    assert world.store.project_lifecycle("tenant") == "deleted"


# --- AC14.6: what a purge leaves behind ----------------------------------------------------------


def test_after_a_call_purge_no_transcript_text_or_key_is_stored(tmp_path) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _close_all(world)
    needles = [*TRANSCRIPT_NEEDLES, SYNTHETIC_SESSION_KEY.encode()]
    assert stored_needles(tmp_path, needles) == set(needles)  # the control: stored

    world.purger.purge_call("default", call_id)
    world.store.close()

    assert stored_needles(tmp_path, needles) == set()
    if current_dialect() == "sqlite":  # the revision artifacts are files: none is left
        objects = [path for path in (tmp_path / "objects").rglob("*") if path.is_file()]
        assert not any(needle in path.read_bytes() for path in objects for needle in needles)
