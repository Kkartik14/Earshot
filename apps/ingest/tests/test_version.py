"""``GET /version``: what this server speaks, for a host's compatibility check.

A host pins the versions it was built against and checks them before it sends anything.
The route is a public process check like ``/healthz``: no credential, no project, no
project data, in every authorization mode.
"""

from __future__ import annotations

from earshot import signals, versions
from earshot.api import ApiConfig, create_app
from earshot.call_projection import CALL_PROJECTION_VERSION
from otlp_support import request, serve
from store_support import make_store
from test_hosted_jwt import _hosted_config, issuer  # noqa: F401  (issuer is a fixture)


def _expected(*, hosted_jwt: bool) -> dict[str, object]:
    return {
        "package_version": versions.PACKAGE_VERSION,
        "api_version": versions.API_VERSION,
        "contract_version": versions.CONTRACT_VERSION,
        "semantic_profile_version": versions.SEMANTIC_PROFILE_VERSION,
        "call_projection_version": CALL_PROJECTION_VERSION,
        "host_profile_version": versions.HOST_PROFILE_VERSION,
        "capabilities": {"hosted_jwt": hosted_jwt, "signals": signals.extra_available()},
    }


def test_an_operator_server_answers_without_a_credential(tmp_path) -> None:
    store = make_store(tmp_path)
    app = create_app(store=store, config=ApiConfig(token="operator-token-0123456789abcdef"))
    with serve(app) as port:
        reply = request(port, "GET", "/version")

    assert reply.status == 200, reply.body
    assert reply.json() == _expected(hosted_jwt=False)


def test_a_hosted_server_answers_without_a_token_or_a_project(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        reply = request(port, "GET", "/version")
        contract = request(port, "GET", "/openapi.json").json()["paths"]

    assert reply.status == 200, reply.body
    assert reply.json() == _expected(hosted_jwt=True)
    # The hosted contract keeps the public route; it needs no scope.
    assert "x-required-scope" not in contract["/version"]["get"]


def test_the_signals_capability_follows_the_installed_extra(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(signals, "extra_available", lambda: False)
    store = make_store(tmp_path)
    app = create_app(store=store, config=ApiConfig(token="operator-token-0123456789abcdef"))
    with serve(app) as port:
        capabilities = request(port, "GET", "/version").json()["capabilities"]

    assert capabilities == {"hosted_jwt": False, "signals": False}
