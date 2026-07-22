from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from earshot.api import ApiConfig, create_app
from earshot.codec import decode_incident_protobuf
from earshot.connectors import (
    ConnectorConfigurationError,
    DeliveryBusyError,
    DeliveryConflictError,
    DeliveryPayloadError,
    DeliveryRateLimitedError,
    DeliveryTrustError,
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

SECRET = "vapi-test-secret"
PRIVATE_TEXT = "private Vapi transcript must never be canonical"


def _payload(*, turn_latency: float = 1.25) -> bytes:
    return json.dumps(
        {
            "message": {
                "type": "end-of-call-report",
                "timestamp": "2026-07-18T09:30:06Z",
                "endedReason": "customer-ended-call",
                "call": {
                    "id": "call-sensitive-vapi-123",
                    "createdAt": "2026-07-18T09:30:00Z",
                    "endedAt": "2026-07-18T09:30:06Z",
                    "status": "ended",
                },
                "artifact": {
                    "transcript": PRIVATE_TEXT,
                    "messages": [
                        {
                            "role": "assistant",
                            "message": PRIVATE_TEXT,
                            "secondsFromStart": 2.0,
                        }
                    ],
                    "variableValues": {"private": PRIVATE_TEXT},
                    "performanceMetrics": {
                        "turnLatencies": [
                            {
                                "modelLatency": 0.4,
                                "voiceLatency": 0.2,
                                "transcriberLatency": 0.1,
                                "endpointingLatency": 0.3,
                                "turnLatency": turn_latency,
                            }
                        ],
                        "numUserInterrupted": 2,
                        "numAssistantInterrupted": 1,
                    },
                },
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _delivery(endpoint_id: str, body: bytes, *, header: str = "authorization"):
    headers = (
        ((b"authorization", f"Bearer {SECRET}".encode()),)
        if header == "authorization"
        else ((b"x-vapi-secret", SECRET.encode()),)
    )
    return RawProviderDelivery(endpoint_id=endpoint_id, headers=headers, body=body)


@pytest.fixture
def connector(tmp_path):
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    endpoint = store.create_connector(
        "support",
        provider="vapi",
        secret_ref="env:VAPI_SERVER_SECRET",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:VAPI_SERVER_SECRET": SECRET}),
    )
    return store, endpoint, ingestion


@pytest.mark.parametrize("header", ("authorization", "x-vapi-secret"))
def test_final_report_accepts_documented_auth_and_keeps_only_metrics(connector, header) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload(), header=header))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    measurements = {
        measurement.name: measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    assert measurements["vapi.turn_latency"].value == 1.25
    assert measurements["vapi.turn_latency"].unit == "provider_unit"
    assert measurements["vapi.num_user_interrupted"].value == 2
    assert len(store.list_turn_facts(project_id="support")) == 1
    assert bundle.profile.operations[0].operation_name == "provider_turn_anchor"
    assert bundle.profile.operations[0].evidence.method == "provider_ordered_index_join"
    assert PRIVATE_TEXT.encode() not in canonical
    assert b"call-sensitive-vapi-123" not in canonical


def test_vapi_rejects_ambiguous_or_wrong_auth_before_persistence(connector) -> None:
    store, endpoint, ingestion = connector
    body = _payload()

    with pytest.raises(DeliveryTrustError):
        ingestion.receive(
            RawProviderDelivery(
                endpoint_id=endpoint.endpoint_id,
                headers=(
                    (b"authorization", f"Bearer {SECRET}".encode()),
                    (b"x-vapi-secret", SECRET.encode()),
                ),
                body=body,
            )
        )
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(
            RawProviderDelivery(
                endpoint_id=endpoint.endpoint_id,
                headers=((b"x-vapi-secret", b"wrong"),),
                body=body,
            )
        )
    assert store.list_incidents(project_id="support").items == ()


def test_vapi_call_identity_is_idempotent_and_changed_final_report_conflicts(connector) -> None:
    store, endpoint, ingestion = connector
    first = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))
    replay = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert replay.disposition == "replayed"
    assert replay.bundle_id == first.bundle_id
    with pytest.raises(DeliveryConflictError):
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload(turn_latency=1.5)))
    assert len(store.list_incidents(project_id="support").items) == 1


def test_vapi_unknown_units_are_not_capped_as_seconds(connector) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload(turn_latency=1200.0)))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    turn_latency = next(
        measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name == "vapi.turn_latency"
    )
    assert turn_latency.value == 1200.0
    assert turn_latency.unit == "provider_unit"


def test_vapi_metrics_without_message_offset_remain_session_scoped(connector) -> None:
    store, endpoint, ingestion = connector
    value = json.loads(_payload())
    del value["message"]["artifact"]["messages"][0]["secondsFromStart"]
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, body))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    latency = next(
        sample
        for sample in bundle.profile.quality_samples
        if any(item.name == "vapi.turn_latency" for item in sample.measurements)
    )
    assert latency.attributes["earshot.correlation"] == "provider_order_only"
    assert "earshot.turn.id" not in latency.attributes
    assert bundle.profile.operations == ()
    assert store.list_turn_facts(project_id="support") == ()


