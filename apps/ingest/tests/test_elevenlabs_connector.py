from __future__ import annotations

import hashlib
import hmac
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
    DeliveryMappingError,
    DeliveryPayloadError,
    DeliveryRateLimitedError,
    DeliveryTrustError,
    EnvironmentSecretResolver,
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "elevenlabs-test-secret"
TRANSCRIPT_SENTINEL = "private-transcript-do-not-store"


def _payload(
    *,
    event_timestamp: int = 1_739_537_297,
    message: str = TRANSCRIPT_SENTINEL,
    status: object = "done",
) -> bytes:
    return json.dumps(
        {
            "type": "post_call_transcription",
            "event_timestamp": event_timestamp,
            "project_id": "attacker-selected-project",
            "data": {
                "agent_id": "agent-sensitive-123",
                "conversation_id": "conversation-sensitive-456",
                "status": status,
                "transcript": [
                    {
                        "role": "user",
                        "message": message,
                        "time_in_call_secs": 2,
                        "conversation_turn_metrics": None,
                    },
                    {
                        "role": "agent",
                        "message": "private-agent-response",
                        "time_in_call_secs": 3,
                        "conversation_turn_metrics": {
                            "convai_llm_service_ttfb": {"elapsed_time": 0.370424701}
                        },
                    },
                ],
                "metadata": {
                    "start_time_unix_secs": event_timestamp,
                    "call_duration_secs": 5,
                },
                "conversation_initiation_client_data": {
                    "dynamic_variables": {"secret": "private-dynamic-variable"}
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _delivery(
    endpoint_id: str,
    body: bytes,
    *,
    secret: str = WEBHOOK_SECRET,
    timestamp: int = 1_739_537_300,
):
    signature = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return RawProviderDelivery(
        endpoint_id=endpoint_id,
        headers=((b"elevenlabs-signature", f"t={timestamp},v0={signature}".encode()),),
        body=body,
    )


def _otel_payload() -> bytes:
    return json.dumps(
        {
            "type": "post_call_transcription_otel",
            "event_timestamp": 1_739_537_297,
            "data": {
                "conversation_id": "conversation-sensitive-otel",
                "agent_id": "agent-sensitive-otel",
                "otlp_traces": {
                    "resourceSpans": [
                        {
                            "resource": {
                                "attributes": [
                                    {
                                        "key": "elevenlabs.conversation_id",
                                        "value": {"stringValue": "conversation-sensitive-otel"},
                                    }
                                ]
                            },
                            "scopeSpans": [
                                {
                                    "scope": {
                                        "name": "elevenlabs.convai",
                                        "version": "1.0.0",
                                    },
                                    "spans": [
                                        {
                                            "traceId": "1" * 32,
                                            "spanId": "1" * 16,
                                            "name": "elevenlabs.conversation",
                                            "startTimeUnixNano": "1739537297000000000",
                                            "endTimeUnixNano": "1739537302000000000",
                                            "status": {"code": 1},
                                        },
                                        {
                                            "traceId": "1" * 32,
                                            "spanId": "2" * 16,
                                            "parentSpanId": "1" * 16,
                                            "name": "elevenlabs.recv.agent_response",
                                            "startTimeUnixNano": "1739537299000000000",
                                            "endTimeUnixNano": "1739537300000000000",
                                            "status": {"code": 1},
                                            "attributes": [
                                                {
                                                    "key": "elevenlabs.agent.text",
                                                    "value": {
                                                        "stringValue": "private OTLP transcript"
                                                    },
                                                }
                                            ],
                                        },
                                        {
                                            "traceId": "1" * 32,
                                            "spanId": "3" * 16,
                                            "parentSpanId": "2" * 16,
                                            "name": "elevenlabs.tool.lookup",
                                            "startTimeUnixNano": "1739537299500000000",
                                            "endTimeUnixNano": "1739537299700000000",
                                            "status": {"code": 1},
                                            "attributes": [
                                                {
                                                    "key": "elevenlabs.tool.result",
                                                    "value": {"stringValue": "private tool result"},
                                                }
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


@pytest.fixture
def connector(tmp_path):
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    endpoint = store.create_connector(
        "support",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_WEBHOOK_SECRET",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET}),
        now_unix_seconds=lambda: 1_739_537_300,
    )
    return store, endpoint, ingestion


def test_signed_transcription_creates_one_private_project_incident(connector) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert outcome.disposition == "applied"
    assert outcome.project_id == "support"
    assert store.list_incidents().items == ()
    records = store.list_incidents(project_id="support").items
    assert [record.bundle_id for record in records] == [outcome.bundle_id]
    facts = store.list_turn_facts(project_id="support")
    assert len(facts) == 1
    assert facts[0].framework == "elevenlabs_agents"
    assert facts[0].first_token_ms == pytest.approx(370.424701)
    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    assert decode_incident_protobuf(canonical).profile.session.status == "completed"
    assert TRANSCRIPT_SENTINEL.encode() not in canonical
    assert b"private-agent-response" not in canonical
    assert b"private-dynamic-variable" not in canonical
    assert b"conversation-sensitive-456" not in canonical


def test_free_form_provider_status_is_rejected_without_persistence(connector, tmp_path) -> None:
    store, endpoint, ingestion = connector

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(
            _delivery(
                endpoint.endpoint_id,
                _payload(status=TRANSCRIPT_SENTINEL),
            )
        )

    assert store.list_incidents(project_id="support").items == ()
    assert all(
        TRANSCRIPT_SENTINEL.encode() not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_same_provider_call_isolated_between_connector_projects(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    store.create_project("sales", display_name="Sales")
    support = store.create_connector(
        "support",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_WEBHOOK_SECRET",
        endpoint_id="elevenlabs_support_0001",
    )
    sales = store.create_connector(
        "sales",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_WEBHOOK_SECRET",
        endpoint_id="elevenlabs_sales_00001",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET}),
        now_unix_seconds=lambda: 1_739_537_300,
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


def test_exact_retry_replays_but_authenticated_changed_retry_conflicts(connector) -> None:
    store, endpoint, ingestion = connector
    first = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    replay = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert replay.disposition == "replayed"
    assert replay.bundle_id == first.bundle_id
    with pytest.raises(DeliveryConflictError):
        ingestion.receive(
            _delivery(endpoint.endpoint_id, _payload(message="changed authenticated body"))
        )
    assert len(store.list_incidents(project_id="support").items) == 1


def test_invalid_signature_creates_no_incident(connector) -> None:
    store, endpoint, ingestion = connector

    with pytest.raises(DeliveryTrustError):
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload(), secret="wrong"))

    assert store.list_incidents(project_id="support").items == ()


def test_environment_secret_resolver_supports_bounded_rotation(monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_PROVIDER_SECRET", "current-secret")
    monkeypatch.setenv("EARSHOT_PROVIDER_SECRET_PREVIOUS", "previous-secret")

    assert EnvironmentSecretResolver().resolve("env:EARSHOT_PROVIDER_SECRET") == (
        "current-secret",
        "previous-secret",
    )


def test_missing_connector_secret_is_retryable_configuration_failure(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({}),
        now_unix_seconds=lambda: 1_739_537_300,
    )
    delivery = _delivery(endpoint.endpoint_id, _payload())

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(delivery)

    client = TestClient(create_app(store=store, connector_ingestion=ingestion))
    response = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=delivery.body,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": delivery.headers[0][1].decode(),
        },
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json() == {
        "error": {
            "code": "EARSHOT_CONNECTOR_CONFIGURATION_INVALID",
            "message": "connector endpoint configuration is incompatible",
            "retryable": True,
        }
    }
    assert "ELEVENLABS_WEBHOOK_SECRET" not in response.text


def test_connector_rejects_unavailable_normalizer_version(connector) -> None:
    store, _, ingestion = connector
    incompatible = store.create_connector(
        "support",
        provider="elevenlabs",
        secret_ref="env:ELEVENLABS_WEBHOOK_SECRET",
        endpoint_id="incompatible_normalizer_001",
        normalizer_version="99.0.0",
    )

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(_delivery(incompatible.endpoint_id, _payload()))


def test_authentication_precedes_malformed_json_and_rejects_stale_or_duplicate_headers(
    connector,
) -> None:
    store, endpoint, ingestion = connector
    malformed = b'{"private":"never-parse",'

    with pytest.raises(DeliveryTrustError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed, secret="wrong"))
    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed))
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(_delivery(endpoint.endpoint_id, malformed, timestamp=1_739_536_999))

    valid = _delivery(endpoint.endpoint_id, _payload())
    with pytest.raises(DeliveryTrustError):
        ingestion.receive(
            RawProviderDelivery(
                endpoint_id=endpoint.endpoint_id,
                headers=(valid.headers[0], valid.headers[0]),
                body=valid.body,
            )
        )
    assert store.list_incidents(project_id="support").items == ()


def test_audio_delivery_is_authenticated_then_ignored(connector) -> None:
    store, endpoint, ingestion = connector
    body = json.dumps(
        {
            "type": "post_call_audio",
            "event_timestamp": 1_739_537_297,
            "data": {
                "conversation_id": "conversation-sensitive-456",
                "agent_id": "agent-sensitive-123",
                "full_audio": "base64-private-audio",
            },
        },
        separators=(",", ":"),
    ).encode()

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, body))

    assert outcome.disposition == "ignored"
    assert outcome.bundle_id is None
    assert store.list_incidents(project_id="support").items == ()


def test_signed_otel_transcription_preserves_trace_shape_but_not_payload_text(connector) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _otel_payload()))

    assert outcome.disposition == "applied"
    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    assert [(operation.trace_id, operation.span_id) for operation in bundle.profile.operations] == [
        ("1" * 32, "1" * 16),
        ("1" * 32, "2" * 16),
        ("1" * 32, "3" * 16),
    ]
    assert (
        next(
            operation for operation in bundle.profile.operations if operation.span_id == "3" * 16
        ).parent_span_id
        == "2" * 16
    )
    facts = store.list_turn_facts(project_id="support")
    assert len(facts) == 1
    assert facts[0].first_token_ms is None
    assert b"private OTLP transcript" not in canonical
    assert b"private tool result" not in canonical
    assert b"conversation-sensitive-otel" not in canonical


def test_http_hook_uses_provider_signature_not_earshot_bearer(connector) -> None:
    store, endpoint, ingestion = connector
    client = TestClient(
        create_app(
            store=store,
            config=ApiConfig(token="operator-api-token"),
            connector_ingestion=ingestion,
        )
    )
    delivery = _delivery(endpoint.endpoint_id, _payload())

    response = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=delivery.body,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": delivery.headers[0][1].decode(),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "receipt_id": response.json()["receipt_id"],
        "disposition": "applied",
        "bundle_id": response.json()["bundle_id"],
        "canonical_sha256": response.json()["canonical_sha256"],
    }
    assert "project_id" not in response.text
    assert len(store.list_incidents(project_id="support").items) == 1


def test_http_hook_errors_are_bounded_and_do_not_reflect_payload(connector) -> None:
    store, endpoint, ingestion = connector
    client = TestClient(create_app(store=store, connector_ingestion=ingestion))
    body = _payload(message="never-reflect-this-private-value")

    response = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=body,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": "t=1739537300,v0=" + "0" * 64,
        },
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "EARSHOT_CONNECTOR_AUTH_FAILED",
            "message": "connector signature verification failed",
            "retryable": False,
        }
    }
    assert "never-reflect" not in response.text
    assert store.list_incidents(project_id="support").items == ()


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


def test_authenticated_connector_rate_limit_is_bounded_and_retryable(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET}),
        now_unix_seconds=lambda: 1_739_537_300,
        now_monotonic=lambda: 10.0,
        max_deliveries_per_minute=1,
    )
    delivery = _delivery(endpoint.endpoint_id, _payload())

    ingestion.receive(delivery)
    with pytest.raises(DeliveryRateLimitedError) as limited:
        ingestion.receive(delivery)
    assert limited.value.retry_after_seconds == 60


def test_http_retryable_connector_errors_include_retry_after(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET}),
        now_unix_seconds=lambda: 1_739_537_300,
        now_monotonic=lambda: 10.0,
        max_deliveries_per_minute=1,
    )
    client = TestClient(create_app(store=store, connector_ingestion=ingestion))
    delivery = _delivery(endpoint.endpoint_id, _payload())
    headers = {
        "content-type": "application/json",
        "elevenlabs-signature": delivery.headers[0][1].decode(),
    }
    assert (
        client.post(
            f"/hooks/v1/connectors/{endpoint.endpoint_id}",
            content=delivery.body,
            headers=headers,
        ).status_code
        == 200
    )

    limited = client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=delivery.body,
        headers=headers,
    )

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.json()["error"]["retryable"] is True

    class BusyIngestion:
        def receive(self, _delivery):
            raise DeliveryBusyError(retry_after_seconds=17)

    busy_client = TestClient(create_app(store=store, connector_ingestion=BusyIngestion()))
    busy = busy_client.post(
        f"/hooks/v1/connectors/{endpoint.endpoint_id}",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "17"
    assert busy.json()["error"]["retryable"] is True


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


def test_unexpected_normalizer_failure_is_non_reflective_mapping_error(connector) -> None:
    store, endpoint, ingestion = connector

    class BrokenAdapter:
        normalizer_version = endpoint.normalizer_version

        def authenticate(self, *_args, **_kwargs) -> None:
            return None

        def normalize(self, *_args, **_kwargs):
            raise ValueError(TRANSCRIPT_SENTINEL)

    ingestion._adapters["elevenlabs"] = BrokenAdapter()

    with pytest.raises(DeliveryMappingError) as raised:
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert TRANSCRIPT_SENTINEL not in str(raised.value)
    assert store.list_incidents(project_id="support").items == ()
