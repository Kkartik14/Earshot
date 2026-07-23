"""Pseudonymize a metadata-only real capture before it becomes a public fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from collections.abc import Iterator
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from earshot.codec import decode_incident_json, encode_incident_json  # noqa: E402
from earshot.validation import validate_incident  # noqa: E402

_IDENTITY_KEYS = {
    "bundle_id": "bundle",
    "session_id": "session",
    "participant_id": "participant",
    "turn_id": "turn",
    "operation_id": "operation",
    "event_id": "event",
    "sample_id": "sample",
    "omission_id": "omission",
    "trace_id": "trace",
    "span_id": "span",
    "parent_span_id": "span",
}
_ATTRIBUTE_IDENTITY_KEYS = {
    "conversation.id": "conversation",
    "earshot.conversation.item.id": "conversation_item",
    "earshot.correlation": "correlation",
    "earshot.operation.id": "operation",
    "earshot.request.id": "correlation",
    "earshot.tts.voice": "voice",
    "earshot.turn.id": "turn",
    "lk.generation_id": "generation",
    "lk.speech_id": "speech",
    "service.name": "service",
}
_CREDENTIAL_VALUE = re.compile(
    r"(?:\b(?:sk|gsk|dg|xai)-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|wss?://[^/@\s]+:[^/@\s]+@)",
    re.IGNORECASE,
)
_EMAIL_VALUE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


def _walk(value: Any) -> Iterator[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield None, child
            yield from _walk(child)


def _pseudonym(surface: str, category: str, index: int) -> str:
    if category in {"trace", "span"}:
        width = 32 if category == "trace" else 16
        return hashlib.sha256(f"{surface}:{category}:{index}".encode()).hexdigest()[:width]
    return f"captured-{surface}-{category}-{index}"


def _identity_category(key: str) -> str | None:
    return _IDENTITY_KEYS.get(key) or _ATTRIBUTE_IDENTITY_KEYS.get(key)


def _is_content_field(key: str) -> bool:
    tokens = tuple(item for item in re.split(r"[._-]+", key.lower()) if item)
    if not tokens:
        return False
    if tokens[-1] in {"audio", "authorization", "payload", "secret", "text", "transcript"}:
        return True
    if tokens[-2:] == ("api", "key"):
        return True
    return tokens[-1] in {"body", "bytes", "content", "data", "delta", "value"} and any(
        item in {"audio", "payload", "text", "transcript"} for item in tokens[:-1]
    )


def _collect_replacements(document: dict[str, Any], surface: str) -> dict[str, str]:
    replacements: dict[str, str] = {}
    counts: dict[str, int] = {}
    for key, value in _walk(document):
        category = _identity_category(key) if key is not None else None
        if category is None or not isinstance(value, str):
            continue
        source = str(value)
        existing = replacements.get(source)
        if existing is not None:
            continue
        counts[category] = counts.get(category, 0) + 1
        replacements[source] = _pseudonym(surface, category, counts[category])
    return replacements


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace(child, replacements) for child in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def _assert_metadata_only(document: dict[str, Any]) -> None:
    profile = document.get("profile", {})
    if document.get("raw_otlp_chunks"):
        raise SystemExit("real-capture fixture cannot retain raw OTLP")
    if profile.get("media_refs"):
        raise SystemExit("real-capture fixture cannot retain media references")
    for policy in profile.get("privacy", {}).get("capture_classes", []):
        if policy.get("captured") and policy.get("capture_class") != "metadata":
            raise SystemExit("real-capture fixture may capture only metadata")
    for key, value in _walk(document):
        if key is not None and _is_content_field(key) and value not in (None, "", [], {}):
            raise SystemExit(f"content-bearing field survived capture policy: {key}")
        if isinstance(value, str) and (
            _CREDENTIAL_VALUE.search(value) or _EMAIL_VALUE.search(value)
        ):
            raise SystemExit("credential-like or email value survived capture scrubbing")


def scrub_capture(source: pathlib.Path, destination: pathlib.Path, surface: str) -> None:
    raw = source.read_bytes()
    document = json.loads(raw)
    _assert_metadata_only(document)
    replacements = _collect_replacements(document, surface)
    scrubbed = _replace(document, replacements)
    _assert_metadata_only(scrubbed)
    encoded = json.dumps(scrubbed, sort_keys=True, separators=(",", ":")).encode()
    bundle = decode_incident_json(encoded, validate=False)
    report = validate_incident(bundle)
    if not report.ok:
        codes = sorted({issue.code for issue in report.errors})
        raise SystemExit(f"scrubbed capture is invalid: {codes}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encode_incident_json(bundle, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("destination", type=pathlib.Path)
    parser.add_argument("--surface", required=True)
    arguments = parser.parse_args()
    scrub_capture(arguments.source, arguments.destination, arguments.surface)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
