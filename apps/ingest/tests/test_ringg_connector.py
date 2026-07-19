from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from earshot.api import create_app
from earshot.codec import decode_incident_protobuf, encode_incident_json
from earshot.connectors import (
    DeliveryConflictError,
    DeliveryPayloadError,
    DeliveryTrustError,
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

SECRET = "ringg-test-webhook-secret"
PRIVATE_TEXT = "private Ringg transcript and analysis must never be canonical"
PRIVATE_PHONE = "+919999999999"
PRIVATE_URL = "https://recordings.example/private-call.mp3"
CALL_ID = "4b30f5dc-7f86-4e4c-a3aa-15c01138559b"
CALL_SID = "provider-call-sensitive"


def _payload(*, event_type: str = "all_processing_completed", latency: float = 0.892345) -> bytes:
    return json.dumps(
        {
            "event_type": event_type,
            "call_id": CALL_ID,
            "call_sid": CALL_SID,
            "agent_id": "agent-sensitive",
            "workspace_id": "workspace-sensitive",
            "status": "completed",
            "sub_status": "ACCEPTED",
            "call_type": "outbound",
            "call_duration": 15.73,
            "called_on": "2025-12-14T19:37:13.310707Z",
            "to_number": PRIVATE_PHONE,
            "from_number": PRIVATE_PHONE,
            "custom_args_values": {"customer_name": PRIVATE_TEXT},
            "agent_name": PRIVATE_TEXT,
            "overall_latency_seconds": latency,
            "first_utterance_seconds": 0.456789,
            "transcript": [{"bot": PRIVATE_TEXT}, {"user": PRIVATE_TEXT}],
            "recording_url": PRIVATE_URL,
            "platform_analysis": {"summary": PRIVATE_TEXT},
            "client_analysis": {"next_action": PRIVATE_TEXT},
            "tool_call_logs": [{"result": PRIVATE_TEXT}],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _delivery(
    endpoint_id: str,
    body: bytes,
    *,
    header: str = "authorization",
    secret: str = SECRET,
) -> RawProviderDelivery:
    headers = (
        ((b"authorization", f"Bearer {secret}".encode()),)
        if header == "authorization"
        else ((b"x-webhook-secret", secret.encode()),)
    )
    return RawProviderDelivery(endpoint_id=endpoint_id, headers=headers, body=body)


@pytest.fixture
def connector(tmp_path):
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    endpoint = store.create_connector(
        "support",
        provider="ringg",
        secret_ref="env:RINGG_WEBHOOK_SECRET",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:RINGG_WEBHOOK_SECRET": SECRET}),
    )
    return store, endpoint, ingestion


def test_final_event_keeps_only_session_metadata_and_pseudonymous_identity(connector) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    incident_json = encode_incident_json(bundle)
    measurements = {
        measurement.name: measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements["ringg.overall_latency"].value == 0.892345
    assert measurements["ringg.overall_latency"].unit == "s"
    assert measurements["ringg.first_utterance"].value == 0.456789
    assert bundle.profile.operations == ()
    assert store.list_turn_facts(project_id="support") == ()
    assert bundle.profile.session.status == "completed"
    assert all(
        sample.attributes["earshot.correlation"] == "session_only"
        for sample in bundle.profile.quality_samples
    )
    for private_value in (
        PRIVATE_TEXT,
        PRIVATE_PHONE,
        PRIVATE_URL,
        CALL_ID,
        CALL_SID,
        "agent-sensitive",
        "workspace-sensitive",
    ):
        assert private_value.encode() not in canonical
        assert private_value.encode() not in incident_json

    with sqlite3.connect(store.database_path) as connection:
        identities = connection.execute(
            """
            SELECT key_kind, value_hmac
            FROM external_identities
            WHERE bundle_id = ?
            ORDER BY key_kind
            """,
            (outcome.bundle_id,),
        ).fetchall()
    assert identities == [
        (
            "call_id",
            store.fingerprint(
                f"identity:{endpoint.endpoint_id}:call_id",
                CALL_ID,
            ),
        ),
        (
            "call_sid",
            store.fingerprint(
                f"identity:{endpoint.endpoint_id}:call_sid",
                CALL_SID,
            ),
        ),
    ]
    assert all(len(value_hmac) == 64 for _, value_hmac in identities)


@pytest.mark.parametrize("header", ("authorization", "x-webhook-secret"))
def test_static_subscription_headers_authenticate_at_public_http_seam(
    connector, header
) -> None:
    store, endpoint, ingestion = connector
    client = TestClient(create_app(store=store, connector_ingestion=ingestion))
    delivery = _delivery(endpoint.endpoint_id, _payload(), header=header)

    response = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=delivery.body,
        headers={
            "content-type": "application/json",
            header: delivery.headers[0][1].decode(),
        },
    )

    assert response.status_code == 200
    assert response.json()["disposition"] == "applied"
    assert len(store.list_incidents(project_id="support").items) == 1


def test_authentication_precedes_json_parsing_and_rejects_ambiguous_headers(
    connector,
) -> None:
    store, endpoint, ingestion = connector
    malformed = b'{"event_type":"unterminated'

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed))
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed, secret="wrong"))
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(
            RawProviderDelivery(
                endpoint_id=endpoint.endpoint_id,
                headers=(
                    (b"authorization", f"Bearer {SECRET}".encode()),
                    (b"x-webhook-secret", SECRET.encode()),
                ),
                body=malformed,
            )
        )
    assert store.list_incidents(project_id="support").items == ()


