"""Scrub a raw hosted-provider capture into ``fixtures/captured/<provider>/`` (M3 WS0, T4).

    python scripts/scrub_provider_capture.py elevenlabs
    python scripts/scrub_provider_capture.py twilio

Reads ``.earshot/raw-captures/<provider>/`` (git-ignored) and writes the scrubbed REST
responses, the audio and ``manifest.json``. The build is deterministic and **refuses to
write** if any leak rule in ``provider_capture.find_leaks`` fires or any env secret or
raw identifier survives. It prints counts only, never a raw value.

Identifiers are replaced by format-valid placeholders (same prefix and length, body of
zeros plus a counter) in first-seen order, everywhere they occur: fields, URIs and
JSON-in-a-string bodies. Phone numbers become ``+1555010000N``.

Audio (T4(a)):
* ElevenLabs: both sides were synthetic (ElevenLabs agent TTS, Cartesia caller), so
  the MP3 is committed as fetched.
* Twilio: the callee may be a person, so the recordings are regenerated with the raw
  media's container, codec, channels and frame count: digital silence on the callee
  channel, synthetic speech (cut from the committed Cartesia track
  ``fixtures/captured/pipecat-otlp-1.5/audio/user.wav``) in the raw speech windows of
  the Twilio-generated channel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_capture import (
    DEFAULT_ENV,
    RAW_ROOT,
    ROOT,
    env_secret_values,
    find_leaks,
    secret_variants,
    sha256_prefixes,
)

FIXTURES = ROOT / "fixtures" / "captured"
SYNTHETIC_SOURCE = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio" / "user.wav"
ENV_FILES = [ROOT / ".env", DEFAULT_ENV]
CAPTURED_ON = "2026-10-19"
VAD_WINDOW_S, VAD_RMS, VAD_HANG = 0.05, 200.0, 6
# MPEG-2 Layer III (ISO/IEC 13818-3): bitrate index -> kbps, sample-rate index -> Hz.
MPEG2_L3_KBPS = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
MPEG2_RATES = (22050, 24000, 16000)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(text.encode())


def _script_digest(name: str) -> str:
    return _sha256((ROOT / "scripts" / name).read_bytes())


class Pseudonyms:
    """Raw value -> placeholder, assigned in first-seen order, applied longest first."""

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def _next(self, kind: str) -> int:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return self._counters[kind]

    def identifier(self, raw: str, prefix: str) -> str:
        if raw not in self.mapping:
            body_len = len(raw) - len(prefix)
            counter = format(self._next(prefix), "x")
            self.mapping[raw] = prefix + counter.rjust(body_len, "0")
        return self.mapping[raw]

    def phone(self, raw: str) -> str:
        if raw not in self.mapping:
            n = self._next("phone")
            self.mapping[raw] = f"+1555010{n:04d}"
            self.mapping["%2B" + raw[1:]] = f"%2B1555010{n:04d}"
        return self.mapping[raw]

    def literal(self, raw: str, placeholder: str) -> None:
        if raw and raw not in self.mapping:
            self.mapping[raw] = placeholder

    def apply(self, text: str) -> str:
        for raw in sorted(self.mapping, key=len, reverse=True):
            text = text.replace(raw, self.mapping[raw])
        return text


def _walk(node: Any, visit: Any) -> None:
    if isinstance(node, dict):
        visit(node)
        for value in node.values():
            _walk(value, visit)
    elif isinstance(node, list):
        for value in node:
            _walk(value, visit)


def _write_json_files(out: Path, documents: dict[str, str], names: Pseudonyms) -> dict[str, Any]:
    """Scrub each raw JSON text, write it, and return name -> parsed scrubbed document."""
    written: dict[str, Any] = {}
    for name, text in documents.items():
        document = json.loads(names.apply(text))
        (out / name).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        written[name] = document
    _prettier([out / name for name in documents])
    return written


def _guard(out: Path, raw_values: set[str]) -> None:
    """Refuse the build on any leak rule, env secret or raw value in the output bytes."""
    secrets = env_secret_values(ENV_FILES)
    prefixes = sha256_prefixes(secrets)
    problems: list[str] = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        rel = path.relative_to(out)
        problems += [f"{rel}: rule {rule}" for rule in find_leaks(data)]
        for key, value in secrets.items():
            if (
                any(v.encode() in data for v in secret_variants(value))
                or prefixes[key].encode() in data
            ):
                problems.append(f"{rel}: env {key}")
        leaked = sum(value.encode() in data for value in raw_values)
        if leaked:
            problems.append(f"{rel}: {leaked} raw value(s)")
    if problems:
        shutil.rmtree(out)
        raise SystemExit("refusing to write the fixture:\n  " + "\n  ".join(problems))
    print(f"guard: {len(secrets)} env secrets, {len(raw_values)} raw values, 0 hits")


def _prettier(paths: list[Path]) -> None:
    """Match the repo's Prettier style so `pnpm format:check` passes; JSON content is unchanged."""
    prettier = ROOT / "node_modules" / ".bin" / "prettier"
    if prettier.exists():
        subprocess.run(
            [str(prettier), "--write", *map(str, paths)], check=True, capture_output=True
        )
    else:
        print("prettier not installed; run `pnpm format` on the fixture JSON before committing")


