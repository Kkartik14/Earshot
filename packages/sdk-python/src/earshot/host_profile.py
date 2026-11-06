"""Offline conformance checks for standard OTLP/JSON host-profile requests.

This module deliberately uses only the standard library. It is suitable for an
emitter's build checks and never opens a database, starts a provider, or imports
the OTLP server extras.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .versions import (
    HOST_PROFILE_VERSION,
    LEGACY_HOST_PROFILE_VERSION,
    SUPPORTED_HOST_PROFILE_VERSIONS,
)

HOST_SCOPE = "earshot.host"

_TRANSCRIPT = "earshot.transcript.text"
_TURN_REPORTED_LATENCY = "earshot.turn.reported_latency_ms"
_STT_AUDIO_SECONDS = "earshot.stt.usage.audio_seconds"
_TTS_CHARACTER_COUNT = "earshot.tts.usage.character_count"
_PROFILE_0_2_ATTRIBUTES = frozenset({_TRANSCRIPT, _STT_AUDIO_SECONDS, _TTS_CHARACTER_COUNT})
_HOST_ATTRIBUTES = _PROFILE_0_2_ATTRIBUTES | {_TURN_REPORTED_LATENCY}
_ATTRIBUTES_BY_SPAN = {
    _TRANSCRIPT: frozenset({"user_speech", "agent_speech"}),
    _TURN_REPORTED_LATENCY: frozenset({"turn"}),
    _STT_AUDIO_SECONDS: frozenset({"stt"}),
    _TTS_CHARACTER_COUNT: frozenset({"tts"}),
}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_SAFE_JSON_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class HostProfileIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class HostProfileReport:
    host_profile_versions: tuple[str, ...]
    spans_checked: int
    issues: tuple[HostProfileIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "host_profile_versions": list(self.host_profile_versions),
            "spans_checked": self.spans_checked,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _issue(issues: list[HostProfileIssue], code: str, path: str, message: str) -> None:
    issues.append(HostProfileIssue(code=code, path=path, message=message))


def _host_attributes(
    span: Mapping[str, Any], path: str, issues: list[HostProfileIssue]
) -> dict[str, list[tuple[str, Any]]]:
    raw = span.get("attributes", [])
    if not isinstance(raw, list):
        _issue(issues, "invalid_attributes", f"{path}.attributes", "must be an array")
        return {}
    selected: dict[str, list[tuple[str, Any]]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            _issue(
                issues,
                "invalid_attribute",
                f"{path}.attributes[{index}]",
                "must be an OTLP key-value object",
            )
            continue
        key = item.get("key")
        if not isinstance(key, str) or key not in _HOST_ATTRIBUTES:
            continue
        value = item.get("value")
        if not isinstance(value, Mapping) or len(value) != 1:
            _issue(
                issues,
                "invalid_any_value",
                f"{path}.attributes[{index}].value",
                "must contain exactly one OTLP AnyValue field",
            )
            continue
        kind, scalar = next(iter(value.items()))
        selected.setdefault(key, []).append((kind, scalar))
    return selected


def _int64(value: Any) -> int | None:
    """Decode the ProtoJSON int64 number/string forms without losing float precision."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else None
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > _MAX_SAFE_JSON_INTEGER
        ):
            return None
        parsed = int(value)
        return parsed if _INT64_MIN <= parsed <= _INT64_MAX else None
    if isinstance(value, str):
        if not value or value.strip() != value or " " in value:
            return None
        try:
            parsed = int(value)
        except ValueError:
            try:
                numeric = float(value)
            except (OverflowError, ValueError):
                return None
            if (
                not math.isfinite(numeric)
                or not numeric.is_integer()
                or abs(numeric) > _MAX_SAFE_JSON_INTEGER
            ):
                return None
            parsed = int(numeric)
        return parsed if _INT64_MIN <= parsed <= _INT64_MAX else None
    return None


def _double(value: Any) -> float | None:
    """Decode a finite ProtoJSON double number or numeric string."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard bare NaN/Infinity JSON extensions."""

    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _check_value(
    key: str,
    kind: str,
    value: Any,
    span_name: str,
    path: str,
    issues: list[HostProfileIssue],
) -> None:
    allowed_spans = _ATTRIBUTES_BY_SPAN[key]
    if span_name not in allowed_spans:
        _issue(
            issues,
            "wrong_span",
            path,
            "is only valid on " + " or ".join(sorted(allowed_spans)) + " spans",
        )
        return
    expected_kind = {
        _TRANSCRIPT: "stringValue",
        _TURN_REPORTED_LATENCY: "doubleValue",
        _STT_AUDIO_SECONDS: "doubleValue",
        _TTS_CHARACTER_COUNT: "intValue",
    }[key]
    if kind != expected_kind:
        _issue(
            issues,
            "wrong_attribute_type",
            path,
            f"must use OTLP AnyValue {expected_kind}",
        )
        return
    if key == _TRANSCRIPT:
        if not isinstance(value, str) or not value.strip():
            _issue(issues, "invalid_transcript_text", path, "must be a nonblank string")
    elif key == _TURN_REPORTED_LATENCY:
        latency = _double(value)
        if latency is None or latency <= 0:
            _issue(
                issues,
                "invalid_turn_reported_latency",
                path,
                "must be a finite positive double when present",
            )
    elif key == _STT_AUDIO_SECONDS:
        seconds = _double(value)
        if seconds is None or seconds <= 0:
            _issue(issues, "invalid_stt_audio_seconds", path, "must be a finite positive double")
    else:
        character_count = _int64(value)
        if character_count is None or character_count <= 0:
            _issue(issues, "invalid_tts_character_count", path, "must be a positive int64")


