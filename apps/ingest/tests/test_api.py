from __future__ import annotations

import asyncio
import gzip
import json
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
from earshot.contract import Diagnosis
from earshot.storage import IncidentStore
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

        session = client.get("/v1/auth/session")
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["csrf_token"] == exchange.json()["csrf_token"]

        authenticated = client.get("/v1/incidents")
        assert authenticated.status_code == 200
        invalid_bearer = client.get(
            "/v1/incidents",
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid_bearer.status_code == 401


def test_tokenless_loopback_viewer_can_discover_that_login_is_not_required(tmp_path) -> None:
    _, client = app_client(tmp_path)
    response = client.get("/v1/auth/session")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "authentication_required": False,
        "project_id": "default",
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

    generic_json = client.get(
        "/v1/incidents/bundle-1", headers={"Accept": "application/json"}
    )
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
    incident_response_content = schema["paths"]["/v1/incidents/{bundle_id}"]["get"][
        "responses"
    ]["200"]["content"]
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
