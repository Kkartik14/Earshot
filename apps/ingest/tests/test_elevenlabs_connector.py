"""ElevenLabs post-call webhooks become call evidence.

The conversation is the real one captured (``fixtures/captured/elevenlabs``:
scrubbed, both sides synthetic), wrapped in the documented webhook envelope and
signed with a test secret. The traces-only OTel event, ``call_initiation_failure``
and ``post_call_audio`` follow the documented shapes.
"""

from __future__ import annotations

import copy
import json

import pytest

from connector_support import (
    ROOT,
    START_SECONDS,
    ConnectorWorld,
    connector_world,
)
from earshot.call_capture import attribute_values, span_payload
from earshot.connectors import DeliveryTrustError

pytestmark = pytest.mark.integration

CAPTURE = ROOT / "fixtures" / "captured" / "elevenlabs"
CONVERSATION = json.loads((CAPTURE / "conversation.json").read_text())
OTEL = json.loads((CAPTURE / "conversation.otel.json").read_text())
CONVERSATION_ID = CONVERSATION["conversation_id"]
SECOND = 1_000_000_000
CALL_START = CONVERSATION["metadata"]["start_time_unix_secs"] * SECOND


def _event(event_type: str, data: dict, *, at: int = START_SECONDS) -> dict:
    return {"type": event_type, "event_timestamp": at, "data": data}


def _transcription(**changes) -> dict:
    return _event("post_call_transcription", {**copy.deepcopy(CONVERSATION), **changes})


def _otel_traces_only() -> dict:
    return _event(
        "post_call_transcription_otel",
        {
            "conversation_id": CONVERSATION_ID,
            "agent_id": OTEL["agent_id"],
            "otlp_traces": copy.deepcopy(OTEL["otlp_traces"]),
        },
    )


@pytest.fixture
def world(tmp_path) -> ConnectorWorld:
    return connector_world(tmp_path, "elevenlabs")


def _by_name(world: ConnectorWorld, name: str) -> list[dict]:
    return [
        attribute_values(span.span.attributes) for span in world.spans() if span.span.name == name
    ]


@pytest.mark.parametrize(("skew", "accepted"), [(-1799, True), (1799, True), (1801, False)])
def test_the_signature_window_is_thirty_minutes(world, skew, accepted) -> None:
    if accepted:
        assert world.deliver(_transcription(), at_seconds=START_SECONDS + skew).disposition
    else:
        with pytest.raises(DeliveryTrustError):
            world.deliver(_transcription(), at_seconds=START_SECONDS + skew)


def test_a_real_conversation_becomes_a_call_with_start_only_lines(world) -> None:
    outcome = world.deliver(_transcription())

    assert outcome.disposition == "applied"
    assert outcome.call_id == world.calls.resolve_call("support", f"elevenlabs:{CONVERSATION_ID}")
    [call] = _by_name(world, "earshot.connector.call")
    assert call["gen_ai.agent.name"] == "earshot-fixture-agent"
    assert call["earshot.connector.call.ended_reason"] == "Client disconnected: 1000"
    lines = sorted(
        (
            attributes["earshot.connector.transcript.index"],
            attributes["earshot.connector.transcript.speaker"],
            attributes["earshot.connector.transcript.timing"],
            attributes["earshot.connector.transcript.interrupted"],
        )
        for attributes in _by_name(world, "earshot.connector.transcript_line")
    )
    assert [line[1] for line in lines] == ["agent", "user"] * 4 + ["agent"]
    assert {line[2] for line in lines} == {"start_only"}
    assert [line[0] for line in lines if line[3]] == [4]
    starts = sorted(
        span.span.start_time_unix_nano - CALL_START
        for span in world.spans()
        if span.span.name == "earshot.connector.transcript_line"
    )
    assert starts == [entry["time_in_call_secs"] * SECOND for entry in CONVERSATION["transcript"]]
    turn = next(
        attributes
        for attributes in _by_name(world, "earshot.connector.turn")
        if attributes["earshot.connector.turn.index"] == 2
    )
    metrics = CONVERSATION["transcript"][2]["conversation_turn_metrics"]["metrics"]
    assert turn["earshot.connector.latency.llm_ms"] == pytest.approx(
        metrics["convai_llm_service_ttfb"]["elapsed_time"] * 1000
    )
    assert turn["earshot.connector.latency.turn_ms"] == pytest.approx(
        metrics["convai_ttf_audio_since_silence"]["elapsed_time"] * 1000
    )
    [cost] = _by_name(world, "earshot.connector.cost")
    assert (cost["earshot.connector.cost.amount"], cost["earshot.connector.cost.unit"]) == (
        386.0,
        "credit",
    )
    # The audio needs an API key: without one, no recording is held.
    assert world.fetches() == []


