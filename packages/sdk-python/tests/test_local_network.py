from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from earshot.api import ApiConfig, create_app

pytestmark = pytest.mark.integration


def test_non_loopback_bind_is_rejected_by_default() -> None:
    # Fail closed: a 0.0.0.0 listener needs an explicit trust decision.
    with pytest.raises(ValueError):
        ApiConfig(host="0.0.0.0")


def test_trust_local_network_permits_the_bind() -> None:
    config = ApiConfig(host="0.0.0.0", trust_local_network=True)
    assert config.trust_local_network is True


def test_trusted_local_network_serves_v1_without_auth(tmp_path) -> None:
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True),
    )
    response = TestClient(app).get("/v1/incidents")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_trusted_local_network_rejects_dns_rebinding_host(tmp_path) -> None:
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True),
    )

    response = TestClient(app).get(
        "/v1/incidents",
        headers={"Host": "attacker.example:4319"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EARSHOT_UNTRUSTED_HOST"


def test_trusted_local_network_needs_no_viewer_login(tmp_path) -> None:
    # The viewer must load without a project-key login the fresh install lacks;
    # the session endpoint has to honor trust_local_network like the middleware.
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True),
    )
    response = TestClient(app).get("/v1/auth/session")
    assert response.status_code == 200
    body = response.json()
    assert body["authentication_required"] is False
    assert body["authenticated"] is False


def test_trusted_local_network_with_token_still_requires_auth(tmp_path) -> None:
    # Opting into a trusted network does not waive an explicitly configured token.
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True, token="s3cret"),
    )
    client = TestClient(app)
    assert client.get("/v1/incidents").status_code == 401
    ok = client.get("/v1/incidents", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def _v1_security(app) -> list[dict]:
    return app.openapi()["paths"]["/v1/incidents"]["get"]["security"]


def test_openapi_security_matches_runtime_trusted_local(tmp_path) -> None:
    # Runtime permits anonymous access in trusted-local mode; the machine
    # contract must advertise the same, i.e. include the empty-security option.
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True),
    )
    security = _v1_security(app)
    assert {} in security
    session_security = app.openapi()["paths"]["/v1/auth/session"]["get"]["security"]
    assert {} in session_security


def test_openapi_security_requires_auth_when_token_set(tmp_path) -> None:
    app = create_app(
        data_dir=tmp_path / "data",
        config=ApiConfig(host="0.0.0.0", trust_local_network=True, token="s3cret"),
    )
    assert {} not in _v1_security(app)


def test_non_loopback_without_optin_or_token_fails_to_build(tmp_path) -> None:
    # Even constructed directly, create_app refuses an unauthenticated remote bind.
    with pytest.raises(ValueError):
        create_app(
            data_dir=tmp_path / "data",
            config=ApiConfig(host="127.0.0.1", behind_tls_proxy=True),
        )