def _file_entry(out: Path, name: str, kind: str, source: str) -> dict[str, Any]:
    data = (out / name).read_bytes()
    entry: dict[str, Any] = {"file": name, "kind": kind, "source": source, "bytes": len(data)}
    if kind == "json":
        entry["canonical_sha256"] = _canonical_sha256(json.loads(data))
    else:
        entry["sha256"] = _sha256(data)
    return entry


def _segments(samples: array, rate: int) -> list[tuple[float, float]]:
    """Energy VAD: 50 ms RMS windows over 200, closing after 300 ms below it."""
    window = int(VAD_WINDOW_S * rate)
    active = []
    for start in range(0, len(samples) - window + 1, window):
        chunk = samples[start : start + window]
        active.append((sum(s * s for s in chunk) / window) ** 0.5 > VAD_RMS)
    segments: list[tuple[float, float]] = []
    begin, end, gap = None, 0, 0
    for index, is_active in enumerate(active):
        if is_active:
            begin = index if begin is None else begin
            end, gap = index, 0
        elif begin is not None:
            gap += 1
            if gap > VAD_HANG:
                segments.append(
                    (round(begin * VAD_WINDOW_S, 2), round((end + 1) * VAD_WINDOW_S, 2))
                )
                begin = None
    if begin is not None:
        segments.append((round(begin * VAD_WINDOW_S, 2), round((end + 1) * VAD_WINDOW_S, 2)))
    return segments


# --------------------------------------------------------------------------- ElevenLabs

EL_ID_KEYS = {"agent_id", "conversation_id", "branch_id", "version_id", "user_id"}
EL_ID_SHAPE = re.compile(r"\b(agent|conv|agtbrch|agtvrsn|user)_[0-9a-z]{20,}\b")
EL_FIELDS = [
    ("conversation_id", "str", "provider call ID; the session key (`earshot.session.id`)"),
    ("agent_id", "str", "agent ID; becomes the `agent` attribute"),
    ("status", "str", "`done` once post-call processing finished"),
    ("has_audio", "bool", "gate for GET /audio"),
    ("has_user_audio", "bool", "user side present in the audio"),
    ("has_response_audio", "bool", "agent side present in the audio"),
    ("transcript", "list", "turns in order"),
    ("transcript[].role", "str", "`agent` | `user`"),
    ("transcript[].message", "str", "turn text; an interrupted agent turn reads `...`"),
    ("transcript[].time_in_call_secs", "int", "turn START, whole seconds from start_time"),
    ("transcript[].interrupted", "bool", "agent turn cut off by the user"),
    ("transcript[].source_medium", "NoneType", "`audio` on user turns, null on agent turns"),
    (
        "transcript[].conversation_turn_metrics",
        "dict",
        "per-turn `metrics.<name>.elapsed_time` (s)",
    ),
    ("metadata.start_time_unix_secs", "int", "call start, epoch seconds; t=0 of every turn"),
    ("metadata.accepted_time_unix_secs", "int", "accept time, epoch seconds"),
    ("metadata.call_duration_secs", "int", "duration, whole seconds"),
    ("metadata.termination_reason", "str", "free text, e.g. `Client disconnected: 1000`"),
    ("metadata.cost", "int", "provider cost in credits = charging.llm_charge + call_charge"),
    ("metadata.cost_fiat", "float", "provider cost as a fiat amount (unit not stated; USD tier)"),
    ("metadata.charging.llm_charge", "int", "credits for the LLM"),
    ("metadata.charging.call_charge", "int", "credits for the call (platform)"),
    ("metadata.charging.llm_price", "float", "LLM price, fiat"),
    ("metadata.phone_call", "NoneType", "null for a websocket call; telephony details otherwise"),
    ("analysis.call_successful", "str", "`success` | `failure` | `unknown`"),
    ("analysis.transcript_summary", "str", "LLM summary"),
]


