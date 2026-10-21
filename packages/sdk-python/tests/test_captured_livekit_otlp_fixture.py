"""Integrity of the real LiveKit Agents 1.8 OTLP capture that later LiveKit work is tested against.

The fixture is the exact sequence of OTLP/HTTP protobuf requests a real headless LiveKit
call exported (scrubbed and re-serialized), plus the call's ``RecorderIO`` stereo
Ogg/Opus recording. These tests decode it with nothing but ``opentelemetry-proto`` and
the stdlib, and pin the facts the receiver, the LiveKit key table, the closer and the
recording analysis rely on.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

trace_service_pb2 = pytest.importorskip(
    "opentelemetry.proto.collector.trace.v1.trace_service_pb2",
    reason="opentelemetry-proto is required to decode the captured OTLP fixture",
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8"
CALLER_AUDIO_SCRIPT = ROOT / "examples" / "pipecat_otlp" / "make_caller_audio.py"
ENV_FILE = ROOT / ".env"
SCOPE = "livekit-agents"
CALL_SPANS = {
    "agent_session",
    "user_speaking",
    "user_turn",
    "agent_turn",
    "agent_speaking",
    "llm_node",
    "llm_request",
    "tts_node",
    "tts_request",
    "session_close",
}
# Emitted by the job runner after the session closes (livekit-agents >= 1.8.2).
JOB_TAIL_SPANS = {"job_shutdown", "on_session_end", "session_end_upload", "room_disconnect"}
USER_TRANSCRIPT_KEYS = ("lk.pii.user_transcript", "lk.user_transcript")
# Model identifiers are public configuration that LiveKit records as
# `gen_ai.request.model`; every other env value is treated as a secret.
NON_SECRET_ENV_KEYS = {"GROQ_MODEL", "CARTESIA_MODEL"}
SECRET_PATTERNS = (
    re.compile(rb"gsk_[A-Za-z0-9]{20,}"),
    re.compile(rb"sk_car_[A-Za-z0-9]{12,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"Bearer\s+[A-Za-z0-9._-]{12,}"),
)
OPUS_GRANULE_RATE = 48_000  # Ogg/Opus granule positions always count 48 kHz samples


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURE / "manifest.json").read_text())


def _value(any_value: Any) -> Any:
    kind = any_value.WhichOneof("value")
    return None if kind is None else getattr(any_value, kind)


def _attributes(span: Any) -> dict[str, Any]:
    return {item.key: _value(item.value) for item in span.attributes}


def _spans() -> list[tuple[str, Any]]:
    """Every exported span with its instrumentation scope name, in export order."""
    spans: list[tuple[str, Any]] = []
    for entry in _manifest()["requests"]:
        request = trace_service_pb2.ExportTraceServiceRequest()
        request.ParseFromString((FIXTURE / entry["file"]).read_bytes())
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                spans.extend((scope_spans.scope.name, span) for span in scope_spans.spans)
    return spans


def _named(name: str) -> list[Any]:
    return [span for _, span in _spans() if span.name == name]


def _descendants(root: Any, spans: list[Any]) -> list[Any]:
    found, frontier = [], {root.span_id}
    while frontier:
        children = [span for span in spans if span.parent_span_id in frontier]
        found += children
        frontier = {span.span_id for span in children}
    return found


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z']+", text.lower()) if len(word) >= 4}


def _example_caller_script() -> list[str]:
    """The `CALLER_SCRIPT` constant of the example that generated the caller audio."""
    for node in ast.parse(CALLER_AUDIO_SCRIPT.read_text()).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(target, ast.Name) and target.id == "CALLER_SCRIPT" for target in targets):
            return list(ast.literal_eval(node.value))
    raise AssertionError("CALLER_SCRIPT constant not found")


def _env_secret_values() -> dict[str, str]:
    """Secret values from the git-ignored `.env` and the process env, keyed by name only."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if match:
                values[match.group(1)] = match.group(2).strip().strip("'\"")
    for key in list(values) + [key for key in os.environ if key.endswith("_API_KEY")]:
        if key in os.environ:
            values.setdefault(key, os.environ[key])
    return {
        key: value
        for key, value in values.items()
        if key not in NON_SECRET_ENV_KEYS and len(value) >= 8
    }