def test_same_provider_call_isolated_between_connector_projects(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    store.create_project("sales", display_name="Sales")
    support = store.create_connector(
        "support",
        provider="vapi",
        secret_ref="env:VAPI_SERVER_SECRET",
        endpoint_id="vapi_support_000001",
    )
    sales = store.create_connector(
        "sales",
        provider="vapi",
        secret_ref="env:VAPI_SERVER_SECRET",
        endpoint_id="vapi_sales_00000001",
    )
    ingestion = HostedProviderIngestion(
        store, secrets=MappingSecretResolver({"env:VAPI_SERVER_SECRET": SECRET})
    )
    body = _payload()

    support_outcome = ingestion.receive(_delivery(support.endpoint_id, body))
    sales_outcome = ingestion.receive(_delivery(sales.endpoint_id, body))

    assert support_outcome.bundle_id != sales_outcome.bundle_id
    assert [record.bundle_id for record in store.list_incidents(project_id="support").items] == [
        support_outcome.bundle_id
    ]
    assert [record.bundle_id for record in store.list_incidents(project_id="sales").items] == [
        sales_outcome.bundle_id
    ]


def test_bearer_rotation_accepts_previous_secret(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver(
            {"env:VAPI_SERVER_SECRET": ("rotated-current-secret", SECRET)}
        ),
    )

    # A provider still transitioning presents the previous secret.
    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert outcome.disposition == "applied"
    assert len(store.list_incidents(project_id="support").items) == 1


def test_missing_connector_secret_is_retryable_configuration_failure(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(store, secrets=MappingSecretResolver({}))

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert store.list_incidents(project_id="support").items == ()


def test_connector_rejects_unavailable_normalizer_version(connector) -> None:
    store, _, ingestion = connector
    incompatible = store.create_connector(
        "support",
        provider="vapi",
        secret_ref="env:VAPI_SERVER_SECRET",
        endpoint_id="vapi_incompatible_0001",
        normalizer_version="99.0.0",
    )

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(_delivery(incompatible.endpoint_id, _payload()))


def test_authentication_precedes_json_parsing(connector) -> None:
    store, endpoint, ingestion = connector
    malformed = b'{"message":"unterminated'

    # Correct auth, malformed body: parsing runs (after auth) and fails cleanly.
    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed))
    # Wrong auth, malformed body: rejected at auth; the body is never parsed.
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(
            RawProviderDelivery(
                endpoint_id=endpoint.endpoint_id,
                headers=((b"authorization", b"Bearer wrong"),),
                body=malformed,
            )
        )
    assert store.list_incidents(project_id="support").items == ()


def test_turn_latency_field_is_required_per_entry(connector) -> None:
    store, endpoint, ingestion = connector
    value = json.loads(_payload())
    del value["message"]["artifact"]["performanceMetrics"]["turnLatencies"][0]["turnLatency"]
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, body))
    assert store.list_incidents(project_id="support").items == ()


def test_absent_performance_metrics_is_rejected(connector) -> None:
    store, endpoint, ingestion = connector
    value = json.loads(_payload())
    del value["message"]["artifact"]["performanceMetrics"]
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, body))
    assert store.list_incidents(project_id="support").items == ()


def test_authenticated_connector_rate_limit_is_bounded_and_retryable(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:VAPI_SERVER_SECRET": SECRET}),
        now_monotonic=lambda: 10.0,
        max_deliveries_per_minute=1,
    )
    delivery = _delivery(endpoint.endpoint_id, _payload())

    ingestion.receive(delivery)
    with pytest.raises(DeliveryRateLimitedError) as limited:
        ingestion.receive(delivery)
    assert limited.value.retry_after_seconds == 60


def test_concurrent_exact_delivery_publishes_at_most_once(connector) -> None:
    store, endpoint, ingestion = connector
    delivery = _delivery(endpoint.endpoint_id, _payload())

    def receive_once() -> str:
        try:
            return ingestion.receive(delivery).disposition
        except DeliveryBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: receive_once(), range(8)))

    assert results.count("applied") == 1
    assert set(results) <= {"applied", "replayed", "busy"}
    assert len(store.list_incidents(project_id="support").items) == 1


def test_http_hook_rejects_media_type_and_oversized_body(connector) -> None:
    store, endpoint, ingestion = connector
    client = TestClient(
        create_app(
            store=store,
            connector_ingestion=ingestion,
            config=ApiConfig(max_connector_body_bytes=16),
        )
    )

    wrong_type = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=b"{}",
        headers={"content-type": "text/plain"},
    )
    oversized = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=b"{" + b"x" * 16 + b"}",
        headers={"content-type": "application/json"},
    )

    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "EARSHOT_UNSUPPORTED_MEDIA_TYPE"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "EARSHOT_CONNECTOR_BODY_TOO_LARGE"