def _el_identifiers(raw: Path, names: Pseudonyms) -> set[str]:
    texts = [(raw / name).read_text() for name in ("conversation.json", "conversation.otel.json")]
    found: list[tuple[str, str]] = []

    def visit(node: dict[str, Any]) -> None:
        for key, value in node.items():
            if not isinstance(value, str) or len(value) < 8:
                continue
            if key in EL_ID_KEYS or (key.startswith("system__") and key.endswith("_id")):
                found.append((value, value.split("_", 1)[0] + "_" if "_" in value else ""))
            elif key == "voice_id":
                found.append((value, "voice"))

    for text in texts:
        _walk(json.loads(text), visit)
        found += [(m.group(0), m.group(1) + "_") for m in EL_ID_SHAPE.finditer(text)]
    otel = json.loads(texts[1])
    for span_attrs in _otel_attribute_lists(otel):
        for item in span_attrs:
            value = next(iter(item["value"].values()))
            if item["key"].endswith("voice_id") and isinstance(value, str):
                found.append((value, "voice"))
    for value, prefix in found:
        names.identifier(value, prefix)
    return {value for value, _ in found}


def _otel_attribute_lists(otel: dict[str, Any]) -> list[list[dict[str, Any]]]:
    lists = []
    for resource_spans in (otel.get("otlp_traces") or {}).get("resourceSpans", []):
        lists.append(resource_spans["resource"]["attributes"])
        for scope_spans in resource_spans["scopeSpans"]:
            lists += [span["attributes"] for span in scope_spans["spans"]]
    return lists


def _mp3_info(data: bytes) -> dict[str, Any]:
    offset, tag = 0, b""
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        offset, tag = 10 + size, data[10 : 10 + size]
    header = int.from_bytes(data[offset : offset + 4], "big")
    if header >> 21 != 0x7FF or ((header >> 19) & 3, (header >> 17) & 3) != (2, 1):
        raise SystemExit("ElevenLabs audio is not MPEG-2 Layer III")
    kbps = MPEG2_L3_KBPS[(header >> 12) & 15]
    rate = MPEG2_RATES[(header >> 10) & 3]
    mode = (header >> 6) & 3
    frames = 0
    while offset < len(data):
        h = int.from_bytes(data[offset : offset + 4], "big")
        offset += 72_000 * MPEG2_L3_KBPS[(h >> 12) & 15] // rate + ((h >> 9) & 1)
        frames += 1
    return {
        "id3_text": [t.decode() for t in re.findall(rb"[ -~]{4,}", tag)],
        "bitrate_kbps": kbps,
        "sample_rate": rate,
        "channels": 1 if mode == 3 else 2,
        "frames": frames,
        "duration_s": frames * 576 / rate,
    }


def _el_audio_onsets(audio: bytes) -> list[tuple[float, float]] | None:
    """Speech segments of the decoded MP3, when macOS `afconvert` is available."""
    afconvert = shutil.which("afconvert")
    if not afconvert:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "a.mp3", Path(tmp) / "a.wav"
        src.write_bytes(audio)
        subprocess.run([afconvert, "-f", "WAVE", "-d", "LEI16", str(src), str(dst)], check=True)
        with wave.open(str(dst)) as wav:
            samples = array("h", wav.readframes(wav.getnframes()))
            return _segments(samples, wav.getframerate())


