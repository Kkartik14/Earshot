"""Integrity of the real Twilio call capture.

One outbound-API call with ``Record=true`` and ``RecordingChannels=dual`` was polled
over the REST API (no webhooks). The fixture holds the scrubbed REST responses plus
recordings **regenerated** with the raw media's exact container, codec, channels and
frame count: the callee may be a person, so no captured audio is committed.
These tests pin the poll contract, the verified channel order, and that no secret,
number or SID from the raw capture reached the committed bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import struct
from array import array
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "fixtures" / "captured" / "twilio"
RAW = ROOT / ".earshot" / "raw-captures" / "twilio" / "call-run"
ENV_FILES = [ROOT / ".env", ROOT.parent / "TVIC" / ".env"]
SID = re.compile(r"^(AC|CA|RE|PN)0+[0-9a-f]{1,4}$")
PHONE_KEYS = {"to", "from", "forwarded_from", "to_formatted", "from_formatted", "caller_name"}
SID_KEYS = {"sid", "account_sid", "call_sid", "phone_number_sid", "parent_call_sid"}


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


def _committed_bytes() -> dict[str, bytes]:
    files = {
        str(path.relative_to(FIXTURE)): path.read_bytes()
        for path in sorted(FIXTURE.rglob("*"))
        if path.is_file()
    }
    assert "manifest.json" in files, f"no fixture at {FIXTURE}"
    return files


def _wav(name: str) -> tuple[list[tuple[str, int]], tuple[int, ...], array]:
    """RIFF chunk list, the ``fmt `` fields and the PCM16 samples of a WAV file."""
    data = (FIXTURE / name).read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
    chunks, fmt, samples, offset = [], (), array("h"), 12
    while offset < len(data):
        chunk_id, size = (
            data[offset : offset + 4].decode(),
            struct.unpack("<I", data[offset + 4 : offset + 8])[0],
        )
        body = data[offset + 8 : offset + 8 + size]
        chunks.append((chunk_id, size))
        if chunk_id == "fmt ":
            fmt = struct.unpack("<HHIIHH", body)
        elif chunk_id == "data":
            samples.frombytes(body)
        offset += 8 + size + (size & 1)
    return chunks, fmt, samples


def test_manifest_lists_every_committed_file_with_matching_digests() -> None:
    listed = {entry["file"]: entry for entry in _manifest()["files"]}
    assert set(listed) | {"manifest.json"} == set(_committed_bytes())
    for name, entry in listed.items():
        if entry["kind"] == "json":
            assert _canonical_sha256(_json(name)) == entry["canonical_sha256"], name
        else:
            assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == entry["sha256"]


def test_call_resource_carries_the_fields_the_poll_reads() -> None:
    call = _json("call.json")
    for row in _manifest()["field_table"]["call"]:
        assert type(call[row["field"]]).__name__ == row["type"], row["field"]
    assert SID.match(call["sid"]) and call["sid"].startswith("CA")
    assert SID.match(call["account_sid"]) and call["account_sid"].startswith("AC")
    assert (call["status"], call["direction"]) == ("completed", "outbound-api")
    start, end = parsedate_to_datetime(call["start_time"]), parsedate_to_datetime(call["end_time"])
    assert (end - start).total_seconds() == int(call["duration"])
    assert float(call["price"]) < 0 and call["price_unit"] == "USD"
    for key in ("to", "from"):
        assert call[key].startswith("+1555"), key
    create = _json("create_call.json")
    assert (create["sid"], create["status"]) == (call["sid"], "queued")


def test_recordings_list_and_resource_describe_one_dual_recording() -> None:
    call = _json("call.json")
    (listed,) = _json("call_recordings.json")["recordings"]
    (by_call_sid,) = _json("recordings_by_callsid.json")["recordings"]
    recording = _json("recording.json")
    for row in _manifest()["field_table"]["recording"]:
        assert type(recording[row["field"]]).__name__ == row["type"], row["field"]
    assert listed == by_call_sid == recording
    assert SID.match(recording["sid"]) and recording["sid"].startswith("RE")
    assert recording["call_sid"] == call["sid"]
    assert (recording["channels"], recording["source"], recording["status"]) == (
        2,
        "OutboundAPI",
        "completed",
    )
    assert recording["media_url"].endswith(f"/Recordings/{recording['sid']}")
    started = parsedate_to_datetime(recording["start_time"])
    assert (
        parsedate_to_datetime(call["start_time"])
        <= started
        <= parsedate_to_datetime(call["end_time"])
    )
    (event,) = _json("call_events.json")["events"]
    parameters = event["request"]["parameters"]
    assert (parameters["record"], parameters["recording_channels"]) == ("true", "dual")
    assert (parameters["recording_track"], parameters["trim"]) == ("both", "trim-silence")


def test_recordings_keep_the_raw_container_codec_channels_and_length() -> None:
    audio = _manifest()["audio"]
    assert audio["provenance"] == "regenerated_synthetic"
    for track, channels in (("dual", 2), ("mono", 1)):
        spec = audio[track]
        chunks, fmt, samples = _wav(spec["file"])
        assert [chunk_id for chunk_id, _ in chunks] == ["fmt ", "data"]
        assert fmt == (1, channels, 8_000, 16_000 * channels, 2 * channels, 16)
        assert len(samples) // channels == spec["frames"] == audio["raw_frames"]
        assert spec["channels"] == channels
    seconds = audio["raw_frames"] / 8_000
    assert abs(seconds - int(_json("recording.json")["duration"])) <= 1


def test_channel_order_matches_the_verified_capture() -> None:
    audio = _manifest()["audio"]
    order = audio["channel_order"]
    assert order["0"]["side"] == "callee" and order["1"]["side"] == "twilio_generated"
    _, _, dual = _wav(audio["dual"]["file"])
    callee, twilio = dual[0::2], dual[1::2]
    assert not any(callee), "the callee channel is regenerated as digital silence"
    rate = 8_000
    outside = list(twilio)
    for start_s, end_s in order["1"]["speech_segments_s"]:
        begin, end = int(start_s * rate), int(end_s * rate)
        assert max(abs(sample) for sample in twilio[begin:end]) > 1_000, (start_s, end_s)
        outside[begin:end] = [0] * (end - begin)
    assert not any(outside), "speech only inside the raw channel-1 speech windows"
    _, _, mono = _wav(audio["mono"]["file"])
    assert mono == twilio, "mono is the mix of both channels"


def test_fixture_bytes_break_no_leak_rule() -> None:
    rules = _leak_rules()
    for name, data in _committed_bytes().items():
        assert rules.find_leaks(data) == [], name


def test_fixture_bytes_contain_no_env_secret() -> None:
    rules = _leak_rules()
    secrets = rules.env_secret_values(ENV_FILES)
    secrets.update({k: v for k, v in os.environ.items() if k.startswith("TWILIO_") and len(v) >= 8})
    if not secrets:
        pytest.skip("no env file with provider secrets on this machine")
    prefixes = rules.sha256_prefixes(secrets)
    for name, data in _committed_bytes().items():
        for key, value in secrets.items():
            for variant in rules.secret_variants(value):
                assert variant.encode() not in data, f"{key} leaked into {name}"
            assert prefixes[key].encode() not in data, f"sha256 of {key} in {name}"


def _raw_sensitive_values() -> set[str]:
    """Numbers, names and SIDs in the raw capture, by key and by shape."""
    rules = _leak_rules()
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PHONE_KEYS | SID_KEYS and isinstance(value, str) and len(value) >= 8:
                    found.update(rules.secret_variants(value))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in RAW.glob("*.json"):
        text = path.read_text()
        walk(json.loads(text))
        found |= set(re.findall(r"\b(?:AC|CA|RE|PN)[0-9a-f]{32}\b", text))
        for number in re.findall(r"\+\d{10,15}", text):
            found |= rules.secret_variants(number)
    return found


def test_fixture_bytes_contain_no_raw_capture_value() -> None:
    if not (RAW / "call.json").is_file():
        pytest.skip("raw capture is local-only and absent on this machine")
    values = _raw_sensitive_values()
    assert len(values) >= 6
    for name, data in _committed_bytes().items():
        leaked = sum(value.encode() in data for value in values)
        assert leaked == 0, f"{leaked} raw value(s) in {name}"
