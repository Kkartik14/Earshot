"""Offline conformance checks for standard OTLP/JSON host-profile requests.

This module deliberately uses only the standard library. It is suitable for an
emitter's build checks and never opens a database, starts a provider, or imports
the OTLP server extras.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .limits import DEFAULT_MAX_VALUE_BYTES
from .versions import (
    LEGACY_HOST_PROFILE_VERSION,
    SUPPORTED_HOST_PROFILE_VERSIONS,
)

HOST_SCOPE = "earshot.host"
HOST_PROFILE_0_2_VERSIONS = frozenset(SUPPORTED_HOST_PROFILE_VERSIONS).difference(
    {LEGACY_HOST_PROFILE_VERSION}
)

_TRANSCRIPT = "earshot.transcript.text"
_TURN_REPORTED_LATENCY = "earshot.turn.reported_latency_ms"
_STT_AUDIO_SECONDS = "earshot.stt.usage.audio_seconds"
_TTS_CHARACTER_COUNT = "earshot.tts.usage.character_count"
_TURN_ID = "earshot.turn.id"
_STT_PROVIDER = "gen_ai.provider.name"
_STT_MODEL = "gen_ai.request.model"
_PROFILE_0_2_ATTRIBUTES = frozenset({_TRANSCRIPT, _STT_AUDIO_SECONDS, _TTS_CHARACTER_COUNT})
_STRUCTURAL_ATTRIBUTES = frozenset({_TURN_ID, _STT_PROVIDER, _STT_MODEL})
_HOST_ATTRIBUTES = _PROFILE_0_2_ATTRIBUTES | _STRUCTURAL_ATTRIBUTES | {_TURN_REPORTED_LATENCY}
_HOST_PROFILE_MARKERS = _PROFILE_0_2_ATTRIBUTES | {_TURN_REPORTED_LATENCY, _TURN_ID}
_ATTRIBUTES_BY_SPAN = {
    _TRANSCRIPT: frozenset({"user_speech", "agent_speech"}),
    _TURN_REPORTED_LATENCY: frozenset({"turn"}),
    _STT_AUDIO_SECONDS: frozenset({"stt"}),
    _TTS_CHARACTER_COUNT: frozenset({"tts"}),
}
HOST_PROFILE_VALUE_KEYS = frozenset(_ATTRIBUTES_BY_SPAN)
_MAX_SAFE_JSON_INTEGER = 2**53 - 1
_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1
_MAX_UINT64 = 2**64 - 1
_PROTOJSON_NUMERIC_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_PROTOJSON_UINT_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_TRACE_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
_SPAN_ID_RE = re.compile(r"[0-9a-fA-F]{16}\Z")
_HOST_SPAN_NAMES = frozenset({"call", "turn", "user_speech", "agent_speech", "stt", "llm", "tts"})
_CHILD_SPAN_NAMES = frozenset({"user_speech", "agent_speech", "stt", "llm", "tts"})
_PROTO_SNAKE_CASE_FIELDS = frozenset(
    {
        "resource_spans",
        "scope_spans",
        "schema_url",
        "dropped_attributes_count",
        "entity_refs",
        "id_keys",
        "description_keys",
        "string_value",
        "bool_value",
        "int_value",
        "double_value",
        "array_value",
        "kvlist_value",
        "bytes_value",
        "string_value_strindex",
        "key_strindex",
        "dropped_events_count",
        "dropped_links_count",
        "trace_id",
        "span_id",
        "parent_span_id",
        "start_time_unix_nano",
        "end_time_unix_nano",
        "trace_state",
        "is_remote",
        "observed_time_unix_nano",
    }
)
_OTLP_ANY_VALUE_FIELDS = frozenset(
    {
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "arrayValue",
        "kvlistValue",
        "bytesValue",
    }
)


def host_value(key: str, span_name: str, version: str | None, value: Any) -> Any | None:
    """Decode one host-profile field when its span and scope version define it.

    This is the shared value gate for the offline checker, Revision materializer and
    Call projection. ``None`` means the value is not valid for that field.
    """

    if key not in _ATTRIBUTES_BY_SPAN or span_name not in _ATTRIBUTES_BY_SPAN[key]:
        return None
    if version not in SUPPORTED_HOST_PROFILE_VERSIONS:
        return None
    if key in _PROFILE_0_2_ATTRIBUTES and version not in HOST_PROFILE_0_2_VERSIONS:
        return None
    if key == _TRANSCRIPT:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            if len(value.encode("utf-8")) > DEFAULT_MAX_VALUE_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        return value
    if key == _TTS_CHARACTER_COUNT:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value <= _MAX_SAFE_JSON_INTEGER
        ):
            return value
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) and value > 0 else None
    if isinstance(value, Decimal):
        return value if value.is_finite() and value > 0 else None
    return value if value > 0 else None


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
    raw = span.get("attributes")
    if raw is None:
        raw = []
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
        if not isinstance(value, Mapping):
            _issue(
                issues,
                "invalid_any_value",
                f"{path}.attributes[{index}].value",
                "must be an OTLP AnyValue object",
            )
            continue
        known_values = [
            (kind, scalar)
            for kind, scalar in value.items()
            if kind in _OTLP_ANY_VALUE_FIELDS and scalar is not None
        ]
        if not known_values:
            _issue(
                issues,
                "invalid_any_value",
                f"{path}.attributes[{index}].value",
                "must contain a recognized OTLP AnyValue field",
            )
            continue
        if len(known_values) > 1:
            _issue(
                issues,
                "invalid_any_value",
                f"{path}.attributes[{index}].value",
                "must not set multiple OTLP AnyValue oneof fields",
            )
            continue
        kind, scalar = known_values[0]
        selected.setdefault(key, []).append((kind, scalar))
    return selected


def _json_safe_int64(value: Any) -> int | None:
    """Decode a JSON-safe integer from the ProtoJSON int64 number/string forms."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) <= _MAX_SAFE_JSON_INTEGER else None
    if isinstance(value, Decimal):
        return _decimal_json_safe_integer(value)
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > _MAX_SAFE_JSON_INTEGER
        ):
            return None
        parsed = int(value)
        return parsed if abs(parsed) <= _MAX_SAFE_JSON_INTEGER else None
    if isinstance(value, str):
        if not _PROTOJSON_NUMERIC_RE.fullmatch(value):
            return None
        try:
            numeric = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        return _decimal_json_safe_integer(numeric)
    return None


