from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from earshot.analysis import ANALYZER_VERSION, analyze_incident
from earshot.api import ApiConfig, create_app
from earshot.codec import (
    JSON_MEDIA_TYPE,
    PROTOBUF_MEDIA_TYPE,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from earshot.contract import DerivedAnalysis, Diagnosis, ExportPolicy, RetentionPolicy
from earshot.storage import IncidentStore, StorageCleanupPendingError
from incident_factory import SECRET_SENTINEL, make_valid_bundle

pytestmark = pytest.mark.integration


def app_client(tmp_path, *, config: ApiConfig | None = None, analyzer=analyze_incident):
    store = IncidentStore(tmp_path)
    app = create_app(store=store, config=config, analyzer=analyzer)
    return store, TestClient(app)


def code(response) -> str:
    return response.json()["error"]["code"]


def changed_status(bundle, status: str):
    session = bundle.profile.session.model_copy(update={"status": status})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"session": session})}
    )


def changed_session_id(bundle, session_id: str):
    profile = bundle.profile
    updated_profile = profile.model_copy(
        update={
            "manifest": profile.manifest.model_copy(update={"session_id": session_id}),
            "session": profile.session.model_copy(update={"session_id": session_id}),
            "participants": tuple(
                item.model_copy(update={"session_id": session_id}) for item in profile.participants
            ),
            "audio_streams": tuple(
                item.model_copy(update={"session_id": session_id}) for item in profile.audio_streams
            ),
            "operations": tuple(
                item.model_copy(update={"session_id": session_id}) for item in profile.operations
            ),
            "events": tuple(
                item.model_copy(update={"session_id": session_id}) for item in profile.events
            ),
        }
    )
    return bundle.model_copy(update={"profile": updated_profile})