def build_elevenlabs() -> None:
    raw = RAW_ROOT / "elevenlabs" / "conversation-run"
    out = FIXTURES / "elevenlabs"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    names = Pseudonyms()
    raw_values = _el_identifiers(raw, names)
    documents = {
        "conversation.json": (raw / "conversation.json").read_text(),
        "conversation.otel.json": (raw / "conversation.otel.json").read_text(),
    }
    written = _write_json_files(out, documents, names)
    audio = (raw / "audio.bin").read_bytes()
    (out / "audio.mp3").write_bytes(audio)
    conversation = written["conversation.json"]
    info = _mp3_info(audio)
    segments = _el_audio_onsets(audio)
    run = json.loads((raw / "run.json").read_text())
    events = json.loads((raw / "ws_events.json").read_text())
    caller = json.loads((ROOT / ".earshot" / "pipecat_otlp" / "caller.json").read_text())
    import capture_elevenlabs as capture

    metadata = conversation["metadata"]
    charging = metadata["charging"]
    manifest = {
        "format_version": 1,
        "provider": "elevenlabs",
        "description": (
            "One real ElevenLabs Agents conversation fetched with the conversations API, "
            "scrubbed. Both sides are synthetic speech. Consumers: the WS3 import by "
            "conversation ID and the `start_only` transcript rule."
        ),
        "provenance": {
            "captured_on": CAPTURED_ON,
            "source_kind": "real_capture_scrubbed",
            "capture_driver": "scripts/capture_elevenlabs.py",
            "capture_driver_sha256": _script_digest("capture_elevenlabs.py"),
            "scrubber": "scripts/scrub_provider_capture.py",
            "scrubber_sha256": _script_digest("scrub_provider_capture.py"),
            "survey": (
                "free GET /v1/convai/agents and /v1/convai/conversations: 0 agents, 0 conversations"
            ),
            "requests": {
                "conversation.json": "GET https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}",
                "conversation.otel.json": "GET …/conversations/{id}?format=opentelemetry",
                "audio.mp3": "GET …/conversations/{id}/audio (content-type audio/mpeg)",
            },
            "conversation": (
                "POST /v1/convai/agents/create (a dedicated test agent), GET "
                "/v1/convai/conversation/get-signed-url, then one session on "
                "wss://api.elevenlabs.io/v1/convai/conversation: user_audio_chunk = base64 "
                "pcm_16000 at real-time pace in 100 ms chunks; pong on every ping; the "
                "client closed the socket (code 1000) after the agent's last reply."
            ),
            "agent_config": {
                "first_message": capture.FIRST_MESSAGE,
                "prompt": capture.AGENT_PROMPT,
                "language": "en",
                "max_duration_seconds": 90,
                "asr.user_input_audio_format": "pcm_16000",
                "tts.agent_output_audio_format": "pcm_16000",
                "llm": "account default (`producing_llm` in the transcript)",
            },
            "caller": {
                "source": "Cartesia TTS lines from the WS0b cache .earshot/pipecat_otlp/caller.wav",
                "script": [line["text"] for line in caller["lines"]],
            },
            "websocket_events": {
                kind: sum(1 for e in events if e["type"] == kind)
                for kind in sorted({e["type"] for e in events})
            },
            "account_tier": charging.get("tier"),
        },
        "cost_reported": {
            "metadata.cost": metadata["cost"],
            "metadata.cost_unit": "credits",
            "metadata.cost_fiat": metadata["cost_fiat"],
            "metadata.charging.llm_charge": charging["llm_charge"],
            "metadata.charging.call_charge": charging["call_charge"],
            "metadata.charging.llm_price": charging["llm_price"],
            "subscription.character_count_delta": (
                run["credits_after"]["character_count"] - run["credits_before"]["character_count"]
            ),
        },
        "scrub_rules": [
            "agent_id, conversation_id, branch_id, version_id, user_id and system__*_id dynamic "
            "variables, plus any agent_/conv_/agtbrch_/agtvrsn_/user_ ID found by shape: "
            "same prefix and length, body = zeros + first-seen counter",
            "voice_id (turn metrics, charging.tts_usage, OTel elevenlabs.voice_id): "
            "`voice` + zeros + counter, same length",
            "replacement is applied to the whole text, so IDs inside URIs, OTel attributes "
            "and system__conversation_history are covered",
            "kept verbatim: timestamps, durations, costs, metrics, model names and all text. "
            "All text is synthetic: the caller script above and the agent's LLM replies",
            "the build refuses to write on any provider_capture.find_leaks rule, env secret "
            "(value, phone variants, sha256 prefix) or raw identifier in the output bytes",
        ],
        "privacy": {
            "t4_audio": "synthetic_both_sides",
            "t4b_note": (
                "All keys are kept, not only the ones a translator reads: the content is "
                "synthetic and every identifier is pseudonymised. Owner to confirm."
            ),
            "agent_name": "kept: `earshot-ws0-capture` was chosen by the capture script",
        },
        "field_table": [{"path": p, "type": t, "meaning": m} for p, t, m in EL_FIELDS],
        "turn_metric_names": sorted(
            {
                name
                for turn in conversation["transcript"]
                for name in (turn["conversation_turn_metrics"] or {}).get("metrics", {})
            }
        ),
        "timing": {
            "semantics": "start_only",
            "turn_start": "transcript[].time_in_call_secs: whole seconds after start_time",
            "turn_end": "absent; the API gives no end time",
            "otel_variant": (
                "recv spans start at time_in_call_secs; each end is the next span's start "
                "(the root's end for the last). The interrupted agent turn lasts 10 ms. "
                "Ends are derived, not measured."
            ),
            "turns": [
                {
                    "index": i,
                    "role": turn["role"],
                    "time_in_call_secs": turn["time_in_call_secs"],
                    "interrupted": turn["interrupted"],
                }
                for i, turn in enumerate(conversation["transcript"])
            ],
            "audio_speech_segments_s": segments,
            "audio_speech_segments_method": (
                "afconvert decode + 50 ms RMS>200 VAD (scripts/scrub_provider_capture.py _segments)"
                if segments
                else "not measured (afconvert unavailable)"
            ),
            "observed": (
                "user turns are floor(speech onset) in all 4 cases (5.15->5, 12.8->12, "
                "14.55->14, 21.55->21); agent turns start at or up to 1.05 s before the "
                "audible onset (16 vs 17.05). Audio t=0 = start_time [inferred: turn 0 at 0 "
                "is audible from 0.25 s]."
            ),
        },
        "audio": {
            "file": "audio.mp3",
            "provenance": "synthetic_both_sides",
            "content_type": "audio/mpeg",
            "container": "MPEG audio elementary stream with an ID3v2.4 tag",
            "codec": "mp3",
            "mpeg": "MPEG-2 Layer III, constant bitrate, no Xing/Info frame",
            "id3_text": info["id3_text"],
            "channels": info["channels"],
            "channel_layout": "mixed: agent and caller in one mono channel",
            "sample_rate": info["sample_rate"],
            "bitrate_kbps": info["bitrate_kbps"],
            "frames": info["frames"],
            "duration_s": info["duration_s"],
            "call_duration_secs": metadata["call_duration_secs"],
        },
    }
    manifest["files"] = [
        _file_entry(out, "conversation.json", "json", "GET /v1/convai/conversations/{id}"),
        _file_entry(out, "conversation.otel.json", "json", "?format=opentelemetry"),
        _file_entry(out, "audio.mp3", "audio", "GET /v1/convai/conversations/{id}/audio"),
    ]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    _prettier([out / "manifest.json"])
    _guard(out, raw_values)
    print(f"elevenlabs: {len(names.mapping)} replacements, {len(manifest['files'])} files")


