"""Integrity of the real Pipecat 1.5 OTLP capture that later receiver work is tested against.

The fixture is the exact sequence of OTLP/HTTP protobuf requests a real Pipecat call
exported (scrubbed and re-serialized), plus that call's per-speaker recordings. These
tests decode it with nothing but ``opentelemetry-proto`` and pin the facts the receiver,
closer and replay e2e rely on.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import wave
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
FIXTURE = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5"
CALLER_AUDIO_SCRIPT = ROOT / "examples" / "pipecat_otlp" / "make_caller_audio.py"
ENV_FILE = ROOT / ".env"
CALL_SPANS = {"conversation", "turn", "stt", "llm", "tts"}
# Model identifiers are public configuration that Pipecat records as
# `gen_ai.request.model`; every other env value is treated as a secret.
NON_SECRET_ENV_KEYS = {"GROQ_MODEL", "CARTESIA_MODEL"}
SECRET_PATTERNS = (
    re.compile(rb"gsk_[A-Za-z0-9]{20,}"),
    re.compile(rb"sk_car_[A-Za-z0-9]{12,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"Bearer\s+[A-Za-z0-9._-]{12,}"),
)


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


def test_requests_decode_in_order_and_match_the_manifest() -> None:
    manifest = _manifest()
    files = [entry["file"] for entry in manifest["requests"]]
    assert files == [f"{index:03d}.pb" for index in range(1, len(files) + 1)]
    assert sorted(path.name for path in FIXTURE.glob("*.pb")) == files

    names: Counter[str] = Counter()
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
    assert dict(names) == manifest["span_names"]
    assert sum(names.values()) == manifest["span_count"]


def test_spans_form_one_pipecat_conversation_keyed_by_the_session_id() -> None:
    manifest = _manifest()
    conversation_id = manifest["provenance"]["conversation_id"]
    spans = _spans()
    assert {span.name for _, span in spans} >= CALL_SPANS

    scopes = {(scope, span.name) for scope, span in spans}
    assert {scope for scope, name in scopes if name in {"conversation", "turn"}} == {"pipecat.turn"}
    assert {scope for scope, name in scopes if name in {"stt", "llm", "tts"}} == {"pipecat"}

    conversations = [span for _, span in spans if span.name == "conversation"]
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.parent_span_id == b""
    assert {span.trace_id for _, span in spans} == {conversation.trace_id}

    attributes = _attributes(conversation)
    assert attributes["conversation.id"] == conversation_id
    assert attributes["earshot.session.id"] == conversation_id
    turns = [span for _, span in spans if span.name == "turn"]
    assert {_attributes(turn)["conversation.id"] for turn in turns} == {conversation_id}
    assert {turn.parent_span_id for turn in turns} == {conversation.span_id}
    # The conversation span closes last: it is Pipecat's end-of-call marker.
    assert conversation.end_time_unix_nano == max(span.end_time_unix_nano for _, span in spans)


def test_stt_spans_carry_the_fixed_caller_script() -> None:
    script = _manifest()["provenance"]["caller_script"]
    assert script == _example_caller_script()

    transcript = " ".join(
        str(_attributes(span).get("transcript", "")) for _, span in _spans() if span.name == "stt"
    )
    heard = _words(transcript)
    for line in script:
        assert len(_words(line) & heard) >= 2, line


def test_every_turn_has_agent_text_in_llm_output_and_tts_text() -> None:
    spans = [span for _, span in _spans()]
    turns = [span for span in spans if span.name == "turn"]
    assert len(turns) == len(_manifest()["provenance"]["caller_script"])
    for turn in turns:
        children = [span for span in spans if span.parent_span_id == turn.span_id]
        llm = [_attributes(span) for span in children if span.name == "llm"]
        tts = [_attributes(span) for span in children if span.name == "tts"]
        # An llm span cancelled by the caller speaking on has `input` but no `output`.
        assert llm and all(str(attributes["input"]).startswith("[") for attributes in llm)
        assert any(str(attributes.get("output", "")).strip() for attributes in llm), turn
        assert tts and all(str(attributes.get("text", "")).strip() for attributes in tts), turn


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


def test_recordings_are_mono_16k_and_aligned_with_the_first_stt_span() -> None:
    audio = _manifest()["audio"]
    for track in ("user", "agent"):
        with wave.open(str(FIXTURE / audio[track]["file"]), "rb") as wav:
            assert wav.getnchannels() == 1, track
            assert wav.getframerate() == 16_000, track
            assert wav.getsampwidth() == 2, track
            assert wav.getnframes() / wav.getframerate() > 1.0, track

    started_at = int(audio["started_at_unix_nano"])
    assert audio["started_at_unix_nano"] == str(started_at)
    first_stt = min(span.start_time_unix_nano for _, span in _spans() if span.name == "stt")
    assert abs(first_stt - started_at) < 5_000_000_000
