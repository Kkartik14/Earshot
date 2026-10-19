"""Ingest the TVIC runtime's provider-neutral observation seam.

TVIC owns execution and Earshot owns evidence.  This adapter is the narrow
boundary between them: it validates the JSON-shaped observation envelope,
preserves its ordered identity, and projects only lifecycle/timing metadata into
Earshot's governed recorder.  Transcript, audio, arbitrary attributes, and
provider payloads are not part of the accepted input shape.

The adapter accepts a single session at a time.  A runtime-wide aggregated drop
fact (``sessionId == "runtime-observation"``) is rejected at this boundary
because TVIC cannot attribute that loss to one call; a runtime-scoped collector
must own that fact instead.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import replace
from threading import Event, RLock, Thread
from time import monotonic
from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from ..clock import ManualClock
from ..contract import (
    UINT64_MAX,
    Adapter,
    Coverage,
    Evidence,
    IncidentBundle,
    Producer,
    RecoveryRecord,
    TimePoint,
)
from ..recorder import IncidentRecorder, RecorderConfig
from ..versions import TVIC_RUNTIME_ADAPTER_VERSION
from .base import stable_id

TVIC_RUNTIME_DROP_SESSION_ID = "runtime-observation"
TVIC_RUNTIME_OBSERVATION_NAMES = (
    "observation.dropped",
    "session.end",
    "session.resume",
    "session.start",
    "turn.end",
    "turn.interruption",
    "turn.start",
)

TVICObservationName: TypeAlias = Literal[
    "observation.dropped",
    "session.end",
    "session.resume",
    "session.start",
    "turn.end",
    "turn.interruption",
    "turn.start",
]
TVICScalar: TypeAlias = StrictBool | StrictInt | StrictFloat | StrictStr
_Milliseconds: TypeAlias = int | float

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_ATTRIBUTE_NUMBER = 1_000_000_000_000
_MAX_OBSERVATIONS = 100_000
_MAX_SEQUENCE = (1 << 53) - 1
_MAX_AT_MS = UINT64_MAX // 1_000_000
_MAX_ATTRIBUTES = 32
_MAX_ENVELOPE_KEYS = 12
_MAX_DIAGNOSTIC_FAILURE_REASONS = 8
_ENVELOPE_KEYS = (
    "schemaVersion",
    "epoch",
    "sequence",
    "factId",
    "name",
    "sessionId",
    "atMs",
    "turnId",
    "attributes",
)
_DIAGNOSTIC_FAILURE_SIGNALS = {
    "tvic_runtime_adapter_queue_diagnostic_failed": "tvic.ingress.queue.failure",
    "tvic_runtime_adapter_admission_failed": "tvic.ingress.admission.failure",
    "tvic_runtime_dispatch_failed": "tvic.ingress.dispatch.failure",
}
_DEFAULT_INGRESS_QUEUE_CAPACITY = 1_024
_INGRESS_CLOSE_TIMEOUT_SECONDS = 5.0
_SESSION_STATUSES = frozenset({"completed", "failed", "cancelled"})
_CHANNELS = frozenset({"phone", "web_audio", "simulated"})
_DROP_REASONS = frozenset({"queue_overflow", "pending_session_limit"})
_TERMINAL_SOURCES = frozenset(
    {
        "operator_stop",
        "caller_abort",
        "run_timeout",
        "remote_transport",
        "provider_runtime",
        "normal_completion",
        "runtime_recovery",
        "runtime_shutdown",
        "legacy_unknown",
    }
)
_CANCEL_REASONS = frozenset(
    {
        "caller_hangup",
        "transport_lost",
        "recovery_expired",
        "operator_requested",
        "shutdown",
        "barge_in",
        "dtmf",
        "explicit",
        "timeout",
        "not_heard",
        "lease_lost",
        "runtime_restarted",
    }
)
_INTERRUPTION_CAUSES = frozenset({"barge_in", "dtmf", "explicit", "timeout"})
_ERROR_CATEGORIES = frozenset(
    {
        "validation",
        "auth",
        "provider",
        "network",
        "timeout",
        "rate_limit",
        "cancelled",
        "interrupted",
        "tool",
        "media",
        "internal",
    }
)
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "observation.dropped": frozenset({"drop_reason", "dropped_count", "queue_capacity"}),
    "session.end": frozenset(
        {
            "cancel_reason",
            "error_category",
            "error_code",
            "error_retriable",
            "at_ms_estimated",
            "sequence_discontinuity",
            "status",
            "terminal_source",
        }
    ),
    "session.resume": frozenset(
        {"recovery_gap_ms", "sequence_discontinuity", "session_elapsed_ms"}
    ),
    "session.start": frozenset({"agent_id", "channel"}),
    "turn.end": frozenset(
        {
            "audio_delivered",
            "audio_error_code",
            "at_ms_estimated",
            "cancel_reason",
            "endpoint_ms",
            "error_category",
            "error_code",
            "error_retriable",
            "first_audio_ms",
            "first_token_ms",
            "interruption_tail_ms",
            "listened_ms",
            "recovery_gap_ms",
            "status",
            "terminal_persisted",
            "text_delivered",
            "tool_ms",
            "total_ms",
            "turn_sequence",
        }
    ),
    "turn.interruption": frozenset({"cause"}),
    "turn.start": frozenset({"endpoint_ms", "listened_ms", "turn_sequence"}),
}
_INTEGER_ATTRIBUTES = frozenset({"dropped_count", "queue_capacity", "turn_sequence"})
_BOOLEAN_ATTRIBUTES = frozenset(
    {
        "audio_delivered",
        "error_retriable",
        "at_ms_estimated",
        "sequence_discontinuity",
        "terminal_persisted",
        "text_delivered",
    }
)
_ENUM_ATTRIBUTES = {
    "channel": _CHANNELS,
    "drop_reason": _DROP_REASONS,
    "error_category": _ERROR_CATEGORIES,
    "status": _SESSION_STATUSES,
    "terminal_source": _TERMINAL_SOURCES,
    "cancel_reason": _CANCEL_REASONS,
    "cause": _INTERRUPTION_CAUSES,
}


class TVICObservationError(ValueError):
    """Raised when a TVIC envelope cannot be safely admitted."""


def _milliseconds_to_nanoseconds(value: _Milliseconds) -> int:
    if isinstance(value, int):
        return value * 1_000_000
    numerator, denominator = value.as_integer_ratio()
    scaled, remainder = divmod(numerator * 1_000_000, denominator)
    doubled_remainder = remainder * 2
    if doubled_remainder > denominator or (doubled_remainder == denominator and scaled % 2):
        scaled += 1
    return scaled


class _TVICObservationSessionMismatch(TVICObservationError):
    """Raised when a valid fact belongs to a different session incident."""


class _TVICObservationDispatchError(TVICObservationError):
    """Raised after an admitted fact fails while writing recorder evidence."""


class _FrozenAttributes(dict[str, TVICScalar]):
    """A dict-shaped Pydantic value that cannot change after validation."""

    __slots__ = ()

    @staticmethod
    def _immutable() -> None:
        raise TypeError("TVIC observation attributes are immutable")

    def __setitem__(self, key: str, value: TVICScalar) -> None:
        self._immutable()

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, *args: object) -> TVICScalar:
        self._immutable()

    def popitem(self) -> tuple[str, TVICScalar]:
        self._immutable()

    def setdefault(self, *args: object) -> TVICScalar:
        self._immutable()

    def update(self, *args: object, **kwargs: TVICScalar) -> None:
        self._immutable()

    def __ior__(self, other: object) -> _FrozenAttributes:
        self._immutable()


class TVICObservation(BaseModel):
    """Strict Python representation of one TVIC runtime observation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )

    schema_version: Literal[1] = Field(alias="schemaVersion")
    epoch: StrictStr = Field(min_length=1, max_length=256)
    sequence: StrictInt = Field(gt=0)
    fact_id: StrictStr = Field(alias="factId", min_length=1, max_length=256)
    name: TVICObservationName
    session_id: StrictStr = Field(alias="sessionId", min_length=1, max_length=256)
    at_ms: StrictInt | StrictFloat = Field(alias="atMs", ge=0)
    turn_id: StrictStr | None = Field(default=None, alias="turnId", max_length=256)
    attributes: dict[str, TVICScalar] = Field(default_factory=dict, max_length=_MAX_ATTRIBUTES)

    def model_post_init(self, _context: object) -> None:
        object.__setattr__(self, "attributes", _FrozenAttributes(self.attributes))

    @model_validator(mode="after")
    def validate_runtime_metadata(self) -> TVICObservation:
        if self.sequence > _MAX_SEQUENCE:
            raise ValueError("sequence must be a JSON-safe positive integer")
        if isinstance(self.at_ms, float) and not math.isfinite(self.at_ms):
            raise ValueError("atMs must be finite")
        if self.at_ms > _MAX_AT_MS:
            raise ValueError("atMs exceeds the representable nanosecond range")
        if not _IDENTIFIER.fullmatch(self.epoch):
            raise ValueError("epoch must be a bounded opaque identifier")
        if not _IDENTIFIER.fullmatch(self.fact_id):
            raise ValueError("factId must be a bounded opaque identifier")
        if not _IDENTIFIER.fullmatch(self.session_id):
            raise ValueError("sessionId must be a bounded opaque identifier")
        if self.turn_id is not None and not _IDENTIFIER.fullmatch(self.turn_id):
            raise ValueError("turnId must be a bounded opaque identifier")
        if self.name.startswith("turn.") and self.turn_id is None:
            raise ValueError(f"{self.name} requires turnId")
        if self.name in {"session.end", "turn.end"} and not isinstance(
            self.attributes.get("status"), str
        ):
            raise ValueError(f"{self.name} requires status")
        if self.name == "turn.interruption" and not isinstance(self.attributes.get("cause"), str):
            raise ValueError("turn.interruption requires cause")
        if self.name == "observation.dropped" and not {
            "drop_reason",
            "dropped_count",
        }.issubset(self.attributes):
            raise ValueError("observation.dropped requires reason and count")
        allowed = _ALLOWED_ATTRIBUTES[self.name]
        unknown = set(self.attributes).difference(allowed)
        if unknown:
            raise ValueError(f"unsupported {self.name} attributes: {sorted(unknown)}")
        for key, value in self.attributes.items():
            if key in _INTEGER_ATTRIBUTES:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > _MAX_ATTRIBUTE_NUMBER
                ):
                    raise ValueError(f"{key} must be a bounded non-negative integer")
            elif key in _BOOLEAN_ATTRIBUTES:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
            elif isinstance(value, str):
                if not 1 <= len(value) <= 128 or any(
                    ord(character) < 0x20 or ord(character) == 0x7F for character in value
                ):
                    raise ValueError(f"{key} must be a bounded safe string")
                allowed_values = _ENUM_ATTRIBUTES.get(key)
                if allowed_values is not None and value not in allowed_values:
                    raise ValueError(f"unsupported {key}: {value}")
                identifier_key = key in {"agent_id", "error_code", "audio_error_code"}
                if identifier_key and not _IDENTIFIER.fullmatch(value):
                    raise ValueError(f"{key} must be a bounded identifier")
            elif isinstance(value, (int, float)):
                if isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ValueError(f"{key} must be finite")
                if value < 0 or value > _MAX_ATTRIBUTE_NUMBER:
                    raise ValueError(f"{key} must be a bounded non-negative number")
        return self


