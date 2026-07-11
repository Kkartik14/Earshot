from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.api import ApiConfig, create_app
from earshot.codec import JSON_MEDIA_TYPE, PROTOBUF_MEDIA_TYPE, encode_incident_json
from earshot.codec import encode_incident_protobuf as encode_protobuf
from earshot.contract import AnalysisProjections, DerivedAnalysis, ExportPolicy
from earshot.storage import IncidentStore
from incident_factory import SECRET_SENTINEL, make_valid_bundle

pytestmark = pytest.mark.integration


def _analysis(store: IncidentStore, bundle_id: str, version: str, marker: str):
    return DerivedAnalysis(
        analyzer_name="security.test",
        analyzer_version=version,
        input_sha256=store.get_record(bundle_id).digest,
        generated_at_unix_nano="1800000000000000000",
        projections=AnalysisProjections(limitations=(marker,)),
    )


def _change_session(bundle, session_id: str):
    manifest = bundle.profile.manifest.model_copy(update={"session_id": session_id})
    session = bundle.profile.session.model_copy(update={"session_id": session_id})
    participants = tuple(
        item.model_copy(update={"session_id": session_id}) for item in bundle.profile.participants
    )
    streams = tuple(
        item.model_copy(update={"session_id": session_id}) for item in bundle.profile.audio_streams
    )
    operations = tuple(
        item.model_copy(update={"session_id": session_id}) for item in bundle.profile.operations
    )
    events = tuple(
        item.model_copy(update={"session_id": session_id}) for item in bundle.profile.events
    )
    profile = bundle.profile.model_copy(
        update={
            "manifest": manifest,
            "session": session,
            "participants": participants,
            "audio_streams": streams,
            "operations": operations,
            "events": events,
        }
    )
    return bundle.model_copy(update={"profile": profile})