# ------------------------------------------------------------------------------ Twilio

TWILIO_SID = re.compile(r"\b(AC|CA|RE|PN|SK)[0-9a-f]{32}\b")
TWILIO_E164 = re.compile(r"\+\d{10,15}")
PHONE_FIELDS = ("from", "to", "forwarded_from")
TWILIO_JSON = {
    "create_call.json": ("create.json", "POST /Accounts/{AccountSid}/Calls.json (201)"),
    "call.json": ("call.json", "GET /Accounts/{AccountSid}/Calls/{CallSid}.json, after completion"),
    "call_recordings.json": ("call_recordings.json", "GET …/Calls/{CallSid}/Recordings.json"),
    "recordings_by_callsid.json": ("recordings_by_callsid.json", "GET …/Recordings.json?CallSid="),
    "recording.json": ("recording_0.json", "GET …/Recordings/{RecordingSid}.json"),
    "call_events.json": ("call_events.json", "GET …/Calls/{CallSid}/Events.json"),
    "call_notifications.json": (
        "call_notifications.json",
        "GET …/Calls/{CallSid}/Notifications.json",
    ),
}
CALL_FIELDS = [
    ("sid", "str", "CallSid; the session key and the merge key with a host's `earshot.session.id`"),
    ("account_sid", "str", "account"),
    ("status", "str", "final: completed | busy | failed | no-answer | canceled"),
    ("direction", "str", "`outbound-api` for a REST-created call"),
    ("start_time", "str", "RFC 2822, 1 s resolution, UTC"),
    ("end_time", "str", "RFC 2822, 1 s resolution, UTC"),
    ("duration", "str", "whole seconds as a string; equals end_time - start_time"),
    ("price", "str", "negative decimal string = charge; null until Twilio prices the call"),
    ("price_unit", "str", "ISO currency"),
    ("from", "str", "E.164 caller (scrubbed to +1555…)"),
    ("to", "str", "E.164 callee (scrubbed to +1555…)"),
    ("date_created", "str", "request time; here 16 s before start_time (queued + ringing)"),
    ("forwarded_from", "str", "set by Twilio; equal to `to` on this outbound call"),
    ("answered_by", "NoneType", "null without machine detection"),
    ("parent_call_sid", "NoneType", "null for a single-leg call"),
    ("subresource_uris", "dict", "relative URIs incl. recordings and events"),
]
RECORDING_FIELDS = [
    ("sid", "str", "RecordingSid; media at Recordings/{sid}.wav"),
    ("call_sid", "str", "owning CallSid"),
    ("channels", "int", "1 or 2; 2 with RecordingChannels=dual"),
    ("source", "str", "`OutboundAPI` for Record=true on Create Call"),
    ("status", "str", "`completed` once media is ready"),
    ("duration", "str", "whole seconds as a string"),
    ("start_time", "str", "RFC 2822, 1 s resolution"),
    ("price", "str", "negative decimal string = charge"),
    ("price_unit", "str", "ISO currency"),
    ("media_url", "str", "absolute https URL without extension; needs Basic auth"),
    ("conference_sid", "NoneType", "null outside a conference"),
    ("error_code", "NoneType", "null on success"),
]


