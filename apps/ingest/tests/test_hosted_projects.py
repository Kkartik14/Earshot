"""Hosted project provisioning: ``PUT /v1/projects/{project_id}``.

A host creates the Earshot project of a signed project ID itself, under its own scope,
with the capture preset stated explicitly. A repeat with the same terms confirms it; other
terms are a conflict, never a silent change; a deleted project is never brought back. Only
this one operation, on the signed project, reaches Earshot before the project exists.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from earshot.api import ApiConfig, create_app
from otlp_support import request, serve
from store_support import make_store
from test_hosted_jwt import _hosted_config, _token, issuer  # noqa: F401  (issuer is a fixture)

PROVISION = "earshot:project:provision"
PROJECT = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
TERMS = {"display_name": "Platform project", "capture_preset": "full"}


def _headers(issuer, scope: str, project_id: str = PROJECT) -> dict[str, str]:  # noqa: F811
    token = _token(issuer, project_id=project_id, scope=scope)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _put(port: int, headers: dict[str, str], body: object, project_id: str = PROJECT):
    return request(
        port, "PUT", f"/v1/projects/{project_id}", json.dumps(body).encode(), headers=headers
    )


def _code(reply) -> str | None:
    try:
        return json.loads(reply.body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def test_a_host_creates_its_project_and_a_repeat_confirms_it(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        before = request(port, "GET", "/v1/incidents", headers=_headers(issuer, "earshot:read"))
        created = _put(port, _headers(issuer, PROVISION), TERMS)
        repeated = _put(port, _headers(issuer, PROVISION), TERMS)
        after = request(port, "GET", "/v1/incidents", headers=_headers(issuer, "earshot:read"))

    assert before.status == 403 and _code(before) == "EARSHOT_PROJECT_NOT_PROVISIONED"
    assert created.status == 201, created.body
    assert json.loads(created.body) == {"project_id": PROJECT, **TERMS}
    assert repeated.status == 200
    assert json.loads(repeated.body) == json.loads(created.body)
    assert after.status == 200
    assert store.get_project(PROJECT).capture_preset == "full"


@pytest.mark.parametrize(
    "changed",
    [{"capture_preset": "metadata_only"}, {"display_name": "Another name"}],
    ids=["preset", "display_name"],
)
def test_other_terms_for_an_existing_project_are_a_conflict_not_a_change(
    tmp_path,
    issuer,  # noqa: F811
    changed,
) -> None:
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        assert _put(port, _headers(issuer, PROVISION), TERMS).status == 201
        conflict = _put(port, _headers(issuer, PROVISION), {**TERMS, **changed})

    assert conflict.status == 409
    assert _code(conflict) == "EARSHOT_PROJECT_CONFLICT"
    project = store.get_project(PROJECT)
    assert (project.display_name, project.capture_preset) == ("Platform project", "full")


def test_only_provisioning_the_signed_project_reaches_earshot_before_it_exists(
    tmp_path,
    issuer,  # noqa: F811
) -> None:
    store = make_store(tmp_path)
    other = "9b2f3c1e-1111-4222-8333-444455556666"
    every_scope = " ".join(
        (
            "earshot:read earshot:write earshot:delete earshot:artifact:delete",
            "earshot:summary:read earshot:project:delete",
            "earshot:calls:read earshot:calls:write earshot:calls:delete",
            PROVISION,
        )
    )
    headers = _headers(issuer, every_scope)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        refused = {
            ("GET", "/v1/incidents"): request(port, "GET", "/v1/incidents", headers=headers),
            ("POST", "/v1/traces"): request(port, "POST", "/v1/traces", b"{}", headers=headers),
            ("GET", "/v1/calls"): request(port, "GET", "/v1/calls", headers=headers),
            ("DELETE", f"/v1/projects/{PROJECT}"): request(
                port, "DELETE", f"/v1/projects/{PROJECT}", headers=headers
            ),
            # Another project's ID is not the signed project's own provisioning.
            ("PUT", f"/v1/projects/{other}"): _put(port, headers, TERMS, project_id=other),
        }
        without_scope = _put(port, _headers(issuer, "earshot:write earshot:calls:write"), TERMS)

    for operation, reply in refused.items():
        assert reply.status == 403, operation
        assert _code(reply) == "EARSHOT_PROJECT_NOT_PROVISIONED", operation
    # Past the exemption, the operation still needs its own scope.
    assert without_scope.status == 403 and _code(without_scope) == "EARSHOT_SCOPE_REQUIRED"
    assert store.get_project(PROJECT) is None
    assert store.get_project(other) is None


def test_a_provisioned_token_cannot_create_another_project(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project(PROJECT, display_name="Platform project")
    other = "9b2f3c1e-1111-4222-8333-444455556666"
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        mismatch = _put(port, _headers(issuer, PROVISION), TERMS, project_id=other)

    assert mismatch.status == 403 and _code(mismatch) == "EARSHOT_PROJECT_MISMATCH"
    assert store.get_project(other) is None


def test_a_deleted_project_is_never_brought_back(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        assert _put(port, _headers(issuer, PROVISION), TERMS).status == 201
        deleter = _headers(issuer, "earshot:project:delete")
        for _ in range(20):
            deleted = request(port, "DELETE", f"/v1/projects/{PROJECT}", headers=deleter)
            if deleted.status == 200:
                break
        recreated = _put(port, _headers(issuer, PROVISION), TERMS)

    assert deleted.status == 200
    assert recreated.status == 410 and _code(recreated) == "EARSHOT_PROJECT_DELETING"


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "Platform project"},
        {"capture_preset": "full"},
        {**TERMS, "capture_preset": "redacted"},
        {**TERMS, "display_name": ""},
        {**TERMS, "display_name": "x" * 129},
        {**TERMS, "display_name": "Support\u0000line"},
        {**TERMS, "unexpected": True},
    ],
    ids=[
        "no-preset",
        "no-name",
        "unknown-preset",
        "empty-name",
        "long-name",
        "nul-in-name",
        "extra-field",
    ],
)
def test_the_terms_are_explicit_and_validated(tmp_path, issuer, body) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        reply = _put(port, _headers(issuer, PROVISION), body)

    assert reply.status == 422, reply.body
    assert store.get_project(PROJECT) is None


def test_concurrent_first_requests_create_the_project_once(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        headers = _headers(issuer, PROVISION)
        # Cache the signing keys first: a cold verifier answers concurrent requests 503
        # while one refresh runs, which is the verifier's behaviour, not provisioning's.
        warm = request(port, "GET", "/v1/incidents", headers=headers)
        assert _code(warm) == "EARSHOT_PROJECT_NOT_PROVISIONED"
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: _put(port, headers, TERMS), range(6)))

    statuses = sorted(reply.status for reply in replies)
    assert statuses == [200, 200, 200, 200, 200, 201], statuses
    assert {json.dumps(json.loads(reply.body), sort_keys=True) for reply in replies} == {
        json.dumps({"project_id": PROJECT, **TERMS}, sort_keys=True)
    }


def test_an_operator_credential_cannot_provision_through_the_api(tmp_path) -> None:
    store = make_store(tmp_path)
    app = create_app(store=store, config=ApiConfig(token="operator-token-0123456789abcdef"))
    with serve(app) as port:
        reply = request(
            port,
            "PUT",
            f"/v1/projects/{PROJECT}",
            json.dumps(TERMS).encode(),
            headers={
                "Authorization": "Bearer operator-token-0123456789abcdef",
                "Content-Type": "application/json",
            },
        )

    assert reply.status == 403 and _code(reply) == "EARSHOT_HOSTED_AUTH_REQUIRED"
    assert store.get_project(PROJECT) is None


def test_the_hosted_contract_lists_provisioning_with_its_scope(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        paths = request(port, "GET", "/openapi.json").json()["paths"]

    operation = paths["/v1/projects/{project_id}"]["put"]
    assert operation["x-required-scope"] == PROVISION
    assert {"200", "201", "409", "410"} <= set(operation["responses"])
