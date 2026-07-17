from __future__ import annotations

import json

import pytest

from earshot.codec import decode_incident_protobuf
from earshot.connectors import (
    DeliveryConflictError,
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

    outcome = ingestion.receive(
        _delivery(endpoint.endpoint_id, _payload(turn_latency=1200.0))
    )

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
