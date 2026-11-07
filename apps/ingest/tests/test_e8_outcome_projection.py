"""Run E-8 evidence through Earshot's receiver, close path, and projector."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from projection_job_support import World

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages/sdk-python/tests/fixtures/e8"
CASES = (
    "barge-in-cancelled.json",
    "policy-allow-graceful.json",
    "failed-vs-cancelled.json",
    "stage-timing-no-inferred-stall.json",
)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _turn_spans(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        span
        for resource in case["request"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        if scope["scope"]["name"] == "earshot.host"
        for span in scope["spans"]
        if span["name"] == "turn"
    ]


def _attribute(span: dict[str, Any], key: str) -> Any:
    return next((item["value"] for item in span.get("attributes", []) if item["key"] == key), {})


def _source_facts(case: dict[str, Any]) -> dict[str, Any]:
    spans = [
        span
        for resource in case["request"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    facts = {}
    for turn in _turn_spans(case):
        turn_id = _attribute(turn, "earshot.turn.id")["stringValue"]
        item = {
            "status": {0: "unset", 1: "ok", 2: "error"}[turn.get("status", {}).get("code", 0)],
            "events": sorted(event["name"] for event in turn.get("events", [])),
        }
        stages = {
            span["name"]: {
                "duration_ms": (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]))
                // 1_000_000,
                "status": {0: "unset", 1: "ok", 2: "error"}[span.get("status", {}).get("code", 0)],
            }
            for span in spans
            if span.get("parentSpanId") == turn["spanId"] and span["name"] in {"stt", "llm", "tts"}
        }
        if stages:
            item["stages"] = stages
        facts[turn_id] = item
    return facts


def _as_proto_json(case: dict[str, Any]) -> dict[str, Any]:
    """The fixtures use OTLP's hex IDs; protobuf JSON expects base64 for bytes."""

    request = json.loads(json.dumps(case["request"]))
    for resource in request["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode("ascii")
    return request


def _contains_stall(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            "stall" in str(key).lower() or _contains_stall(child) for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_stall(item) for item in value)
    return isinstance(value, str) and "stall" in value.lower()


@pytest.mark.parametrize("name", CASES)
def test_e8_fixture_is_received_closed_and_projected(tmp_path: Path, name: str) -> None:
    case = _load(name)
    source_facts = _source_facts(case)
    assert case["metadata"]["source_facts"] == source_facts, name
    request = ParseDict(_as_proto_json(case), ExportTraceServiceRequest())

    world = World(tmp_path)
    call_id = world.replay([request])
    source_turns = _turn_spans(case)
    for source in source_turns:
        stored = world.reads.get_span(
            "default",
            call_id,
            bytes.fromhex(source["traceId"]),
            bytes.fromhex(source["spanId"]),
        )
        assert stored is not None, (name, source["spanId"])
        assert stored.span.status.code == source.get("status", {}).get("code", 0), name

    world.close()
    world.project()
    projection = world.projection(call_id)
    expected_outcome = (
        "error"
        if any(
            fact["status"] == "error"
            or any(stage["status"] == "error" for stage in fact.get("stages", {}).values())
            for fact in source_facts.values()
        )
        else "completed"
    )

    assert projection["state"] == "current", name
    assert projection["summary"].outcome == expected_outcome, name
    assert len(projection["detail"]["turns"]) == len(source_turns), name
    assert not _contains_stall(projection["detail"]), name

    projected_turn_ids = {
        turn["turn_id"] for turn in projection["detail"]["turns"] if turn["turn_id"] is not None
    }
    source_turn_ids = {
        attribute["value"]["stringValue"]
        for span in source_turns
        for attribute in span["attributes"]
        if attribute["key"] == "earshot.turn.id"
    }
    assert projected_turn_ids == source_turn_ids, name

    projected_by_id = {
        turn["turn_id"]: turn
        for turn in projection["detail"]["turns"]
        if turn["turn_id"] is not None
    }
    for turn_id, facts in source_facts.items():
        # Call turns expose stage-span statuses, while root-turn ERROR contributes
        # to the call outcome above rather than a separate turn lifecycle field.
        projected_stage_statuses = {
            span["name"]: span["status"] for span in projected_by_id[turn_id]["spans"]
        }
        expected_stage_statuses = {
            stage_name: stage["status"] for stage_name, stage in facts.get("stages", {}).items()
        }
        assert projected_stage_statuses == expected_stage_statuses, name
