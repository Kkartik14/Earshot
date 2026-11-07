"""Hosted JWTs on the Call surfaces.

Hosted authorization is a closed table: each operation a hosted JWT may perform and the
one scope that grants it. The Call operations have their own scopes, which the generic
incident scopes never grant; the operations a host does not need stay closed; and a call's
revisions, which are incidents, are hidden from a token that may not read calls.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from captured_otlp import captured_requests
from earshot.api import create_app
from earshot.blobstore import BlobNotFoundError, LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.signals.peaks import PeaksHeader
from earshot.storage_analysis import AnalysisStore
from earshot.storage_call_purge import CallPurger
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingManifest, RecordingStore
from otlp_support import request, serve
from store_support import catalog_rows, make_store
from test_hosted_jwt import _hosted_config, _token, issuer  # noqa: F401  (issuer is a fixture)
from test_recordings_api import wav

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
CALL = "call_0123456789abcdef0123456789abcdef"

CALLS_READ = "earshot:calls:read"
CALLS_WRITE = "earshot:calls:write"
CALLS_DELETE = "earshot:calls:delete"
GENERIC_SCOPES = (
    "earshot:read",
    "earshot:write",
    "earshot:delete",
    "earshot:artifact:delete",
    "earshot:summary:read",
    "earshot:project:delete",
)
CALL_SCOPES = (CALLS_READ, CALLS_WRITE, CALLS_DELETE)

# Each Call operation a host performs, and the scope that grants it.
CALL_OPERATIONS = (
    ("POST", "/v1/traces", CALLS_WRITE),
    ("POST", "/v1/recordings", CALLS_WRITE),
    ("PUT", "/v1/recordings/user-1/content", CALLS_WRITE),
    ("GET", "/v1/recordings/user-1/content", CALLS_READ),
    ("HEAD", "/v1/recordings/user-1/content", CALLS_READ),
    ("GET", "/v1/recordings/user-1/peaks", CALLS_READ),
    ("HEAD", "/v1/recordings/user-1/peaks", CALLS_READ),
    ("GET", "/v1/recordings/user-1/rendition", CALLS_READ),
    ("HEAD", "/v1/recordings/user-1/rendition", CALLS_READ),
    ("GET", "/v1/recordings/user-1/rendition/peaks", CALLS_READ),
    ("HEAD", "/v1/recordings/user-1/rendition/peaks", CALLS_READ),
    ("GET", "/v1/calls", CALLS_READ),
    ("GET", f"/v1/calls/{CALL}", CALLS_READ),
    ("GET", f"/v1/calls/{CALL}/live-turns", CALLS_READ),
    ("POST", "/v1/calls:resolve", CALLS_READ),
    ("GET", "/v1/dashboard", CALLS_READ),
    ("DELETE", f"/v1/calls/{CALL}", CALLS_DELETE),
)

# Call surfaces a host does not need: raw span attributes, recording metadata,
# transcript upload, prices and the receiver's counters. Closed for every method.
CLOSED_CALL_PATHS = (
    f"/v1/calls/{CALL}/spans/0123456789abcdef0123456789abcdef/0123456789abcdef",
    "/v1/recordings/user-1",
    "/v1/recordings/user-1/transcript",
    "/v1/prices",
    "/v1/otlp/stats",
)
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


def _headers(issuer, scopes) -> dict[str, str]:  # noqa: F811
    token = _token(issuer, project_id="project-a", scope=" ".join(scopes))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _error_code(reply) -> str | None:
    try:
        return json.loads(reply.body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _store_recording(
    store,
    blobs: LocalBlobStore,
    *,
    project_id: str,
    recording_id: str,
    speaker: str,
    session_key: str,
    started_at_unix_nano: str = str(START),
) -> str:
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=blobs)
    recordings.create(
        project_id,
        RecordingManifest(
            recording_id=recording_id,
            session_key=session_key,
            speaker=speaker,
            content_type="audio/wav",
            started_at_unix_nano=started_at_unix_nano,
        ),
    )
    attempt = recordings.begin_upload(project_id, recording_id)
    assert attempt.blob_key is not None
    try:
        body = wav(channels=1, seconds=0.5)
        digest, size = put_stream(blobs, attempt.blob_key, [body])
        stored = recordings.finish_upload(
            project_id,
            recording_id,
            attempt.blob_key,
            sha256=digest,
            size=size,
            duration_ms=500,
        )
        assert stored is not None
        return stored.call_id
    finally:
        recordings.end_upload(project_id, attempt.blob_key)


def _store_analysis_outputs(
    store,
    blobs: LocalBlobStore,
    *,
    peaks: bytes | None = None,
) -> int:
    analyses = AnalysisStore(store)
    mono_peaks = peaks or PeaksHeader(16_000, 256, 1, 1).pack() + b"\x01\x02"
    stored = 0
    while (claim := analyses.claim()) is not None:
        keys = analyses.output_keys(claim)
        assert analyses.reserve_outputs(claim, keys)
        put_stream(blobs, keys.peaks, [mono_peaks])
        if keys.rendition is not None:
            put_stream(blobs, keys.rendition, [b"fLaC\x00hosted-rendition"])
        assert analyses.store_result(
            claim,
            keys,
            {"peaks": {"channels": len(claim.recording.speakers)}},
            rendition=keys.rendition is not None,
        )
        stored += 1
    return stored


def test_each_call_operation_is_granted_by_its_own_calls_scope_only(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        for method, path, scope in CALL_OPERATIONS:
            granted = request(port, method, path, b"{}", headers=_headers(issuer, [scope]))
            # Past the gate: whatever the operation answers, it is not an authorization refusal.
            assert granted.status not in (401, 403), (method, path, granted.status, granted.body)

            others = [*GENERIC_SCOPES, *(other for other in CALL_SCOPES if other != scope)]
            refused = request(port, method, path, b"{}", headers=_headers(issuer, others))
            assert refused.status == 403, (method, path)
            if method != "HEAD":
                assert _error_code(refused) == "EARSHOT_SCOPE_REQUIRED", (method, path)
            assert f'scope="{scope}"' in refused.headers["www-authenticate"]


def test_the_closed_call_surfaces_stay_closed_whatever_the_token_grants(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    every_scope = [*GENERIC_SCOPES, *CALL_SCOPES, "earshot:project:provision"]
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        replies = {
            (method, path): request(port, method, path, b"", headers=_headers(issuer, every_scope))
            for path in CLOSED_CALL_PATHS
            for method in METHODS
        }
    for (method, path), reply in replies.items():
        assert reply.status == 403, (method, path)
        if method != "HEAD":  # a HEAD reply carries no body
            assert _error_code(reply) == "EARSHOT_OPERATION_UNAVAILABLE", (method, path)


def test_a_recording_stays_readable_after_switching_to_metadata_only(
    tmp_path,
    issuer,  # noqa: F811
) -> None:
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blobs = LocalBlobStore(tmp_path / "blobs")
    call_id = _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="call-private",
    )
    store.set_capture_preset("project-a", "metadata_only")
    reader = _headers(issuer, [CALLS_READ])

    with serve(create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)) as port:
        content = request(port, "GET", "/v1/recordings/user-1/content", headers=reader)

    assert call_id
    assert content.status == 200 and content.body == wav(channels=1, seconds=0.5)


def test_hosted_reads_stream_peaks_and_stereo_renditions(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blobs = LocalBlobStore(tmp_path / "blobs")
    call_id = _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="paired-call",
    )
    assert (
        _store_recording(
            store,
            blobs,
            project_id="project-a",
            recording_id="agent-1",
            speaker="agent",
            session_key="paired-call",
        )
        == call_id
    )
    assert _store_analysis_outputs(store, blobs) == 2
    reader = _headers(issuer, [CALLS_READ])
    expected_peaks = PeaksHeader(16_000, 256, 1, 1).pack() + b"\x01\x02"
    expected_pair_peaks = PeaksHeader(16_000, 256, 1, 2).pack() + b"\x01\x02\x01\x02"

    with serve(create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)) as port:
        content = request(port, "GET", "/v1/recordings/user-1/content", headers=reader)
        peaks = request(port, "GET", "/v1/recordings/user-1/peaks", headers=reader)
        rendition = request(port, "GET", "/v1/recordings/user-1/rendition", headers=reader)
        rendition_peaks = request(
            port, "GET", "/v1/recordings/user-1/rendition/peaks", headers=reader
        )
        head_content = request(port, "HEAD", "/v1/recordings/user-1/content", headers=reader)
        head_peaks = request(port, "HEAD", "/v1/recordings/user-1/peaks", headers=reader)
        head_rendition = request(port, "HEAD", "/v1/recordings/user-1/rendition", headers=reader)
        head_rendition_peaks = request(
            port, "HEAD", "/v1/recordings/user-1/rendition/peaks", headers=reader
        )

    assert content.status == 200 and content.body == wav(channels=1, seconds=0.5)
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert peaks.status == 200 and peaks.body == expected_peaks
    assert rendition.status == 200 and rendition.body == b"fLaC\x00hosted-rendition"
    assert rendition.headers["x-content-type-options"] == "nosniff"
    assert rendition_peaks.status == 200 and rendition_peaks.body == expected_pair_peaks
    for head, reply in (
        (head_content, content),
        (head_peaks, peaks),
        (head_rendition, rendition),
        (head_rendition_peaks, rendition_peaks),
    ):
        assert head.status == reply.status == 200 and head.body == b""
        for header in ("content-type", "content-length", "cache-control", "etag"):
            assert head.headers.get(header) == reply.headers.get(header), header


def test_rendition_reads_are_scoped_to_the_recording_project(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    store.create_project("project-b", display_name="Project B")
    blobs = LocalBlobStore(tmp_path / "blobs")
    _store_recording(
        store,
        blobs,
        project_id="project-b",
        recording_id="user-1",
        speaker="user",
        session_key="project-b-call",
    )
    _store_recording(
        store,
        blobs,
        project_id="project-b",
        recording_id="agent-1",
        speaker="agent",
        session_key="project-b-call",
    )
    _store_analysis_outputs(store, blobs)

    def project_headers(project_id: str):
        return {
            "Authorization": f"Bearer {_token(issuer, project_id=project_id, scope=CALLS_READ)}"
        }

    with serve(create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)) as port:
        allowed = request(
            port,
            "GET",
            "/v1/recordings/user-1/rendition",
            headers=project_headers("project-b"),
        )
        hidden = request(
            port,
            "GET",
            "/v1/recordings/user-1/rendition",
            headers=project_headers("project-a"),
        )

    assert allowed.status == 200 and allowed.body == b"fLaC\x00hosted-rendition"
    assert (hidden.status, hidden.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND")


def test_call_delete_removes_hosted_audio_reads_and_the_blob(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blob_root = tmp_path / "blobs"
    blobs = LocalBlobStore(blob_root)
    call_id = _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="call-audio",
    )
    assert _store_analysis_outputs(store, blobs) == 1
    app = create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)
    reader = _headers(issuer, [CALLS_READ])
    deleter = _headers(issuer, [CALLS_DELETE])

    with serve(app) as port:
        before = request(
            port,
            "GET",
            "/v1/recordings/user-1/content",
            headers=reader,
        )
        peaks_before = request(port, "GET", "/v1/recordings/user-1/peaks", headers=reader)
        deleted = request(port, "DELETE", f"/v1/calls/{call_id}", headers=deleter)
        after = request(
            port,
            "GET",
            "/v1/recordings/user-1/content",
            headers=reader,
        )
        peaks_after = request(port, "GET", "/v1/recordings/user-1/peaks", headers=reader)

    assert before.status == 200 and before.body
    assert peaks_before.status == 200 and peaks_before.body
    assert (deleted.status, deleted.body) == (204, b"")
    assert (after.status, after.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND")
    assert (peaks_after.status, peaks_after.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND")
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(0,)]
    assert not [path for path in blob_root.rglob("*") if path.is_file()]


@pytest.mark.parametrize("media", ["content", "peaks"])
def test_a_media_read_racing_call_purge_answers_404(
    tmp_path,
    issuer,  # noqa: F811
    monkeypatch,
    media,
) -> None:
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blobs = LocalBlobStore(tmp_path / "blobs")
    call_id = _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="purge-race",
    )
    assert _store_analysis_outputs(store, blobs) == 1
    purger = CallPurger(
        store,
        calls=CallStore(store),
        recordings=RecordingStore(store, calls=CallStore(store), blobs=blobs),
    )

    def deleted_before_blob_open(_self, _key, _start, _end):
        purger.purge_call("project-a", call_id)
        raise BlobNotFoundError("concurrent Call deletion")

    monkeypatch.setattr(LocalBlobStore, "open_range", deleted_before_blob_open)
    path = f"/v1/recordings/user-1/{media}"
    with serve(create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)) as port:
        reply = request(port, "GET", path, headers=_headers(issuer, [CALLS_READ]))

    assert (reply.status, reply.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND"), reply.body


def test_peaks_response_refuses_an_oversized_derived_blob(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blobs = LocalBlobStore(tmp_path / "blobs")
    _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="oversized-peaks",
    )
    oversized_peaks = b"x" * (8 * 1024 * 1024 + 1)
    assert _store_analysis_outputs(store, blobs, peaks=oversized_peaks) == 1

    with serve(create_app(store=store, config=_hosted_config(issuer), blob_store=blobs)) as port:
        reply = request(
            port,
            "GET",
            "/v1/recordings/user-1/peaks",
            headers=_headers(issuer, [CALLS_READ]),
        )

    assert (reply.status, reply.problem_code()) == (500, "EARSHOT_PEAKS_TOO_LARGE")


def test_media_reads_have_a_configured_concurrency_limit(
    tmp_path,
    issuer,  # noqa: F811
    monkeypatch,
) -> None:
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    blobs = LocalBlobStore(tmp_path / "blobs")
    _store_recording(
        store,
        blobs,
        project_id="project-a",
        recording_id="user-1",
        speaker="user",
        session_key="limited-reads",
    )
    config = replace(_hosted_config(issuer), media_read_max_concurrent_requests=1)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    opens = 0
    open_range = LocalBlobStore.open_range

    def hold_first_read(self, key, start, end):
        nonlocal opens
        with lock:
            opens += 1
            first = opens == 1
        if first:
            entered.set()
            assert release.wait(5)
        return open_range(self, key, start, end)

    monkeypatch.setattr(LocalBlobStore, "open_range", hold_first_read)
    headers = _headers(issuer, [CALLS_READ])
    with (
        serve(create_app(store=store, config=config, blob_store=blobs)) as port,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        pending = executor.submit(
            request, port, "GET", "/v1/recordings/user-1/content", headers=headers
        )
        assert entered.wait(5)
        try:
            second = request(port, "GET", "/v1/recordings/user-1/content", headers=headers)
        finally:
            release.set()
        first = pending.result(timeout=5)
        after = request(port, "GET", "/v1/recordings/user-1/content", headers=headers)

    assert first.status == 200
    assert (second.status, second.problem_code()) == (503, "EARSHOT_MEDIA_READ_LIMIT")
    assert second.headers["retry-after"] == "1"
    assert after.status == 200


def test_an_operation_missing_from_the_table_fails_closed(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="Project A")
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        headers = _headers(issuer, GENERIC_SCOPES)
        # Before: any method fell back to a generic scope by its verb.
        unlisted = request(port, "PATCH", "/v1/incidents", b"", headers=headers)
        unknown = request(port, "GET", "/v1/not-a-route", headers=headers)
        listed = request(port, "GET", "/v1/incidents", headers=headers)
    assert unlisted.status == 403 and _error_code(unlisted) == "EARSHOT_OPERATION_UNAVAILABLE"
    assert unknown.status == 403 and _error_code(unknown) == "EARSHOT_OPERATION_UNAVAILABLE"
    assert listed.status == 200


def test_the_hosted_contract_lists_the_call_operations_with_their_scopes(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        paths = request(port, "GET", "/openapi.json").json()["paths"]

    templates = {
        ("post", "/v1/traces"): CALLS_WRITE,
        ("post", "/v1/recordings"): CALLS_WRITE,
        ("put", "/v1/recordings/{recording_id}/content"): CALLS_WRITE,
        ("get", "/v1/recordings/{recording_id}/content"): CALLS_READ,
        ("get", "/v1/recordings/{recording_id}/peaks"): CALLS_READ,
        ("get", "/v1/recordings/{recording_id}/rendition"): CALLS_READ,
        ("get", "/v1/recordings/{recording_id}/rendition/peaks"): CALLS_READ,
        ("get", "/v1/calls"): CALLS_READ,
        ("get", "/v1/calls/{call_id}"): CALLS_READ,
        ("get", "/v1/calls/{call_id}/live-turns"): CALLS_READ,
        ("post", "/v1/calls:resolve"): CALLS_READ,
        ("get", "/v1/dashboard"): CALLS_READ,
        ("delete", "/v1/calls/{call_id}"): CALLS_DELETE,
    }
    for (method, path), scope in templates.items():
        assert paths[path][method]["x-required-scope"] == scope, (method, path)
    media_paths = (
        "/v1/recordings/{recording_id}/content",
        "/v1/recordings/{recording_id}/peaks",
        "/v1/recordings/{recording_id}/rendition",
        "/v1/recordings/{recording_id}/rendition/peaks",
    )
    for path in media_paths:
        retry_after = paths[path]["get"]["responses"]["503"]["headers"]["Retry-After"]
        assert retry_after["schema"]["minimum"] == 1, path
    for path in (
        "/v1/calls/{call_id}/spans/{trace_id}/{span_id}",
        "/v1/recordings/{recording_id}",
        "/v1/recordings/{recording_id}/transcript",
        "/v1/prices",
        "/v1/otlp/stats",
    ):
        assert path not in paths, path
    # A path with open reads and a write lists only those methods.
    assert set(paths["/v1/recordings/{recording_id}/content"]) == {"get", "put"}


def _project_with_a_call_revision(tmp_path: Path, project_id: str):
    """A store whose project holds one closed call with its revision; the revision's bundle ID."""

    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    store.create_project(project_id, display_name="Project A")
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    closer = Closer(store, calls=calls, recordings=recordings)
    for item in captured_requests():
        calls.record_spans(project_id, spans_from_request(item))
        clock.advance(5 * SECOND)
    clock.advance(TIMINGS.long_quiet_nano + TIMINGS.end_grace_nano)
    while closer.close_next() is not None:
        pass
    [(bundle_id, session_id)] = catalog_rows(
        tmp_path,
        "SELECT incidents.bundle_id, incidents.session_id FROM call_revisions "
        "JOIN incidents ON incidents.bundle_id = call_revisions.bundle_id "
        "WHERE call_revisions.project_id = ?",
        (project_id,),
    )
    return store, bundle_id, session_id


