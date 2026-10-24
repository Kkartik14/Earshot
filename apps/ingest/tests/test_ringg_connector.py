"""Ringg ``all_processing_completed`` deliveries become call evidence.

Verified on documented payloads only: the delivery below follows Ringg's webhook
field reference and call-event examples (``docs.ringg.ai``); no real Ringg delivery
has been captured.
"""

from __future__ import annotations

import pytest

from connector_support import START, ConnectorWorld, body_of, connector_world
from earshot.call_capture import attribute_values
from earshot.connectors import DeliveryTrustError, RawProviderDelivery

pytestmark = pytest.mark.integration

CALL_ID = "00000000-0000-4000-8000-0000000000c4"


def _final() -> dict:
    return {
        "event_type": "all_processing_completed",
        "call_id": CALL_ID,
        "parent_call_id": None,
        "call_sid": "CA00000000000000000000000000000001",
        "agent_id": "00000000-0000-4000-8000-0000000000a1",
        "workspace_id": "00000000-0000-4000-8000-0000000000b1",
        "agent_name": "EMI reminder",
        "status": "completed",
        "sub_status": "ACCEPTED",
        "call_type": "outbound",
        "call_duration": 15.73,
        "called_on": "2026-10-20T10:00:00.000000Z",
        "created_at": "2026-10-20T09:59:59.000000Z",
        "to_number": "+15550100002",
        "from_number": "+15550100003",
        "call_cost": 0.15,
        "overall_latency_seconds": 0.892,
        "first_utterance_seconds": 0.456,
        "transcript": [
            {"bot": "Hello, this is a payment reminder."},
            {"user": "Yes, I paid yesterday."},
            {"bot": "Thank you, I have noted that."},
        ],
        "recording_url": "https://recordings.ringg.example/00000000-c4.mp3",
        "platform_analysis": {
            "summary": "The caller confirmed the payment.",
            "classification": "payment_confirmed",
            "status": "analyzed",
        },
        "platform_analysis_status": "success",
        "tool_call_logs": [],
    }


@pytest.fixture
def world(tmp_path) -> ConnectorWorld:
    return connector_world(tmp_path, "ringg")


@pytest.mark.parametrize(
    "header",
    [(b"authorization", b"Bearer ringg-test-secret"), (b"x-webhook-secret", b"ringg-test-secret")],
)
def test_either_configured_header_authenticates(world, header) -> None:
    outcome = world.ingestion.receive(
        RawProviderDelivery(
            world.endpoint_id, ((b"content-type", b"application/json"), header), body_of(_final())
        )
    )

    assert outcome.disposition == "applied"


def test_a_wrong_secret_is_refused(world) -> None:
    with pytest.raises(DeliveryTrustError):
        world.ingestion.receive(
            RawProviderDelivery(
                world.endpoint_id, ((b"x-webhook-secret", b"wrong"),), body_of(_final())
            )
        )


@pytest.mark.parametrize(
    "event", ["call_started", "call_completed", "recording_completed", "client_analysis_completed"]
)
def test_progress_events_are_ignored(world, event) -> None:
    assert world.deliver({"event_type": event, "call_id": CALL_ID}).disposition == "ignored"
    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(0,)]


def test_the_final_event_becomes_a_call_with_order_only_lines(world) -> None:
    outcome = world.deliver(_final())

    assert outcome.call_id == world.calls.resolve_call("support", f"ringg:{CALL_ID}")
    spans = world.spans()
    lines = sorted(
        (
            attribute_values(span.span.attributes)["earshot.connector.transcript.index"],
            attribute_values(span.span.attributes)["earshot.connector.transcript.speaker"],
            attribute_values(span.span.attributes)["earshot.connector.transcript.timing"],
            span.span.start_time_unix_nano,
        )
        for span in spans
        if span.span.name == "earshot.connector.transcript_line"
    )
    assert lines == [
        (0, "agent", "order_only", START),
        (1, "user", "order_only", START),
        (2, "agent", "order_only", START),
    ]
    latencies = sorted(
        (
            attribute_values(span.span.attributes)["earshot.connector.latency.component"],
            attribute_values(span.span.attributes)["earshot.connector.latency.values_ms"],
        )
        for span in spans
        if span.span.name == "earshot.connector.latency"
    )
    assert latencies == [("first_utterance", [456.0]), ("overall", [892.0])]
    assert not [span for span in spans if span.span.name == "earshot.connector.turn"]
    [call] = [span for span in spans if span.span.name == "earshot.connector.call"]
    assert call.span.end_time_unix_nano - call.span.start_time_unix_nano == 15_730_000_000
    assert [(row[0], row[1], row[3]) for row in world.fetches()] == [
        ("mixed", "audio/mpeg", "payload_url")
    ]
