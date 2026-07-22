from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from earshot.api import ApiConfig, create_app
from earshot.codec import encode_incident_protobuf
from earshot.storage import (
    DEFAULT_PROJECT_ID,
    IncidentConflictError,
    IncidentNotFoundError,
    IncidentPurgedError,
    IncidentStore,
)
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.integration


def test_legacy_ingest_belongs_to_the_default_project(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="default-project-bundle")

    result = store.ingest(bundle, encode_incident_protobuf(bundle))

    assert result.record.project_id == DEFAULT_PROJECT_ID
    assert store.get_record(bundle.profile.manifest.bundle_id).project_id == DEFAULT_PROJECT_ID


def test_project_scope_hides_an_incident_owned_by_another_project(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    bundle = make_valid_bundle(bundle_id="scoped-bundle")
    store.ingest(bundle, encode_incident_protobuf(bundle), project_id="support")

    assert store.get_record(bundle.profile.manifest.bundle_id, project_id="support").project_id == (
        "support"
    )
    assert [item.bundle_id for item in store.list_incidents(project_id="support").items] == [
        bundle.profile.manifest.bundle_id
    ]
    assert store.list_incidents(project_id=DEFAULT_PROJECT_ID).items == ()
    with pytest.raises(IncidentNotFoundError):
        store.get_record(bundle.profile.manifest.bundle_id, project_id=DEFAULT_PROJECT_ID)


def test_api_key_authenticates_its_project_until_revoked(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("sales", display_name="Sales")

    issued = store.issue_api_key("sales", label="production ingest")

    principal = store.authenticate_api_key(issued.credential)
    assert principal is not None
    assert principal.project_id == "sales"
    assert principal.key_id == issued.key_id
    assert store.authenticate_api_key(f"{issued.credential}altered") is None

    store.revoke_api_key("sales", issued.key_id)
    assert store.authenticate_api_key(issued.credential) is None


def test_project_identifiers_are_rejected_before_storage(tmp_path) -> None:
    store = IncidentStore(tmp_path)

    for invalid in ("", "Default", "contains space", "../escape", "a" * 65):
        with pytest.raises(ValueError):
            store.create_project(invalid, display_name="Invalid")


def test_remote_project_key_only_sees_its_project(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("sales", display_name="Sales")
    default_bundle = make_valid_bundle(bundle_id="default-api-bundle")
    sales_bundle = make_valid_bundle(bundle_id="sales-api-bundle")
    store.ingest(default_bundle, encode_incident_protobuf(default_bundle))
    store.ingest(
        sales_bundle,
        encode_incident_protobuf(sales_bundle),
        project_id="sales",
    )
    issued = store.issue_api_key("sales", label="remote")
    client = TestClient(
        create_app(
            store=store,
            config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
        )
    )

    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/v1/incidents"]["get"]["security"] == [
        {"BearerAuth": []},
        {"BrowserSession": []},
    ]

    unauthorized = client.get("/v1/incidents")
    visible = client.get("/v1/incidents", headers={"Authorization": f"Bearer {issued.credential}"})
    hidden = client.get(
        f"/v1/incidents/{default_bundle.profile.manifest.bundle_id}",
        headers={"Authorization": f"Bearer {issued.credential}"},
    )

    assert unauthorized.status_code == 401
    assert visible.status_code == 200
    assert [item["bundle_id"] for item in visible.json()["items"]] == [
        sales_bundle.profile.manifest.bundle_id
    ]
    assert hidden.status_code == 404


def test_sdk_project_assertion_cannot_silently_route_to_the_token_project(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("sales", display_name="Sales")
    issued = store.issue_api_key("sales", label="sdk")
    client = TestClient(
        create_app(
            store=store,
            config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
        )
    )
    authorization = {"Authorization": f"Bearer {issued.credential}"}

    accepted = client.get(
        "/v1/incidents",
        headers={**authorization, "X-Earshot-Project-Id": "sales"},
    )
    mismatch = client.get(
        "/v1/incidents",
        headers={**authorization, "X-Earshot-Project-Id": "support"},
    )

    assert accepted.status_code == 200
    assert mismatch.status_code == 403
    assert mismatch.json() == {
        "error": {
            "code": "EARSHOT_PROJECT_MISMATCH",
            "message": "asserted SDK project does not match the authenticated project",
        }
    }


def test_remote_viewer_session_preserves_project_scope(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("sales", display_name="Sales")
    default_bundle = make_valid_bundle(bundle_id="default-viewer-bundle")
    sales_bundle = make_valid_bundle(bundle_id="sales-viewer-bundle")
    store.ingest(default_bundle, encode_incident_protobuf(default_bundle))
    store.ingest(
        sales_bundle,
        encode_incident_protobuf(sales_bundle),
        project_id="sales",
    )
    issued = store.issue_api_key("sales", label="viewer")
    app = create_app(
        store=store,
        config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
    )

    with TestClient(app, base_url="https://viewer.example") as client:
        exchange = client.post(
            "/v1/auth/session",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        assert exchange.status_code == 201
        page = client.get("/v1/incidents")
        assert page.status_code == 200
        assert [item["bundle_id"] for item in page.json()["items"]] == ["sales-viewer-bundle"]
        assert client.get("/v1/incidents/default-viewer-bundle").status_code == 404


def test_remote_listener_requires_tls_proxy_and_a_credential(tmp_path) -> None:
    with pytest.raises(ValueError):
        ApiConfig(host="0.0.0.0")

    store = IncidentStore(tmp_path)
    with pytest.raises(ValueError):
        create_app(store=store, config=ApiConfig(host="0.0.0.0", behind_tls_proxy=True))


def test_tombstones_do_not_leak_cross_project_purge_history(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    store.create_project("sales", display_name="Sales")
    bundle = make_valid_bundle(bundle_id="globally-scoped-purged-id")
    store.ingest(bundle, encode_incident_protobuf(bundle), project_id="support")
    support_key = store.issue_api_key("support", label="support")
    sales_key = store.issue_api_key("sales", label="sales")
    store.purge(bundle.profile.manifest.bundle_id, project_id="support")

    with pytest.raises(IncidentPurgedError):
        store.get_record(bundle.profile.manifest.bundle_id, project_id="support")
    with pytest.raises(IncidentNotFoundError):
        store.get_record(bundle.profile.manifest.bundle_id, project_id="sales")
    with pytest.raises(IncidentConflictError):
        store.ingest(
            bundle,
            encode_incident_protobuf(bundle),
            project_id="sales",
        )

    client = TestClient(create_app(store=store))
    path = f"/v1/incidents/{bundle.profile.manifest.bundle_id}"
    purged = client.get(
        path,
        headers={"Authorization": f"Bearer {support_key.credential}"},
    )
    hidden = client.get(
        path,
        headers={"Authorization": f"Bearer {sales_key.credential}"},
    )
    assert purged.status_code == 410
    assert hidden.status_code == 404
