from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from earshot.codec import decode_incident_protobuf
from earshot.connectors import (
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
