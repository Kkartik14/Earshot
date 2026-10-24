"""Retell ``call_ended`` and ``call_analyzed`` deliveries become call evidence.

Verified on documented payloads only: the call object below follows Retell's API
reference (``V2CallBase``) and webhook overview; no real Retell delivery has been
captured. Both events describe one call, so the facts they share are written once,
and the analysis, cost and latency only ``call_analyzed`` carries are new evidence
of their own, never a span conflict.
"""

from __future__ import annotations

import copy

import pytest

from connector_support import (
    START,
    START_SECONDS,
    ConnectorWorld,
    body_of,
    connector_world,
    signed_headers,
)
from earshot.call_capture import attribute_values
from earshot.connectors import DeliveryTooLargeError, DeliveryTrustError, RawProviderDelivery

pytestmark = pytest.mark.integration

CALL_ID = "Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6"


def _call(event: str) -> dict:
    call = {
        "call_type": "web_call",
        "call_id": CALL_ID,
        "agent_id": "oBeDLoLOeuAbiuaMFXRtDOLriTJ5tSxD",
        "agent_name": "Clinic receptionist",
        "agent_version": 3,
        "call_status": "ended",
        "start_timestamp": START // 1_000_000,
        "end_timestamp": START // 1_000_000 + 15_791,
        "duration_ms": 15_791,
        "disconnection_reason": "user_hangup",
        "transcript": "Agent: Hi, Green Valley Clinic.\nUser: I need to move my visit.\n",
        "transcript_object": [
            {
                "role": "agent",
                "content": "Hi, Green Valley Clinic.",
                "words": [
                    {"word": "Hi,", "start": 0.7, "end": 1.0},
                    {"word": "Clinic.", "start": 1.9, "end": 2.3},
                ],
            },
            {
                "role": "user",
                "content": "I need to move my visit.",
                "words": [
                    {"word": "I", "start": 3.4, "end": 3.5},
                    {"word": "visit.", "start": 4.6, "end": 5.1},
                ],
            },
        ],
        "recording_url": "https://dxc03zgurdly9.cloudfront.net/Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6/recording.wav",
        "public_log_url": "https://dxc03zgurdly9.cloudfront.net/Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6/public.log",
        "latency": {
            "e2e": {"p50": 800, "max": 950, "min": 640, "num": 3, "values": [640, 800, 950]},
            "llm": {"p50": 400, "values": [380, 400, 450]},
            "tts": {"p50": 200, "values": [190, 200, 230]},
        },
        "call_cost": {
            "product_costs": [{"product": "elevenlabs_tts", "unit_price": 1, "cost": 60}],
            "total_duration_seconds": 16,
            "total_duration_unit_price": 1,
            "combined_cost": 70,
        },
        "data_storage_setting": "everything",
    }
    if event == "call_analyzed":
        call["call_analysis"] = {
            "call_summary": "The caller asked to move a clinic visit.",
            "user_sentiment": "Positive",
            "call_successful": True,
            "in_voicemail": False,
            "custom_analysis_data": {},
        }
    return {"event": event, "call": call}


@pytest.fixture
def world(tmp_path) -> ConnectorWorld:
    return connector_world(tmp_path, "retell")


def _names(world: ConnectorWorld) -> dict[str, int]:
    names: dict[str, int] = {}
    for name in world.span_names():
        names[name] = names.get(name, 0) + 1
    return names


@pytest.mark.parametrize(("skew", "accepted"), [(-299, True), (299, True), (301, False)])
def test_the_signature_window_is_five_minutes(world, skew, accepted) -> None:
    if accepted:
        assert world.deliver(_call("call_ended"), at_seconds=START_SECONDS + skew).disposition
    else:
        with pytest.raises(DeliveryTrustError):
            world.deliver(_call("call_ended"), at_seconds=START_SECONDS + skew)


def test_a_body_that_does_not_match_its_signature_is_refused(world) -> None:
    raw = body_of(_call("call_ended"))
    headers = signed_headers("retell", raw)

    with pytest.raises(DeliveryTrustError):
        world.ingestion.receive(RawProviderDelivery(world.endpoint_id, headers, raw + b" "))


def test_a_body_over_the_cap_is_refused_unread(tmp_path) -> None:
    world = connector_world(tmp_path, "retell", max_body_bytes=1024)

    with pytest.raises(DeliveryTooLargeError):
        world.deliver(_call("call_ended"))


@pytest.mark.parametrize("event", ["call_started", "transfer_started", "transcript_updated"])
def test_events_that_end_nothing_are_ignored(world, event) -> None:
    outcome = world.deliver({"event": event, "call": {"call_id": CALL_ID}})

    assert (outcome.disposition, outcome.receipt_id, outcome.call_id) == ("ignored", None, None)
    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(0,)]


def test_call_ended_holds_the_shared_facts_and_its_recording(world) -> None:
    outcome = world.deliver(_call("call_ended"))

    assert outcome.call_id == world.calls.resolve_call("support", f"retell:{CALL_ID}")
    assert _names(world) == {
        "earshot.connector.call": 1,
        "earshot.connector.transcript_line": 2,
        "earshot.connector.translator": 1,
    }
    lines = {
        attribute_values(span.span.attributes)["earshot.connector.transcript.index"]: span.span
        for span in world.spans()
        if span.span.name == "earshot.connector.transcript_line"
    }
    assert (lines[1].start_time_unix_nano, lines[1].end_time_unix_nano) == (
        START + 3_400_000_000,
        START + 5_100_000_000,
    )
    assert [(row[0], row[3], row[4]) for row in world.fetches()] == [
        ("mixed", "payload_url", "mixed")
    ]


def test_call_analyzed_adds_its_own_facts_without_a_conflict(world) -> None:
    ended = world.deliver(_call("call_ended"))
    analyzed = world.deliver(_call("call_analyzed"))

    assert analyzed.disposition == "applied"
    assert analyzed.call_id == ended.call_id
    assert analyzed.receipt_id != ended.receipt_id
    assert world.counters()["span_conflicts"] == 0
    assert _names(world) == {
        "earshot.connector.analysis": 1,
        "earshot.connector.call": 1,
        "earshot.connector.cost": 1,
        "earshot.connector.latency": 3,
        "earshot.connector.transcript_line": 2,
        "earshot.connector.translator": 1,
        "earshot.connector.turn": 3,
    }
    analysis = next(
        attribute_values(span.span.attributes)
        for span in world.spans()
        if span.span.name == "earshot.connector.analysis"
    )
    assert analysis["earshot.connector.analysis.successful"] == "true"
    assert analysis["earshot.connector.analysis.summary_message"].startswith("The caller")
    cost = next(
        attribute_values(span.span.attributes)
        for span in world.spans()
        if span.span.name == "earshot.connector.cost"
    )
    assert (cost["earshot.connector.cost.amount"], cost["earshot.connector.cost.unit"]) == (
        70.0,
        "usd_cent",
    )
    assert len(world.fetches()) == 1  # the same recording: no second row, no second fetch


def test_an_error_call_is_marked_failed(world) -> None:
    payload = copy.deepcopy(_call("call_ended"))
    payload["call"]["call_status"] = "error"
    payload["call"]["disconnection_reason"] = "error_llm_websocket_open"

    world.deliver(payload)

    [call] = [span for span in world.spans() if span.span.name == "earshot.connector.call"]
    assert call.span.status.code == 2