def test_a_token_that_may_not_read_calls_never_sees_a_call_revision(tmp_path, issuer) -> None:  # noqa: F811
    store, revision, session_id = _project_with_a_call_revision(tmp_path, "project-a")
    incident_reader = _headers(issuer, ["earshot:read", "earshot:summary:read"])
    call_reader = _headers(issuer, ["earshot:read", "earshot:summary:read", CALLS_READ])
    detail_paths = (
        f"/v1/incidents/{revision}",
        f"/v1/incidents/{revision}/analysis",
        f"/v1/incidents/{revision}/explanation",
        f"/v1/incidents/{revision}/export?format=json",
        f"/v1/incidents/{revision}/references",
        f"/v1/incidents/{revision}/contradictions",
        f"/v1/incidents/{revision}/evidence/summary",
        f"/v1/incidents/{revision}/evidence/not_observed",
    )

    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        hidden_list = request(port, "GET", "/v1/incidents", headers=incident_reader).json()
        hidden_details = {
            path: request(port, "GET", path, headers=incident_reader) for path in detail_paths
        }
        hidden_metrics = request(port, "GET", "/v1/metrics/turns", headers=incident_reader)
        hidden_summary = request(
            port, "GET", "/v1/projects/project-a/summary", headers=incident_reader
        ).json()
        shown_list = request(port, "GET", "/v1/incidents", headers=call_reader).json()
        shown_detail = request(port, "GET", f"/v1/incidents/{revision}", headers=call_reader)
        shown_summary = request(
            port, "GET", "/v1/projects/project-a/summary", headers=call_reader
        ).json()

    assert revision not in [item["bundle_id"] for item in hidden_list["items"]]
    for path, reply in hidden_details.items():
        # Exactly what a missing incident answers: no hint the revision exists.
        assert reply.status == 404, (path, reply.status)
        assert _error_code(reply) == "EARSHOT_INCIDENT_NOT_FOUND", path
    assert hidden_metrics.status == 200
    assert revision.encode() not in hidden_metrics.body
    assert session_id not in [item["session_id"] for item in hidden_summary["items"]]

    assert revision in [item["bundle_id"] for item in shown_list["items"]]
    assert shown_detail.status == 200
    assert session_id in [item["session_id"] for item in shown_summary["items"]]


