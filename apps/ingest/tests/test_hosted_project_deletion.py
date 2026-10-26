"""Deleting a hosted project with its Calls, and what races its fence.

A host deletes its project over HTTP with a hosted JWT: every Call, its spans, its
Recordings with their analyses and peaks, its projection and its revision go, on the
catalog and in the blob store, and the neighbouring project keeps all of its own. An
upload or an analysis that started before the fence and finishes after it leaves
nothing behind, and the projection job writes nothing for a deleting project.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("soundfile")

from earshot.api import create_app
from earshot.blobstore import LocalBlobStore, put_stream
from earshot.clock import ManualClock
from earshot.storage import ProjectInactiveError
from earshot.storage_analysis import AnalysisStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_recordings import RecordingManifest, RecordingStore
from host_profile_support import HostStage, HostTurn, host_request
from otlp_support import post_otlp, request, serve
from store_support import catalog_rows, make_store
from test_hosted_jwt import _hosted_config, _token, issuer  # noqa: F401  (issuer is a fixture)
from test_recordings_api import wav

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TENANT = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
NEIGHBOUR = "9b2f3c1e-1111-4222-8333-444455556666"
# Every catalog table that holds a Call's evidence, with its project column.
CALL_EVIDENCE_TABLES = (
    "calls",
    "call_traces",
    "spans",
    "call_revisions",
    "recordings",
    "audio_analysis",
    "call_projections",
    "pending_blob_deletions",
    "incidents",
)


def _headers(issuer, project: str, *scopes: str) -> dict[str, str]:  # noqa: F811
    token = _token(issuer, project_id=project, scope=" ".join(scopes))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _turns() -> list[HostTurn]:
    return [
        HostTurn(
            "turn-1",
            START,
            START + 3 * SECOND,
            user_speech=((START, START + SECOND),),
            agent_speech=(START + 2 * SECOND, START + 3 * SECOND),
            stages=(HostStage("llm", START + SECOND, START + 2 * SECOND),),
        )
    ]


def _send_call(port: int, issuer, project: str) -> None:  # noqa: F811
    provisioned = request(
        port,
        "PUT",
        f"/v1/projects/{project}",
        json.dumps({"display_name": "Support line", "capture_preset": "full"}).encode(),
        headers=_headers(issuer, project, "earshot:project:provision"),
    )
    assert provisioned.status == 201, provisioned.body
    writer = _headers(issuer, project, "earshot:calls:write")
    sent = post_otlp(
        port,
        host_request(_turns(), session_key="call-1").SerializeToString(),
        credential=writer["Authorization"].removeprefix("Bearer "),
    )
    assert sent.status == 200, sent.body
    manifest = {
        "session_key": "call-1",
        "speaker": "mixed",
        "channel_speakers": ["user", "agent"],
        "content_type": "audio/wav",
        "started_at_unix_nano": str(START),
    }
    created = request(port, "POST", "/v1/recordings", json.dumps(manifest).encode(), headers=writer)
    assert created.status in (200, 201), created.body
    uploaded = request(
        port,
        "PUT",
        created.json()["upload_path"],
        wav(channels=2, seconds=3.0),
        headers={**writer, "Content-Type": "audio/wav"},
    )
    assert uploaded.status in (200, 201), uploaded.body


def _projected(port: int, issuer, project: str) -> dict:  # noqa: F811
    deadline = time.monotonic() + 30
    while True:
        items = request(
            port, "GET", "/v1/calls", headers=_headers(issuer, project, "earshot:calls:read")
        ).json()["items"]
        if (
            items
            and items[0]["projection_state"] == "current"
            and items[0]["projection"]["analysis_state"] not in ("none", "pending")
        ):
            return items[0]
        assert time.monotonic() < deadline, items
        time.sleep(0.05)


def _rows(path: Path, project: str) -> dict[str, int]:
    return {
        table: catalog_rows(path, f"SELECT count(*) FROM {table} WHERE project_id = ?", (project,))[
            0
        ][0]
        for table in CALL_EVIDENCE_TABLES
    }


def _blobs(root: Path, project: str) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and f"/{project}/" in f"/{path.relative_to(root)}"
    )


def test_a_hosted_project_delete_removes_its_calls_recordings_analyses_and_peaks(
    tmp_path,
    issuer,  # noqa: F811
) -> None:
    store = make_store(tmp_path)
    blobs = tmp_path / "blobs"
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        call_timings=CallTimings(end_grace_seconds=0.05),
        blob_store=LocalBlobStore(blobs),
    )
    with serve(app) as port:
        for project in (TENANT, NEIGHBOUR):
            _send_call(port, issuer, project)
        for project in (TENANT, NEIGHBOUR):
            _projected(port, issuer, project)
        tenant_before = _rows(tmp_path, TENANT)
        tenant_blobs = _blobs(blobs, TENANT)
        neighbour_before = _rows(tmp_path, NEIGHBOUR)
        neighbour_blobs = _blobs(blobs, NEIGHBOUR)

        deleter = _headers(issuer, TENANT, "earshot:project:delete")
        for _ in range(50):
            deleted = request(port, "DELETE", f"/v1/projects/{TENANT}", headers=deleter)
            if deleted.status == 200:
                break
            time.sleep(0.1)
        after = request(
            port, "GET", "/v1/calls", headers=_headers(issuer, TENANT, "earshot:calls:read")
        )
        neighbour_read = _projected(port, issuer, NEIGHBOUR)

    # What there was to delete: a call with its evidence, a recording and its peaks.
    assert all(
        tenant_before[table] for table in CALL_EVIDENCE_TABLES if table != "pending_blob_deletions"
    ), tenant_before
    assert any(key.startswith("recordings/") for key in tenant_blobs), tenant_blobs
    assert any(key.startswith("analysis/") for key in tenant_blobs), tenant_blobs

    assert deleted.status == 200 and deleted.json()["state"] == "deleted"
    assert _rows(tmp_path, TENANT) == dict.fromkeys(CALL_EVIDENCE_TABLES, 0)
    assert _blobs(blobs, TENANT) == []
    assert after.status == 410
    assert _rows(tmp_path, NEIGHBOUR) == neighbour_before
    assert _blobs(blobs, NEIGHBOUR) == neighbour_blobs
    assert neighbour_read["projection"]["framework"] == "host"


# --- what races the fence ----------------------------------------------------------------------


def _world(tmp_path: Path):
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    store.create_project("leaving", display_name="Leaving")
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    return store, recordings, clock


def _manifest(recording_id: str) -> RecordingManifest:
    return RecordingManifest(
        recording_id=recording_id,
        session_key="call-1",
        speaker="user",
        content_type="audio/wav",
        started_at_unix_nano=str(START),
    )


def _finish_deletion(store, recordings, clock) -> None:
    for _ in range(20):
        if store.delete_project_data("leaving"):
            store.mark_project_deleted("leaving")
            return
        clock.advance(3600 * SECOND)  # past every reservation and retry delay
        recordings.drain_blob_deletions(project_id="leaving")
    raise AssertionError("project deletion never finished")


def test_an_upload_streaming_across_the_fence_is_refused_and_leaves_no_blob(tmp_path) -> None:
    store, recordings, clock = _world(tmp_path)
    recordings.create("leaving", _manifest("r1"))
    attempt = recordings.begin_upload("leaving", "r1")
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [wav()])

    store.begin_project_deletion("leaving")
    with pytest.raises(ProjectInactiveError):
        recordings.finish_upload(
            "leaving", "r1", attempt.blob_key, sha256=sha256, size=size, duration_ms=1500
        )
    recordings.end_upload("leaving", attempt.blob_key)
    _finish_deletion(store, recordings, clock)

    assert store.project_lifecycle("leaving") == "deleted"
    assert _blobs(tmp_path / "blobs", "leaving") == []
    assert _rows(tmp_path, "leaving") == dict.fromkeys(CALL_EVIDENCE_TABLES, 0)


def test_an_analysis_finishing_after_the_fence_writes_no_result_and_leaves_no_blob(
    tmp_path,
) -> None:
    store, recordings, clock = _world(tmp_path)
    recordings.create("leaving", _manifest("r1"))
    attempt = recordings.begin_upload("leaving", "r1")
    sha256, size = put_stream(recordings.blobs, attempt.blob_key, [wav()])
    recordings.finish_upload(
        "leaving", "r1", attempt.blob_key, sha256=sha256, size=size, duration_ms=1500
    )
    recordings.end_upload("leaving", attempt.blob_key)
    analyses = AnalysisStore(store)
    claim = analyses.claim()
    assert claim is not None and claim.project_id == "leaving"
    keys = analyses.output_keys(claim)
    assert analyses.reserve_outputs(claim, keys)
    put_stream(recordings.blobs, keys.peaks, [b"peaks"])  # written before the fence

    store.begin_project_deletion("leaving")
    store.delete_project_data("leaving")  # its recording and job row go
    stored = analyses.store_result(claim, keys, {"channels": []}, rendition=False)
    _finish_deletion(store, recordings, clock)

    assert stored is False
    assert store.project_lifecycle("leaving") == "deleted"
    assert _blobs(tmp_path / "blobs", "leaving") == []
    assert _rows(tmp_path, "leaving") == dict.fromkeys(CALL_EVIDENCE_TABLES, 0)


def test_the_projection_job_writes_nothing_for_a_deleting_project(tmp_path) -> None:
    import asyncio

    from earshot.call_capture import spans_from_request
    from earshot.storage_call_projections import CallProjector, projection_job
    from earshot.storage_closer import Closer

    store, recordings, clock = _world(tmp_path)
    calls = recordings.calls
    calls.record_spans("leaving", spans_from_request(host_request(_turns())))
    clock.advance(CallTimings().long_quiet_nano + CallTimings().end_grace_nano)
    closer = Closer(store, calls=calls, recordings=recordings)
    while closer.close_next() is not None:
        pass
    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_revisions") == [(1,)]

    store.begin_project_deletion("leaving")
    job = projection_job(CallProjector(store))
    while asyncio.run(job(clock.unix_nano())) is not None:
        pass

    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_projections") == [(0,)]


def test_a_projection_computed_before_the_fence_is_not_written_after_it(tmp_path) -> None:
    from earshot.call_capture import spans_from_request
    from earshot.storage_call_projections import CallProjector
    from earshot.storage_closer import Closer

    store, recordings, clock = _world(tmp_path)
    calls = recordings.calls
    [call_id] = calls.record_spans("leaving", spans_from_request(host_request(_turns()))).call_ids
    clock.advance(CallTimings().long_quiet_nano + CallTimings().end_grace_nano)
    closer = Closer(store, calls=calls, recordings=recordings)
    while closer.close_next() is not None:
        pass
    projector = CallProjector(store)
    snapshot = projector.snapshot("leaving", call_id)
    assert snapshot is not None
    computed = projector.compute(snapshot)

    store.begin_project_deletion("leaving")
    written = projector.write(snapshot, computed)

    assert written is False
    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_projections") == [(0,)]
