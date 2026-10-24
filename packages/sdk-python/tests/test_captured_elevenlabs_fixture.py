"""Integrity of the real ElevenLabs Agents conversation capture.

The fixture is one real conversation fetched with the conversations API
(``GET /v1/convai/conversations/{id}``, its ``?format=opentelemetry`` variant and
``/audio``), scrubbed by ``scripts/scrub_provider_capture.py``. Both sides of the call
are synthetic speech, so the audio is committed as captured. These tests pin the facts
the conversation import and the ``start_only`` transcript rule rely on, and prove that no secret
or identifier from the raw capture reached the committed bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "fixtures" / "captured" / "elevenlabs"
RAW = ROOT / ".earshot" / "raw-captures" / "elevenlabs" / "conversation-run"
ENV_FILES = [ROOT / ".env", ROOT.parent / "TVIC" / ".env"]
ID_KEYS = {"agent_id", "conversation_id", "branch_id", "version_id", "voice_id", "user_id"}
# MPEG-2 Layer III tables (ISO/IEC 13818-3): bitrate index -> kbps, rate index -> Hz.
MPEG2_L3_KBPS = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
MPEG2_RATES = (22050, 24000, 16000)


def _leak_rules() -> Any:
    spec = importlib.util.spec_from_file_location(
        "provider_capture", ROOT / "scripts" / "provider_capture.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURE / "manifest.json").read_text())


def _json(name: str) -> Any:
    return json.loads((FIXTURE / name).read_text())


def _canonical_sha256(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _resolve(document: Any, path: str) -> Any:
    """Follow a dotted path; ``[]`` steps into the first element of a list."""
    node = document
    for part in path.split("."):
        node = node[part[:-2]][0] if part.endswith("[]") else node[part]
    return node


def _attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in items}


def _committed_bytes() -> dict[str, bytes]:
    files = {
        str(path.relative_to(FIXTURE)): path.read_bytes()
        for path in sorted(FIXTURE.rglob("*"))
        if path.is_file()
    }
    assert "manifest.json" in files, f"no fixture at {FIXTURE}"
    return files


def _mp3_frames(data: bytes) -> list[tuple[int, int, int, int, int]]:
    """(version, layer, kbps, rate, channel_mode) of every frame after an ID3v2 tag."""
    offset = 0
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        offset = 10 + size
    frames = []
    while offset < len(data):
        header = int.from_bytes(data[offset : offset + 4], "big")
        assert header >> 21 == 0x7FF, f"lost MPEG frame sync at byte {offset}"
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        assert (version, layer) == (2, 1), "expected MPEG-2 Layer III"
        kbps = MPEG2_L3_KBPS[(header >> 12) & 15]
        rate = MPEG2_RATES[(header >> 10) & 3]
        padding = (header >> 9) & 1
        frames.append((version, layer, kbps, rate, (header >> 6) & 3))
        offset += 72_000 * kbps // rate + padding
    assert offset == len(data), "trailing bytes after the last MPEG frame"
    return frames


def test_manifest_lists_every_committed_file_with_matching_digests() -> None:
    manifest = _manifest()
    listed = {entry["file"]: entry for entry in manifest["files"]}
    assert set(listed) | {"manifest.json"} == set(_committed_bytes())
    for name, entry in listed.items():
        if entry["kind"] == "json":
            assert _canonical_sha256(_json(name)) == entry["canonical_sha256"], name
        else:
            assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == entry["sha256"]


def test_conversation_carries_the_fields_the_import_reads() -> None:
    conversation = _json("conversation.json")
    manifest = _manifest()
    for row in manifest["field_table"]:
        value = _resolve(conversation, row["path"])
        assert type(value).__name__ == row["type"], row["path"]
    assert conversation["status"] == "done"
    assert conversation["has_audio"] is True
    assert conversation["has_user_audio"] is True
    assert conversation["has_response_audio"] is True
    metadata = conversation["metadata"]
    assert metadata["accepted_time_unix_secs"] >= metadata["start_time_unix_secs"]
    assert metadata["phone_call"] is None
    charging = metadata["charging"]
    assert metadata["cost"] == charging["llm_charge"] + charging["call_charge"]
    assert metadata["cost_fiat"] > 0


def test_turns_are_whole_second_starts_with_no_end_time() -> None:
    turns = _json("conversation.json")["transcript"]
    assert {turn["role"] for turn in turns} == {"agent", "user"}
    starts = [turn["time_in_call_secs"] for turn in turns]
    assert all(type(start) is int for start in starts)
    assert starts == sorted(starts)
    for turn in turns:
        timing_keys = [k for k in turn if "time" in k or "secs" in k or "duration" in k]
        assert timing_keys == ["time_in_call_secs"], timing_keys
    interrupted = [turn for turn in turns if turn["interrupted"]]
    assert [turn["role"] for turn in interrupted] == ["agent"]
    assert _manifest()["timing"]["semantics"] == "start_only"


def test_otel_variant_derives_each_end_from_the_next_start() -> None:
    conversation = _json("conversation.json")
    otel = _json("conversation.otel.json")
    assert otel["transcript"] == conversation["transcript"]
    (resource_spans,) = otel["otlp_traces"]["resourceSpans"]
    (scope_spans,) = resource_spans["scopeSpans"]
    assert scope_spans["scope"]["name"] == "elevenlabs.convai"
    resource = _attributes(resource_spans["resource"]["attributes"])
    assert resource["elevenlabs.conversation_id"] == conversation["conversation_id"]
    assert resource["elevenlabs.agent_id"] == conversation["agent_id"]
    (root,) = [s for s in scope_spans["spans"] if s["name"] == "elevenlabs.conversation"]
    root_start, root_end = int(root["startTimeUnixNano"]), int(root["endTimeUnixNano"])
    assert root_start == conversation["metadata"]["start_time_unix_secs"] * 10**9
    turns = [s for s in scope_spans["spans"] if s is not root]
    assert len(turns) == len(conversation["transcript"])
    for index, span in enumerate(turns):
        turn = conversation["transcript"][index]
        assert span["parentSpanId"] == root["spanId"]
        event = "agent_response" if turn["role"] == "agent" else "user_transcript"
        assert span["name"] == f"elevenlabs.recv.{event}"
        assert int(_attributes(span["attributes"])["elevenlabs.turn.index"]) == index
        start = int(span["startTimeUnixNano"])
        assert start - root_start == turn["time_in_call_secs"] * 10**9
        following = [int(s["startTimeUnixNano"]) for s in turns[index + 1 :]]
        derived_end = following[0] if following else root_end
        if not turn["interrupted"]:
            assert int(span["endTimeUnixNano"]) == derived_end


def test_audio_is_mono_16k_mp3_covering_the_call() -> None:
    audio = _manifest()["audio"]
    data = (FIXTURE / audio["file"]).read_bytes()
    frames = _mp3_frames(data)
    assert {frame[2:] for frame in frames} == {(128, 16_000, 3)}, "128 kbps, 16 kHz, mono"
    duration = len(frames) * 576 / 16_000
    assert duration == pytest.approx(audio["duration_s"], abs=1e-6)
    assert (audio["codec"], audio["channels"], audio["sample_rate"]) == ("mp3", 1, 16_000)
    call_secs = _json("conversation.json")["metadata"]["call_duration_secs"]
    assert abs(duration - call_secs) < 1.5
    assert audio["provenance"] == "synthetic_both_sides"


def test_identifiers_are_consistent_placeholders() -> None:
    conversation = _json("conversation.json")
    variables = conversation["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["system__conversation_id"] == conversation["conversation_id"]
    assert variables["system__agent_id"] == conversation["agent_id"]
    assert conversation["visited_agents"][0]["agent_id"] == conversation["agent_id"]
    for turn in conversation["transcript"]:
        if turn["agent_metadata"]:
            assert turn["agent_metadata"]["agent_id"] == conversation["agent_id"]
    placeholder = re.compile(r"^[a-z]+_0+[0-9a-f]{1,4}$")
    for key in ("agent_id", "conversation_id", "branch_id", "version_id"):
        assert placeholder.match(conversation[key]), key


def test_fixture_bytes_break_no_leak_rule() -> None:
    rules = _leak_rules()
    for name, data in _committed_bytes().items():
        assert rules.find_leaks(data) == [], name


def test_fixture_bytes_contain_no_env_secret() -> None:
    rules = _leak_rules()
    secrets = rules.env_secret_values(ENV_FILES)
    secrets.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY") and len(v) >= 8})
    if not secrets:
        pytest.skip("no env file with provider secrets on this machine")
    prefixes = rules.sha256_prefixes(secrets)
    for name, data in _committed_bytes().items():
        for key, value in secrets.items():
            for variant in rules.secret_variants(value):
                assert variant.encode() not in data, f"{key} leaked into {name}"
            assert prefixes[key].encode() not in data, f"sha256 of {key} in {name}"


def _raw_identifiers() -> set[str]:
    """Every identifier in the raw capture: ID-keyed values and provider ID shapes."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_KEYS and isinstance(value, str) and len(value) >= 8:
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in RAW.glob("*.json"):
        text = path.read_text()
        walk(json.loads(text))
        found |= set(re.findall(r"\b(?:agent|conv|agtbrch|agtvrsn|user)_[0-9a-z]{20,}\b", text))
    return found


def test_fixture_bytes_contain_no_raw_capture_identifier() -> None:
    if not (RAW / "conversation.json").is_file():
        pytest.skip("raw capture is local-only and absent on this machine")
    identifiers = _raw_identifiers()
    assert len(identifiers) >= 4
    for name, data in _committed_bytes().items():
        leaked = sum(identifier.encode() in data for identifier in identifiers)
        assert leaked == 0, f"{leaked} raw identifier(s) in {name}"