def _restricted_bundle():
    bundle = _change_session(make_valid_bundle(), f"restricted-{SECRET_SENTINEL}")
    policies = list(bundle.profile.privacy.capture_classes)
    policies[0] = policies[0].model_copy(
        update={"export": ExportPolicy(allowed=False, policy_id="restricted")}
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": tuple(policies)})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


@pytest.fixture
def restricted_api(tmp_path):
    store = IncidentStore(tmp_path)
    bundle = _restricted_bundle()
    store.ingest(bundle, encode_protobuf(bundle))
    store.put_analysis(
        bundle.profile.manifest.bundle_id,
        ANALYZER_VERSION,
        _analysis(
            store,
            bundle.profile.manifest.bundle_id,
            ANALYZER_VERSION,
            "restricted_analysis",
        ),
    )
    client = TestClient(create_app(store=store, analyzer=analyze_incident))
    return store, client, bundle


def test_restricted_artifact_export_is_denied(restricted_api) -> None:
    _, client, bundle = restricted_api
    response = client.get(f"/v1/incidents/{bundle.profile.manifest.bundle_id}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EARSHOT_EXPORT_DENIED"
    assert SECRET_SENTINEL not in response.text


def test_restricted_incident_is_filtered_or_redacted_from_list(restricted_api) -> None:
    _, client, _ = restricted_api
    response = client.get("/v1/incidents")
    assert response.status_code == 200
    assert SECRET_SENTINEL not in response.text


def test_restricted_cached_analysis_export_is_denied(restricted_api) -> None:
    _, client, bundle = restricted_api
    response = client.get(f"/v1/incidents/{bundle.profile.manifest.bundle_id}/analysis")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EARSHOT_EXPORT_DENIED"
    assert SECRET_SENTINEL not in response.text


def test_cleanup_cannot_delete_an_object_between_cas_put_and_incident_commit(
    tmp_path, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="inflight-bundle")
    payload = encode_protobuf(bundle)
    object_written = threading.Event()
    resume_ingest = threading.Event()
    cleanup_snapshot_started = threading.Event()
    original_put = store.objects.put
    original_iter = store.iter_referenced_digests

    def pausing_put(value: bytes):
        result = original_put(value)
        object_written.set()
        assert resume_ingest.wait(3)
        return result

    def signaling_iter():
        cleanup_snapshot_started.set()
        yield from original_iter()

    monkeypatch.setattr(store.objects, "put", pausing_put)
    monkeypatch.setattr(store, "iter_referenced_digests", signaling_iter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest_future = pool.submit(store.ingest, bundle, payload)
        assert object_written.wait(2)
        cleanup_future = pool.submit(store.cleanup_unreferenced_objects)
        # An unsafe cleanup reaches the uncommitted-reference snapshot. A safe
        # implementation blocks here on the store mutation lock until ingest commits.
        cleanup_snapshot_started.wait(0.2)
        resume_ingest.set()
        result = ingest_future.result(timeout=3)
        removed = cleanup_future.result(timeout=3)

    assert result.created
    assert removed == 0, "cleanup deleted an object owned by an in-flight ingest"
    _, recovered = store.get_artifact("inflight-bundle")
    assert recovered == payload


def _nested(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    return value


def test_configured_depth_limit_applies_equally_to_json_and_protobuf(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    client = TestClient(create_app(store=store, config=ApiConfig(max_json_depth=8)))
    bundle = make_valid_bundle()
    policies = tuple(
        policy.model_copy(update={"decision": "allow", "captured": True})
        if policy.capture_class == "extension_payload"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    bundle = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"privacy": privacy, "future_extension": _nested(12)}
            )
        }
    )
    json_response = client.post(
        "/v1/incidents/validate",
        content=encode_incident_json(bundle),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    protobuf_response = client.post(
        "/v1/incidents/validate",
        content=encode_protobuf(bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert json_response.status_code == 400
    assert json_response.json()["error"]["code"] == "EARSHOT_JSON_TOO_DEEP"
    assert protobuf_response.status_code == json_response.status_code
    assert protobuf_response.json()["error"]["code"] == json_response.json()["error"]["code"]
    assert store.list_incidents().items == ()


def _expected_strong_etag(body: bytes) -> str:
    return f'"sha256:{hashlib.sha256(body).hexdigest()}"'


def test_content_negotiated_representations_have_sound_etags_and_vary(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    client = TestClient(create_app(store=store))
    bundle = make_valid_bundle()
    client.post(
        "/v1/incidents",
        content=encode_protobuf(bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    json_response = client.get("/v1/incidents/bundle-1", headers={"Accept": JSON_MEDIA_TYPE})
    protobuf_response = client.get(
        "/v1/incidents/bundle-1", headers={"Accept": PROTOBUF_MEDIA_TYPE}
    )
    assert json_response.status_code == protobuf_response.status_code == 200
    assert "accept" in json_response.headers.get("vary", "").lower()
    assert "accept" in protobuf_response.headers.get("vary", "").lower()

    json_etag = json_response.headers["etag"]
    protobuf_etag = protobuf_response.headers["etag"]
    if json_etag.startswith("W/") and protobuf_etag.startswith("W/"):
        # A shared explicitly weak resource tag is legal for negotiated views.
        assert json_etag == protobuf_etag
    else:
        assert json_etag == _expected_strong_etag(json_response.content)
        assert protobuf_etag == _expected_strong_etag(protobuf_response.content)
        assert json_etag != protobuf_etag


def test_purge_removes_sensitive_bytes_from_sqlite_wal_and_shm(tmp_path) -> None:
    data_dir = tmp_path / "secure-purge"
    store = IncidentStore(data_dir)
    bundle = _change_session(make_valid_bundle(), SECRET_SENTINEL)
    store.ingest(bundle, encode_protobuf(bundle))
    store.put_analysis(
        "bundle-1",
        "sensitive-analysis",
        _analysis(
            store,
            "bundle-1",
            "sensitive-analysis",
            "sensitive_analysis",
        ),
    )
    store.purge("bundle-1")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    store.close()

    needle = SECRET_SENTINEL.encode()
    residuals = []
    for path in data_dir.rglob("*"):
        if path.is_file() and needle in path.read_bytes():
            residuals.append(path.relative_to(data_dir).as_posix())
    assert residuals == [], (
        "purge left recoverable secret bytes in live SQLite/CAS files; "
        "this check does not claim erasure from SSD wear-leveling or copy-on-write snapshots: "
        + ", ".join(residuals)
    )