def test_a_host_provisions_its_project_sends_a_call_in_the_host_profile_and_reads_it(
    tmp_path,
    issuer,  # noqa: F811
) -> None:
    import time

    from earshot.storage_calls import CallTimings as Timings
    from host_profile_support import HostStage, HostTurn, host_request
    from otlp_support import post_otlp

    project = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

    def headers(*scopes):
        token = _token(issuer, project_id=project, scope=" ".join(scopes))
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    turns = [
        HostTurn(
            f"turn-{number}",
            START + number * 10 * SECOND,
            START + (number * 10 + 4) * SECOND,
            user_speech=((START + number * 10 * SECOND, START + (number * 10 + 1) * SECOND),),
            agent_speech=(START + (number * 10 + 2) * SECOND, START + (number * 10 + 4) * SECOND),
            stages=(
                HostStage(
                    "llm", START + (number * 10 + 1) * SECOND, START + (number * 10 + 2) * SECOND
                ),
            ),
            reported_latency_ms=1000.0 + number,
        )
        for number in (1, 2)
    ]
    store = make_store(tmp_path)
    app = create_app(
        store=store,
        config=_hosted_config(issuer),
        call_timings=Timings(end_grace_seconds=0.05),
    )
    with serve(app) as port:
        provisioned = request(
            port,
            "PUT",
            f"/v1/projects/{project}",
            json.dumps({"display_name": "Support line", "capture_preset": "full"}).encode(),
            headers=headers("earshot:project:provision"),
        )
        sent = post_otlp(
            port,
            host_request(turns).SerializeToString(),
            credential=_token(issuer, project_id=project, scope=CALLS_WRITE),
        )
        deadline = time.monotonic() + 15
        while True:
            listed = request(port, "GET", "/v1/calls", headers=headers(CALLS_READ)).json()
            items = listed["items"]
            if items and items[0]["projection_state"] == "current":
                break
            assert time.monotonic() < deadline, listed
            time.sleep(0.05)
        detail = request(
            port, "GET", f"/v1/calls/{items[0]['call_id']}", headers=headers(CALLS_READ)
        ).json()

    assert provisioned.status == 201
    assert sent.status == 200
    assert detail["projection"]["framework"] == "host"
    turns_read = detail["projection"]["turns"]
    assert [turn["turn_id"] for turn in turns_read] == ["turn-1", "turn-2"]
    assert [turn["reported_latency_ms"] for turn in turns_read] == [1001, 1002]
    assert {turn["audible_gap"]["reason"] for turn in turns_read} == {"no_recording"}
    assert [[span["role"] for span in turn["spans"]] for turn in turns_read] == [["llm"], ["llm"]]


