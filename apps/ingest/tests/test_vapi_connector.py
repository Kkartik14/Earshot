"""Vapi ``end-of-call-report`` deliveries become call evidence (FR-1, FR-2).

Verified on documented payloads only: the report below follows the
``ServerMessageEndOfCallReport`` and ``Artifact`` schemas of Vapi's OpenAPI document
(``https://api.vapi.ai/api-json``); no real Vapi delivery has been captured. Every
delivery goes through the real kernel, call store and recordings store.
"""

from __future__ import annotations

import copy

import pytest

from connector_support import SECOND, START, ConnectorWorld, body_of, connector_world
from earshot.call_capture import attribute_values
from earshot.connectors import DeliveryRateLimitedError, DeliveryTrustError, RawProviderDelivery

pytestmark = pytest.mark.integration

CALL_ID = "6f1c2b9e-0d7a-4c55-9a51-3e0f8c2d1a77"
CANARY = "X-Amz-Signature=CANARY0123456789abcdef"
EXPIRES = "2026-10-20T11:00:35.000Z"


def _report(*, signature: str = CANARY, metrics: bool = True) -> dict:
    artifact = {
        "transcript": "AI: Thanks for calling Green Valley Clinic.\nUser: I need to move my visit.",
        "messages": [
            {
                "role": "system",
                "message": "You are a clinic receptionist.",
                "time": 1792490400000,
                "secondsFromStart": 0,
            },
            {
                "role": "bot",
                "message": "Thanks for calling Green Valley Clinic.",
                "time": 1792490400500,
                "endTime": 1792490402300,
                "secondsFromStart": 0.5,
                "duration": 1800,
            },
            {
                "role": "user",
                "message": "I need to move my visit.",
                "time": 1792490403100,
                "endTime": 1792490404400,
                "secondsFromStart": 3.1,
                "duration": 1300,
            },
            {"role": "tool_calls", "message": "", "time": 1792490404600, "secondsFromStart": 4.6},
            {"role": "bot", "message": "Sure, which day works?", "secondsFromStart": 5.2},
        ],
        "recording": {
            "stereoUrl": "https://storage.vapi.ai/6f1c2b9e-stereo.wav",
            "mono": {
                "combinedUrl": "https://storage.vapi.ai/6f1c2b9e-mono.wav",
                "assistantUrl": "https://storage.vapi.ai/6f1c2b9e-assistant.wav",
                "customerUrl": "https://storage.vapi.ai/6f1c2b9e-customer.wav",
            },
        },
        "presignedCustomerUrl": f"https://storage.vapi.ai/6f1c2b9e-customer.wav?{signature}",
        "presignedAssistantUrl": f"https://storage.vapi.ai/6f1c2b9e-assistant.wav?{signature}",
        "presignedUrlsExpiresAt": EXPIRES,
    }
    if metrics:
        artifact["performanceMetrics"] = {
            "turnLatencies": [
                {
                    "modelLatency": 420,
                    "voiceLatency": 180,
                    "transcriberLatency": 90,
                    "endpointingLatency": 300,
                    "turnLatency": 990,
                }
            ],
            "turnLatencyAverage": 990,
            "numUserInterrupted": 0,
            "numAssistantInterrupted": 1,
        }
    return {
        "message": {
            "type": "end-of-call-report",
            "timestamp": 1792490436000,
            "endedReason": "customer-ended-call",
            "startedAt": "2026-10-20T10:00:00.000Z",
            "endedAt": "2026-10-20T10:00:35.000Z",
            "cost": 0.1234,
            "analysis": {
                "summary": "The caller moved a clinic visit.",
                "successEvaluation": "true",
            },
            "artifact": artifact,
            "call": {
                "id": CALL_ID,
                "orgId": "org-0001",
                "type": "webCall",
                "status": "ended",
                "assistantId": "asst-0001",
                "createdAt": "2026-10-20T09:59:58.000Z",
                "startedAt": "2026-10-20T10:00:00.000Z",
                "endedAt": "2026-10-20T10:00:35.000Z",
                "cost": 0.1234,
            },
            "customer": {"number": "+15550100001"},
        }
    }


@pytest.fixture
def world(tmp_path) -> ConnectorWorld:
    return connector_world(tmp_path, "vapi")


def _attributes(world: ConnectorWorld, name: str) -> list[dict]:
    return [
        attribute_values(span.span.attributes) for span in world.spans() if span.span.name == name
    ]


@pytest.mark.parametrize(
    "header",
    [(b"authorization", b"Bearer vapi-test-secret"), (b"x-vapi-secret", b"vapi-test-secret")],
)
def test_either_documented_credential_is_accepted(world, header) -> None:
    raw = body_of(_report())
    outcome = world.ingestion.receive(
        RawProviderDelivery(
            world.endpoint_id, ((b"content-type", b"application/json"), header), raw
        )
    )

    assert outcome.disposition == "applied"


@pytest.mark.parametrize(
    "headers",
    [
        ((b"authorization", b"Bearer wrong"),),
        ((b"authorization", b"Bearer vapi-test-secret"), (b"x-vapi-secret", b"vapi-test-secret")),
        (),
    ],
    ids=["wrong", "both", "none"],
)
def test_a_wrong_or_ambiguous_credential_is_refused_before_the_body(world, headers) -> None:
    endpoint = world.ingestion.endpoint(world.endpoint_id)

    with pytest.raises(DeliveryTrustError):
        world.ingestion.open_body(endpoint, headers)