def _parse_observation(value: TVICObservation | Mapping[str, object]) -> TVICObservation:
    try:
        if isinstance(value, TVICObservation):
            # ``frozen=True`` does not freeze nested dicts. Snapshot bounded
            # scalar fields and attributes instead of trusting the instance.
            value = _snapshot_observation(value)
        return TVICObservation.model_validate(value)
    except ValidationError as error:
        codes = sorted(
            {
                str(item.get("type", "validation_error"))
                for item in error.errors(include_input=False, include_context=False)
            }
        )
        detail = ",".join(codes) if codes else "validation_error"
        raise TVICObservationError(f"TVIC observation validation failed: {detail}") from None


def _preflight_observation(value: TVICObservation | Mapping[str, object]) -> None:
    """Bound the input containers before retaining a snapshot in the ingress queue."""

    if isinstance(value, TVICObservation):
        if type(value) is not TVICObservation:
            raise TVICObservationError("TVIC observation model must not be subclassed")
        attributes_value = value.attributes
        if type(attributes_value) is not _FrozenAttributes:
            raise TVICObservationError(
                "TVIC observation attributes must be immutable built-in data"
            )
        if type(value.schema_version) is not int or value.schema_version != 1:
            raise TVICObservationError("TVIC observation schema version is invalid")
        if (
            type(value.sequence) is not int
            or not 1 <= value.sequence <= _MAX_SEQUENCE
            or type(value.name) is not str
            or value.name not in _ALLOWED_ATTRIBUTES
        ):
            raise TVICObservationError("TVIC observation envelope is invalid")
        for field in (value.epoch, value.fact_id, value.session_id):
            if type(field) is not str or len(field) > 256:
                raise TVICObservationError("TVIC observation identifier is too large")
        if value.turn_id is not None and (
            type(value.turn_id) is not str or len(value.turn_id) > 256
        ):
            raise TVICObservationError("TVIC observation turn identifier is too large")
        if type(value.at_ms) is int:
            if value.at_ms < 0 or value.at_ms > _MAX_AT_MS:
                raise TVICObservationError("TVIC observation timestamp is out of range")
        elif (
            type(value.at_ms) is not float
            or not math.isfinite(value.at_ms)
            or value.at_ms < 0
            or value.at_ms > _MAX_AT_MS
        ):
            raise TVICObservationError("TVIC observation timestamp is invalid")
        attributes = attributes_value
        envelope_length = 0
    elif isinstance(value, Mapping):
        try:
            envelope_length = len(value)
            attributes_value = value.get("attributes", {})
        except Exception:
            raise TVICObservationError(
                "TVIC observation envelope is not a bounded mapping"
            ) from None
        if not isinstance(attributes_value, Mapping):
            raise TVICObservationError("TVIC observation attributes must be a mapping")
        attributes = attributes_value
    else:
        raise TVICObservationError("TVIC observation must be a mapping")
    attributes_length = (
        dict.__len__(attributes) if type(value) is TVICObservation else len(attributes)
    )
    if envelope_length > _MAX_ENVELOPE_KEYS or attributes_length > _MAX_ATTRIBUTES:
        raise TVICObservationError("TVIC observation envelope is too large")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str or len(key) > 256:
                raise TVICObservationError("TVIC observation envelope key is too large")
            if key == "attributes":
                continue
            if type(item) is str:
                if len(item) > 256:
                    raise TVICObservationError("TVIC observation envelope value is too large")
            elif type(item) is int:
                if item.bit_length() > 64:
                    raise TVICObservationError(
                        "TVIC observation validation failed: envelope number is too large"
                    )
            elif type(item) not in {bool, float, type(None)}:
                raise TVICObservationError("TVIC observation envelope value must be scalar")
    attribute_items = (
        dict.items(attributes) if type(value) is TVICObservation else attributes.items()
    )
    for index, (key, attribute) in enumerate(attribute_items):
        if index >= _MAX_ATTRIBUTES:
            raise TVICObservationError("TVIC observation envelope is too large")
        if type(key) is not str or len(key) > 128:
            raise TVICObservationError("TVIC observation attribute key is too large")
        if type(attribute) is str and len(attribute) > 128:
            raise TVICObservationError("TVIC observation attribute value is too large")
        if type(attribute) is int:
            if attribute < 0 or attribute > _MAX_ATTRIBUTE_NUMBER:
                raise TVICObservationError("TVIC observation attribute number is out of range")
        elif type(attribute) is float and (
            not math.isfinite(attribute) or attribute < 0 or attribute > _MAX_ATTRIBUTE_NUMBER
        ):
            raise TVICObservationError("TVIC observation attribute number is out of range")
        elif type(attribute) not in {bool, int, float, str}:
            raise TVICObservationError("TVIC observation attribute must be scalar")