def test_progress_events_are_authenticated_then_ignored(connector) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(
        _delivery(endpoint.endpoint_id, _payload(event_type="call_completed"))
    )

    assert outcome.disposition == "ignored"
    assert outcome.bundle_id is None
    assert store.list_incidents(project_id="support").items == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", {"private": PRIVATE_TEXT}),
        ("call_duration", -1),
        ("overall_latency_seconds", "0.9"),
        ("first_utterance_seconds", None),
        ("called_on", "not-a-timestamp"),
        ("call_sid", None),
        ("call_sid", ""),
        ("call_sid", "x" * 513),
    ),
)
def test_required_session_metadata_is_strictly_validated(
    connector, field, value
) -> None:
    store, endpoint, ingestion = connector
    payload = json.loads(_payload())
    payload[field] = value

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(
            _delivery(
                endpoint.endpoint_id,
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            )
        )

    assert store.list_incidents(project_id="support").items == ()


def test_exact_replay_is_idempotent_and_changed_final_event_conflicts(connector) -> None:
    store, endpoint, ingestion = connector
    first = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))
    replay = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert replay.disposition == "replayed"
    assert replay.bundle_id == first.bundle_id
    with pytest.raises(DeliveryConflictError):
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload(latency=1.0)))
    assert len(store.list_incidents(project_id="support").items) == 1


def test_rotated_previous_subscription_secret_is_accepted(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver(
            {"env:RINGG_WEBHOOK_SECRET": ("rotated-current", SECRET)}
        ),
    )

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert outcome.disposition == "applied"


def test_same_call_id_is_isolated_between_connector_projects(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    store.create_project("sales", display_name="Sales")
    support = store.create_connector(
        "support",
        provider="ringg",
        secret_ref="env:RINGG_WEBHOOK_SECRET",
        endpoint_id="ringg_support_000001",
    )
    sales = store.create_connector(
        "sales",
        provider="ringg",
        secret_ref="env:RINGG_WEBHOOK_SECRET",
        endpoint_id="ringg_sales_00000001",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:RINGG_WEBHOOK_SECRET": SECRET}),
    )

    support_outcome = ingestion.receive(_delivery(support.endpoint_id, _payload()))
    sales_outcome = ingestion.receive(_delivery(sales.endpoint_id, _payload()))

    assert support_outcome.bundle_id != sales_outcome.bundle_id
    assert [item.bundle_id for item in store.list_incidents(project_id="support").items] == [
        support_outcome.bundle_id
    ]
    assert [item.bundle_id for item in store.list_incidents(project_id="sales").items] == [
        sales_outcome.bundle_id
    ]
