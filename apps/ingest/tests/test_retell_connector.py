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
    DeliveryPayloadError,
    DeliveryRateLimitedError,
    DeliveryTrustError,
    HostedProviderIngestion,
    MappingSecretResolver,
    RawProviderDelivery,
)
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

SECRET = "retell-webhook-api-key"
NOW_MS = 1_752_828_600_000
PRIVATE_TEXT = "private Retell transcript must never be canonical"


def _payload() -> bytes:
    return json.dumps(
        {
            "event": "call_analyzed",
            "call": {
                "call_id": "call-sensitive-retell-123",
                "agent_id": "agent-sensitive-retell-456",
                "call_status": "ended",
                "start_timestamp": NOW_MS - 10_000,
                "end_timestamp": NOW_MS,
                "duration_ms": 10_000,
                "transcript": PRIVATE_TEXT,
                "transcript_object": [
                    {
                        "role": "user",
                        "content": PRIVATE_TEXT,
                        "words": [{"word": "private", "start": 0.5, "end": 1.0}],
                    },
                    {
                        "role": "agent",
                        "content": PRIVATE_TEXT,
                        "words": [
                            {"word": "private", "start": 1.5, "end": 1.8},
                            {"word": "response", "start": 1.8, "end": 2.2},
                        ],
                    },
                ],
                "retell_llm_dynamic_variables": {"private": PRIVATE_TEXT},
                "public_log_url": "https://private.example/log",
                "latency": {
                    "e2e": {
                        "p50": 800,
                        "p90": 1200,
                        "p95": 1500,
                        "p99": 2500,
                        "min": 500,
                        "max": 2700,
                        "num": 2,
                        "values": [800, 1200],
                    },
                    "llm": {
                        "p50": 400,
                        "p90": 400,
                        "p95": 400,
                        "p99": 400,
                        "min": 400,
                        "max": 400,
                        "num": 1,
                        "values": [400],
                    },
                    "tts": {
                        "p50": 150,
                        "p90": 150,
                        "p95": 150,
                        "p99": 150,
                        "min": 150,
                        "max": 150,
                        "num": 1,
                        "values": [150],
                    },
                },
                "call_analysis": {"call_summary": PRIVATE_TEXT},
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _delivery(endpoint_id: str, body: bytes, *, timestamp_ms: int = NOW_MS, secret=SECRET):
    signature = hmac.new(
        secret.encode(),
        body + str(timestamp_ms).encode(),
        hashlib.sha256,
    ).hexdigest()
    return RawProviderDelivery(
        endpoint_id=endpoint_id,
        headers=((b"x-retell-signature", f"v={timestamp_ms},d={signature}".encode()),),
        body=body,
    )


@pytest.fixture
def connector(tmp_path):
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    endpoint = store.create_connector(
        "support",
        provider="retell",
        secret_ref="env:RETELL_WEBHOOK_API_KEY",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:RETELL_WEBHOOK_API_KEY": SECRET}),
        now_unix_seconds=lambda: NOW_MS // 1_000,
    )
    return store, endpoint, ingestion


def test_call_analyzed_keeps_word_timing_and_uncorrelated_latency_without_text(
    connector,
) -> None:
    store, endpoint, ingestion = connector

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    assert len(bundle.profile.operations) == 1
    operation = bundle.profile.operations[0]
    assert operation.operation_name == "provider_agent_speech_timing"
    assert operation.started_at.monotonic_time_nano == "1500000000"
    assert operation.ended_at.monotonic_time_nano == "2200000000"
    samples = [
        (sample.measurements[0].name, sample.measurements[0].value)
        for sample in bundle.profile.quality_samples
    ]
    assert samples == [
        ("retell.e2e", 800.0),
        ("retell.e2e", 1200.0),
        ("retell.llm", 400.0),
        ("retell.tts", 150.0),
    ]
    assert all(sample.measurements[0].unit == "ms" for sample in bundle.profile.quality_samples)
    assert all(
        "earshot.turn.id" not in sample.attributes
        for sample in bundle.profile.quality_samples
    )
    facts = store.list_turn_facts(project_id="support")
    assert len(facts) == 1
    assert facts[0].first_token_ms is None
    assert PRIVATE_TEXT.encode() not in canonical
    assert b"call-sensitive-retell-123" not in canonical
    assert b"https://private.example/log" not in canonical


def test_retell_signature_rejects_stale_wrong_or_duplicate_headers(connector) -> None:
    store, endpoint, ingestion = connector
    body = _payload()

    for delivery in (
        _delivery(endpoint.endpoint_id, body, timestamp_ms=NOW_MS - 301_000),
        _delivery(endpoint.endpoint_id, body, secret="wrong"),
        RawProviderDelivery(
            endpoint_id=endpoint.endpoint_id,
            headers=(
                _delivery(endpoint.endpoint_id, body).headers[0],
                _delivery(endpoint.endpoint_id, body).headers[0],
            ),
            body=body,
        ),
    ):
        with pytest.raises(DeliveryTrustError):
            ingestion.receive(delivery)
    assert store.list_incidents(project_id="support").items == ()


def test_retell_coverage_reflects_absent_optional_fields(connector) -> None:
    store, endpoint, ingestion = connector
    value = json.loads(_payload())
    value["call"]["transcript_object"] = []
    value["call"]["latency"] = {}
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, body))

    _, canonical = store.get_artifact(outcome.bundle_id, project_id="support")
    bundle = decode_incident_protobuf(canonical)
    coverage = {item.signal: item.availability for item in bundle.profile.coverage}
    assert coverage["provider.word_timing"] == "not_observed"
    assert coverage["provider.latency_samples"] == "not_observed"


def _replace_call(overrides) -> bytes:
    value = json.loads(_payload())
    overrides(value["call"])
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_same_provider_call_isolated_between_connector_projects(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("support", display_name="Support")
    store.create_project("sales", display_name="Sales")
    support = store.create_connector(
        "support",
        provider="retell",
        secret_ref="env:RETELL_WEBHOOK_API_KEY",
        endpoint_id="retell_support_00001",
    )
    sales = store.create_connector(
        "sales",
        provider="retell",
        secret_ref="env:RETELL_WEBHOOK_API_KEY",
        endpoint_id="retell_sales_000001",
    )
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:RETELL_WEBHOOK_API_KEY": SECRET}),
        now_unix_seconds=lambda: NOW_MS // 1_000,
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


def test_signature_rotation_accepts_previous_secret(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver(
            {"env:RETELL_WEBHOOK_API_KEY": ("rotated-current-key", SECRET)}
        ),
        now_unix_seconds=lambda: NOW_MS // 1_000,
    )

    # The provider still signs with the previous webhook key during rotation.
    outcome = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert outcome.disposition == "applied"
    assert len(store.list_incidents(project_id="support").items) == 1


def test_missing_connector_secret_is_retryable_configuration_failure(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({}),
        now_unix_seconds=lambda: NOW_MS // 1_000,
    )

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    assert store.list_incidents(project_id="support").items == ()


def test_connector_rejects_unavailable_normalizer_version(connector) -> None:
    store, _, ingestion = connector
    incompatible = store.create_connector(
        "support",
        provider="retell",
        secret_ref="env:RETELL_WEBHOOK_API_KEY",
        endpoint_id="retell_incompatible1",
        normalizer_version="99.0.0",
    )

    with pytest.raises(ConnectorConfigurationError):
        ingestion.receive(_delivery(incompatible.endpoint_id, _payload()))


def test_exact_retry_replays_but_changed_body_conflicts(connector) -> None:
    store, endpoint, ingestion = connector
    first = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))

    replay = ingestion.receive(_delivery(endpoint.endpoint_id, _payload()))
    assert replay.disposition == "replayed"
    assert replay.bundle_id == first.bundle_id

    changed = _replace_call(lambda call: call["latency"]["llm"].__setitem__("values", [401]))
    with pytest.raises(DeliveryConflictError):
        ingestion.receive(_delivery(endpoint.endpoint_id, changed))
    assert len(store.list_incidents(project_id="support").items) == 1