def _snapshot_observation(value: TVICObservation | dict[str, object]) -> dict[str, object]:
    """Copy inert, bounded input so caller code cannot run in the runtime callback."""

    if type(value) is TVICObservation:
        _preflight_observation(value)
        attributes = _snapshot_attributes(value.attributes)
        snapshot = {
            "schemaVersion": value.schema_version,
            "epoch": value.epoch,
            "sequence": value.sequence,
            "factId": value.fact_id,
            "name": value.name,
            "sessionId": value.session_id,
            "atMs": value.at_ms,
            "turnId": value.turn_id,
            "attributes": attributes,
        }
        _preflight_observation(snapshot)
        return snapshot
    if type(value) is not dict:
        raise TVICObservationError("TVIC callback input must be a built-in dict or TVICObservation")
    attributes = dict.get(value, "attributes", {})
    if type(attributes) is not dict:
        raise TVICObservationError("TVIC callback attributes must be a built-in dict")
    if dict.__len__(value) > _MAX_ENVELOPE_KEYS:
        raise TVICObservationError("TVIC observation envelope is too large")
    try:
        for index, key in enumerate(dict.__iter__(value)):
            if index >= _MAX_ENVELOPE_KEYS:
                raise TVICObservationError("TVIC observation envelope is too large")
            if type(key) is not str or len(key) > 256 or key not in _ENVELOPE_KEYS:
                raise TVICObservationError("TVIC observation envelope key is unsupported")
    except RuntimeError:
        raise TVICObservationError("TVIC observation changed during callback snapshot") from None
    snapshot = {key: dict.get(value, key) for key in _ENVELOPE_KEYS if key in value}
    if "attributes" in snapshot:
        snapshot["attributes"] = _snapshot_attributes(attributes)
    _preflight_observation(snapshot)
    return snapshot


def _snapshot_attributes(
    value: dict[str, object] | _FrozenAttributes,
) -> dict[str, object]:
    if type(value) not in {dict, _FrozenAttributes}:
        raise TVICObservationError("TVIC callback attributes must be a built-in dict")
    if dict.__len__(value) > _MAX_ATTRIBUTES:
        raise TVICObservationError("TVIC observation envelope is too large")
    snapshot: dict[str, object] = {}
    try:
        for index, key in enumerate(dict.__iter__(value)):
            if index >= _MAX_ATTRIBUTES:
                raise TVICObservationError("TVIC observation envelope is too large")
            if type(key) is not str or len(key) > 128:
                raise TVICObservationError("TVIC observation attribute key is too large")
            snapshot[key] = dict.__getitem__(value, key)
    except (KeyError, RuntimeError):
        raise TVICObservationError("TVIC observation changed during callback snapshot") from None
    return snapshot


