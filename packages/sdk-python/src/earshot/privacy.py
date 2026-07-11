"""Capture-policy enforcement for framework telemetry.

Filtering happens before data enters queues, protobufs, logs, or storage.  The
metadata-only default is deliberately allowlist based: unknown attributes are
treated as potentially sensitive rather than silently retained.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from .contract import IncidentBundle


class CaptureClass(StrEnum):
    METADATA = "metadata"
    EXTENSION_PAYLOAD = "extension_payload"
    TRANSCRIPT = "transcript"
    AUDIO = "audio"
    TOOL_PAYLOAD = "tool_payload"
    MODEL_PAYLOAD = "model_payload"
    DIAGNOSTIC_PAYLOAD = "diagnostic_payload"
    IDENTITY = "identity"
    RAW_OTLP = "raw_otlp"


@dataclass(frozen=True)
class ConsentConfig:
    status: str
    legal_basis: str | None = None
    recorded_at_unix_nano: str | None = None
    authority: str | None = None


@dataclass(frozen=True)
class RedactionConfig:
    policy_id: str
    policy_version: str
    status: str
    findings_count: int | None = None
    redacted_count: int | None = None
    executed_at_unix_nano: str | None = None


@dataclass(frozen=True)
class RetentionConfig:
    expires_at_unix_nano: str | None = None
    ttl_nano: str | None = None
    policy_id: str | None = None


@dataclass(frozen=True)
class ExportConfig:
    allowed: bool
    destinations: tuple[str, ...] = ()
    policy_id: str | None = None


@dataclass(frozen=True)
class CaptureGovernance:
    consent: ConsentConfig | None = None
    redaction: RedactionConfig | None = None
    retention: RetentionConfig | None = None
    export: ExportConfig | None = None


@dataclass(frozen=True)
class CapturePolicy:
    """The SDK default keeps only explicitly safe metadata."""

    enabled: frozenset[CaptureClass] = frozenset({CaptureClass.METADATA})
    policy_id: str = "earshot.metadata-only"
    policy_version: str = "1"
    governance: Mapping[CaptureClass, CaptureGovernance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if CaptureClass.METADATA not in self.enabled:
            raise ValueError("metadata capture is required for an incident envelope")
        for capture_class in self.governance:
            if not isinstance(capture_class, CaptureClass):
                raise ValueError("governance keys must be CaptureClass values")

    @classmethod
    def metadata_only(cls) -> CapturePolicy:
        return cls()

    def allows(self, capture_class: CaptureClass) -> bool:
        return capture_class in self.enabled


@dataclass(frozen=True)
class Omission:
    field_key_sha256: str
    capture_class: CaptureClass
    reason: str = "capture_class_disabled"

    def as_dict(self) -> dict[str, str]:
        return {
            "field_key_sha256": self.field_key_sha256,
            "capture_class": self.capture_class.value,
            "reason": self.reason,
        }


# This is intentionally narrow. Values for these keys are operational metadata,
# identifiers, booleans, counts, or model names rather than user content.
_SAFE_EXACT = {
    "service.name",
    "service.namespace",
    "service.version",
    "service.instance.id",
    "deployment.environment.name",
    "telemetry.sdk.name",
    "telemetry.sdk.language",
    "telemetry.sdk.version",
    "gen_ai.operation.name",
    "gen_ai.conversation.id",
    "gen_ai.system",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.output.type",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "error.type",
    "session.id",
    "field_key_sha256",
    "earshot.operation.name",
    "earshot.operation.id",
    "earshot.request.id",
    "earshot.framework.name",
    "earshot.framework.operation.name",
    "earshot.framework.version",
    "earshot.participant.id",
    "earshot.stream.id",
    "earshot.turn.id",
    "earshot.turn.index",
    "earshot.link.type",
    "earshot.link.target_scope",
    "earshot.clock.domain.id",
    "earshot.time.monotonic_nano",
    "earshot.time.observed_unix_nano",
    "earshot.time.uncertainty_nano",
    "earshot.evidence.source",
    "earshot.evidence.observer",
    "earshot.source.field",
    "earshot.source.event.name",
    "earshot.source.name_sha256",
    "earshot.source.status_sha256",
    "earshot.source.schema_url_sha256",
    "earshot.source.resource_schema_url_sha256",
    "earshot.evidence.method",
    "earshot.evidence.method_version",
    "earshot.evidence.confidence",
    "earshot.evidence.availability",
    "earshot.event.id",
    "earshot.event.name",
    "earshot.privacy.capture_class",
    "earshot.quality.aggregation",
    # LiveKit source-native numeric facts. These keys are metadata-safe; other
    # unknown lk.* attributes remain denied by default.
    "lk.response.ttft",
    "lk.response.ttfb",
    "lk.eou.endpointing_delay",
    "lk.eou.transcription_delay",
    "lk.end_of_turn_delay",
    "lk.e2e_latency",
    "lk.generation_id",
    "lk.parent_generation_id",
    "lk.participant_id",
    "lk.participant_kind",
    "lk.speech_id",
    "lk.job_id",
    "lk.interrupted",
    "lk.transcript_confidence",
    "lk.transcription_delay",
    "turn.was_interrupted",
    # Current Pipecat OTel correlation and latency attributes.
    "conversation.id",
    "conversation.type",
    "earshot.conversation.item.id",
    "earshot.conversation.role",
    "turn.number",
    "turn.type",
    "turn.duration_seconds",
    "metrics.ttfb",
    "metrics.character_count",
}

_SAFE_PREFIXES = (
    "gen_ai.usage.",
    "earshot.metric.",
    "earshot.duration.",
)
_IJSON_INTEGER_MAX = 9_007_199_254_740_991
_UINT64_DECIMAL_MAX = "18446744073709551615"
_DIGEST_METADATA_KEYS = frozenset(
    {
        "earshot.source.name_sha256",
        "earshot.source.status_sha256",
        "earshot.source.schema_url_sha256",
        "earshot.source.resource_schema_url_sha256",
        "field_key_sha256",
    }
)

_BOOLEAN_METADATA_KEYS = frozenset(
    {
        "lk.interrupted",
        "turn.was_interrupted",
        "earshot.metric.connection.reused",
        "earshot.metric.interruption.accepted",
        "earshot.metric.interruption.resumed",
        "earshot.metric.request.cancelled",
        "earshot.metric.request.streamed",
    }
)
_INTEGER_METADATA_KEYS = frozenset(
    {
        "earshot.turn.index",
        "turn.number",
        "metrics.character_count",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    }
)
_NUMERIC_METADATA_KEYS = frozenset(
    {
        "lk.response.ttft",
        "lk.response.ttfb",
        "lk.eou.endpointing_delay",
        "lk.eou.transcription_delay",
        "lk.end_of_turn_delay",
        "lk.e2e_latency",
        "lk.transcript_confidence",
        "lk.transcription_delay",
        "turn.duration_seconds",
        "metrics.ttfb",
    }
)
_DECIMAL_METADATA_KEYS = frozenset(
    {
        "earshot.time.monotonic_nano",
        "earshot.time.observed_unix_nano",
        "earshot.time.uncertainty_nano",
    }
)
_SEMANTIC_METADATA_KEYS = frozenset(
    {
        "earshot.operation.name",
        "earshot.framework.name",
        "earshot.framework.operation.name",
        "earshot.link.type",
        "earshot.link.target_scope",
        "earshot.evidence.source",
        "earshot.evidence.observer",
        "earshot.evidence.method",
        "earshot.evidence.confidence",
        "earshot.evidence.availability",
        "earshot.event.name",
        "earshot.privacy.capture_class",
        "earshot.quality.aggregation",
        "gen_ai.operation.name",
        "gen_ai.output.type",
        "conversation.type",
        "earshot.conversation.role",
        "turn.type",
    }
)
_VERSION_METADATA_KEYS = frozenset(
    {
        "service.version",
        "telemetry.sdk.version",
        "earshot.framework.version",
        "earshot.evidence.method_version",
    }
)
_SOURCE_METADATA_KEYS = frozenset(
    {
        "earshot.source.field",
        "earshot.source.event.name",
    }
)

SAFE_OPERATION_NAMES = frozenset(
    {
        "agent",
        "avatar",
        "framework_metric",
        "framework_operation",
        "interruption_detection",
        "llm",
        "render",
        "stt",
        "tool",
        "transport_receive",
        "transport_send",
        "tts",
        "turn_detection",
        "vad",
    }
)

SAFE_EVENT_NAMES = frozenset(
    {
        "earshot.audio.first_byte_sent",
        "earshot.audio.first_packet_received",
        "earshot.audio.render.started",
        "earshot.interruption.accepted",
        "earshot.interruption.detected",
        "earshot.interruption.ignored",
        "earshot.response.first_audio_generated",
        "earshot.response.first_token",
        "earshot.speech.ended",
        "earshot.transport.reconnecting",
        "earshot.turn.committed",
        "framework.event",
        "otel.span_event",
    }
)

_SAFE_SOURCE_LABELS = frozenset(
    {
        "LiveKit span attributes",
        "lk.llm_metrics",
        "lk.realtime_model_metrics",
        "lk.tts_metrics",
        "agent_false_interruption",
        "agent_session",
        "agent_turn",
        "conversation_item_added.item.interrupted",
        "conversation_item_added.item.metrics",
        "eot_inference_metrics",
        "eotinferencemetrics",
        "eou_metrics",
        "eoumetrics",
        "eou_detection",
        "exception",
        "function_tool",
        "llm",
        "llm generation",
        "llm_node",
        "llm_request",
        "llm_request_run",
        "llm_tool_call",
        "llm_tool_result",
        "metrics.ttfb",
        "overlapping_speech",
        "realtime_model_metrics",
        "stt",
        "stt processing",
        "stt_metrics",
        "sttmetrics",
        "tts",
        "tts synthesis",
        "tts_metrics",
        "tts_node",
        "tts_request",
        "tts_request_run",
        "ttsmetrics",
        "turn detection",
        "turn",
        "turn.was_interrupted",
        "user_interruption_detected",
        "user_turn",
        "vad_metrics",
        "vadmetrics",
    }
)
_SAFE_SEMANTIC_LABEL = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_SHA256_LABEL = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_VERSION_LABEL = re.compile(
    r"^(?:[a-z][a-z0-9_.-]{0,127}|v?[0-9]+(?:\.[0-9]+)*(?:[-+][a-z0-9.-]+)?)$"
)
_SAFE_PROVENANCE_LABELS = frozenset(
    {
        "ChatMessage.metrics",
        "ITU-T P.563",
        "POST",
        "RTCPeerConnection.getStats",
        "getOutputTimestamp",
        "getStats",
    }
)
_SAFE_MEASUREMENT_LABELS = frozenset({"packet loss ratio", "roundTripTime"})
_SAFE_MEASUREMENT_UNITS = frozenset({"1", "MOS-LQO"})
_SAFE_ERROR_LABELS = frozenset(
    {
        "CancelledError",
        "ConnectionError",
        "Exception",
        "OSError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
    }
)


def is_safe_operation_name(value: str) -> bool:
    return value in SAFE_OPERATION_NAMES or _SAFE_SEMANTIC_LABEL.fullmatch(value) is not None


def is_safe_event_name(value: str) -> bool:
    return value in SAFE_EVENT_NAMES or _SAFE_SEMANTIC_LABEL.fullmatch(value) is not None


def is_safe_semantic_label(value: str) -> bool:
    return _SAFE_SHA256_LABEL.fullmatch(value) is not None or (
        _SAFE_SEMANTIC_LABEL.fullmatch(value) is not None
    )


def sanitize_semantic_label(value: str | None) -> str | None:
    if value is None or is_safe_semantic_label(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def is_safe_provenance_label(value: str) -> bool:
    return value in _SAFE_PROVENANCE_LABELS or is_safe_semantic_label(value)


def sanitize_provenance_label(value: str | None) -> str | None:
    if value is None or is_safe_provenance_label(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def is_safe_version_label(value: str) -> bool:
    return _SAFE_SHA256_LABEL.fullmatch(value) is not None or (
        _SAFE_VERSION_LABEL.fullmatch(value) is not None
    )


def sanitize_version_label(value: str | None) -> str | None:
    if value is None or is_safe_version_label(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def is_safe_measurement_label(value: str) -> bool:
    return value in _SAFE_MEASUREMENT_LABELS or is_safe_semantic_label(value)


def sanitize_measurement_label(value: str) -> str:
    if is_safe_measurement_label(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def is_safe_measurement_unit(value: str) -> bool:
    return value in _SAFE_MEASUREMENT_UNITS or is_safe_semantic_label(value)


def sanitize_measurement_unit(value: str) -> str:
    if is_safe_measurement_unit(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def is_safe_error_label(value: str) -> bool:
    return value in _SAFE_ERROR_LABELS or is_safe_semantic_label(value)


def sanitize_error_label(value: str) -> str:
    if is_safe_error_label(value):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def sanitize_source_label(value: str | None) -> str | None:
    if (
        value is None
        or value in _SAFE_SOURCE_LABELS
        or _SAFE_SHA256_LABEL.fullmatch(value) is not None
    ):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def normalize_operation_name(value: str) -> tuple[str, str | None]:
    if is_safe_operation_name(value):
        return value, None
    return "framework_operation", hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def normalize_event_name(value: str) -> tuple[str, str | None]:
    if is_safe_event_name(value):
        return value, None
    return "framework.event", hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


_CLASS_PATTERNS: tuple[tuple[CaptureClass, tuple[str, ...]], ...] = (
    (
        CaptureClass.TRANSCRIPT,
        ("transcript", "caption", "utterance.text", "speech.text"),
    ),
    (
        CaptureClass.AUDIO,
        ("audio", "recording", "waveform", "media.uri", "media.url"),
    ),
    (
        CaptureClass.TOOL_PAYLOAD,
        ("tool.arguments", "tool.result", "tool.args", "function.arguments"),
    ),
    (
        CaptureClass.DIAGNOSTIC_PAYLOAD,
        ("error.message", "exception.message", "exception.stacktrace", "log.body"),
    ),
    (
        CaptureClass.MODEL_PAYLOAD,
        (
            "prompt",
            "completion",
            "message",
            "system_instruction",
            "gen_ai.input",
            "gen_ai.output.messages",
        ),
    ),
    (
        CaptureClass.IDENTITY,
        ("phone_number", "caller_id", "user.email", "participant.name"),
    ),
)


def classify_attribute(key: str) -> CaptureClass:
    lowered = key.lower()
    if key in _SAFE_EXACT or key.startswith(_SAFE_PREFIXES):
        return CaptureClass.METADATA
    if ".usage." in lowered and (lowered.startswith("lk.") or lowered.startswith("gen_ai.")):
        return CaptureClass.METADATA
    for capture_class, patterns in _CLASS_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return capture_class
    return CaptureClass.METADATA


def is_safe_metadata_key(key: str) -> bool:
    return key in _SAFE_EXACT or key.startswith(_SAFE_PREFIXES)


def is_safe_metadata_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        return abs(value) <= _IJSON_INTEGER_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(
            item is None or isinstance(item, (str, bool, int, float)) for item in value
        ) and all(not isinstance(item, float) or math.isfinite(item) for item in value)
    # Nested maps under a supposedly safe key can smuggle arbitrary content.
    return False


def _safe_scalar_metadata(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return False
    return all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)


def is_safe_unknown_metadata_value(value: Any) -> bool:
    """Unknown metadata is safe only when it cannot carry free-form content."""

    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= _IJSON_INTEGER_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(
            item is None
            or isinstance(item, bool)
            or (isinstance(item, int) and abs(item) <= _IJSON_INTEGER_MAX)
            or (isinstance(item, float) and math.isfinite(item))
            for item in value
        )
    return False


def metadata_value_allowed(key: str, value: Any) -> bool:
    if key in _DIGEST_METADATA_KEYS:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if key in _BOOLEAN_METADATA_KEYS:
        return isinstance(value, bool)
    if key in _INTEGER_METADATA_KEYS:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _IJSON_INTEGER_MAX
        )
    if key in _NUMERIC_METADATA_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, int):
            return 0 <= value <= _IJSON_INTEGER_MAX
        return math.isfinite(value) and value >= 0
    if key in _DECIMAL_METADATA_KEYS:
        return (
            isinstance(value, str)
            and re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is not None
            and (len(value) < 20 or (len(value) == 20 and value <= _UINT64_DECIMAL_MAX))
        )
    if key in _SEMANTIC_METADATA_KEYS:
        return isinstance(value, str) and is_safe_semantic_label(value)
    if key in _VERSION_METADATA_KEYS:
        return isinstance(value, str) and is_safe_version_label(value)
    if key in _SOURCE_METADATA_KEYS:
        return isinstance(value, str) and sanitize_source_label(value) == value
    if key in _SAFE_EXACT:
        # Producer-controlled identifiers, model/service names, and correlation
        # labels remain a documented trust boundary, but they must be bounded
        # scalar text—not arrays or arbitrary nested payloads.
        return _safe_scalar_metadata(value)
    # Open metric/duration namespaces can grow, but payload strings are not metrics.
    if key.startswith(_SAFE_PREFIXES):
        return is_safe_unknown_metadata_value(value)
    return is_safe_unknown_metadata_value(value)


def locator_has_credentials(value: str) -> bool:
    parsed = urlsplit(value)
    query_names = {name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    credential_names = {
        "access_token",
        "api_key",
        "key",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-security-token",
        "x-goog-signature",
    }
    signed_query_prefixes = ("x-amz-", "x-goog-")
    return (
        parsed.username is not None
        or parsed.password is not None
        or bool(query_names & credential_names)
        or any(name.startswith(signed_query_prefixes) for name in query_names)
    )


def _schema_url_properties(value: str) -> tuple[bool, bool]:
    """Return (portable, canonical_registry_url) without raising on hostile input."""

    try:
        parsed = urlsplit(value)
        _ = parsed.port
        portable = (
            len(value) <= 2048
            and parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/")
            and not any(
                ord(character) <= 0x20 or ord(character) == 0x7F for character in value
            )
        )
        canonical = (
            portable
            and parsed.netloc == "opentelemetry.io"
            and re.fullmatch(
                r"/schemas/v?[0-9]+(?:\.[0-9]+)*(?:[-+][a-z0-9.-]+)?",
                parsed.path,
            )
            is not None
        )
    except ValueError:
        return False, False
    return portable, canonical


def is_canonical_otel_schema_url(value: str) -> bool:
    return _schema_url_properties(value)[1]


def sanitize_schema_url(
    value: str | None,
    *,
    allow_extension: bool = False,
) -> tuple[str | None, str | None]:
    """Return a policy-safe OTel schema URL or an irreversible source digest.

    Canonical OpenTelemetry registry URLs are safe metadata. Other portable HTTPS
    schema URLs are retained only under the explicit extension-payload grant.
    """

    if value is None:
        return None, None
    portable, canonical = _schema_url_properties(value)
    if canonical or (portable and allow_extension):
        return value, None
    return None, hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def sanitize_attributes(
    attributes: Mapping[str, Any],
    policy: CapturePolicy | None = None,
) -> tuple[dict[str, Any], list[Omission]]:
    """Return policy-safe attributes and a non-sensitive omission ledger.

    Unknown metadata keys are omitted under metadata-only mode. Forward-compatible
    free-form extension fields require the separate extension_payload grant; raw_otlp
    authorizes only opaque raw chunks and never broadens attribute capture.
    """

    selected = policy or CapturePolicy.metadata_only()
    kept: dict[str, Any] = {}
    omissions: list[Omission] = []

    for key, value in attributes.items():
        capture_class = classify_attribute(key)
        allowed = selected.allows(capture_class) and is_safe_semantic_label(key)
        if capture_class is CaptureClass.METADATA:
            allowed = allowed and (
                is_safe_metadata_key(key) or selected.allows(CaptureClass.EXTENSION_PAYLOAD)
            )
            allowed = allowed and (
                metadata_value_allowed(key, value)
                if is_safe_metadata_key(key)
                else selected.allows(CaptureClass.EXTENSION_PAYLOAD)
            )
        credential_locator = (
            isinstance(value, str)
            and ("uri" in key.lower() or "url" in key.lower())
            and locator_has_credentials(value)
        )
        if credential_locator:
            allowed = False
        if allowed:
            kept[key] = value
        else:
            omissions.append(
                Omission(
                    field_key_sha256=hashlib.sha256(key.encode("utf-8")).hexdigest(),
                    capture_class=capture_class,
                    reason=(
                        "credential_bearing_locator"
                        if credential_locator
                        else "capture_class_disabled"
                    ),
                )
            )

    return kept, omissions


def contains_secret_sentinel(value: Any, sentinels: Iterable[str]) -> bool:
    """Recursive leak scanner used by conformance and exporter tests."""

    needles = tuple(sentinels)
    if isinstance(value, str):
        return any(needle in value for needle in needles)
    if isinstance(value, Mapping):
        return any(
            contains_secret_sentinel(key, needles) or contains_secret_sentinel(item, needles)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_secret_sentinel(item, needles) for item in value)
    return False


class ExportPolicyError(PermissionError):
    pass


def assert_export_allowed(bundle: IncidentBundle, destination: str) -> None:
    """Enforce declared per-class export restrictions."""

    for policy in bundle.profile.privacy.capture_classes:
        if not policy.captured or policy.export is None:
            continue
        if not policy.export.allowed:
            raise ExportPolicyError("incident export is denied by capture policy")
        if policy.export.destinations and destination not in policy.export.destinations:
            raise ExportPolicyError("incident export destination is not permitted")