def _twilio_identifiers(raw: Path, names: Pseudonyms) -> set[str]:
    found: set[str] = set()
    texts = [(raw / src).read_text() for src, _ in TWILIO_JSON.values()]
    for text in texts:
        for match in TWILIO_SID.finditer(text):
            names.identifier(match.group(0), match.group(1))
            found.add(match.group(0))

    def visit(node: dict[str, Any]) -> None:
        for field in PHONE_FIELDS:
            value = node.get(field)
            if isinstance(value, str) and TWILIO_E164.fullmatch(value):
                placeholder = names.phone(value)
                found.update(secret_variants(value))
                formatted = node.get(f"{field}_formatted")
                if isinstance(formatted, str) and formatted:
                    n = placeholder[-4:]
                    names.literal(formatted, f"(555) 010-{n}")
                    found.add(formatted)
        caller_name = node.get("caller_name")
        if isinstance(caller_name, str) and caller_name:
            names.literal(caller_name, "Captured Caller")
            found.add(caller_name)

    for text in texts:
        _walk(json.loads(text), visit)
        for number in TWILIO_E164.findall(text):
            names.phone(number)
            found.update(secret_variants(number))
    return found


def _read_wav(path: Path) -> tuple[tuple[int, ...], list[str], array]:
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SystemExit(f"{path.name} is not RIFF/WAVE")
    offset, chunks, fmt, samples = 12, [], (), array("h")
    while offset < len(data):
        chunk_id, size = (
            data[offset : offset + 4],
            struct.unpack("<I", data[offset + 4 : offset + 8])[0],
        )
        body = data[offset + 8 : offset + 8 + size]
        chunks.append(chunk_id.decode())
        if chunk_id == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk_id == b"data":
            samples.frombytes(body)
        offset += 8 + size + (size & 1)
    return fmt, chunks, samples


def _write_wav(path: Path, fmt: tuple[int, ...], samples: array) -> None:
    body = samples.tobytes()
    header = b"RIFF" + struct.pack("<I", 4 + 8 + 16 + 8 + len(body)) + b"WAVE"
    header += b"fmt " + struct.pack("<I", 16) + struct.pack("<HHIIHH", *fmt)
    path.write_bytes(header + b"data" + struct.pack("<I", len(body)) + body)


def _synthetic_lines_8k() -> list[array]:
    """The committed synthetic caller lines, low-passed ([1,2,1]/4) and decimated to 8 kHz."""
    with wave.open(str(SYNTHETIC_SOURCE)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16_000):
            raise SystemExit("synthetic source is not mono PCM16 16 kHz")
        source = array("h", wav.readframes(wav.getnframes()))
    lines = []
    for start_s, end_s in _segments(source, 16_000):
        chunk = source[int(start_s * 16_000) : int(end_s * 16_000)]
        lines.append(
            array(
                "h",
                (
                    (chunk[i - 1] + 2 * chunk[i] + chunk[i + 1]) // 4
                    for i in range(1, len(chunk) - 1, 2)
                ),
            )
        )
    return lines