class TVICRuntimeAdapter:
    """Project ordered TVIC runtime facts into one governed Earshot incident."""

    def __init__(
        self,
        recorder: IncidentRecorder,
        *,
        started_at_unix_nano: int,
        max_observations: int = 10_000,
    ) -> None:
        if isinstance(started_at_unix_nano, bool) or not isinstance(started_at_unix_nano, int):
            raise ValueError("started_at_unix_nano must be an integer")
        if not 0 <= started_at_unix_nano <= UINT64_MAX:
            raise ValueError("started_at_unix_nano must fit the uint64 nanosecond domain")
        if (
            isinstance(max_observations, bool)
            or not isinstance(max_observations, int)
            or not 1 <= max_observations <= _MAX_OBSERVATIONS
        ):
            raise ValueError(f"max_observations must be between 1 and {_MAX_OBSERVATIONS}")
        self.recorder = recorder
        self._started_at_unix_nano = started_at_unix_nano
        self._max_at_ms = (UINT64_MAX - started_at_unix_nano) // 1_000_000
        self._max_observations = max_observations
        self._lock = RLock()
        self._seen_facts: dict[tuple[str, str], TVICObservation] = {}
        self._last_sequence: dict[tuple[str, str], int] = {}
        self._last_at_ms: dict[tuple[str, str], _Milliseconds] = {}
        self._turn_starts: dict[str, _Milliseconds] = {}
        self._interrupted_turns: set[str] = set()
        self._provisional_turn_failures: dict[str, tuple[str | None, str | None]] = {}
        self._first_observation: TVICObservation | None = None
        self._last_observation: TVICObservation | None = None
        self._last_session_sequence: int | None = None
        self._terminal_status: str | None = None
        self._terminal_cancel_reason: str | None = None
        self._sequence_discontinuity_observed = False
        self._required_observation_missing = False
        self._closed_bundle: IncidentBundle | None = None
        self._accepted_observations = 0
        self._manual_clock = recorder.clock if isinstance(recorder.clock, ManualClock) else None
        self._normalized_at_ms: dict[tuple[str, str], _Milliseconds] = {}
        self._last_normalized_at_ms: _Milliseconds | None = None
        self._last_epoch: str | None = None
        self._failed_facts: set[tuple[str, str]] = set()
        self._sequence_gap_count = 0
        self._observation_drop_counts: dict[str, int] = {}
        self._ingress_failure_counts: dict[str, int] = {}
        self._pending_ingress_failure_reasons: dict[str, int] = {}
        self._diagnostic_failure_reasons: set[str] = set()
        self._ingress_loss_observed = False
        self.recorder.register_adapter(
            Adapter(
                name="earshot.tvic",
                version=TVIC_RUNTIME_ADAPTER_VERSION,
                framework="tvic-runtime",
            )
        )
        self.recorder.add_participant("participant-user", role="user", endpoint_kind="runtime")
        self.recorder.add_participant("participant-agent", role="agent", endpoint_kind="runtime")
        self.recorder.record_coverage(
            "client.render", "not_observed", "tvic_runtime_has_no_client_render_observer"
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        started_at_unix_nano: int,
        bundle_id: str | None = None,
        config: RecorderConfig | None = None,
        max_observations: int = 10_000,
    ) -> TVICRuntimeAdapter:
        """Create a deterministic adapter backed by a clock advanced by TVIC offsets."""

        adapter = Adapter(
            name="earshot.tvic",
            version=TVIC_RUNTIME_ADAPTER_VERSION,
            framework="tvic-runtime",
        )
        source_config = config or RecorderConfig()
        adapters = source_config.adapters
        if adapter not in adapters:
            adapters = (*adapters, adapter)
        recorder_config = replace(
            source_config,
            producer_name="earshot.tvic",
            producer_version=TVIC_RUNTIME_ADAPTER_VERSION,
            clock_domain_id=source_config.clock_domain_id or "tvic-runtime",
            adapters=adapters,
        )
        clock = ManualClock(wall=started_at_unix_nano, monotonic=0)
        recorder = IncidentRecorder(
            session_id=session_id,
            bundle_id=bundle_id,
            config=recorder_config,
            clock=clock,
        )
        return cls(
            recorder,
            started_at_unix_nano=started_at_unix_nano,
            max_observations=max_observations,
        )

    @property
    def accepted_observations(self) -> int:
        with self._lock:
            return self._accepted_observations

    def consume(self, value: TVICObservation | Mapping[str, object]) -> bool:
        """Admit one observation; identical replay is a no-op and returns ``False``."""

        _preflight_observation(value)
        observation = _parse_observation(value)
        with self._lock:
            return self._consume_locked(observation)

    def _consume_locked(self, observation: TVICObservation) -> bool:
        if self._closed_bundle is not None:
            raise TVICObservationError("cannot consume after the incident is closed")
        if observation.session_id != self.recorder.session_id:
            raise _TVICObservationSessionMismatch(
                "observation sessionId does not match the adapter's incident session; "
                "runtime-wide drops require a runtime-scoped sink"
            )
        fact_key = (observation.session_id, observation.fact_id)
        existing = self._seen_facts.get(fact_key)
        if existing is not None:
            if existing != observation:
                raise TVICObservationError(
                    "factId was reused with different observation content in one session"
                )
            return False
        if fact_key in self._failed_facts:
            raise TVICObservationError("TVIC fact previously failed recorder admission")
        if self._terminal_status is not None:
            raise TVICObservationError("a non-replay observation arrived after session.end")
        if len(self._seen_facts) + len(self._failed_facts) >= self._max_observations:
            raise TVICObservationError("TVIC observation admission limit exceeded")
        sequence_key = (observation.session_id, observation.epoch)
        previous_sequence = self._last_sequence.get(sequence_key)
        if previous_sequence is not None and observation.sequence <= previous_sequence:
            raise TVICObservationError("TVIC sequence regressed within an observation epoch")
        at_ms = observation.at_ms
        if observation.at_ms > self._max_at_ms:
            raise TVICObservationError("atMs plus started_at_unix_nano exceeds uint64 range")
        previous_at_ms = self._last_at_ms.get(sequence_key)
        if previous_at_ms is not None and at_ms < previous_at_ms:
            raise TVICObservationError("TVIC atMs regressed within an observation epoch")
        normalized_at_ms = self._normalized_time(observation, at_ms)
        sequence_gap_count = (
            observation.sequence - 1
            if previous_sequence is None
            else observation.sequence - previous_sequence - 1
        )
        if observation.name == "observation.dropped":
            dropped_count = observation.attributes.get("dropped_count")
            if type(dropped_count) is int:
                sequence_gap_count = max(
                    0,
                    sequence_gap_count - max(0, dropped_count - 1),
                )
        coordinate_key = (observation.epoch, observation.fact_id)
        previous_terminal_status = self._terminal_status
        turn_key = observation.turn_id
        previous_turn_start = None if turn_key is None else self._turn_starts.get(turn_key)
        previous_interrupted = turn_key is not None and turn_key in self._interrupted_turns
        self._normalized_at_ms[coordinate_key] = normalized_at_ms
        try:
            self._dispatch(observation)
            self._advance_clock(normalized_at_ms)
        except TVICObservationError as error:
            self._rollback_failed_dispatch(
                fact_key,
                coordinate_key,
                previous_terminal_status,
                turn_key,
                previous_turn_start,
                previous_interrupted,
            )
            raise _TVICObservationDispatchError(
                f"TVIC observation dispatch failed: {type(error).__name__}"
            ) from None
        except Exception as error:
            self._rollback_failed_dispatch(
                fact_key,
                coordinate_key,
                previous_terminal_status,
                turn_key,
                previous_turn_start,
                previous_interrupted,
            )
            raise _TVICObservationDispatchError(
                f"TVIC observation dispatch failed: {type(error).__name__}"
            ) from None
        self._seen_facts[fact_key] = observation
        self._last_sequence[sequence_key] = observation.sequence
        self._last_at_ms[sequence_key] = at_ms
        if sequence_gap_count:
            self._sequence_gap_count = min(
                _MAX_ATTRIBUTE_NUMBER,
                self._sequence_gap_count + sequence_gap_count,
            )
            self._ingress_loss_observed = True
        if self._first_observation is None:
            self._first_observation = observation
        self._last_observation = observation
        self._last_session_sequence = observation.sequence
        self._last_normalized_at_ms = normalized_at_ms
        self._last_epoch = observation.epoch
        self._accepted_observations += 1
        return True

    def _rollback_failed_dispatch(
        self,
        fact_key: tuple[str, str],
        coordinate_key: tuple[str, str],
        previous_terminal_status: str | None,
        turn_key: str | None,
        previous_turn_start: float | None,
        previous_interrupted: bool,
    ) -> None:
        self._normalized_at_ms.pop(coordinate_key, None)
        self._terminal_status = previous_terminal_status
        if turn_key is not None:
            if previous_turn_start is None:
                self._turn_starts.pop(turn_key, None)
            else:
                self._turn_starts[turn_key] = previous_turn_start
            if previous_interrupted:
                self._interrupted_turns.add(turn_key)
            else:
                self._interrupted_turns.discard(turn_key)
        self._failed_facts.add(fact_key)
        self._accumulate_ingress_failure("tvic_runtime_dispatch_failed", 1)

    def consume_many(self, values: Iterable[TVICObservation | Mapping[str, object]]) -> int:
        """Consume an ordered batch and return the number of newly admitted facts."""

        admitted = 0
        with self._lock:
            for value in values:
                _preflight_observation(value)
                admitted += int(self._consume_locked(_parse_observation(value)))
        return admitted

    def record_ingress_drop(self, dropped_count: int) -> None:
        """Record loss at the adapter's bounded handoff, outside the runtime callback."""

        if (
            isinstance(dropped_count, bool)
            or not isinstance(dropped_count, int)
            or not 1 <= dropped_count <= _MAX_ATTRIBUTE_NUMBER
        ):
            raise ValueError("dropped_count must be a bounded positive integer")
        with self._lock:
            if self._closed_bundle is not None:
                raise TVICObservationError(
                    "cannot record ingress loss after the incident is closed"
                )
            self._ingress_loss_observed = True
            try:
                self.recorder.record_coverage(
                    "tvic.ingress.queue",
                    "partial",
                    "tvic_runtime_adapter_queue_overflow",
                    dropped_count=dropped_count,
                )
            except Exception:
                self._remember_diagnostic_failure("tvic_runtime_adapter_queue_diagnostic_failed")
                raise

    def record_ingress_failure(self, dropped_count: int = 1) -> None:
        """Record that a queued fact failed admission without exposing its payload."""

        if (
            isinstance(dropped_count, bool)
            or not isinstance(dropped_count, int)
            or not 1 <= dropped_count <= _MAX_ATTRIBUTE_NUMBER
        ):
            raise ValueError("dropped_count must be a bounded positive integer")
        with self._lock:
            if self._closed_bundle is not None:
                return
            self._accumulate_ingress_failure(
                "tvic_runtime_adapter_admission_failed",
                dropped_count,
            )

    def _accumulate_ingress_failure(self, reason: str, dropped_count: int) -> None:
        self._ingress_loss_observed = True
        self._ingress_failure_counts[reason] = min(
            _MAX_ATTRIBUTE_NUMBER,
            self._ingress_failure_counts.get(reason, 0) + dropped_count,
        )

    def _try_record_ingress_failure(self, reason: str, dropped_count: int | None = None) -> bool:
        try:
            self.recorder.record_coverage(
                "tvic.ingress.failure",
                "unavailable",
                reason,
                dropped_count=dropped_count,
            )
        except Exception:
            self._remember_diagnostic_failure(reason, dropped_count)
            return False
        self._pending_ingress_failure_reasons.pop(reason, None)
        return True

    def _remember_diagnostic_failure(self, reason: str, dropped_count: int | None = None) -> None:
        if len(self._diagnostic_failure_reasons) < _MAX_DIAGNOSTIC_FAILURE_REASONS:
            self._diagnostic_failure_reasons.add(reason)
        self._pending_ingress_failure_reasons[reason] = max(
            self._pending_ingress_failure_reasons.get(reason, 0), dropped_count or 0
        )

    def _flush_pending_ingress_failure(self) -> None:
        for reason, dropped_count in tuple(self._pending_ingress_failure_reasons.items()):
            self._try_record_ingress_failure(reason, dropped_count or None)

    def close(self) -> IncidentBundle:
        """Close on an observed TVIC session end, otherwise return a provisional incident."""

        with self._lock:
            if self._closed_bundle is not None:
                return self._closed_bundle.model_copy(deep=True)
            if self._ingress_failure_counts:
                if len(self._ingress_failure_counts) == 1:
                    reason, dropped_count = next(iter(self._ingress_failure_counts.items()))
                else:
                    reason = "tvic_runtime_multiple_ingress_failures"
                    dropped_count = min(
                        _MAX_ATTRIBUTE_NUMBER,
                        sum(self._ingress_failure_counts.values()),
                    )
                self._try_record_ingress_failure(reason, dropped_count)
            self._flush_pending_ingress_failure()
            if self._turn_starts:
                with suppress(Exception):
                    self.recorder.record_coverage(
                        "tvic.turn.end",
                        "partial",
                        "tvic_runtime_turn_end_not_observed",
                    )
                self._required_observation_missing = True
            for reason, count in sorted(self._observation_drop_counts.items()):
                try:
                    self.recorder.record_coverage(
                        f"tvic.observation.{reason}",
                        "partial",
                        "tvic_runtime_observation_dropped",
                        dropped_count=count,
                    )
                except Exception:
                    self._remember_diagnostic_failure(
                        "tvic_runtime_observation_drop_diagnostic_failed"
                    )
            if self._sequence_gap_count:
                try:
                    self.recorder.record_coverage(
                        "tvic.observation.sequence_gap",
                        "partial",
                        "tvic_runtime_sequence_gap",
                        dropped_count=self._sequence_gap_count,
                    )
                except Exception:
                    self._remember_diagnostic_failure("tvic_runtime_sequence_gap_diagnostic_failed")
            if self._sequence_discontinuity_observed:
                try:
                    self.recorder.record_coverage(
                        "tvic.observation.sequence_discontinuity",
                        "partial",
                        "tvic_runtime_observation_sequence_discontinuity",
                    )
                except Exception:
                    self._remember_diagnostic_failure(
                        "tvic_runtime_sequence_discontinuity_diagnostic_failed"
                    )
            if self._terminal_status is not None:
                bundle = self.recorder.close(status=self._terminal_status)
            else:
                with suppress(Exception):
                    self.recorder.record_coverage(
                        "tvic.session.end",
                        "not_observed",
                        "tvic_runtime_session_end_not_observed",
                    )
                recovery = RecoveryRecord(
                    method="tvic_runtime_observation",
                    reason="tvic_session_end_not_observed",
                    close_observed=False,
                    last_sequence=self._last_session_sequence,
                    first_observation=(
                        None
                        if self._first_observation is None
                        else self._point(self._first_observation)
                    ),
                    last_observation=(
                        None
                        if self._last_observation is None
                        else self._point(self._last_observation)
                    ),
                    recoverer=Producer(
                        name="earshot.tvic",
                        version=TVIC_RUNTIME_ADAPTER_VERSION,
                        sdk_version=TVIC_RUNTIME_ADAPTER_VERSION,
                    ),
                )
                bundle = self.recorder.close_partial(recovery, status="interrupted")
            if (
                self._ingress_loss_observed
                or self._diagnostic_failure_reasons
                or self._sequence_discontinuity_observed
                or self._required_observation_missing
            ):
                coverage = tuple(bundle.profile.coverage)
                for reason in sorted(self._diagnostic_failure_reasons):
                    signal = _DIAGNOSTIC_FAILURE_SIGNALS.get(
                        reason, "tvic.ingress.diagnostic.failure"
                    )
                    diagnostic_coverage = Coverage(
                        signal=signal,
                        availability="unavailable",
                        reason=reason,
                    )
                    if any(item.signal == signal for item in coverage):
                        continue
                    coverage = (*coverage, diagnostic_coverage)
                profile = bundle.profile.model_copy(
                    update={
                        "manifest": bundle.profile.manifest.model_copy(
                            update={"completeness": "incomplete"}
                        ),
                        "coverage": coverage,
                    }
                )
                bundle = bundle.model_copy(update={"profile": profile})
            if (
                self._terminal_status == "cancelled"
                and self._terminal_cancel_reason == "caller_hangup"
                and not self._turn_starts
                and not self._ingress_loss_observed
                and not self._diagnostic_failure_reasons
                and not self._sequence_discontinuity_observed
                and not self._required_observation_missing
                and bundle.profile.manifest.finality == "final"
            ):
                profile = bundle.profile.model_copy(
                    update={
                        "manifest": bundle.profile.manifest.model_copy(
                            update={"completeness": "complete"}
                        )
                    }
                )
                bundle = bundle.model_copy(update={"profile": profile})
            self._closed_bundle = bundle
            return self._closed_bundle.model_copy(deep=True)

    def _dispatch(self, observation: TVICObservation) -> None:
        if observation.name == "session.end":
            terminal_status = str(observation.attributes["status"])
            if observation.attributes.get("at_ms_estimated") is True:
                self.recorder.record_coverage(
                    "tvic.session.end.time",
                    "partial",
                    "tvic_runtime_session_end_time_estimated",
                )
                self._required_observation_missing = True
            if terminal_status == "failed":
                self.recorder.record_coverage(
                    "tvic.session.result",
                    "unavailable",
                    f"tvic_runtime_session_{terminal_status}",
                )
                self._record_event(observation, "framework.error")
            if terminal_status == "cancelled":
                cancel_reason = observation.attributes.get("cancel_reason")
                self._terminal_cancel_reason = (
                    cancel_reason if isinstance(cancel_reason, str) else None
                )
            self._record_event(observation, "framework.event")
            self._terminal_status = terminal_status
            self._sequence_discontinuity_observed = (
                observation.attributes.get("sequence_discontinuity") is True
            ) or self._sequence_discontinuity_observed
            return
        if observation.name == "session.start":
            self._record_event(observation, "framework.event")
            return
        if observation.name == "session.resume":
            self._record_event(observation, "framework.event")
            if observation.attributes.get("sequence_discontinuity") is True:
                self._sequence_discontinuity_observed = True
            return
        if observation.name == "turn.start":
            if observation.turn_id is None:
                raise TVICObservationError("turn.start requires turnId")
            normalized_at_ms = self._normalized_at_ms.get((observation.epoch, observation.fact_id))
            if normalized_at_ms is None:
                raise TVICObservationError("turn.start lacks an admitted chronology coordinate")
            self._turn_starts[observation.turn_id] = normalized_at_ms
            self._record_event(
                observation,
                "earshot.turn.committed",
                participant_id="participant-user",
            )
            return
        if observation.name == "turn.interruption":
            cause = observation.attributes.get("cause")
            self._record_event(
                observation,
                "earshot.interruption.accepted",
                participant_id=("participant-user" if cause in {"barge_in", "dtmf"} else None),
            )
            if observation.turn_id is not None:
                self._interrupted_turns.add(observation.turn_id)
            return
        if observation.name == "observation.dropped":
            reason = observation.attributes.get("drop_reason", "queue_overflow")
            count = observation.attributes.get("dropped_count")
            if not isinstance(reason, str) or not isinstance(count, int):
                raise TVICObservationError("observation.dropped has invalid reason or count")
            self._record_event(observation, "framework.event")
            self._observation_drop_counts[reason] = min(
                _MAX_ATTRIBUTE_NUMBER,
                self._observation_drop_counts.get(reason, 0) + count,
            )
            self._ingress_loss_observed = True
            return
        if observation.name != "turn.end" or observation.turn_id is None:
            raise TVICObservationError("turn.end requires turnId")
        if observation.turn_id not in self._turn_starts:
            self.recorder.record_coverage(
                "tvic.turn.start",
                "not_observed",
                "tvic_runtime_turn_start_not_observed",
            )
            self._required_observation_missing = True
        terminal_persisted = observation.attributes.get("terminal_persisted")
        if terminal_persisted is None:
            self.recorder.record_coverage(
                "tvic.turn.terminal_persistence",
                "partial",
                "tvic_runtime_terminal_persistence_unknown",
            )
            self._required_observation_missing = True
        elif terminal_persisted is False:
            self.recorder.record_coverage(
                "tvic.turn.terminal_persistence",
                "partial",
                "tvic_runtime_terminal_not_persisted",
            )
            self._record_turn_outcome_coverage(observation)
            return
        self._record_turn_events(observation)
        self._record_turn_outcome_coverage(observation)
        self._record_event(observation, "framework.event", participant_id="participant-agent")
        self._turn_starts.pop(observation.turn_id, None)
        self._interrupted_turns.discard(observation.turn_id)

    def _record_event(
        self,
        observation: TVICObservation,
        event_name: str,
        *,
        participant_id: str | None = None,
        at_ms: _Milliseconds | None = None,
        source_field: str | None = None,
    ) -> None:
        self.recorder.record_event(
            event_name,
            event_id=self._event_id(observation, event_name),
            time=(
                self._point(observation) if at_ms is None else self._point_at_ms(observation, at_ms)
            ),
            participant_id=participant_id,
            turn_id=observation.turn_id,
            evidence=self._evidence(observation, source_field=source_field),
            attributes=self._metadata(observation),
        )

    def _record_turn_events(self, observation: TVICObservation) -> None:
        if observation.turn_id is None:
            raise TVICObservationError("turn events require turnId")
        attributes = observation.attributes
        turn_start_ms = self._turn_starts.get(observation.turn_id)
        for attribute, event_name in (
            ("first_token_ms", "earshot.response.first_token"),
            ("first_audio_ms", "earshot.response.first_audio_generated"),
        ):
            value = attributes.get(attribute)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if turn_start_ms is None:
                self.recorder.record_coverage(
                    "tvic.turn.start",
                    "not_observed",
                    "tvic_runtime_turn_start_not_observed_for_latency_event",
                )
                self._required_observation_missing = True
                continue
            self._record_event(
                observation,
                event_name,
                participant_id="participant-agent",
                at_ms=turn_start_ms + value,
                source_field=f"tvic.runtime.turn.end.{attribute}",
            )

    def _record_turn_outcome_coverage(self, observation: TVICObservation) -> None:
        attributes = observation.attributes
        status = attributes.get("status")
        provisional = attributes.get("terminal_persisted") is False
        error_code = attributes.get("error_code")
        error_category = attributes.get("error_category")
        error_identity = (
            error_code if isinstance(error_code, str) else None,
            error_category if isinstance(error_category, str) else None,
        )
        if status == "failed":
            self.recorder.record_coverage(
                "tvic.turn.result",
                "unavailable",
                f"tvic_runtime_turn_{status}",
            )
            if not (
                not provisional
                and observation.turn_id is not None
                and self._provisional_turn_failures.get(observation.turn_id) == error_identity
            ):
                self._record_event(
                    observation,
                    "framework.error",
                    participant_id="participant-agent",
                )
        elif (
            status == "cancelled"
            and attributes.get("cancel_reason") in {"barge_in", "dtmf", "explicit"}
            and observation.turn_id is not None
            and observation.turn_id not in self._interrupted_turns
        ):
            self.recorder.record_coverage(
                "tvic.turn.interruption",
                "not_observed",
                "tvic_runtime_interruption_fact_not_observed",
            )
            self._required_observation_missing = True
        if attributes.get("audio_delivered") is False and status != "cancelled":
            self.recorder.record_coverage(
                "tvic.output.audio",
                "unavailable",
                "tvic_runtime_audio_not_delivered",
            )
        if attributes.get("audio_error_code") is not None:
            self.recorder.record_coverage(
                "tvic.output.audio.error",
                "partial",
                "tvic_runtime_audio_error",
            )
        if attributes.get("text_delivered") is False and status != "cancelled":
            self.recorder.record_coverage(
                "tvic.output.text",
                "unavailable",
                "tvic_runtime_text_not_delivered",
            )
        if observation.turn_id is not None:
            if provisional and status == "failed":
                self._provisional_turn_failures[observation.turn_id] = error_identity
            elif not provisional:
                self._provisional_turn_failures.pop(observation.turn_id, None)

    def _metadata(self, observation: TVICObservation) -> dict[str, TVICScalar | str]:
        metadata: dict[str, TVICScalar | str] = {
            "earshot.framework.name": "tvic-runtime",
            "earshot.framework.version": TVIC_RUNTIME_ADAPTER_VERSION,
            "earshot.integration.tvic.epoch": observation.epoch,
            "earshot.integration.tvic.sequence": observation.sequence,
            "earshot.integration.tvic.fact_id": observation.fact_id,
        }
        for key, value in observation.attributes.items():
            metadata[f"earshot.integration.tvic.{key}"] = value
        return metadata

    @staticmethod
    def _event_id(observation: TVICObservation, event_name: str) -> str:
        return stable_id("tvic-event", observation.fact_id, event_name)

    def _evidence(
        self,
        observation: TVICObservation,
        *,
        source_field: str | None = None,
    ) -> Evidence:
        at_ms_estimated = observation.attributes.get("at_ms_estimated") is True
        return Evidence(
            source="tvic_runtime",
            observer="tvic_runtime",
            method="runtime_observation",
            method_version=TVIC_RUNTIME_ADAPTER_VERSION,
            source_field=source_field or f"tvic.runtime.{observation.name}",
            confidence="estimated" if at_ms_estimated else "measured",
            availability="available",
        )

    def _normalized_time(
        self,
        observation: TVICObservation,
        at_ms: _Milliseconds,
    ) -> _Milliseconds:
        if self._last_normalized_at_ms is None:
            return at_ms
        if observation.epoch == self._last_epoch:
            if at_ms < self._last_normalized_at_ms:
                raise TVICObservationError("TVIC atMs regressed within an observation epoch")
            return at_ms
        if observation.name == "session.resume" or (
            observation.name == "session.end"
            and observation.attributes.get("sequence_discontinuity") is True
        ):
            return max(self._last_normalized_at_ms, at_ms)
        raise TVICObservationError("TVIC chronology regressed across observation epochs")

    def _point(self, observation: TVICObservation) -> TimePoint:
        normalized_at_ms = self._normalized_at_ms.get((observation.epoch, observation.fact_id))
        if normalized_at_ms is None:
            raise TVICObservationError("observation lacks an admitted chronology coordinate")
        return self._point_at_ms(observation, normalized_at_ms)

    def _point_at_ms(self, observation: TVICObservation, at_ms: _Milliseconds) -> TimePoint:
        monotonic_nano = _milliseconds_to_nanoseconds(at_ms)
        if self._started_at_unix_nano + monotonic_nano > UINT64_MAX:
            raise TVICObservationError("observation timestamp exceeds uint64 range")
        return TimePoint(
            source_time_unix_nano=str(self._started_at_unix_nano + monotonic_nano),
            monotonic_time_nano=str(monotonic_nano),
            clock_domain_id=self.recorder.clock_domain_id,
            uncertainty_nano=(
                None if observation.attributes.get("at_ms_estimated") is True else "1000000"
            ),
        )

    def _advance_clock(self, at_ms: _Milliseconds) -> None:
        if self._manual_clock is None:
            return
        target_nano = _milliseconds_to_nanoseconds(at_ms)
        current_nano = self._manual_clock.monotonic
        if target_nano > current_nano:
            self._manual_clock.advance(target_nano - current_nano)