def test_with_an_api_key_the_audio_is_fetched_from_the_conversations_api(tmp_path) -> None:
    world = connector_world(
        tmp_path, "elevenlabs", config={"api_key_ref": "env:ELEVENLABS_API_KEY"}
    )

    world.deliver(_transcription())

    assert world.fetches() == [
        ("mixed", "audio/mpeg", "pending", "provider_api", "mixed", None, None, "pending")
    ]


def test_the_otel_event_with_the_conversation_writes_the_same_spans(world) -> None:
    world.deliver(_transcription())
    stored = world.rows("SELECT count(*) FROM spans")

    otel = world.deliver(_event("post_call_transcription_otel", copy.deepcopy(OTEL)))

    assert otel.disposition == "applied"
    assert world.rows("SELECT count(*) FROM spans") == stored
    assert world.counters()["span_conflicts"] == 0


def test_the_traces_only_otel_event_gives_the_same_call(tmp_path) -> None:
    plain = connector_world(tmp_path / "plain", "elevenlabs")
    traced = connector_world(tmp_path / "traced", "elevenlabs")

    plain.deliver(_transcription())
    traced.deliver(_otel_traces_only())

    def call(world):
        return next(
            span_payload(span.span)
            for span in world.spans()
            if span.span.name == "earshot.connector.call"
        )

    assert call(traced) == call(plain)
    assert len(_by_name(traced, "earshot.connector.transcript_line")) == 9
    assert {
        line["earshot.connector.transcript.timing"]
        for line in _by_name(traced, "earshot.connector.transcript_line")
    } == {"start_only"}


def test_a_failed_initiation_is_a_failed_call(world) -> None:
    outcome = world.deliver(
        _event(
            "call_initiation_failure",
            {
                "agent_id": CONVERSATION["agent_id"],
                "conversation_id": "conv_0000000000000000000000000002",
                "failure_reason": "busy",
                "metadata": {"type": "sip", "body": {"sip_status_code": 486}},
            },
        )
    )

    assert outcome.disposition == "applied"
    [call] = [span for span in world.spans() if span.span.name == "earshot.connector.call"]
    assert call.span.status.code == 2
    assert attribute_values(call.span.attributes)["earshot.connector.call.ended_reason"] == "busy"


def test_audio_within_the_cap_is_authenticated_then_ignored(world) -> None:
    outcome = world.deliver(
        _event(
            "post_call_audio",
            {"agent_id": "agent_x", "conversation_id": CONVERSATION_ID, "full_audio": "SUQz"},
        )
    )

    assert (outcome.disposition, outcome.receipt_id) == ("ignored", None)
    assert world.rows("SELECT count(*) FROM delivery_receipts") == [(0,)]


def test_a_retry_with_a_new_event_time_is_the_same_delivery(world) -> None:
    first = world.deliver(_transcription())
    retry = world.deliver(
        _event("post_call_transcription", copy.deepcopy(CONVERSATION), at=START_SECONDS + 60)
    )

    assert (retry.disposition, retry.receipt_id) == ("replayed", first.receipt_id)
