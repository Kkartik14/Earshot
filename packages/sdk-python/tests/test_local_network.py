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


def test_non_loopback_without_optin_or_token_fails_to_build(tmp_path) -> None:
    # Even constructed directly, create_app refuses an unauthenticated remote bind.
    with pytest.raises(ValueError):
        create_app(
            data_dir=tmp_path / "data",
            config=ApiConfig(host="127.0.0.1", behind_tls_proxy=True),
        )