class TVICRuntimeObservationSink:
    """Bounded non-blocking handoff from a TVIC callback into Earshot.

    ``enqueue`` accepts plain dictionaries or TVICObservation models, performs
    bounded input checks, and takes a shallow snapshot before admitting it under
    a lock. It rejects custom mappings rather than executing caller code on the
    runtime callback. Full schema validation, journal work, and
    incident-record admission happen only when the owning ingestion loop calls
    ``drain`` or ``close``. A TVIC runtime observation
    callback therefore never performs synchronous Earshot I/O. Queue overflow
    and admission failures before bundle publication are retained as coverage.
    Rejected callbacks after publication remain visible in ``dropped_count`` but
    cannot change the finalized incident.
    """

    def __init__(
        self,
        adapter: TVICRuntimeAdapter,
        *,
        max_queue: int = _DEFAULT_INGRESS_QUEUE_CAPACITY,
    ) -> None:
        if (
            isinstance(max_queue, bool)
            or not isinstance(max_queue, int)
            or not 1 <= max_queue <= _MAX_OBSERVATIONS
        ):
            raise ValueError(f"max_queue must be between 1 and {_MAX_OBSERVATIONS}")
        self._adapter = adapter
        self._max_queue = max_queue
        self._lock = RLock()
        self._drain_lock = RLock()
        self._queue: deque[dict[str, object]] = deque()
        self._closed = False
        self._closing = False
        self._dropped_count = 0
        self._queue_overflow_count = 0
        self._admission_failure_count = 0
        self._closed_drop_count = 0
        self._error_code: str | None = None
        self._routing_error_code: str | None = None
        self._closed_bundle: IncidentBundle | None = None
        self._close_error: BaseException | None = None
        self._close_done = Event()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    @property
    def error(self) -> str | None:
        with self._lock:
            # Routing mismatches are observable to the owner, but are not per-call
            # evidence loss and therefore do not make this incident incomplete.
            return self._error_code or self._routing_error_code

    def enqueue(self, observation: TVICObservation | dict[str, object]) -> None:
        """Admit inert input without waiting; rejection counts as transport loss.

        Losses before the close worker publishes its bundle are included in that
        incident. Losses after publication are visible through ``dropped_count``
        only because the finalized artifact cannot be revised.
        """

        with self._lock:
            if self._closed or self._closing:
                self._dropped_count += 1
                if self._closed_bundle is None:
                    self._closed_drop_count += 1
                return
            if len(self._queue) >= self._max_queue:
                self._dropped_count += 1
                self._queue_overflow_count += 1
                return
            try:
                queued_observation = _snapshot_observation(observation)
            except Exception as error:
                self._dropped_count += 1
                self._admission_failure_count += 1
                if self._error_code is None:
                    self._error_code = type(error).__name__
                return
            self._queue.append(queued_observation)

    def drain(
        self,
        *,
        max_items: int | None = None,
        timeout: float | None = None,
    ) -> int:
        """Process queued observations off the runtime callback and return its count."""

        if max_items is not None and (
            isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1
        ):
            raise ValueError("max_items must be a positive integer when supplied")
        deadline = None
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("timeout must be a non-negative finite number")
            if not math.isfinite(float(timeout)) or timeout < 0:
                raise ValueError("timeout must be a non-negative finite number")
            deadline = monotonic() + float(timeout)
        admitted = 0
        processed = 0
        with self._drain_lock:
            while max_items is None or processed < max_items:
                if deadline is not None and monotonic() >= deadline:
                    break
                with self._lock:
                    if not self._queue:
                        break
                    observation = self._queue.popleft()
                processed += 1
                try:
                    self._adapter.consume(observation)
                except _TVICObservationSessionMismatch as error:
                    with self._lock:
                        if self._routing_error_code is None:
                            self._routing_error_code = type(error).__name__
                    continue
                except _TVICObservationDispatchError as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
                        self._dropped_count += 1
                    continue
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
                        self._dropped_count += 1
                        self._admission_failure_count += 1
                    continue
                admitted += 1
        return admitted

    def close(self, timeout: float = _INGRESS_CLOSE_TIMEOUT_SECONDS) -> IncidentBundle:
        """Drain deterministically, record handoff loss, and close the incident.

        Closing runs the serialized drain/finalization worker off the caller's
        thread.  If that worker is not quiescent before ``timeout``, this call
        raises ``TimeoutError``; the worker continues and no adapter finalizes
        until its drain is complete.  A later close call can collect the bundle.
        """

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a non-negative finite number")
        if not math.isfinite(float(timeout)) or timeout < 0:
            raise ValueError("timeout must be a non-negative finite number")
        deadline = monotonic() + float(timeout)
        with self._lock:
            closed_bundle = self._closed_bundle
            if closed_bundle is None:
                if self._close_error is not None:
                    raise self._close_error
                start_worker = not self._closing
                if start_worker:
                    self._closing = True
                    self._closed = True
                    self._close_done.clear()
        if closed_bundle is not None:
            return closed_bundle.model_copy(deep=True)
        if start_worker:
            Thread(target=self._finish_close, name="earshot-tvic-close", daemon=True).start()
        if not self._close_done.wait(timeout=max(0.0, deadline - monotonic())):
            raise TimeoutError("TVIC sink close did not quiesce before timeout")
        with self._lock:
            if self._close_error is not None:
                raise self._close_error
            if self._closed_bundle is None:
                raise TVICObservationError("TVIC sink close completed without a bundle")
            closed_bundle = self._closed_bundle
        return closed_bundle.model_copy(deep=True)

    def _finish_close(self) -> None:
        try:
            self.drain()
            with self._lock:
                queue_overflow_count = self._queue_overflow_count
                admission_failure_count = self._admission_failure_count
                closed_drop_count = self._closed_drop_count
            if queue_overflow_count:
                try:
                    self._adapter.record_ingress_drop(
                        min(queue_overflow_count, _MAX_ATTRIBUTE_NUMBER)
                    )
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
            failure_count = admission_failure_count + closed_drop_count
            failure_count_at_finalize = failure_count
            if failure_count:
                try:
                    self._adapter.record_ingress_failure(min(failure_count, _MAX_ATTRIBUTE_NUMBER))
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
            bundle = self._adapter.close()
            with self._lock:
                coverage = list(bundle.profile.coverage)
                if self._queue_overflow_count:
                    queue_coverage = Coverage(
                        signal="tvic.ingress.queue",
                        availability="partial",
                        reason="tvic_runtime_adapter_queue_overflow",
                        dropped_count=min(self._queue_overflow_count, _MAX_ATTRIBUTE_NUMBER),
                    )
                    self._replace_coverage(coverage, queue_coverage)
                failure_count = self._admission_failure_count + self._closed_drop_count
                if failure_count:
                    prior_failure = next(
                        (item for item in coverage if item.signal == "tvic.ingress.failure"),
                        None,
                    )
                    prior_count = 0 if prior_failure is None else prior_failure.dropped_count or 0
                    late_failure_count = max(0, failure_count - failure_count_at_finalize)
                    failure_coverage = Coverage(
                        signal="tvic.ingress.failure",
                        availability="unavailable",
                        reason=(
                            "tvic_runtime_adapter_admission_failed"
                            if prior_failure is None
                            else prior_failure.reason
                        ),
                        dropped_count=min(
                            max(prior_count + late_failure_count, failure_count),
                            _MAX_ATTRIBUTE_NUMBER,
                        ),
                    )
                    self._replace_coverage(coverage, failure_coverage)
                if self._dropped_count:
                    profile = bundle.profile.model_copy(
                        update={
                            "manifest": bundle.profile.manifest.model_copy(
                                update={"completeness": "incomplete"}
                            ),
                            "coverage": tuple(coverage),
                        }
                    )
                    bundle = bundle.model_copy(update={"profile": profile})
                self._closed_bundle = bundle
        except BaseException as error:
            with self._lock:
                self._close_error = error
        finally:
            with self._lock:
                self._closing = False
            self._close_done.set()

    @staticmethod
    def _replace_coverage(coverage: list[Coverage], replacement: Coverage) -> None:
        for index, item in enumerate(coverage):
            if item.signal == replacement.signal:
                coverage[index] = replacement
                return
        coverage.append(replacement)


def ingest_tvic_observations(
    observations: Iterable[TVICObservation | Mapping[str, object]],
    *,
    session_id: str,
    started_at_unix_nano: int,
    bundle_id: str | None = None,
    config: RecorderConfig | None = None,
) -> IncidentBundle:
    """Ingest one ordered TVIC batch and return its final or provisional incident."""

    adapter = TVICRuntimeAdapter.create(
        session_id=session_id,
        started_at_unix_nano=started_at_unix_nano,
        bundle_id=bundle_id,
        config=config,
    )
    adapter.consume_many(observations)
    return adapter.close()


__all__ = [
    "TVIC_RUNTIME_ADAPTER_VERSION",
    "TVIC_RUNTIME_DROP_SESSION_ID",
    "TVIC_RUNTIME_OBSERVATION_NAMES",
    "TVICObservation",
    "TVICObservationError",
    "TVICRuntimeAdapter",
    "TVICRuntimeObservationSink",
    "ingest_tvic_observations",
]