def test_a_report_becomes_one_call_with_its_transcript_latency_cost_and_recordings(world) -> None:
    outcome = world.deliver(_report())

    assert outcome.disposition == "applied"
    assert outcome.call_id == world.calls.resolve_call("support", f"vapi:{CALL_ID}")
    assert world.rows("SELECT count(*) FROM incidents") == [(0,)]  # no standalone incident
    assert sorted(set(world.span_names())) == [
        "earshot.connector.analysis",
        "earshot.connector.call",
        "earshot.connector.cost",
        "earshot.connector.transcript_line",
        "earshot.connector.translator",
        "earshot.connector.turn",
    ]
    [call] = _attributes(world, "earshot.connector.call")
    assert call["earshot.call.ended"] is True
    assert call["gen_ai.agent.id"] == "asst-0001"
    assert call["earshot.connector.call.ended_reason"] == "customer-ended-call"
    assert call["earshot.connector.coverage.turn_latency"] == "available"
    lines = sorted(
        _attributes(world, "earshot.connector.transcript_line"),
        key=lambda line: line["earshot.connector.transcript.index"],
    )
    assert [
        (
            line["earshot.connector.transcript.index"],
            line["earshot.connector.transcript.speaker"],
            line["earshot.connector.transcript.timing"],
        )
        for line in lines
    ] == [(1, "agent", "measured"), (2, "user", "measured"), (4, "agent", "start_only")]
    first = next(
        span
        for span in world.spans()
        if attribute_values(span.span.attributes).get("earshot.connector.transcript.index") == 1
    )
    assert (first.span.start_time_unix_nano, first.span.end_time_unix_nano) == (
        START + SECOND // 2,
        START + 2_300_000_000,
    )
    [turn] = _attributes(world, "earshot.connector.turn")
    assert turn["earshot.connector.latency.turn_ms"] == 990.0
    assert turn["earshot.connector.latency.llm_ms"] == 420.0
    [cost] = _attributes(world, "earshot.connector.cost")
    assert (cost["earshot.connector.cost.amount"], cost["earshot.connector.cost.unit"]) == (
        0.1234,
        "usd",
    )
    # Two mono tracks on one timeline, fetched from the payload's presigned URLs.
    assert world.fetches() == [
        (
            "agent",
            "audio/wav",
            "pending",
            "payload_url",
            "assistant",
            f"https://storage.vapi.ai/6f1c2b9e-assistant.wav?{CANARY}",
            1792494035000000000,
            "pending",
        ),
        (
            "user",
            "audio/wav",
            "pending",
            "payload_url",
            "customer",
            f"https://storage.vapi.ai/6f1c2b9e-customer.wav?{CANARY}",
            1792494035000000000,
            "pending",
        ),
    ]
    assert world.rows("SELECT DISTINCT started_at_unix_nano FROM recordings") == [(str(START),)]


def test_a_report_without_performance_metrics_keeps_its_transcript_and_audio(world) -> None:
    outcome = world.deliver(_report(metrics=False))

    assert outcome.disposition == "applied"
    [call] = _attributes(world, "earshot.connector.call")
    assert call["earshot.connector.coverage.turn_latency"] == "not_exposed"
    assert _attributes(world, "earshot.connector.turn") == []
    assert len(_attributes(world, "earshot.connector.transcript_line")) == 3
    assert len(world.fetches()) == 2


def test_a_retry_with_re_signed_urls_replays_and_refreshes_the_unfinished_fetches(world) -> None:
    first = world.deliver(_report())
    spans = world.rows("SELECT count(*) FROM spans")
    retry = world.deliver(_report(signature="X-Amz-Signature=RESIGNED0123456789"))

    assert (retry.disposition, retry.receipt_id, retry.call_id) == (
        "replayed",
        first.receipt_id,
        first.call_id,
    )
    assert world.rows("SELECT count(*) FROM spans") == spans
    assert {row[5] for row in world.fetches()} == {
        "https://storage.vapi.ai/6f1c2b9e-assistant.wav?X-Amz-Signature=RESIGNED0123456789",
        "https://storage.vapi.ai/6f1c2b9e-customer.wav?X-Amz-Signature=RESIGNED0123456789",
    }


def test_with_an_api_key_the_recordings_come_from_vapis_api_by_call_id(tmp_path) -> None:
    world = connector_world(tmp_path, "vapi", config={"api_key_ref": "env:VAPI_PRIVATE_KEY"})

    world.deliver(_report())

    assert [(row[3], row[4], row[5]) for row in world.fetches()] == [
        ("provider_api", "assistant", None),
        ("provider_api", "customer", None),
    ]


def test_events_other_than_the_report_are_ignored_without_a_write_or_a_rate_debit(
    tmp_path,
) -> None:
    world = connector_world(tmp_path, "vapi", max_deliveries_per_minute=1)
    status = {
        "message": {"type": "status-update", "status": "in-progress", "call": {"id": CALL_ID}}
    }
    partial = {"message": {"type": "transcript", "transcriptType": "partial", "transcript": "I"}}

    ignored = [world.deliver(status).disposition, world.deliver(partial).disposition]
    report = world.deliver(_report())

    assert ignored == ["ignored", "ignored"]
    assert report.disposition == "applied"
    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(1,)]
    with pytest.raises(DeliveryRateLimitedError):
        report_again = copy.deepcopy(_report())
        report_again["message"]["call"]["id"] = "another-call"
        world.deliver(report_again)


def test_an_out_of_range_time_is_a_malformed_payload_not_a_server_error(world) -> None:
    from earshot.connectors import DeliveryPayloadError

    report = _report()
    report["message"]["artifact"]["messages"][4]["secondsFromStart"] = 1e15

    with pytest.raises(DeliveryPayloadError):
        world.deliver(report)
    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(0,)]