def with_expired_metadata(bundle):
    policies = tuple(
        policy.model_copy(update={"retention": RetentionPolicy(expires_at_unix_nano="0")})
        if policy.capture_class == "metadata"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def test_health_and_readiness_are_available_without_incident_auth(tmp_path) -> None:
    config = ApiConfig(token="test-token")
    _, client = app_client(tmp_path, config=config)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_blocked_storage_ingest_does_not_stall_asgi_health(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    original_ingest = store.ingest
    started = threading.Event()
    release = threading.Event()

    def blocked_ingest(bundle, payload, **kwargs):
        started.set()
        assert release.wait(2)
        return original_ingest(bundle, payload, **kwargs)

    monkeypatch.setattr(store, "ingest", blocked_ingest)
    app = create_app(store=store, analyzer=analyze_incident)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ingest = asyncio.create_task(
            client.post(
                "/v1/incidents",
                content=encode_incident_protobuf(valid_bundle),
                headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            health = await asyncio.wait_for(client.get("/healthz"), timeout=0.25)
            assert health.status_code == 200
        finally:
            release.set()
        assert (await ingest).status_code == 201


@pytest.mark.asyncio
async def test_api_key_verification_does_not_stall_asgi_health(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="liveness test")
    original_authenticate = store.authenticate_api_key
    started = threading.Event()
    release = threading.Event()

    def blocked_authenticate(credential: str):
        started.set()
        release.wait(2)
        return original_authenticate(credential)

    monkeypatch.setattr(store, "authenticate_api_key", blocked_authenticate)
    app = create_app(store=store, analyzer=analyze_incident)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        started_at = time.monotonic()
        authenticated = asyncio.create_task(
            client.get(
                "/v1/incidents",
                headers={"Authorization": f"Bearer {issued.credential}"},
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            health = await asyncio.wait_for(client.get("/healthz"), timeout=0.25)
            assert health.status_code == 200
            assert time.monotonic() - started_at < 0.5
        finally:
            release.set()
        assert (await authenticated).status_code == 200


@pytest.mark.parametrize(
    ("host", "allowed"),
    [("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", False)],
)
def test_remote_binding_requires_an_explicit_tls_proxy(host: str, allowed: bool) -> None:
    if allowed:
        assert ApiConfig(host=host).host == host
    else:
        with pytest.raises(ValueError, match="requires an explicitly trusted TLS proxy"):
            ApiConfig(host=host)


@pytest.mark.parametrize(
    "settings",
    [
        {"retention_cleanup_interval_seconds": 0},
        {"retention_cleanup_interval_seconds": float("nan")},
        {"retention_cleanup_batch_size": 0},
        {"retention_cleanup_batch_size": 10_001},
        {"retention_cleanup_batch_size": True},
    ],
)
def test_retention_cleanup_settings_are_finite_and_bounded(settings) -> None:
    with pytest.raises(ValueError):
        ApiConfig(**settings)


def test_tls_proxy_allows_an_authenticated_non_loopback_socket() -> None:
    config = ApiConfig(
        host="0.0.0.0",
        token="secret-token",
        behind_tls_proxy=True,
    )
    assert config.host == "0.0.0.0"


def test_tls_proxy_application_requires_a_configured_credential(tmp_path) -> None:
    config = ApiConfig(host="127.0.0.1", behind_tls_proxy=True)
    with pytest.raises(ValueError, match="requires a bearer token or an active project API key"):
        create_app(store=IncidentStore(tmp_path), config=config)


def test_project_key_exchanges_for_a_secure_http_only_viewer_session(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="viewer")
    app = create_app(
        store=store,
        config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
        analyzer=analyze_incident,
    )
    with TestClient(app, base_url="https://viewer.example") as client:
        unauthorized = client.get("/v1/incidents/bundle-1/references")
        assert unauthorized.status_code == 401
        exchange = client.post(
            "/v1/auth/session",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        assert exchange.status_code == 201
        cookie = exchange.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Secure" in cookie
        assert issued.credential not in cookie
        assert issued.credential not in exchange.text
        assert exchange.json()["project_id"] == "default"
        assert exchange.json()["csrf_token"]
        assert exchange.json()["auth_context_id"]
        assert exchange.json()["auth_context_id"] != exchange.json()["csrf_token"]

        session = client.get("/v1/auth/session")
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["csrf_token"] == exchange.json()["csrf_token"]
        assert session.json()["auth_context_id"] == exchange.json()["auth_context_id"]

        no_csrf = client.put(
            "/v1/incidents/bundle-1/references/platform/call",
            json={"external_id": "call_123"},
        )
        assert no_csrf.status_code == 403

        authenticated = client.get("/v1/incidents")
        assert authenticated.status_code == 200
        invalid_bearer = client.get(
            "/v1/incidents",
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid_bearer.status_code == 401


def test_incident_references_are_idempotent_and_returned_only_within_project(
    tmp_path, valid_bundle
) -> None:
    store = IncidentStore(tmp_path)
    store.ingest(valid_bundle, encode_incident_protobuf(valid_bundle))
    default_key = store.issue_api_key("default", label="platform service")
    store.create_project("other-project", display_name="Other project")
    other_key = store.issue_api_key("other-project", label="other project service")
    app = create_app(store=store, analyzer=analyze_incident)

    with TestClient(app) as client:
        call = client.put(
            "/v1/incidents/bundle-1/references/platform/call",
            json={"external_id": "call_123"},
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert call.status_code == 200
        assert call.json()["external_id"] == "call_123"

        run = client.put(
            "/v1/incidents/bundle-1/references/voice_labs/run",
            json={"external_id": "run_456"},
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert run.status_code == 200
        first_link_time = run.json()["linked_at_unix_nano"]
        retry = client.put(
            "/v1/incidents/bundle-1/references/voice_labs/run",
            json={"external_id": "run_456"},
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert retry.status_code == 200
        assert retry.json()["linked_at_unix_nano"] == first_link_time

        listing = client.get(
            "/v1/incidents/bundle-1/references",
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert listing.status_code == 200
        assert [
            (item["namespace"], item["record_type"], item["external_id"])
            for item in listing.json()["items"]
        ] == [
            ("platform", "call", "call_123"),
            ("voice_labs", "run", "run_456"),
        ]
        assert default_key.credential not in listing.text

        cross_project = client.get(
            "/v1/incidents/bundle-1/references",
            headers={"Authorization": f"Bearer {other_key.credential}"},
        )
        assert cross_project.status_code == 404
        assert code(cross_project) == "EARSHOT_INCIDENT_NOT_FOUND"

        mismatch = client.get(
            "/v1/incidents/bundle-1/references",
            headers={
                "Authorization": f"Bearer {default_key.credential}",
                "X-Earshot-Project-Id": "other-project",
            },
        )
        assert mismatch.status_code == 403
        assert code(mismatch) == "EARSHOT_PROJECT_MISMATCH"

        removed = client.delete(
            "/v1/incidents/bundle-1/references/voice_labs/run",
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert removed.status_code == 204
        remaining = client.get(
            "/v1/incidents/bundle-1/references",
            headers={"Authorization": f"Bearer {default_key.credential}"},
        )
        assert [item["external_id"] for item in remaining.json()["items"]] == ["call_123"]


@pytest.mark.parametrize(
    "external_id",
    ["", "call/123", "call 123", "<script>", "secret?token=value"],
)
def test_incident_references_reject_nonportable_or_untrusted_ids(
    tmp_path, valid_bundle, external_id: str
) -> None:
    store, client = app_client(tmp_path)
    store.ingest(valid_bundle, encode_incident_protobuf(valid_bundle))

    response = client.put(
        "/v1/incidents/bundle-1/references/platform/call",
        json={"external_id": external_id},
    )

    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_REQUEST"
    assert not external_id or external_id not in response.text


def test_incident_references_reject_nonportable_path_keys_without_echoing_them(
    tmp_path, valid_bundle
) -> None:
    store, client = app_client(tmp_path)
    store.ingest(valid_bundle, encode_incident_protobuf(valid_bundle))
    response = client.put(
        "/v1/incidents/bundle-1/references/Not+Portable/call",
        json={"external_id": "call_123"},
    )

    assert response.status_code == 422
    assert "Not+Portable" not in response.text


def test_tokenless_loopback_viewer_can_discover_that_login_is_not_required(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.get("/v1/auth/session")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "authentication_required": False,
        "project_id": "default",
        "auth_context_id": "anonymous-default",
        "csrf_token": None,
        "expires_in_seconds": None,
    }


def test_viewer_logout_requires_csrf_and_revokes_the_server_session(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="viewer logout")
    app = create_app(
        store=store,
        config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
        analyzer=analyze_incident,
    )
    with TestClient(app, base_url="https://viewer.example") as client:
        exchange = client.post(
            "/v1/auth/session",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        csrf = exchange.json()["csrf_token"]
        session_cookie = client.cookies.get("earshot_session")
        assert session_cookie

        missing = client.post("/v1/auth/logout")
        assert missing.status_code == 403
        assert code(missing) == "EARSHOT_CSRF_REQUIRED"

        logout = client.post(
            "/v1/auth/logout",
            headers={"X-Earshot-CSRF": csrf},
        )
        assert logout.status_code == 204
        assert "Max-Age=0" in logout.headers["set-cookie"]

        client.cookies.set("earshot_session", session_cookie, domain="viewer.example", path="/")
        revoked = client.get("/v1/incidents")
        assert revoked.status_code == 401


def test_viewer_session_expires_server_side(tmp_path, monkeypatch) -> None:
    now = [10.0]
    monkeypatch.setattr("earshot.browser_session.time.monotonic", lambda: now[0])
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="expiring viewer")
    app = create_app(
        store=store,
        config=ApiConfig(
            host="0.0.0.0",
            behind_tls_proxy=True,
            viewer_session_ttl_seconds=1,
        ),
        analyzer=analyze_incident,
    )
    with TestClient(app, base_url="https://viewer.example") as client:
        exchange = client.post(
            "/v1/auth/session",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        assert exchange.status_code == 201
        now[0] = 12.0
        expired = client.get("/v1/incidents")
        assert expired.status_code == 401


def test_revoking_project_key_invalidates_its_viewer_sessions(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="revoked viewer")
    app = create_app(
        store=store,
        config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
        analyzer=analyze_incident,
    )
    with TestClient(app, base_url="https://viewer.example") as client:
        exchange = client.post(
            "/v1/auth/session",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        assert exchange.status_code == 201
        store.revoke_api_key("default", issued.key_id)

        revoked = client.get("/v1/incidents")
        assert revoked.status_code == 401


def test_viewer_session_capacity_evicts_the_oldest_session(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    issued = store.issue_api_key("default", label="bounded viewers")
    app = create_app(
        store=store,
        config=ApiConfig(
            host="0.0.0.0",
            behind_tls_proxy=True,
            viewer_session_capacity=1,
        ),
        analyzer=analyze_incident,
    )
    with (
        TestClient(app, base_url="https://viewer.example") as first,
        TestClient(app, base_url="https://viewer.example") as second,
    ):
        assert (
            first.post(
                "/v1/auth/session",
                headers={"Authorization": f"Bearer {issued.credential}"},
            ).status_code
            == 201
        )
        assert (
            second.post(
                "/v1/auth/session",
                headers={"Authorization": f"Bearer {issued.credential}"},
            ).status_code
            == 201
        )

        assert first.get("/v1/incidents").status_code == 401
        assert second.get("/v1/incidents").status_code == 200


def test_actual_asgi_listener_cannot_bypass_declared_loopback_security(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    app = create_app(store=store, config=ApiConfig(), analyzer=analyze_incident)
    with TestClient(app, base_url="http://0.0.0.0") as client:
        response = client.get("/v1/incidents")
    assert response.status_code == 503
    assert code(response) == "EARSHOT_REMOTE_BINDING_UNSAFE"


def test_actual_asgi_listener_guard_also_protects_provider_hooks(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    app = create_app(store=store, config=ApiConfig(), analyzer=analyze_incident)
    with TestClient(app, base_url="http://0.0.0.0") as client:
        response = client.post(
            "/hooks/v1/connectors/opaque_connector_0001",
            content=b'{"transcript":"must-not-cross-plaintext"}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 503
    assert code(response) == "EARSHOT_REMOTE_BINDING_UNSAFE"
    assert "must-not-cross-plaintext" not in response.text


def test_tokenless_loopback_api_rejects_dns_rebinding_host_header(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.get("/v1/incidents", headers={"Host": "attacker.example:4319"})
    assert response.status_code == 400
    assert code(response) == "EARSHOT_UNTRUSTED_HOST"


def test_json_ingest_get_metadata_and_get_protobuf_roundtrip(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=encode_incident_json(valid_bundle),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 201
    assert response.json()["created"] is True
    assert isinstance(response.json()["ingested_at_unix_nano"], str)

    rendered = client.get("/v1/incidents/bundle-1")
    assert rendered.status_code == 200
    assert rendered.headers["content-type"].startswith(JSON_MEDIA_TYPE)
    assert decode_incident_json(rendered.content).profile == valid_bundle.profile
    assert rendered.headers["etag"].startswith('"sha256:')

    generic_json = client.get("/v1/incidents/bundle-1", headers={"Accept": "application/json"})
    assert generic_json.status_code == 200
    assert generic_json.headers["content-type"].startswith("application/json")
    assert decode_incident_json(generic_json.content).profile == valid_bundle.profile

    binary = client.get("/v1/incidents/bundle-1", headers={"Accept": PROTOBUF_MEDIA_TYPE})
    assert binary.status_code == 200
    assert decode_incident_protobuf(binary.content).profile == valid_bundle.profile


def test_ingest_location_roundtrips_every_allowed_bundle_id_character(
    tmp_path, valid_bundle
) -> None:
    manifest = valid_bundle.profile.manifest.model_copy(
        update={"bundle_id": "bundle.with_~allowed-chars"}
    )
    bundle = valid_bundle.model_copy(
        update={"profile": valid_bundle.profile.model_copy(update={"manifest": manifest})}
    )
    _, client = app_client(tmp_path)
    ingested = client.post(
        "/v1/incidents",
        content=encode_incident_json(bundle),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert ingested.status_code == 201
    location = ingested.headers["location"]
    assert location.endswith("/bundle.with_~allowed-chars")
    assert client.get(location).status_code == 200


def test_protobuf_ingest_is_idempotent_with_prior_json_ingest(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    first = client.post(
        "/v1/incidents",
        content=encode_incident_json(valid_bundle),
        headers={"Content-Type": "application/json"},
    )
    second = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["digest"] == first.json()["digest"]


def test_validate_endpoint_has_no_persistence_side_effect(tmp_path, valid_bundle) -> None:
    store, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents/validate",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert store.list_incidents().items == ()


def test_missing_wire_otlp_digest_is_rejected_to_match_public_schema(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path)
    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0].pop("sha256")
    response = client.post(
        "/v1/incidents/validate",
        json=value,
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_INCIDENT"


def test_non_unicode_extension_key_is_rejected_without_server_error(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    value = json.loads(encode_incident_json(valid_bundle))
    value["profile"]["future_extension"] = {"\ud800": 1}
    response = client.post(
        "/v1/incidents/validate",
        content=json.dumps(value, ensure_ascii=True).encode(),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_INCIDENT"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("lk.response.ttft", 10**400),
        ("earshot.time.monotonic_nano", "18446744073709551616"),
    ],
)
def test_out_of_range_metadata_is_rejected_without_server_error(
    tmp_path,
    valid_bundle,
    key: str,
    value: object,
) -> None:
    _, client = app_client(tmp_path)
    document = json.loads(encode_incident_json(valid_bundle))
    document["profile"]["operations"][0]["attributes"][key] = value
    response = client.post(
        "/v1/incidents/validate",
        content=json.dumps(document).encode(),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_INCIDENT"


@pytest.mark.parametrize(
    ("content", "content_type", "status", "expected_code"),
    [
        (b"", JSON_MEDIA_TYPE, 400, "EARSHOT_EMPTY_BODY"),
        (b"{", JSON_MEDIA_TYPE, 400, "EARSHOT_MALFORMED_JSON"),
        (b"not protobuf", PROTOBUF_MEDIA_TYPE, 422, "EARSHOT_INVALID_INCIDENT"),
        (b"{}", "text/plain", 415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE"),
    ],
)
def test_malformed_requests_have_stable_nonreflective_errors(
    tmp_path, content: bytes, content_type: str, status: int, expected_code: str
) -> None:
    _, client = app_client(tmp_path)
    response = client.post("/v1/incidents", content=content, headers={"Content-Type": content_type})
    assert response.status_code == status
    assert code(response) == expected_code


def test_duplicate_json_key_is_rejected_before_contract_decode(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=b'{"profile":{},"profile":{}}',
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 400
    assert code(response) == "EARSHOT_DUPLICATE_JSON_KEY"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_number_is_rejected(tmp_path, constant: str) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=f'{{"profile":{{"x":{constant}}}}}',
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 400
    assert code(response) == "EARSHOT_MALFORMED_JSON"


def test_excessive_json_nesting_is_rejected_before_pydantic(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(max_json_depth=8))
    value: object = "leaf"
    for _ in range(10):
        value = {"nested": value}
    response = client.post(
        "/v1/incidents",
        content=json.dumps(value),
        headers={"Content-Type": JSON_MEDIA_TYPE},
    )
    assert response.status_code == 400
    assert code(response) == "EARSHOT_JSON_TOO_DEEP"


def test_body_limit_is_enforced_before_decode(tmp_path, valid_bundle) -> None:
    body = encode_incident_json(valid_bundle)
    _, client = app_client(tmp_path, config=ApiConfig(max_body_bytes=len(body) - 1))
    response = client.post("/v1/incidents", content=body, headers={"Content-Type": JSON_MEDIA_TYPE})
    assert response.status_code == 413
    assert code(response) == "EARSHOT_BODY_TOO_LARGE"


def test_gzip_compressed_incident_is_bounded_and_accepted(tmp_path, valid_bundle) -> None:
    payload = encode_incident_protobuf(valid_bundle)
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=gzip.compress(payload, mtime=0),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE, "Content-Encoding": "gzip"},
    )
    assert response.status_code == 201


def test_gzip_decompressed_body_limit_is_enforced(tmp_path, valid_bundle) -> None:
    payload = encode_incident_json(valid_bundle)
    _, client = app_client(tmp_path, config=ApiConfig(max_body_bytes=len(payload) - 1))
    response = client.post(
        "/v1/incidents",
        content=gzip.compress(payload, mtime=0),
        headers={"Content-Type": JSON_MEDIA_TYPE, "Content-Encoding": "gzip"},
    )
    assert response.status_code == 413
    assert code(response) == "EARSHOT_BODY_TOO_LARGE"


def test_malformed_gzip_is_rejected_without_decode(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=b"not-gzip",
        headers={"Content-Type": JSON_MEDIA_TYPE, "Content-Encoding": "gzip"},
    )
    assert response.status_code == 400
    assert code(response) == "EARSHOT_MALFORMED_GZIP"


def test_unknown_content_encoding_is_explicitly_rejected(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    response = client.post(
        "/v1/incidents",
        content=encode_incident_json(valid_bundle),
        headers={"Content-Type": JSON_MEDIA_TYPE, "Content-Encoding": "br"},
    )
    assert response.status_code == 415
    assert code(response) == "EARSHOT_UNSUPPORTED_CONTENT_ENCODING"


def test_invalid_incident_response_never_echoes_secret_values(tmp_path, valid_bundle) -> None:
    store, client = app_client(tmp_path)
    value = json.loads(encode_incident_json(valid_bundle))
    value["profile"]["manifest"]["session_id"] = SECRET_SENTINEL
    response = client.post("/v1/incidents", json=value, headers={"Content-Type": JSON_MEDIA_TYPE})
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_INCIDENT"
    assert SECRET_SENTINEL not in response.text
    assert store.list_incidents().items == ()
    assert list(store.iter_referenced_digests()) == []


def test_conflicting_retry_returns_409_without_changing_original(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    original = encode_incident_protobuf(valid_bundle)
    conflicting = encode_incident_protobuf(changed_status(valid_bundle, "failed"))
    first = client.post(
        "/v1/incidents", content=original, headers={"Content-Type": PROTOBUF_MEDIA_TYPE}
    )
    second = client.post(
        "/v1/incidents", content=conflicting, headers={"Content-Type": PROTOBUF_MEDIA_TYPE}
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert code(second) == "EARSHOT_INCIDENT_CONFLICT"
    retrieved = client.get("/v1/incidents/bundle-1", headers={"Accept": PROTOBUF_MEDIA_TYPE})
    assert retrieved.content == original


def test_incident_listing_uses_catalog_metadata_without_reading_artifacts(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    def artifact_read_is_not_needed(*_args, **_kwargs):
        raise AssertionError("incident listing must not read artifact bytes")

    monkeypatch.setattr(store, "get_artifact", artifact_read_is_not_needed)

    response = client.get("/v1/incidents")

    assert response.status_code == 200
    assert [item["bundle_id"] for item in response.json()["items"]] == ["bundle-1"]


def test_expiry_reaper_runs_without_a_list_request(tmp_path, valid_bundle) -> None:
    expired = with_expired_metadata(valid_bundle)
    store = IncidentStore(tmp_path)
    record = store.ingest(expired, encode_incident_protobuf(expired)).record
    object_path = store.objects.path_for(record.digest)
    config = ApiConfig(
        retention_cleanup_interval_seconds=0.01,
        retention_cleanup_batch_size=1,
    )

    with TestClient(create_app(store=store, config=config)):
        deadline = time.monotonic() + 2
        while object_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    assert not object_path.exists()
    with sqlite3.connect(store.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE bundle_id = 'bundle-1'"
            ).fetchone()[0]
            == 0
        )


def test_expiry_backlog_releases_the_store_lock_between_batches(tmp_path, monkeypatch):
    expired_bundles = []
    for bundle_id in ("expiry-lock-a", "expiry-lock-b"):
        bundle = make_valid_bundle(bundle_id=bundle_id)
        policies = tuple(
            policy.model_copy(update={"retention": RetentionPolicy(expires_at_unix_nano="0")})
            if policy.capture_class == "metadata"
            else policy
            for policy in bundle.profile.privacy.capture_classes
        )
        privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
        expired_bundles.append(
            bundle.model_copy(
                update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
            )
        )

    store = IncidentStore(tmp_path)
    object_paths = tuple(
        store.objects.path_for(store.ingest(bundle, encode_incident_protobuf(bundle)).record.digest)
        for bundle in expired_bundles
    )
    original_purge_expired = store.purge_expired
    first_batch_finished = threading.Event()
    resume_reaper = threading.Event()
    second_batch_finished = threading.Event()
    calls = [0]

    def pause_between_batches(*args, **kwargs):
        result = original_purge_expired(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            first_batch_finished.set()
            assert resume_reaper.wait(2)
        elif calls[0] == 2:
            second_batch_finished.set()
        return result

    monkeypatch.setattr(store, "purge_expired", pause_between_batches)
    config = ApiConfig(
        retention_cleanup_interval_seconds=0.01,
        retention_cleanup_batch_size=1,
    )
    try:
        with TestClient(create_app(store=store, config=config)) as client:
            assert first_batch_finished.wait(2)
            response = client.get("/v1/incidents")
            assert response.status_code == 200
            assert response.json()["items"] == []
            with sqlite3.connect(store.database_path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1

            resume_reaper.set()
            assert second_batch_finished.wait(2)
            assert all(not path.exists() for path in object_paths)
    finally:
        resume_reaper.set()


def test_expiry_reaper_retries_a_failed_scrub_without_new_expired_rows(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    expired = with_expired_metadata(valid_bundle)
    result = store.ingest(expired, encode_incident_protobuf(expired))
    object_path = store.objects.path_for(result.record.digest)
    original_scrub = store._scrub_deleted_pages
    first_scrub_failed = threading.Event()
    retry_scrub_succeeded = threading.Event()
    scrub_calls = 0

    def fail_first_scrub(*, compact=True, skip_if_current=False):
        nonlocal scrub_calls
        scrub_calls += 1
        if scrub_calls == 1:
            first_scrub_failed.set()
            raise StorageCleanupPendingError("simulated busy checkpoint")
        result = original_scrub(compact=compact, skip_if_current=skip_if_current)
        retry_scrub_succeeded.set()
        return result

    monkeypatch.setattr(store, "_scrub_deleted_pages", fail_first_scrub)
    config = ApiConfig(
        retention_cleanup_interval_seconds=0.01,
        retention_cleanup_batch_size=1,
    )

    with TestClient(create_app(store=store, config=config)):
        assert first_scrub_failed.wait(2)
        assert retry_scrub_succeeded.wait(2)
        time.sleep(0.05)

    assert scrub_calls == 2
    assert not object_path.exists()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_final_expiry_compaction_does_not_hold_the_store_lock(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store = IncidentStore(tmp_path)
    expired = with_expired_metadata(valid_bundle)
    store.ingest(expired, encode_incident_protobuf(expired))
    original_scrub = store._scrub_deleted_pages
    compaction_started = threading.Event()
    release_compaction = threading.Event()

    def pause_compaction(*, compact=True, skip_if_current=False):
        if compact:
            compaction_started.set()
            assert release_compaction.wait(3)
        return original_scrub(compact=compact, skip_if_current=skip_if_current)

    monkeypatch.setattr(store, "_scrub_deleted_pages", pause_compaction)
    config = ApiConfig(
        retention_cleanup_interval_seconds=0.01,
        retention_cleanup_batch_size=1,
    )

    try:
        with TestClient(create_app(store=store, config=config)) as client:
            assert compaction_started.wait(2)
            with ThreadPoolExecutor(max_workers=1) as executor:
                listing = executor.submit(client.get, "/v1/incidents")
                try:
                    response = listing.result(timeout=0.5)
                finally:
                    release_compaction.set()
            assert response.status_code == 200
            assert response.json()["items"] == []
    finally:
        release_compaction.set()


def test_list_pagination_and_session_filter_are_stable(tmp_path) -> None:
    _, client = app_client(tmp_path)
    for index in range(5):
        bundle = make_valid_bundle(bundle_id=f"bundle-{index}")
        assert (
            client.post(
                "/v1/incidents",
                content=encode_incident_protobuf(bundle),
                headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
            ).status_code
            == 201
        )
    first = client.get("/v1/incidents", params={"limit": 2, "session_id": "session-1"})
    second = client.get(
        "/v1/incidents",
        params={"limit": 2, "session_id": "session-1", "cursor": first.json()["next_cursor"]},
    )
    first_ids = {item["bundle_id"] for item in first.json()["items"]}
    second_ids = {item["bundle_id"] for item in second.json()["items"]}
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)


def test_invalid_cursor_has_stable_400(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.get("/v1/incidents", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert code(response) == "EARSHOT_INVALID_CURSOR"


def test_query_validation_is_stable_and_never_reflects_input(tmp_path) -> None:
    _, client = app_client(tmp_path)
    secret = "not-a-number-SENSITIVE_SENTINEL"
    response = client.get("/v1/incidents", params={"limit": secret})
    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_REQUEST"
    assert secret not in response.text


def test_analysis_is_generated_once_and_cached_by_input_digest(tmp_path, valid_bundle) -> None:
    calls = 0

    def analyzer(*args, **kwargs):
        nonlocal calls
        calls += 1
        return analyze_incident(*args, **kwargs)

    _, client = app_client(tmp_path, analyzer=analyzer)
    client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    first = client.get("/v1/incidents/bundle-1/analysis")
    second = client.get("/v1/incidents/bundle-1/analysis")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls == 1
    assert first.json()["analyzer_version"] == ANALYZER_VERSION


def test_explanation_returns_backend_authored_exact_timeline_facts(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    ingested = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert ingested.status_code == 201

    response = client.get("/v1/incidents/bundle-1/explanation")

    assert response.status_code == 200
    [turn] = response.json()["turns"]
    llm = next(item for item in turn["operations"] if item["operation_id"] == "op-llm")
    assert llm["shape"] == "interval"
    assert llm["start_nano"] == "1050000000"
    assert llm["end_nano"] == "1300000000"
    assert llm["duration_nano"] == "250000000"
    assert response.headers["cache-control"] == "no-store"


def _ingest(client, bundle) -> None:
    response = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert response.status_code == 201


def _with_render_coverage_denied(bundle):
    """Claim render was never observed while the render evidence is still present.

    That is exactly the ``render_claim_conflict`` the contradiction detector exists
    to catch: one part of the artifact asserts a fact another part denies.
    """

    coverage = tuple(
        item.model_copy(update={"availability": "not_observed", "reason": "collector_absent"})
        if item.signal == "client.render"
        else item
        for item in bundle.profile.coverage
    )
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"coverage": coverage})}
    )


def _with_export_destinations(bundle, *destinations: str):
    policies = list(bundle.profile.privacy.capture_classes)
    policies[0] = policies[0].model_copy(
        update={"export": ExportPolicy(allowed=True, destinations=destinations)}
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": tuple(policies)})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def test_contradictions_are_evidence_linked_and_bound_to_the_analysis(tmp_path) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, _with_render_coverage_denied(make_valid_bundle()))

    response = client.get("/v1/incidents/bundle-1/contradictions")

    assert response.status_code == 200
    body = response.json()
    assert body["bundle_id"] == "bundle-1"
    assert body["analyzer_version"] == ANALYZER_VERSION
    assert len(body["input_digest"]) == 64
    conflict = next(
        item for item in body["contradictions"] if item["kind"] == "render_claim_conflict"
    )
    assert conflict["boundary"] == "render"
    assert conflict["turn_id"] == "turn-1"
    # Every asserted contradiction cites real evidence ids from this artifact.
    assert "op-render" in conflict["evidence_ids"]
    # Deterministic: the same incident yields byte-identical contradictions.
    assert client.get("/v1/incidents/bundle-1/contradictions").json() == body
    assert response.headers["cache-control"] == "no-store"


def test_contradiction_free_incident_reports_an_examined_empty_result(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    body = client.get("/v1/incidents/bundle-1/contradictions").json()

    # "None found" is only sayable because detection ran; the digest it ran against
    # is named so an empty list can never be mistaken for "no analysis".
    assert body["contradictions"] == []
    assert (
        body["input_digest"] == client.get("/v1/incidents/bundle-1/analysis").json()["input_digest"]
    )


def test_contradictions_absence_is_explicit_when_no_analyzer_configured(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path, analyzer=None)
    _ingest(client, valid_bundle)

    response = client.get("/v1/incidents/bundle-1/contradictions")

    # Never an empty list that would read as "no problems found".
    assert response.status_code == 404
    assert code(response) == "EARSHOT_ANALYSIS_NOT_AVAILABLE"


def test_contradictions_refuse_an_analysis_derived_from_other_evidence(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store, client = app_client(tmp_path)
    _ingest(client, valid_bundle)
    assert client.get("/v1/incidents/bundle-1/contradictions").status_code == 200
    stored = store.get_analysis("bundle-1", ANALYZER_VERSION)
    foreign = DerivedAnalysis.model_validate(stored.value).model_copy(
        update={"input_sha256": "0" * 64}
    )
    monkeypatch.setattr(
        store,
        "get_analysis",
        lambda *args, **kwargs: dataclasses.replace(
            stored, value=foreign.model_dump(mode="json", exclude_none=True)
        ),
    )

    response = client.get("/v1/incidents/bundle-1/contradictions")

    # A stale or foreign analysis is a state conflict, never a 500.
    assert response.status_code == 409
    assert code(response) == "EARSHOT_ANALYSIS_BINDING_MISMATCH"


def test_evidence_summary_digests_the_incident_bound_to_its_analysis(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    response = client.get("/v1/incidents/bundle-1/evidence/summary")

    assert response.status_code == 200
    body = response.json()
    # Mirrors SummaryDigest.as_dict() exactly: the session id, the examined counts,
    # the diagnoses, and the earliest boundary (or an honest 'unknown').
    assert set(body) == {"session_id", "counts", "diagnoses", "first_abnormal_boundary"}
    assert set(body["counts"]) == {
        "turn_count",
        "operation_count",
        "event_count",
        "quality_sample_count",
        "failed_operation_count",
        "diagnosis_count",
        "boundary_diagnosis_count",
        "coverage_gap_count",
        "contradiction_count",
    }
    # An examined 'unknown' or a real boundary — either way the flag is a real bool.
    assert isinstance(body["first_abnormal_boundary"]["found"], bool)
    # Deterministic, and never cached.
    assert client.get("/v1/incidents/bundle-1/evidence/summary").json() == body
    assert response.headers["cache-control"] == "no-store"


def test_not_observed_reports_coverage_gaps_as_explicit_unknowns(tmp_path) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, _with_render_coverage_denied(make_valid_bundle()))

    response = client.get("/v1/incidents/bundle-1/evidence/not_observed")

    assert response.status_code == 200
    body = response.json()
    # Mirrors NotObserved.as_dict() exactly: the three unified lists of what the
    # evidence does not tell us, each entry carrying its own reason.
    assert set(body) == {"coverage_gaps", "limitations", "omissions"}
    gap = next(item for item in body["coverage_gaps"] if item["signal"] == "client.render")
    assert gap["availability"] == "not_observed"
    assert gap["reason"] == "collector_absent"
    # Deterministic, and never cached.
    assert client.get("/v1/incidents/bundle-1/evidence/not_observed").json() == body
    assert response.headers["cache-control"] == "no-store"


def test_evidence_projections_are_absent_when_no_analyzer_configured(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path, analyzer=None)
    _ingest(client, valid_bundle)

    for path in ("summary", "not_observed"):
        response = client.get(f"/v1/incidents/bundle-1/evidence/{path}")
        # Never a fabricated empty digest or a blank "nothing is missing".
        assert response.status_code == 404
        assert code(response) == "EARSHOT_ANALYSIS_NOT_AVAILABLE"


def test_evidence_projections_refuse_an_analysis_derived_from_other_evidence(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store, client = app_client(tmp_path)
    _ingest(client, valid_bundle)
    assert client.get("/v1/incidents/bundle-1/evidence/summary").status_code == 200
    stored = store.get_analysis("bundle-1", ANALYZER_VERSION)
    foreign = DerivedAnalysis.model_validate(stored.value).model_copy(
        update={"input_sha256": "0" * 64}
    )
    monkeypatch.setattr(
        store,
        "get_analysis",
        lambda *args, **kwargs: dataclasses.replace(
            stored, value=foreign.model_dump(mode="json", exclude_none=True)
        ),
    )

    for path in ("summary", "not_observed"):
        response = client.get(f"/v1/incidents/bundle-1/evidence/{path}")
        # A stale or foreign analysis is a state conflict, never an unhandled 500.
        assert response.status_code == 409
        assert code(response) == "EARSHOT_ANALYSIS_BINDING_MISMATCH"


def test_comparison_reports_structured_change_against_a_known_good_incident(tmp_path) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, make_valid_bundle())
    _ingest(client, make_valid_bundle(bundle_id="bundle-known-good", include_render=False))

    response = client.get(
        "/v1/incidents/bundle-1/comparison",
        params={"known_good_bundle_id": "bundle-known-good"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bundle_id"] == "bundle-1"
    assert body["known_good_bundle_id"] == "bundle-known-good"
    assert body["analyzer_version"] == ANALYZER_VERSION
    assert body["input_digest"] != body["known_good_input_digest"]
    # The known-good session never observed render; the incident did. That is an
    # availability change and a removed coverage gap, not a fabricated delta.
    assert {gap["signal"] for gap in body["coverage_gaps_removed"]} == {"client.render"}
    assert body["coverage_gaps_new"] == []
    changed = {
        (item["metric"], item["known_good_availability"], item["incident_availability"])
        for item in body["turn_metric_availability_changes"]
    }
    assert ("render_start_response_latency", "not_observed", "available") in changed
    # Both sides report a response_latency in ms, but they are not the same
    # quantity: without render the known-good measured to the transport estimate,
    # while the incident measured all the way to audio render. Their difference is
    # the render leg the known-good never observed, not a regression, so the pair is
    # reported incomparable instead of subtracted.
    incomparable = {
        (item["metric"], item["comparable"]) for item in body["turn_metric_availability_changes"]
    }
    assert ("response_latency", False) in incomparable
    # A delta exists only where both sides are available on one unit *and* one
    # basis, and it is the arithmetic difference of the two reported values —
    # nothing is imputed.
    deltas = {item["metric"]: item for item in body["turn_metric_deltas"]}
    assert "response_latency" not in deltas
    assert "render_start_response_latency" not in deltas
    assert deltas["sent_response_latency"]["delta"] == 0.0
    assert all(
        item["delta"] == item["incident_value"] - item["known_good_value"]
        for item in deltas.values()
    )
    assert body["unmatched_turns"] == {"only_in_incident": [], "only_in_known_good": []}
    assert response.headers["cache-control"] == "no-store"


def test_comparison_reports_only_contradictions_the_known_good_does_not_share(
    tmp_path,
) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, _with_render_coverage_denied(make_valid_bundle()))
    _ingest(client, make_valid_bundle(bundle_id="bundle-known-good"))

    body = client.get(
        "/v1/incidents/bundle-1/comparison",
        params={"known_good_bundle_id": "bundle-known-good"},
    ).json()

    assert [item["kind"] for item in body["contradictions_new"]] == ["render_claim_conflict"]


def test_comparison_names_the_unavailable_known_good_side(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    missing = client.get(
        "/v1/incidents/bundle-1/comparison",
        params={"known_good_bundle_id": "bundle-absent"},
    )
    reversed_sides = client.get(
        "/v1/incidents/bundle-absent/comparison",
        params={"known_good_bundle_id": "bundle-1"},
    )

    # Which of the two incidents is missing is stated, never left ambiguous.
    assert missing.status_code == 404
    assert code(missing) == "EARSHOT_KNOWN_GOOD_NOT_FOUND"
    assert reversed_sides.status_code == 404
    assert code(reversed_sides) == "EARSHOT_INCIDENT_NOT_FOUND"


def test_comparison_names_a_purged_known_good_incident(tmp_path) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, make_valid_bundle())
    _ingest(client, make_valid_bundle(bundle_id="bundle-known-good"))
    assert client.delete("/v1/incidents/bundle-known-good").status_code == 204

    response = client.get(
        "/v1/incidents/bundle-1/comparison",
        params={"known_good_bundle_id": "bundle-known-good"},
    )

    assert response.status_code == 410
    assert code(response) == "EARSHOT_KNOWN_GOOD_PURGED"


def test_comparison_states_when_the_known_good_has_no_analysis(tmp_path) -> None:
    _, client = app_client(tmp_path, analyzer=None)
    _ingest(client, make_valid_bundle())
    _ingest(client, make_valid_bundle(bundle_id="bundle-known-good"))

    response = client.get(
        "/v1/incidents/bundle-1/comparison",
        params={"known_good_bundle_id": "bundle-known-good"},
    )

    # Without an analyzer neither side is analysable; the incident is reported first
    # and by its own code, so no empty diff is ever manufactured.
    assert response.status_code == 404
    assert code(response) == "EARSHOT_ANALYSIS_NOT_AVAILABLE"


def test_comparison_requires_an_explicit_known_good_bundle_id(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    response = client.get("/v1/incidents/bundle-1/comparison")

    assert response.status_code == 422
    assert code(response) == "EARSHOT_INVALID_REQUEST"


def test_export_projects_an_incident_through_the_registry(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    response = client.get("/v1/incidents/bundle-1/export", params={"format": "otlp"})

    assert response.status_code == 200
    body = response.json()
    assert body["bundle_id"] == "bundle-1"
    assert body["format"] == "otlp"
    # The destination the capture policy had to permit travels with the document.
    assert body["destination"] == "otlp"
    assert body["document"]["resourceSpans"]
    # Deterministic projection: the same incident exports byte-identically.
    assert client.get("/v1/incidents/bundle-1/export", params={"format": "otlp"}).json() == body
    assert response.headers["cache-control"] == "no-store"


def test_export_defaults_to_otlp_and_offers_every_registered_format(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    default = client.get("/v1/incidents/bundle-1/export")
    openinference = client.get("/v1/incidents/bundle-1/export", params={"format": "openinference"})

    assert default.json()["format"] == "otlp"
    assert openinference.status_code == 200
    assert openinference.json()["format"] == "openinference"
    # Both OTLP-shaped projections declare the same governed destination.
    assert openinference.json()["destination"] == "otlp"
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/v1/incidents/{bundle_id}/export"]["get"]["parameters"]
    formats = next(item for item in parameters if item["name"] == "format")
    assert formats["schema"]["enum"] == ["openinference", "otlp"]


def test_export_refuses_a_format_no_exporter_is_registered_under(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, valid_bundle)

    response = client.get("/v1/incidents/bundle-1/export", params={"format": "acme"})

    assert response.status_code == 400
    assert code(response) == "EARSHOT_UNKNOWN_EXPORT_FORMAT"


def test_export_is_refused_when_the_capture_policy_denies_that_destination(tmp_path) -> None:
    _, client = app_client(tmp_path)
    _ingest(client, _with_export_destinations(make_valid_bundle(), "local_api"))

    export = client.get("/v1/incidents/bundle-1/export", params={"format": "otlp"})

    # A clean refusal from the registry's policy gate, not a crash — and reading the
    # incident through the API still works, so it is the destination being refused.
    assert export.status_code == 403
    assert code(export) == "EARSHOT_EXPORT_DENIED"
    assert client.get("/v1/incidents/bundle-1").status_code == 200


def test_analysis_accepts_event_turn_inherited_through_trace_span(tmp_path, valid_bundle) -> None:
    source_event = valid_bundle.profile.events[0]
    root_operation = valid_bundle.profile.operations[0]
    event = source_event.model_copy(
        update={
            "turn_id": None,
            "operation_id": None,
            "trace_id": root_operation.trace_id,
            "span_id": root_operation.span_id,
        }
    )
    bundle = valid_bundle.model_copy(
        update={
            "profile": valid_bundle.profile.model_copy(
                update={"events": (event, *valid_bundle.profile.events[1:])}
            )
        }
    )
    _, client = app_client(tmp_path)
    ingest = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert ingest.status_code == 201

    response = client.get("/v1/incidents/bundle-1/analysis")
    assert response.status_code == 200
    turn = next(
        item
        for item in response.json()["analysis"]["projections"]["turns"]
        if item["turn_id"] == "turn-1"
    )
    assert event.event_id in turn["event_ids"]


def test_analyzer_cannot_return_a_mismatched_inner_binding(tmp_path, valid_bundle) -> None:
    def analyzer(bundle, *, input_sha256, generated_at_unix_nano):
        analysis = analyze_incident(
            bundle,
            input_sha256=input_sha256,
            generated_at_unix_nano=generated_at_unix_nano,
        )
        return analysis.model_copy(update={"input_sha256": "0" * 64})

    _, client = app_client(tmp_path, analyzer=analyzer)
    client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    response = client.get("/v1/incidents/bundle-1/analysis")
    assert response.status_code == 500
    assert code(response) == "EARSHOT_ANALYZER_BINDING_MISMATCH"


def test_analyzer_cannot_cache_a_diagnosis_with_dangling_evidence(tmp_path, valid_bundle) -> None:
    def analyzer(bundle, *, input_sha256, generated_at_unix_nano):
        analysis = analyze_incident(
            bundle,
            input_sha256=input_sha256,
            generated_at_unix_nano=generated_at_unix_nano,
        )
        return analysis.model_copy(
            update={
                "diagnoses": (
                    Diagnosis(
                        diagnosis_id="bad-diagnosis",
                        code="bad",
                        summary="invalid_evidence_reference",
                        confidence="measured",
                        evidence_refs=("missing",),
                    ),
                )
            }
        )

    _, client = app_client(tmp_path, analyzer=analyzer)
    client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    response = client.get("/v1/incidents/bundle-1/analysis")
    assert response.status_code == 500
    assert code(response) == "EARSHOT_ANALYZER_CONTRACT"


def test_openapi_exposes_both_wire_formats_models_and_optional_loopback_auth(tmp_path) -> None:
    _, client = app_client(tmp_path)
    schema = client.get("/openapi.json").json()
    content = schema["paths"]["/v1/incidents"]["post"]["requestBody"]["content"]
    assert {
        JSON_MEDIA_TYPE,
        "application/json",
        PROTOBUF_MEDIA_TYPE,
        "application/x-protobuf",
    } <= set(content)
    assert "IncidentBundleJson" in schema["components"]["schemas"]
    assert "StoredAnalysisResponse" in schema["components"]["schemas"]
    incident_response_content = schema["paths"]["/v1/incidents/{bundle_id}"]["get"]["responses"][
        "200"
    ]["content"]
    incident_schema = {"$ref": "#/components/schemas/IncidentBundleJson"}
    assert incident_response_content["application/json"]["schema"] == incident_schema
    assert incident_response_content[JSON_MEDIA_TYPE]["schema"] == incident_schema
    content_encoding = next(
        parameter
        for parameter in schema["paths"]["/v1/incidents"]["post"]["parameters"]
        if parameter["name"] == "Content-Encoding"
    )
    assert content_encoding["schema"]["enum"] == ["identity", "gzip"]
    project_assertion = next(
        parameter
        for parameter in schema["paths"]["/v1/incidents"]["post"]["parameters"]
        if parameter["name"] == "X-Earshot-Project-Id"
    )
    assert project_assertion["required"] is False
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert schema["components"]["securitySchemes"]["BrowserSession"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "earshot_session",
    }
    assert "/v1/platform/projects/{project_id}/observe/summary" not in schema["paths"]
    generic_summary = schema["paths"]["/v1/projects/{project_id}/summary"]["get"]
    assert generic_summary["security"] == [
        {"BearerAuth": []},
        {"BrowserSession": []},
        {},
    ]
    summary_fields = schema["components"]["schemas"]["ProjectSummaryItemResponse"]["properties"]
    assert set(summary_fields) == {
        "session_id",
        "status",
        "framework",
        "framework_truncated",
        "created_at_unix_nano",
    }
    assert schema["paths"]["/v1/incidents"]["post"]["security"] == [
        {"BearerAuth": []},
        {"BrowserSession": []},
        {},
    ]
    assert schema["paths"]["/v1/auth/session"]["post"]["security"] == [{"BearerAuth": []}]
    assert schema["paths"]["/v1/auth/session"]["get"]["security"] == [
        {"BrowserSession": []},
        {},
    ]
    assert schema["paths"]["/v1/auth/logout"]["post"]["security"] == [{"BrowserSession": []}]


def test_openapi_marks_viewer_or_bearer_auth_mandatory_when_server_has_a_token(tmp_path) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="test-token"))
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/v1/incidents"]["post"]["security"] == [
        {"BearerAuth": []},
        {"BrowserSession": []},
    ]
    assert schema["paths"]["/v1/auth/session"]["get"]["security"] == [{"BrowserSession": []}]


def test_hosted_auth_configuration_requires_all_verifier_settings() -> None:
    with pytest.raises(ValueError, match="issuer, audience, and JWKS URL"):
        ApiConfig(auth_mode="hosted_jwt")


def test_openapi_keeps_connector_trust_separate_and_documents_retryable_errors(
    tmp_path,
) -> None:
    client = TestClient(create_app(store=IncidentStore(tmp_path)))

    operation = client.get("/openapi.json").json()["paths"]["/hooks/v1/connectors/{endpoint_id}"][
        "post"
    ]

    assert "security" not in operation
    expected = {
        "#/components/schemas/ProblemResponse",
        "#/components/schemas/ConnectorProblemResponse",
    }
    for status in ("429", "503"):
        response = operation["responses"][status]
        response_schema = response["content"]["application/json"]["schema"]
        assert {item["$ref"] for item in response_schema["anyOf"]} == expected
        assert response["headers"]["Retry-After"]["schema"] == {
            "type": "integer",
            "minimum": 1,
        }


def test_analysis_absence_is_explicit_when_no_analyzer_configured(tmp_path, valid_bundle) -> None:
    _, client = app_client(tmp_path, analyzer=None)
    client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    response = client.get("/v1/incidents/bundle-1/analysis")
    assert response.status_code == 404
    assert code(response) == "EARSHOT_ANALYSIS_NOT_AVAILABLE"


def test_privacy_purge_removes_artifact_and_returns_tombstone_semantics(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path)
    client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert client.delete("/v1/incidents/bundle-1").status_code == 204
    gone = client.get("/v1/incidents/bundle-1")
    assert gone.status_code == 410
    assert code(gone) == "EARSHOT_INCIDENT_PURGED"
    retry = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert retry.status_code == 410


def test_missing_incident_is_404(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.get("/v1/incidents/missing")
    assert response.status_code == 404
    assert code(response) == "EARSHOT_INCIDENT_NOT_FOUND"


def test_corrupt_stored_artifact_is_nonreflective_500(tmp_path, valid_bundle) -> None:
    store, client = app_client(tmp_path)
    ingest = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    store.objects.path_for(ingest.json()["digest"]).write_bytes(SECRET_SENTINEL.encode())
    response = client.get("/v1/incidents/bundle-1")
    assert response.status_code == 500
    assert code(response) == "EARSHOT_ARTIFACT_CORRUPT"
    assert SECRET_SENTINEL not in response.text


def test_storage_system_failure_is_503_and_does_not_expose_exception(
    tmp_path, valid_bundle, monkeypatch
) -> None:
    store, client = app_client(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError(SECRET_SENTINEL)

    monkeypatch.setattr(store, "ingest", fail)
    response = client.post(
        "/v1/incidents",
        content=encode_incident_protobuf(valid_bundle),
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
    )
    assert response.status_code == 503
    assert code(response) == "EARSHOT_STORAGE_UNAVAILABLE"
    assert SECRET_SENTINEL not in response.text
    assert store.list_incidents().items == ()


def test_v1_routes_require_constant_time_bearer_auth_when_configured(
    tmp_path, valid_bundle
) -> None:
    _, client = app_client(tmp_path, config=ApiConfig(token="correct-token"))
    body = encode_incident_protobuf(valid_bundle)
    missing = client.post(
        "/v1/incidents", content=body, headers={"Content-Type": PROTOBUF_MEDIA_TYPE}
    )
    wrong = client.post(
        "/v1/incidents",
        content=body,
        headers={"Content-Type": PROTOBUF_MEDIA_TYPE, "Authorization": "Bearer wrong"},
    )
    correct = client.post(
        "/v1/incidents",
        content=body,
        headers={
            "Content-Type": PROTOBUF_MEDIA_TYPE,
            "Authorization": "bearer correct-token",
        },
    )
    assert missing.status_code == wrong.status_code == 401
    assert correct.status_code == 201


def test_concurrent_http_retries_create_exactly_one_incident(tmp_path, valid_bundle) -> None:
    store, client = app_client(tmp_path)
    body = encode_incident_protobuf(valid_bundle)

    def post(_: int) -> int:
        return client.post(
            "/v1/incidents", content=body, headers={"Content-Type": PROTOBUF_MEDIA_TYPE}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, range(20)))
    assert statuses.count(201) == 1
    assert set(statuses) <= {200, 201}
    assert len(store.list_incidents().items) == 1