def _regenerate_twilio_audio(raw: Path, out: Path) -> dict[str, Any]:
    dual_fmt, dual_chunks, dual = _read_wav(raw / "recording_0.dual.wav")
    mono_fmt, mono_chunks, mono = _read_wav(raw / "recording_0.mono.wav")
    default_is_dual = (raw / "recording_0.default.wav").read_bytes() == (
        raw / "recording_0.dual.wav"
    ).read_bytes()
    if dual_fmt != (1, 2, 8000, 32000, 4, 16) or mono_fmt != (1, 1, 8000, 16000, 2, 16):
        raise SystemExit("unexpected Twilio WAV format; update the regeneration")
    frames = len(dual) // 2
    if len(mono) != frames:
        raise SystemExit("dual and mono frame counts differ")
    rate = dual_fmt[2]
    twilio_segments = _segments(dual[1::2], rate)
    callee_active = sum(e - s for s, e in _segments(dual[0::2], rate))
    pieces = _synthetic_lines_8k()
    gap = array("h", bytes(2 * int(0.15 * rate)))
    twilio = array("h", bytes(2 * frames))
    fade = int(0.02 * rate)
    next_piece = 0
    for start_s, end_s in twilio_segments:
        # Fill the raw speech window with consecutive synthetic pieces 150 ms apart
        # (under the VAD hang), so speech activity matches the raw window.
        begin, length = int(start_s * rate), int((end_s - start_s) * rate)
        fill = array("h")
        while len(fill) < length:
            fill += pieces[next_piece % len(pieces)] + gap
            next_piece += 1
        for i in range(length):
            twilio[begin + i] = int(fill[i] * min(1.0, (length - i) / fade))
    regenerated = array("h", bytes(4 * frames))
    regenerated[1::2] = twilio
    (out / "audio").mkdir()
    _write_wav(out / "audio" / "recording.dual.wav", dual_fmt, regenerated)
    _write_wav(out / "audio" / "recording.mono.wav", mono_fmt, array("h", twilio))
    return {
        "frames": frames,
        "rate": rate,
        "chunks": dual_chunks,
        "mono_chunks": mono_chunks,
        "default_is_dual": default_is_dual,
        "twilio_segments": twilio_segments,
        "callee_active_s": round(callee_active, 2),
        "dual_fmt": dual_fmt,
        "mono_fmt": mono_fmt,
    }