def check_host_profile(payload: bytes | str | Mapping[str, Any]) -> HostProfileReport:
    """Check host-profile 0.1.0/0.2.0 fields in one OTLP/JSON trace request.

    Non-host scopes and unknown extension attributes are ignored. Missing or empty
    scope versions retain profile 0.1.0 behavior. Profile 0.2.0 attributes are
    rejected under every other version and on every span role not defined for them.
    The report contains paths and stable codes only; it never echoes attribute values.
    """

    issues: list[HostProfileIssue] = []
    if isinstance(payload, (bytes, str)):
        try:
            text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
            request = json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError, TypeError):
            _issue(issues, "invalid_json", "$", "must be a UTF-8 OTLP/JSON request")
            return HostProfileReport((), 0, tuple(issues))
    else:
        request = payload
    if not isinstance(request, Mapping):
        _issue(issues, "invalid_request", "$", "must be an OTLP ExportTraceServiceRequest object")
        return HostProfileReport((), 0, tuple(issues))
    resource_spans = request.get("resourceSpans")
    if not isinstance(resource_spans, list):
        _issue(issues, "invalid_request", "$.resourceSpans", "must be an array")
        return HostProfileReport((), 0, tuple(issues))

    versions: set[str] = set()
    spans_checked = 0
    for resource_index, resource_span in enumerate(resource_spans):
        resource_path = f"$.resourceSpans[{resource_index}]"
        if not isinstance(resource_span, Mapping):
            _issue(
                issues,
                "invalid_resource_span",
                resource_path,
                "must be an OTLP resource spans object",
            )
            continue
        scope_spans = resource_span.get("scopeSpans", [])
        if not isinstance(scope_spans, list):
            _issue(issues, "invalid_scope_spans", f"{resource_path}.scopeSpans", "must be an array")
            continue
        for scope_index, scope_span in enumerate(scope_spans):
            scope_path = f"{resource_path}.scopeSpans[{scope_index}]"
            if not isinstance(scope_span, Mapping):
                _issue(
                    issues,
                    "invalid_scope_span",
                    scope_path,
                    "must be an OTLP scope spans object",
                )
                continue
            scope = scope_span.get("scope")
            if scope is None:
                scope = {}
            if not isinstance(scope, Mapping):
                _issue(issues, "invalid_scope", f"{scope_path}.scope", "must be an object")
                continue
            scope_name = scope.get("name")
            if scope_name is not None and not isinstance(scope_name, str):
                _issue(
                    issues,
                    "invalid_scope_name",
                    f"{scope_path}.scope.name",
                    "must be a string",
                )
                continue
            if scope_name != HOST_SCOPE:
                continue
            raw_version = scope.get("version", "")
            version = LEGACY_HOST_PROFILE_VERSION if raw_version in (None, "") else raw_version
            if not isinstance(version, str):
                version = "<invalid>"
            if version not in SUPPORTED_HOST_PROFILE_VERSIONS:
                _issue(
                    issues,
                    "unsupported_version",
                    f"{scope_path}.scope.version",
                    "must be 0.1.0 or 0.2.0",
                )
                continue
            versions.add(version)
            spans = scope_span.get("spans", [])
            if not isinstance(spans, list):
                _issue(issues, "invalid_spans", f"{scope_path}.spans", "must be an array")
                continue
            for span_index, span in enumerate(spans):
                span_path = f"{scope_path}.spans[{span_index}]"
                if not isinstance(span, Mapping):
                    _issue(issues, "invalid_span", span_path, "must be an OTLP span object")
                    continue
                spans_checked += 1
                name = span.get("name")
                span_name = name if isinstance(name, str) else ""
                attributes = _host_attributes(span, span_path, issues)
                for key, values in attributes.items():
                    attr_path = f'{span_path}.attributes["{key}"]'
                    if len(values) > 1:
                        _issue(issues, "duplicate_attribute", attr_path, "must occur at most once")
                        continue
                    if key in _PROFILE_0_2_ATTRIBUTES and version != HOST_PROFILE_VERSION:
                        _issue(
                            issues,
                            "wrong_scope_version",
                            attr_path,
                            "is defined only by host profile 0.2.0",
                        )
                        continue
                    kind, value = values[0]
                    _check_value(key, kind, value, span_name, attr_path, issues)

    return HostProfileReport(tuple(sorted(versions)), spans_checked, tuple(issues))