def _decimal_json_safe_integer(value: Decimal) -> int | None:
    """Convert an exact decimal integer within the interoperable JSON range."""

    if not value.is_finite() or abs(value) > _MAX_SAFE_JSON_INTEGER:
        return None
    try:
        if value != value.to_integral_value():
            return None
        parsed = int(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if abs(parsed) <= _MAX_SAFE_JSON_INTEGER else None


def _int64(value: Any) -> int | None:
    """Decode an exact signed int64 from ProtoJSON's numeric forms."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _MIN_INT64 <= value <= _MAX_INT64 else None
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or not _MIN_INT64 <= value <= _MAX_INT64
        ):
            return None
        return int(value)
    if isinstance(value, str) and _PROTOJSON_NUMERIC_RE.fullmatch(value):
        try:
            decimal_value = Decimal(value)
            if (
                decimal_value.is_finite()
                and decimal_value == decimal_value.to_integral_value()
                and _MIN_INT64 <= decimal_value <= _MAX_INT64
            ):
                return int(decimal_value)
        except (InvalidOperation, ValueError, OverflowError):
            return None
    if isinstance(value, Decimal) and (
        value.is_finite()
        and value == value.to_integral_value()
        and _MIN_INT64 <= value <= _MAX_INT64
    ):
        return int(value)
    return None


def _uint64(value: Any) -> int | None:
    """Decode a fixed64 timestamp in ProtoJSON's unsigned decimal forms."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_UINT64 else None
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value() and 0 <= value <= _MAX_UINT64:
            return int(value)
        return None
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and 0 <= value <= _MAX_UINT64:
            return int(value)
        return None
    if isinstance(value, str) and _PROTOJSON_UINT_RE.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed <= _MAX_UINT64 else None
    return None


def _double(value: Any) -> float | None:
    """Decode a finite ProtoJSON double number or numeric string."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, str) and not _PROTOJSON_NUMERIC_RE.fullmatch(value):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard bare NaN/Infinity JSON extensions."""

    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _flag_snake_case_fields(value: Any, path: str, issues: list[HostProfileIssue]) -> None:
    pending = [(value, path)]
    visited: set[int] = set()
    while pending:
        current, current_path = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, child in current.items():
                child_path = f"{current_path}.{key}" if isinstance(key, str) else current_path
                if key in _PROTO_SNAKE_CASE_FIELDS:
                    _issue(
                        issues,
                        "snake_case_proto_field",
                        child_path,
                        "OTLP/JSON field names must use lowerCamelCase",
                    )
                pending.append((child, child_path))
        elif isinstance(current, list):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(
                (child, f"{current_path}[{index}]")
                for index, child in enumerate(current)
            )


def _scope_spans(request: Mapping[str, Any]):
    resources = request.get("resourceSpans")
    if not isinstance(resources, list):
        return
    for resource_index, resource_span in enumerate(resources):
        if not isinstance(resource_span, Mapping):
            continue
        scopes = resource_span.get("scopeSpans")
        if not isinstance(scopes, list):
            continue
        for scope_index, scope_span in enumerate(scopes):
            if isinstance(scope_span, Mapping):
                yield (
                    f"$.resourceSpans[{resource_index}].scopeSpans[{scope_index}]",
                    scope_span,
                )


def _span_index(
    request: Mapping[str, Any],
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], tuple[str, str]]]:
    ids: set[tuple[str, str]] = set()
    details: dict[tuple[str, str], tuple[str, str]] = {}
    for _, scope_span in _scope_spans(request):
        scope = scope_span.get("scope")
        scope_name = scope.get("name", "") if isinstance(scope, Mapping) else ""
        spans = scope_span.get("spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, Mapping):
                continue
            trace_id, span_id = span.get("traceId"), span.get("spanId")
            if (
                isinstance(trace_id, str)
                and _TRACE_ID_RE.fullmatch(trace_id)
                and isinstance(span_id, str)
                and _SPAN_ID_RE.fullmatch(span_id)
            ):
                key = (trace_id.lower(), span_id.lower())
                ids.add(key)
                details[key] = (str(span.get("name", "")), str(scope_name))
    return ids, details


def _scope_has_host_fields(scope_span: Mapping[str, Any]) -> bool:
    spans = scope_span.get("spans")
    if not isinstance(spans, list):
        return False
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        attributes = span.get("attributes")
        if not isinstance(attributes, list):
            continue
        if any(
            isinstance(item, Mapping) and item.get("key") in _HOST_PROFILE_MARKERS
            for item in attributes
        ):
            return True
    return False


def _structural_string(
    attributes: dict[str, list[tuple[str, Any]]],
    key: str,
    *,
    path: str,
    missing_code: str,
    invalid_code: str,
    issues: list[HostProfileIssue],
) -> str | None:
    values = attributes.get(key, [])
    if not values:
        _issue(issues, missing_code, f'{path}.attributes["{key}"]', "is required")
        return None
    kind, value = values[0]
    if kind != "stringValue" or not isinstance(value, str) or not value.strip():
        _issue(
            issues,
            invalid_code,
            f'{path}.attributes["{key}"]',
            "must be a nonblank string",
        )
        return None
    return value


def _check_value(
    key: str,
    kind: str,
    value: Any,
    span_name: str,
    version: str,
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
    allowed_kinds = {
        _TRANSCRIPT: {"stringValue"},
        _TURN_REPORTED_LATENCY: {"doubleValue", "intValue"},
        _STT_AUDIO_SECONDS: {"doubleValue", "intValue"},
        _TTS_CHARACTER_COUNT: {"intValue"},
    }[key]
    if kind not in allowed_kinds:
        _issue(
            issues,
            "wrong_attribute_type",
            path,
            "must use OTLP AnyValue " + " or ".join(sorted(allowed_kinds)),
        )
        return
    if key == _TRANSCRIPT:
        decoded = value
        message = f"must be a nonblank UTF-8 string no larger than {DEFAULT_MAX_VALUE_BYTES} bytes"
    elif key == _TURN_REPORTED_LATENCY:
        decoded = _double(value) if kind == "doubleValue" else _int64(value)
        message = "must be a finite positive double or int64 when present"
    elif key == _STT_AUDIO_SECONDS:
        decoded = _double(value) if kind == "doubleValue" else _int64(value)
        message = "must be a finite positive double or int64"
    else:
        decoded = _json_safe_int64(value)
        message = "must be a positive JSON-safe int64 no greater than 9007199254740991"
    if host_value(key, span_name, version, decoded) is None:
        code = {
            _TRANSCRIPT: "invalid_transcript_text",
            _TURN_REPORTED_LATENCY: "invalid_turn_reported_latency",
            _STT_AUDIO_SECONDS: "invalid_stt_audio_seconds",
            _TTS_CHARACTER_COUNT: "invalid_tts_character_count",
        }[key]
        _issue(issues, code, path, message)


def check_host_profile(payload: bytes | str | Mapping[str, Any]) -> HostProfileReport:
    """Check host-profile 0.1.0/0.2.0 fields in one OTLP/JSON trace request.

    Non-host scopes and unknown extension attributes are ignored. Missing or empty
    scope versions retain profile 0.1.0 behavior. Profile 0.2.0 attributes are
    rejected under every other version and on every span role not defined for them.
    The report contains paths and stable codes only; it never echoes attribute values.
    Byte and string inputs preserve ProtoJSON numeric lexemes for exact validation;
    already-decoded mappings are checked by the numeric values they contain.
    """

    issues: list[HostProfileIssue] = []
    if isinstance(payload, (bytes, str)):
        try:
            text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
            request = json.loads(
                text,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, TypeError, InvalidOperation, RecursionError):
            _issue(issues, "invalid_json", "$", "must be a UTF-8 OTLP/JSON request")
            return HostProfileReport((), 0, tuple(issues))
    else:
        request = payload
    if not isinstance(request, Mapping):
        _issue(issues, "invalid_request", "$", "must be an OTLP ExportTraceServiceRequest object")
        return HostProfileReport((), 0, tuple(issues))
    resource_spans = request.get("resourceSpans")
    if resource_spans is None:
        resource_spans = []
    if not isinstance(resource_spans, list):
        _issue(issues, "invalid_request", "$.resourceSpans", "must be an array")
        return HostProfileReport((), 0, tuple(issues))

    _flag_snake_case_fields(request, "$", issues)
    all_span_ids, span_details = _span_index(request)
    versions: set[str] = set()
    spans_checked = 0
    parent_refs: list[tuple[str, str, str | None, str, bool]] = []
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
        scope_spans = resource_span.get("scopeSpans")
        if scope_spans is None:
            scope_spans = []
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
                resembles_host_scope = (
                    isinstance(scope_name, str)
                    and scope_name.casefold().replace("_", "-") == "earshot-host"
                )
                if resembles_host_scope or _scope_has_host_fields(scope_span):
                    _issue(
                        issues,
                        "wrong_scope",
                        f"{scope_path}.scope.name",
                        f"host-profile spans must use instrumentation scope {HOST_SCOPE!r}",
                    )
                continue
            raw_version = scope.get("version", "")
            version = LEGACY_HOST_PROFILE_VERSION if raw_version in (None, "") else raw_version
            if not isinstance(version, str):
                version = "<invalid>"
            if version not in SUPPORTED_HOST_PROFILE_VERSIONS:
                supported = ", ".join(SUPPORTED_HOST_PROFILE_VERSIONS)
                _issue(
                    issues,
                    "unsupported_version",
                    f"{scope_path}.scope.version",
                    f"must be one of the supported versions: {supported}",
                )
                continue
            versions.add(version)
            spans = scope_span.get("spans")
            if spans is None:
                spans = []
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
                if span_name not in _HOST_SPAN_NAMES:
                    _issue(
                        issues,
                        "unknown_span_name",
                        f"{span_path}.name",
                        "is not a host-profile span name",
                    )

                trace_id = span.get("traceId")
                normalized_trace_id = None
                if not isinstance(trace_id, str) or not _TRACE_ID_RE.fullmatch(trace_id):
                    _issue(
                        issues,
                        "invalid_trace_id",
                        f"{span_path}.traceId",
                        "must be exactly 32 hexadecimal characters",
                    )
                else:
                    normalized_trace_id = trace_id.lower()
                span_id = span.get("spanId")
                normalized_span_id = None
                if not isinstance(span_id, str) or not _SPAN_ID_RE.fullmatch(span_id):
                    _issue(
                        issues,
                        "invalid_span_id",
                        f"{span_path}.spanId",
                        "must be exactly 16 hexadecimal characters",
                    )
                else:
                    normalized_span_id = span_id.lower()
                parent_id = span.get("parentSpanId")
                normalized_parent_id = None
                invalid_parent_id = False
                if parent_id not in (None, ""):
                    if not isinstance(parent_id, str) or not _SPAN_ID_RE.fullmatch(parent_id):
                        invalid_parent_id = True
                        _issue(
                            issues,
                            "invalid_parent_span_id",
                            f"{span_path}.parentSpanId",
                            "must be empty or exactly 16 hexadecimal characters",
                        )
                    else:
                        normalized_parent_id = parent_id.lower()

                start = _uint64(span.get("startTimeUnixNano"))
                end = _uint64(span.get("endTimeUnixNano"))
                if start is None:
                    _issue(
                        issues,
                        "invalid_span_time",
                        f"{span_path}.startTimeUnixNano",
                        "must be an unsigned 64-bit nanosecond timestamp",
                    )
                if end is None:
                    _issue(
                        issues,
                        "invalid_span_time",
                        f"{span_path}.endTimeUnixNano",
                        "must be an unsigned 64-bit nanosecond timestamp",
                    )
                if start is not None and end is not None and start > end:
                    _issue(
                        issues,
                        "span_time_order",
                        span_path,
                        "startTimeUnixNano must not be after endTimeUnixNano",
                    )

                attributes = _host_attributes(span, span_path, issues)
                duplicate_keys: set[str] = set()
                for key, values in attributes.items():
                    attr_path = f'{span_path}.attributes["{key}"]'
                    if len(values) > 1:
                        _issue(issues, "duplicate_attribute", attr_path, "must occur at most once")
                        duplicate_keys.add(key)
                        continue
                    if key in _STRUCTURAL_ATTRIBUTES:
                        continue
                    if key in _PROFILE_0_2_ATTRIBUTES and version not in HOST_PROFILE_0_2_VERSIONS:
                        _issue(
                            issues,
                            "wrong_scope_version",
                            attr_path,
                            "is defined only by host profile 0.2.0",
                        )
                        continue
                    kind, value = values[0]
                    _check_value(key, kind, value, span_name, version, attr_path, issues)

                if span_name != "call" and _TURN_ID not in duplicate_keys:
                    _structural_string(
                        attributes,
                        _TURN_ID,
                        path=span_path,
                        missing_code="missing_turn_id",
                        invalid_code="invalid_turn_id",
                        issues=issues,
                    )
                if span_name == "stt":
                    for key, missing_code, invalid_code in (
                        (_STT_PROVIDER, "missing_stt_provider", "invalid_stt_provider"),
                        (_STT_MODEL, "missing_stt_model", "invalid_stt_model"),
                    ):
                        if key not in duplicate_keys:
                            _structural_string(
                                attributes,
                                key,
                                path=span_path,
                                missing_code=missing_code,
                                invalid_code=invalid_code,
                                issues=issues,
                            )
                if normalized_trace_id is not None and normalized_span_id is not None:
                    parent_refs.append(
                        (
                            span_path,
                            normalized_trace_id,
                            normalized_parent_id,
                            span_name,
                            invalid_parent_id,
                        )
                    )

    for span_path, trace_id, parent_id, span_name, invalid_parent_id in parent_refs:
        if invalid_parent_id:
            continue
        if parent_id is None:
            if span_name in _CHILD_SPAN_NAMES:
                _issue(
                    issues,
                    "missing_parent",
                    f"{span_path}.parentSpanId",
                    "must identify the enclosing turn span",
                )
            continue
        parent_key = (trace_id, parent_id)
        if parent_key not in all_span_ids:
            _issue(
                issues,
                "missing_parent",
                f"{span_path}.parentSpanId",
                "does not identify a span in this request with the same traceId",
            )
        elif span_name in _CHILD_SPAN_NAMES:
            parent_name, parent_scope = span_details.get(parent_key, ("", ""))
            if (parent_name, parent_scope) != ("turn", HOST_SCOPE):
                _issue(
                    issues,
                    "parent_not_turn",
                    f"{span_path}.parentSpanId",
                    "must identify a host-profile turn span",
                )

    if spans_checked == 0 and not any(
        issue.code in {"wrong_scope", "unsupported_version"} for issue in issues
    ):
        _issue(issues, "no_host_spans", "$.resourceSpans", "must contain at least one host span")

    return HostProfileReport(tuple(sorted(versions)), spans_checked, tuple(issues))