def test_non_call_analyzed_event_is_rejected(connector) -> None:
    store, endpoint, ingestion = connector
    value = json.loads(_payload())
    value["event"] = "call_started"
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, body))
    assert store.list_incidents(project_id="support").items == ()


def test_non_monotonic_word_offsets_are_rejected(connector) -> None:
    store, endpoint, ingestion = connector
    body = _replace_call(
        lambda call: call.__setitem__(
            "transcript_object",
            [
                {
                    "role": "agent",
                    "words": [
                        {"word": "a", "start": 1.5, "end": 1.8},
                        {"word": "b", "start": 1.7, "end": 2.0},
                    ],
                }
            ],
        )
    )

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, body))
    assert store.list_incidents(project_id="support").items == ()


def test_reversed_word_span_is_rejected(connector) -> None:
    store, endpoint, ingestion = connector
    body = _replace_call(
        lambda call: call.__setitem__(
            "transcript_object",
            [{"role": "agent", "words": [{"word": "a", "start": 2.0, "end": 1.0}]}],
        )
    )

    with pytest.raises(DeliveryPayloadError):
        ingestion.receive(_delivery(endpoint.endpoint_id, body))
    assert store.list_incidents(project_id="support").items == ()


def test_authenticated_connector_rate_limit_is_bounded_and_retryable(connector) -> None:
    store, endpoint, _ = connector
    ingestion = HostedProviderIngestion(
        store,
        secrets=MappingSecretResolver({"env:RETELL_WEBHOOK_API_KEY": SECRET}),
        now_unix_seconds=lambda: NOW_MS // 1_000,
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
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "EARSHOT_CONNECTOR_BODY_TOO_LARGE"