def build_twilio() -> None:
    raw = RAW_ROOT / "twilio" / "call-run"
    out = FIXTURES / "twilio"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    names = Pseudonyms()
    raw_values = _twilio_identifiers(raw, names)
    documents = {name: (raw / src).read_text() for name, (src, _) in TWILIO_JSON.items()}
    written = _write_json_files(out, documents, names)
    audio = _regenerate_twilio_audio(raw, out)
    run = json.loads((raw / "run.json").read_text())
    call, recording = written["call.json"], written["recording.json"]
    (event,) = written["call_events.json"]["events"]
    parameters = event["request"]["parameters"]
    manifest = {
        "format_version": 1,
        "provider": "twilio",
        "description": (
            "One real outbound-API Twilio call with Record=true and RecordingChannels=dual, "
            "polled over the REST API (no webhooks, no tunnel). REST responses scrubbed; "
            "recordings regenerated. Consumers: the WS3 poll/import and recording fetch."
        ),
        "provenance": {
            "captured_on": CAPTURED_ON,
            "source_kind": "real_capture_scrubbed",
            "capture_driver": "scripts/capture_twilio.py",
            "capture_driver_sha256": _script_digest("capture_twilio.py"),
            "scrubber": "scripts/scrub_provider_capture.py",
            "scrubber_sha256": _script_digest("scrub_provider_capture.py"),
            "account_type": "Trial",
            "create_call_form": {
                "To": "TWILIO_TO_NUMBER",
                "From": "TWILIO_FROM_NUMBER",
                "Twiml": run["twiml"],
                "Record": "true",
                "RecordingChannels": "dual",
                "Timeout": "40",
                "TimeLimit": "60",
            },
            "applied_parameters": {
                k: parameters.get(k)
                for k in ("record", "recording_channels", "recording_track", "trim", "timeout")
            },
            "status_sequence_polled": run["status_sequence"],
            "outcome": (
                "Answered. The trial-account announcement played on the Twilio channel and "
                "asked for a key press; no key was pressed, so the inline TwiML <Say> never "
                f"ran and the call ended after {call['duration']} s."
            ),
            "requests": {name: description for name, (_, description) in TWILIO_JSON.items()},
            "media_requests": {
                "Recordings/{sid}.wav": "dual WAV, byte-identical to ?RequestedChannels=2"
                if audio["default_is_dual"]
                else "differs from ?RequestedChannels=2",
                "Recordings/{sid}.wav?RequestedChannels=2": "2 ch",
                "Recordings/{sid}.wav?RequestedChannels=1": "1 ch mix",
            },
        },
        "cost_reported": {
            "call.price": call["price"],
            "recording.price": recording["price"],
            "price_unit": call["price_unit"],
            "note": "Twilio reports charges as negative strings; this account is on trial credit",
        },
        "scrub_rules": [
            "Twilio SIDs (AC, CA, RE, PN, SK + 32 hex): same prefix, body = zeros + first-seen "
            "counter, in every string (fields, URIs, events response_body)",
            "E.164 numbers (+ and %2B forms) -> +1555010NNNN in first-seen order; a "
            "*_formatted form that differs from its E.164 -> (555) 010-NNNN (from_formatted "
            "here; to_formatted was already E.164 and follows that mapping)",
            "a non-empty caller_name -> `Captured Caller` (it was empty)",
            "kept verbatim: statuses, times, durations, prices, the TwiML and request defaults",
            "the build refuses to write on any provider_capture.find_leaks rule, env secret "
            "(value, phone variants, sha256 prefix) or raw value in the output bytes",
        ],
        "field_table": {
            "call": [{"field": f, "type": t, "meaning": m} for f, t, m in CALL_FIELDS],
            "recording": [{"field": f, "type": t, "meaning": m} for f, t, m in RECORDING_FIELDS],
        },
        "timing": {
            "resolution": "1 s: every Twilio time is an RFC 2822 string",
            "call_window": f"{call['start_time']} .. {call['end_time']} ({call['duration']} s)",
            "recording_start": recording["start_time"],
            "recording_duration_s": recording["duration"],
            "trim": "Trim defaulted to trim-silence: recording t=0 is not guaranteed = answer",
        },
        "audio": {
            "provenance": "regenerated_synthetic",
            "raw_frames": audio["frames"],
            "raw_container": {
                "dual": {"chunks": audio["chunks"], "fmt": list(audio["dual_fmt"])},
                "mono": {"chunks": audio["mono_chunks"], "fmt": list(audio["mono_fmt"])},
                "fmt_fields": "format_tag, channels, sample_rate, byte_rate, block_align, bits",
                "content_type": "audio/x-wav",
            },
            "dual": {
                "file": "audio/recording.dual.wav",
                "channels": 2,
                "frames": audio["frames"],
            },
            "mono": {
                "file": "audio/recording.mono.wav",
                "channels": 1,
                "frames": audio["frames"],
            },
            "regeneration": (
                "Same RIFF layout (fmt 16 + data), PCM16, 8 kHz, channels and frame count as "
                "the raw media. Channel 0 is digital silence. Channel 1 holds synthetic "
                "Cartesia speech cut from fixtures/captured/pipecat-otlp-1.5/audio/user.wav "
                "(low-passed and decimated to 8 kHz), one line per raw channel-1 speech "
                "window, truncated to the window with a 20 ms fade. Mono = ch0 + ch1."
            ),
            "synthetic_source_sha256": _sha256(SYNTHETIC_SOURCE.read_bytes()),
            "channel_order": {
                "0": {
                    "side": "callee",
                    "track": "inbound (audio Twilio received from the called party)",
                    "raw_speech_s": audio["callee_active_s"],
                    "evidence": "offline ASR: none of the announcement words; raw not committed",
                },
                "1": {
                    "side": "twilio_generated",
                    "track": "outbound (what Twilio played: trial announcement; TwiML plays here)",
                    "speech_segments_s": [list(s) for s in audio["twilio_segments"]],
                    "evidence": (
                        "offline ASR (faster-whisper base.en, CPU) of raw channel 1 found the "
                        "trial announcement words trial/account/upgrading/press/key/execute. "
                        "[I] <Say> audio would use the same outbound track; not observed "
                        "because the TwiML never ran."
                    ),
                },
                "mono": "a mix of both channels (raw mono correlates 1.0 with channel 1)",
            },
        },
    }
    files = [_file_entry(out, name, "json", desc) for name, (_, desc) in TWILIO_JSON.items()]
    files += [
        _file_entry(out, "audio/recording.dual.wav", "audio", "regenerated: Recordings/{sid}.wav"),
        _file_entry(out, "audio/recording.mono.wav", "audio", "regenerated: ?RequestedChannels=1"),
    ]
    manifest["files"] = files
    for spec in (manifest["audio"]["dual"], manifest["audio"]["mono"]):
        spec["sha256"] = _sha256((out / spec["file"]).read_bytes())
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    _prettier([out / "manifest.json"])
    _guard(out, raw_values)
    print(f"twilio: {len(names.mapping)} replacements, {len(files)} files")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("provider", choices=["elevenlabs", "twilio"])
    args = parser.parse_args()
    if args.provider == "elevenlabs":
        build_elevenlabs()
    else:
        build_twilio()


if __name__ == "__main__":
    main()