def _ogg_opus(path: Path) -> tuple[bytes, bytes, int]:
    """The OpusHead and OpusTags packets and the final granule position (stdlib only)."""
    data, position, last_granule = path.read_bytes(), 0, -1
    headers: list[bytes] = []
    packet = b""
    while position < len(data):
        assert data[position : position + 4] == b"OggS", f"no Ogg page at byte {position}"
        granule = int.from_bytes(data[position + 6 : position + 14], "little", signed=True)
        segments = data[position + 26]
        lacing = data[position + 27 : position + 27 + segments]
        body = position + 27 + segments
        for size in lacing:
            if len(headers) < 2:
                packet += data[body : body + size]
                if size < 255:
                    headers.append(packet)
                    packet = b""
            body += size
        if granule >= 0:
            last_granule = granule
        position = body
    assert len(headers) == 2, "missing OpusHead/OpusTags packets"
    return headers[0], headers[1], last_granule


def test_requests_decode_in_order_and_match_the_manifest() -> None:
    manifest = _manifest()
    files = [entry["file"] for entry in manifest["requests"]]
    assert files == [f"{index:03d}.pb" for index in range(1, len(files) + 1)]
    assert sorted(path.name for path in FIXTURE.glob("*.pb")) == files

    names: Counter[str] = Counter()
    keys: dict[str, set[str]] = {}
    for entry in manifest["requests"]:
        body = (FIXTURE / entry["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"], entry["file"]
        request = trace_service_pb2.ExportTraceServiceRequest()
        request.ParseFromString(body)
        assert request.SerializeToString(deterministic=True) == body, entry["file"]
        request_spans = [
            span
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]
        assert len(request_spans) == entry["span_count"] > 0, entry["file"]
        names.update(span.name for span in request_spans)
        for span in request_spans:
            keys.setdefault(span.name, set()).update(item.key for item in span.attributes)
    assert dict(names) == manifest["span_names"]
    assert sum(names.values()) == manifest["span_count"]
    # The per-span-name key table is what the LiveKit key-table work reads.
    assert {name: set(shape["attributes"]) for name, shape in manifest["span_shapes"].items()} == (
        keys
    )


def test_spans_form_one_livekit_session_keyed_by_the_session_id() -> None:
    manifest = _manifest()
    session_key = manifest["provenance"]["session_key"]
    spans = _spans()
    assert {span.name for _, span in spans} >= CALL_SPANS | JOB_TAIL_SPANS | {"job_entrypoint"}
    assert {scope for scope, _ in spans} == {SCOPE}

    roots = [span for _, span in spans if not span.parent_span_id]
    assert [root.name for root in roots] == ["job_entrypoint"]
    assert {span.trace_id for _, span in spans} == {roots[0].trace_id}
    for _, span in spans:
        attributes = _attributes(span)
        assert attributes.get("earshot.session.id") == session_key, span.name
        # livekit-agents >= 1.8.5 stamps the room SID on every span inside a job.
        assert attributes.get("gen_ai.conversation.id") == manifest["provenance"]["room_sid"]


def test_session_close_is_the_end_marker_and_the_job_tail_follows_it() -> None:
    spans = [span for _, span in _spans()]
    (session,) = _named("agent_session")
    (close,) = _named("session_close")
    assert close.parent_span_id == session.span_id
    assert _attributes(close)["lk.close_reason"] == "user_initiated"
    call_spans = [span for span in spans if span.name in CALL_SPANS - {"session_close"}]
    assert max(span.end_time_unix_nano for span in call_spans) == session.end_time_unix_nano
    assert session.end_time_unix_nano >= close.end_time_unix_nano

    tail = [span for span in spans if span.end_time_unix_nano > close.end_time_unix_nano]
    assert {span.name for span in tail} >= JOB_TAIL_SPANS | {"job_entrypoint"}
    tail_order = [span.name for span in sorted(tail, key=lambda span: span.end_time_unix_nano)]
    assert tail_order == [entry["span"] for entry in _manifest()["after_end_marker"]]
    assert tail_order[-1] == "job_entrypoint"


def test_user_turns_carry_the_fixed_caller_script() -> None:
    script = _manifest()["provenance"]["caller_script"]
    assert script == _example_caller_script()

    turns = [_attributes(span) for span in _named("user_turn")]
    assert len(turns) == len(script)
    transcripts = [
        next(str(turn[key]) for key in USER_TRANSCRIPT_KEYS if key in turn) for turn in turns
    ]
    for line, transcript in zip(script, transcripts, strict=True):
        assert len(_words(line) & _words(transcript)) >= 3, (line, transcript)


def test_every_agent_turn_has_llm_output_messages_and_spoken_text() -> None:
    spans = [span for _, span in _spans()]
    turns = _named("agent_turn")
    assert len(turns) == len(_manifest()["provenance"]["caller_script"])
    for turn in turns:
        assert str(_attributes(turn).get("lk.pii.response.text", "")).strip(), turn.span_id
        below = _descendants(turn, spans)
        outputs = []
        for request in (span for span in below if span.name == "llm_request"):
            attributes = _attributes(request)
            assert isinstance(json.loads(attributes["gen_ai.input.messages"]), list)
            outputs += json.loads(attributes.get("gen_ai.output.messages", "[]"))
        texts = [
            part.get("content", "")
            for message in outputs
            if message.get("role") == "assistant"
            for part in message.get("parts", [])
            if part.get("type") == "text"
        ]
        assert any(text.strip() for text in texts), turn.span_id
        tts = [_attributes(span) for span in below if span.name == "tts_request"]
        assert tts and all(str(item.get("lk.pii.input_text", "")).strip() for item in tts)


def test_fixture_bytes_contain_no_env_secret_values() -> None:
    secrets = _env_secret_values()
    files = [path for path in sorted(FIXTURE.rglob("*")) if path.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        leaked = [key for key, value in secrets.items() if value.encode() in data]
        # Report key names only: a failure message must never carry a secret value.
        assert not leaked, f"{path.relative_to(ROOT)} contains the value of {leaked}"
        patterns = [pattern.pattern.decode() for pattern in SECRET_PATTERNS if pattern.search(data)]
        assert not patterns, f"{path.relative_to(ROOT)} matches secret patterns {patterns}"


def test_recording_is_stereo_ogg_opus_on_the_call_timeline() -> None:
    audio = _manifest()["audio"]
    assert audio["channel_speakers"] == ["user", "agent"]
    head, tags, last_granule = _ogg_opus(FIXTURE / audio["file"])
    assert head[:8] == b"OpusHead" and tags[:8] == b"OpusTags"
    assert head[9] == 2, "RecorderIO writes stereo: channel 0 user, channel 1 agent"
    pre_skip = int.from_bytes(head[10:12], "little")
    duration_s = (last_granule - pre_skip) / OPUS_GRANULE_RATE
    assert duration_s > 1.0

    started_at = int(audio["started_at_unix_nano"])
    assert audio["started_at_unix_nano"] == str(started_at)
    spans = [span for _, span in _spans()]
    first_user = min(span.start_time_unix_nano for span in spans if span.name == "user_turn")
    last_agent = max(span.end_time_unix_nano for span in spans if span.name == "agent_speaking")
    # Every turn happens inside the recording: the stereo file covers the whole call.
    assert 0 < first_user - started_at < 5_000_000_000
    assert last_agent - started_at < duration_s * 1e9