def _committed_after_the_first_check(store, monkeypatch) -> None:
    """``is_call_revision`` says no the first time it is asked about each bundle, as if
    the revision committed between the gate and the route."""

    asked: set[str] = set()
    real = store.is_call_revision

    def racing(bundle_id: str, *, project_id: str) -> bool:
        if bundle_id not in asked:
            asked.add(bundle_id)
            return False
        return real(bundle_id, project_id=project_id)

    monkeypatch.setattr(store, "is_call_revision", racing)


def test_a_revision_committed_while_the_route_runs_is_still_hidden(
    tmp_path,
    issuer,  # noqa: F811
    monkeypatch,
) -> None:
    store, revision, _ = _project_with_a_call_revision(tmp_path, "project-a")
    _committed_after_the_first_check(store, monkeypatch)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        reply = request(
            port, "GET", f"/v1/incidents/{revision}", headers=_headers(issuer, ["earshot:read"])
        )

    assert reply.status == 404 and _error_code(reply) == "EARSHOT_INCIDENT_NOT_FOUND"


def test_a_hidden_revision_as_the_comparison_baseline_is_a_missing_baseline(
    tmp_path,
    issuer,  # noqa: F811
) -> None:
    from incident_factory import make_valid_bundle

    store, revision, _ = _project_with_a_call_revision(tmp_path, "project-a")
    from earshot.analysis import analyze_incident

    visible = make_valid_bundle(bundle_id="visible-1")
    ingested = store.ingest(visible, project_id="project-a")
    analysis = analyze_incident(
        visible, input_sha256=ingested.record.digest, generated_at_unix_nano=str(START)
    )
    store.put_analysis(
        "visible-1",
        analysis.analyzer_version,
        analysis.model_dump(mode="json"),
        project_id="project-a",
    )
    reader = _headers(issuer, ["earshot:read"])
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        hidden, missing = (
            request(
                port,
                "GET",
                f"/v1/incidents/visible-1/comparison?known_good_bundle_id={baseline}",
                headers=reader,
            )
            for baseline in (revision, "call_ffffffffffffffffffffffffffffffff.r1")
        )

    assert missing.status == 404 and _error_code(missing) == "EARSHOT_KNOWN_GOOD_NOT_FOUND"
    assert (hidden.status, json.loads(hidden.body)) == (missing.status, json.loads(missing.body))
