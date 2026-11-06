"""Synthetic examples for the host-neutral outcome mapping in docs/calls.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from earshot.host_profile import check_host_profile

FIXTURES = Path(__file__).parent / "fixtures" / "e8"
CASES = (
    "barge-in-cancelled.json",
    "policy-allow-graceful.json",
    "failed-vs-cancelled.json",
    "stage-timing-no-inferred-stall.json",
)
PAYLOAD_KEYS = {
    "earshot.transcript.text",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.prompt",
    "gen_ai.completion",
    "audio.content",
    "recording.content",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _spans(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        span
        for resource in case["request"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        if scope["scope"]["name"] == "earshot.host"
        for span in scope["spans"]
    ]


def _turn_id(span: dict[str, Any]) -> str | None:
    for attribute in span.get("attributes", []):
        if attribute.get("key") == "earshot.turn.id":
            return attribute["value"].get("stringValue")
    return None


def _attribute_keys(case: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for resource in case["request"]["resourceSpans"]:
        keys.update(
            attribute["key"] for attribute in resource.get("resource", {}).get("attributes", [])
        )
        for scope in resource["scopeSpans"]:
            keys.update(
                attribute["key"] for attribute in scope.get("scope", {}).get("attributes", [])
            )
            for span in scope["spans"]:
                keys.update(attribute["key"] for attribute in span.get("attributes", []))
                for event in span.get("events", []):
                    keys.update(attribute["key"] for attribute in event.get("attributes", []))
    return keys


@pytest.mark.parametrize("name", CASES)
def test_each_e8_case_is_valid_profile_0_2_and_payload_free(name: str) -> None:
    case = _load(name)
    report = check_host_profile(case["request"])

    assert report.valid, (name, report.as_dict())
    assert report.host_profile_versions == ("0.2.0",)
    assert all(span["name"] in {"turn", "llm", "tts"} for span in _spans(case))
    assert not _attribute_keys(case).intersection(PAYLOAD_KEYS), name


def test_barge_in_and_cancellation_remain_separate_observations() -> None:
    by_turn = {_turn_id(span): span for span in _spans(_load(CASES[0]))}

    assert {event["name"] for event in by_turn["turn-accepted"]["events"]} == {
        "earshot.interruption.accepted",
        "earshot.turn.cancelled",
    }
    assert {event["name"] for event in by_turn["turn-unobserved"]["events"]} == {
        "earshot.turn.cancelled"
    }


def test_allow_and_graceful_modes_map_the_same_observed_event() -> None:
    case = _load(CASES[1])
    modes = case["metadata"]["source_configuration"]
    by_turn = {_turn_id(span): span for span in _spans(case)}

    assert modes == {
        "turn-allow": {"interruption_policy_mode": "allow"},
        "turn-graceful": {"interruption_policy_mode": "graceful"},
    }
    assert {
        turn_id: {event["name"] for event in span["events"]} for turn_id, span in by_turn.items()
    } == {
        "turn-allow": {"earshot.interruption.accepted"},
        "turn-graceful": {"earshot.interruption.accepted"},
    }
    assert all(
        "interruption_policy_mode"
        not in {attribute["key"] for attribute in span.get("attributes", [])}
        for span in by_turn.values()
    )


def test_failed_and_cancelled_turns_use_different_direct_evidence() -> None:
    by_turn = {_turn_id(span): span for span in _spans(_load(CASES[2]))}

    assert by_turn["turn-failed"]["status"]["code"] == 2
    assert "events" not in by_turn["turn-failed"]
    assert by_turn["turn-cancelled"]["status"]["code"] == 0
    assert [event["name"] for event in by_turn["turn-cancelled"]["events"]] == [
        "earshot.turn.cancelled"
    ]


def test_stage_timing_uses_stage_windows_without_inventing_a_stall() -> None:
    by_name = {span["name"]: span for span in _spans(_load(CASES[3]))}

    assert (
        int(by_name["llm"]["endTimeUnixNano"]) - int(by_name["llm"]["startTimeUnixNano"])
        == 5_000_000_000
    )
    assert by_name["llm"]["status"]["code"] == 0
    assert (
        int(by_name["tts"]["endTimeUnixNano"]) - int(by_name["tts"]["startTimeUnixNano"])
        == 500_000_000
    )
    assert by_name["tts"]["status"]["code"] == 2
    assert all(
        "stall" not in attribute["key"].lower()
        for span in by_name.values()
        for attribute in span.get("attributes", [])
    )
