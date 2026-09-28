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
    QualityMeasurement,
    QualitySample,
    RecoveryRecord,
    TimePoint,
    TimeRange,
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

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_ATTRIBUTE_NUMBER = 1_000_000_000_000
_MAX_OBSERVATIONS = 100_000
_MAX_SEQUENCE = (1 << 53) - 1
_MAX_AT_MS = UINT64_MAX // 1_000_000
_MAX_ATTRIBUTES = 32
_MAX_ENVELOPE_KEYS = 12
_MAX_DIAGNOSTIC_FAILURE_REASONS = 8
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
            # ``frozen=True`` does not freeze nested dicts. Round-tripping an
            # existing model makes the adapter boundary revalidate a caller's
            # post-construction mutation instead of trusting the instance.
            value = value.model_dump(mode="python", by_alias=True)
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
    """Bound container work before the callback enqueues a value for parsing."""

    if isinstance(value, TVICObservation):
        attributes: Mapping[str, object] = value.attributes
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
    if envelope_length > _MAX_ENVELOPE_KEYS or len(attributes) > _MAX_ATTRIBUTES:
        raise TVICObservationError("TVIC observation envelope is too large")
    for key, attribute in attributes.items():
        if not isinstance(key, str) or len(key) > 128:
            raise TVICObservationError("TVIC observation attribute key is too large")
        if isinstance(attribute, str) and len(attribute) > 128:
            raise TVICObservationError("TVIC observation attribute value is too large")
        if isinstance(attribute, int) and not isinstance(attribute, bool):
            if attribute < 0 or attribute > _MAX_ATTRIBUTE_NUMBER:
                raise TVICObservationError("TVIC observation attribute number is out of range")
        elif isinstance(attribute, float) and (
            not math.isfinite(attribute) or attribute < 0 or attribute > _MAX_ATTRIBUTE_NUMBER
        ):
            raise TVICObservationError("TVIC observation attribute number is out of range")
        elif not isinstance(attribute, (bool, int, float, str)):
            raise TVICObservationError("TVIC observation attribute must be scalar")


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
        self._last_at_ms: dict[tuple[str, str], float] = {}
        self._turn_starts: dict[tuple[str, str], float] = {}
        self._interrupted_turns: set[tuple[str, str]] = set()
        self._first_observation: TVICObservation | None = None
        self._last_observation: TVICObservation | None = None
        self._last_session_sequence: int | None = None
        self._terminal_status: str | None = None
        self._closed_bundle: IncidentBundle | None = None
        self._accepted_observations = 0
        self._manual_clock = recorder.clock if isinstance(recorder.clock, ManualClock) else None
        self._epoch_offsets: dict[str, float] = {}
        self._epoch_uncertainty: dict[str, str] = {}
        self._rebased_epochs: set[str] = set()
        self._normalized_at_ms: dict[tuple[str, str], float] = {}
        self._last_normalized_at_ms: float | None = None
        self._failed_facts: set[tuple[str, str]] = set()
        self._pending_ingress_failure_reasons: set[str] = set()
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
            raise TVICObservationError(
                "observation sessionId does not match the adapter's incident session; "
                "runtime-wide drops require a runtime-scoped sink"
            )
        fact_key = (observation.epoch, observation.fact_id)
        existing = self._seen_facts.get(fact_key)
        if existing is not None:
            if existing != observation:
                raise TVICObservationError(
                    "factId was reused with different observation content in one epoch"
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
        at_ms = float(observation.at_ms)
        if observation.at_ms > self._max_at_ms:
            raise TVICObservationError("atMs plus started_at_unix_nano exceeds uint64 range")
        previous_at_ms = self._last_at_ms.get(sequence_key)
        if previous_at_ms is not None and at_ms < previous_at_ms:
            raise TVICObservationError("TVIC atMs regressed within an observation epoch")
        normalized_at_ms = self._normalized_time(observation, at_ms)
        previous_terminal_status = self._terminal_status
        turn_key = (
            (
                observation.epoch,
                observation.turn_id,
            )
            if observation.turn_id is not None
            else None
        )
        previous_turn_start = None if turn_key is None else self._turn_starts.get(turn_key)
        previous_interrupted = turn_key is not None and turn_key in self._interrupted_turns
        self._normalized_at_ms[fact_key] = normalized_at_ms
        try:
            self._dispatch(observation)
            self._advance_clock(normalized_at_ms)
        except TVICObservationError:
            self._rollback_failed_dispatch(
                fact_key,
                previous_terminal_status,
                turn_key,
                previous_turn_start,
                previous_interrupted,
            )
            raise
        except Exception as error:
            self._rollback_failed_dispatch(
                fact_key,
                previous_terminal_status,
                turn_key,
                previous_turn_start,
                previous_interrupted,
            )
            raise TVICObservationError(
                f"TVIC observation dispatch failed: {type(error).__name__}"
            ) from None
        self._seen_facts[fact_key] = observation
        self._last_sequence[sequence_key] = observation.sequence
        self._last_at_ms[sequence_key] = at_ms
        if self._first_observation is None:
            self._first_observation = observation
        self._last_observation = observation
        self._last_session_sequence = observation.sequence
        self._last_normalized_at_ms = normalized_at_ms
        self._accepted_observations += 1
        return True

    def _rollback_failed_dispatch(
        self,
        fact_key: tuple[str, str],
        previous_terminal_status: str | None,
        turn_key: tuple[str, str] | None,
        previous_turn_start: float | None,
        previous_interrupted: bool,
    ) -> None:
        self._normalized_at_ms.pop(fact_key, None)
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
        self._try_record_ingress_failure("tvic_runtime_dispatch_failed")

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

    def record_ingress_failure(self) -> None:
        """Record that a queued fact failed admission without exposing its payload."""

        with self._lock:
            if self._closed_bundle is not None:
                return
            self._ingress_loss_observed = True
            if not self._try_record_ingress_failure("tvic_runtime_adapter_admission_failed"):
                raise TVICObservationError("TVIC ingress failure diagnostic could not be recorded")

    def _try_record_ingress_failure(self, reason: str) -> bool:
        try:
            self.recorder.record_coverage(
                "tvic.ingress.failure",
                "unavailable",
                reason,
            )
        except Exception:
            self._remember_diagnostic_failure(reason)
            return False
        self._pending_ingress_failure_reasons.discard(reason)
        return True

    def _remember_diagnostic_failure(self, reason: str) -> None:
        if len(self._diagnostic_failure_reasons) < _MAX_DIAGNOSTIC_FAILURE_REASONS:
            self._diagnostic_failure_reasons.add(reason)
        self._pending_ingress_failure_reasons.add(reason)

    def _flush_pending_ingress_failure(self) -> None:
        for reason in tuple(self._pending_ingress_failure_reasons):
            self._try_record_ingress_failure(reason)

    def close(self) -> IncidentBundle:
        """Close on an observed TVIC session end, otherwise return a provisional incident."""

        with self._lock:
            if self._closed_bundle is not None:
                return self._closed_bundle.model_copy(deep=True)
            self._flush_pending_ingress_failure()
            if self._turn_starts:
                with suppress(Exception):
                    self.recorder.record_coverage(
                        "tvic.turn.end",
                        "partial",
                        "tvic_runtime_turn_end_not_observed",
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
            if self._ingress_loss_observed or self._diagnostic_failure_reasons:
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
            self._closed_bundle = bundle
            return self._closed_bundle.model_copy(deep=True)

    def _dispatch(self, observation: TVICObservation) -> None:
        if observation.name == "session.end":
            terminal_status = str(observation.attributes["status"])
            if terminal_status in {"failed", "cancelled"}:
                self.recorder.record_coverage(
                    "tvic.session.result",
                    "unavailable",
                    f"tvic_runtime_session_{terminal_status}",
                )
                if terminal_status == "failed":
                    self._record_event(observation, "framework.error")
            self._record_event(observation, "framework.event")
            self._terminal_status = terminal_status
            return
        if observation.name == "session.start":
            self._record_event(observation, "framework.event")
            return
        if observation.name == "session.resume":
            self._record_event(observation, "framework.event")
            return
        if observation.name == "turn.start":
            if observation.turn_id is None:
                raise TVICObservationError("turn.start requires turnId")
            self._turn_starts[(observation.epoch, observation.turn_id)] = float(observation.at_ms)
            self._record_event(
                observation,
                "earshot.turn.committed",
                participant_id="participant-user",
            )
            return
        if observation.name == "turn.interruption":
            self._record_event(
                observation,
                "earshot.interruption.accepted",
                participant_id="participant-user",
            )
            if observation.turn_id is not None:
                self._interrupted_turns.add((observation.epoch, observation.turn_id))
            return
        if observation.name == "observation.dropped":
            reason = observation.attributes.get("drop_reason", "queue_overflow")
            count = observation.attributes.get("dropped_count")
            if not isinstance(reason, str) or not isinstance(count, int):
                raise TVICObservationError("observation.dropped has invalid reason or count")
            self.recorder.record_coverage(
                f"tvic.observation.{reason}",
                "partial",
                "tvic_runtime_observation_dropped",
                dropped_count=count,
            )
            self._record_event(observation, "framework.event")
            return
        if observation.name != "turn.end" or observation.turn_id is None:
            raise TVICObservationError("turn.end requires turnId")
        self._record_turn_metrics(observation)
        self._record_turn_outcome_coverage(observation)
        self._record_event(observation, "framework.event", participant_id="participant-agent")
        turn_key = (observation.epoch, observation.turn_id)
        self._turn_starts.pop(turn_key, None)
        self._interrupted_turns.discard(turn_key)

    def _record_event(
        self,
        observation: TVICObservation,
        event_name: str,
        *,
        participant_id: str | None = None,
    ) -> None:
        self.recorder.record_event(
            event_name,
            event_id=self._event_id(observation, event_name),
            time=self._point(observation),
            participant_id=participant_id,
            turn_id=observation.turn_id,
            evidence=self._evidence(observation),
            attributes=self._metadata(observation),
        )

    def _record_turn_metrics(self, observation: TVICObservation) -> None:
        if observation.turn_id is None:
            raise TVICObservationError("turn metrics require turnId")
        attributes = observation.attributes
        for attribute, measurement_name in (
            ("endpoint_ms", "earshot.turn.endpointing_delay"),
            ("first_token_ms", "earshot.llm.ttft"),
            ("interruption_tail_ms", "earshot.turn.interruption_tail_latency"),
            ("listened_ms", "earshot.turn.listened_duration"),
            ("tool_ms", "earshot.turn.tool_latency"),
            ("total_ms", "earshot.turn.total_latency"),
        ):
            value = attributes.get(attribute)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                participant_id = {
                    "endpoint_ms": "participant-user",
                    "listened_ms": "participant-user",
                    "first_token_ms": "participant-agent",
                    "interruption_tail_ms": "participant-agent",
                    "tool_ms": "participant-agent",
                }.get(attribute)
                self._record_measurement(
                    observation,
                    measurement_name,
                    float(value),
                    participant_id=participant_id,
                )

        first_audio = attributes.get("first_audio_ms")
        if isinstance(first_audio, (int, float)) and not isinstance(first_audio, bool):
            self._record_measurement(
                observation,
                "tvic.turn.first_audio_latency",
                float(first_audio),
                participant_id="participant-agent",
            )

    def _record_turn_outcome_coverage(self, observation: TVICObservation) -> None:
        attributes = observation.attributes
        status = attributes.get("status")
        if status in {"failed", "cancelled"}:
            self.recorder.record_coverage(
                "tvic.turn.result",
                "unavailable",
                f"tvic_runtime_turn_{status}",
            )
            if status == "failed":
                self._record_event(
                    observation,
                    "framework.error",
                    participant_id="participant-agent",
                )
            elif (
                attributes.get("cancel_reason") in {"barge_in", "dtmf"}
                and observation.turn_id is not None
                and (observation.epoch, observation.turn_id) not in self._interrupted_turns
            ):
                self._record_event(
                    observation,
                    "earshot.interruption.accepted",
                    participant_id="participant-user",
                )
                self._interrupted_turns.add((observation.epoch, observation.turn_id))
        if attributes.get("audio_delivered") is False:
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
        if attributes.get("text_delivered") is False:
            self.recorder.record_coverage(
                "tvic.output.text",
                "unavailable",
                "tvic_runtime_text_not_delivered",
            )
        if attributes.get("terminal_persisted") is False:
            self.recorder.record_coverage(
                "tvic.turn.terminal_persistence",
                "partial",
                "tvic_runtime_terminal_not_persisted",
            )

    def _record_measurement(
        self,
        observation: TVICObservation,
        name: str,
        value: float,
        *,
        participant_id: str | None,
    ) -> None:
        if observation.turn_id is None:
            raise TVICObservationError("turn measurements require turnId")
        self.recorder.record_quality_sample(
            QualitySample(
                sample_id=stable_id("tvic-quality", observation.epoch, observation.fact_id, name),
                session_id=self.recorder.session_id,
                quality_kind="tvic_runtime",
                sample_window=TimeRange(
                    start=self._point(observation),
                    end=self._point(observation),
                ),
                measurements=(QualityMeasurement(name=name, value=value, unit="ms"),),
                evidence=self._evidence(observation),
                participant_id=participant_id,
                attributes={
                    **self._metadata(observation),
                    "earshot.turn.id": observation.turn_id,
                    "earshot.correlation": "tvic_runtime_observation",
                    "earshot.chronology": "turn_relative",
                },
            )
        )

    def _metadata(self, observation: TVICObservation) -> dict[str, TVICScalar | str]:
        metadata: dict[str, TVICScalar | str] = {
            "earshot.framework.name": "tvic-runtime",
            "earshot.framework.version": TVIC_RUNTIME_ADAPTER_VERSION,
            "earshot.integration.tvic.epoch": observation.epoch,
            "earshot.integration.tvic.sequence": observation.sequence,
            "earshot.integration.tvic.fact_id": observation.fact_id,
        }
        if observation.epoch in self._rebased_epochs:
            metadata["earshot.integration.tvic.epoch_rebased"] = True
        for key, value in observation.attributes.items():
            metadata[f"earshot.integration.tvic.{key}"] = value
        return metadata

    @staticmethod
    def _event_id(observation: TVICObservation, event_name: str) -> str:
        return stable_id(
            "tvic-event",
            observation.epoch,
            observation.fact_id,
            event_name,
        )

    def _evidence(self, observation: TVICObservation) -> Evidence:
        return Evidence(
            source="tvic_runtime",
            observer="tvic_runtime",
            method="runtime_observation",
            method_version=TVIC_RUNTIME_ADAPTER_VERSION,
            source_field=f"tvic.runtime.{observation.name}",
            confidence=("estimated" if observation.epoch in self._rebased_epochs else "measured"),
            availability="available",
        )

    def _normalized_time(self, observation: TVICObservation, at_ms: float) -> float:
        epoch = observation.epoch
        offset = self._epoch_offsets.get(epoch)
        if offset is None:
            offset = 0.0
            if self._last_normalized_at_ms is not None:
                recovery_gap = observation.attributes.get("recovery_gap_ms")
                gap_ms = (
                    float(recovery_gap)
                    if isinstance(recovery_gap, (int, float)) and not isinstance(recovery_gap, bool)
                    else 0.0
                )
                target = self._last_normalized_at_ms + gap_ms
                offset = max(0.0, target - at_ms)
                if offset > 0:
                    self._rebased_epochs.add(epoch)
                    uncertainty_nano = (
                        round((gap_ms + 1.0) * 1_000_000) if gap_ms > 0 else UINT64_MAX
                    )
                    self._epoch_uncertainty[epoch] = str(min(UINT64_MAX, uncertainty_nano))
                    with suppress(Exception):
                        self.recorder.record_coverage(
                            "tvic.runtime.epoch",
                            "partial",
                            "tvic_runtime_epoch_boundary_rebased",
                        )
            self._epoch_offsets[epoch] = offset
        normalized = offset + at_ms
        if self._last_normalized_at_ms is not None and normalized < self._last_normalized_at_ms:
            raise TVICObservationError("TVIC chronology regressed across observation epochs")
        return normalized

    def _point(self, observation: TVICObservation) -> TimePoint:
        normalized_at_ms = self._normalized_at_ms.get((observation.epoch, observation.fact_id))
        if normalized_at_ms is None:
            raise TVICObservationError("observation lacks an admitted chronology coordinate")
        monotonic_nano = round(normalized_at_ms * 1_000_000)
        if self._started_at_unix_nano + monotonic_nano > UINT64_MAX:
            raise TVICObservationError("observation timestamp exceeds uint64 range")
        return TimePoint(
            source_time_unix_nano=str(self._started_at_unix_nano + monotonic_nano),
            monotonic_time_nano=str(monotonic_nano),
            clock_domain_id=self.recorder.clock_domain_id,
            uncertainty_nano=self._epoch_uncertainty.get(observation.epoch, "1000000"),
        )

    def _advance_clock(self, at_ms: float) -> None:
        if self._manual_clock is None:
            return
        target_nano = round(at_ms * 1_000_000)
        current_nano = self._manual_clock.monotonic
        if target_nano > current_nano:
            self._manual_clock.advance(target_nano - current_nano)


class TVICRuntimeObservationSink:
    """Bounded non-blocking handoff from a TVIC callback into Earshot.

    ``enqueue`` performs only a short deque admission under a lock. Validation,
    journal work, and incident-record admission happen only when the owning
    ingestion loop calls ``drain`` or ``close``. A TVIC runtime observation
    callback therefore never performs synchronous Earshot I/O. Queue overflow
    and admission failures are retained as coverage when the sink closes.
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
        self._queue: deque[TVICObservation | Mapping[str, object]] = deque()
        self._closed = False
        self._closing = False
        self._dropped_count = 0
        self._error_code: str | None = None
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
            return self._error_code

    def enqueue(self, observation: TVICObservation | Mapping[str, object]) -> None:
        """Admit without waiting; full or closed queues count a transport loss."""

        with self._lock:
            if self._closed or self._closing:
                self._dropped_count += 1
                return
            if len(self._queue) >= self._max_queue:
                self._dropped_count += 1
                return
            try:
                _preflight_observation(observation)
                queued_observation = _parse_observation(observation)
            except Exception as error:
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
        with self._drain_lock:
            while max_items is None or admitted < max_items:
                if deadline is not None and monotonic() >= deadline:
                    break
                with self._lock:
                    if not self._queue:
                        break
                    observation = self._queue.popleft()
                try:
                    self._adapter.consume(observation)
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
                        self._dropped_count += len(self._queue)
                        self._queue.clear()
                    break
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
            if self._closed_bundle is not None:
                return self._closed_bundle.model_copy(deep=True)
            if self._close_error is not None:
                raise self._close_error
            start_worker = not self._closing
            if start_worker:
                self._closing = True
                self._closed = True
                self._close_done.clear()
        if start_worker:
            Thread(target=self._finish_close, name="earshot-tvic-close", daemon=True).start()
        if not self._close_done.wait(timeout=max(0.0, deadline - monotonic())):
            raise TimeoutError("TVIC sink close did not quiesce before timeout")
        with self._lock:
            if self._close_error is not None:
                raise self._close_error
            if self._closed_bundle is None:
                raise TVICObservationError("TVIC sink close completed without a bundle")
            return self._closed_bundle.model_copy(deep=True)

    def _finish_close(self) -> None:
        try:
            self.drain()
            with self._lock:
                self._dropped_count += len(self._queue)
                self._queue.clear()
                dropped_count = self._dropped_count
                error_code = self._error_code
            if dropped_count:
                try:
                    self._adapter.record_ingress_drop(dropped_count)
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
                    error_code = self._error_code
            if error_code is not None:
                try:
                    self._adapter.record_ingress_failure()
                except Exception as error:
                    with self._lock:
                        if self._error_code is None:
                            self._error_code = type(error).__name__
            bundle = self._adapter.close()
            with self._lock:
                if self._dropped_count:
                    queue_coverage = Coverage(
                        signal="tvic.ingress.queue",
                        availability="partial",
                        reason="tvic_runtime_adapter_queue_overflow",
                        dropped_count=self._dropped_count,
                    )
                    coverage = tuple(
                        queue_coverage if item.signal == queue_coverage.signal else item
                        for item in bundle.profile.coverage
                    )
                    if not any(item.signal == queue_coverage.signal for item in coverage):
                        coverage = (*coverage, queue_coverage)
                    bundle = bundle.model_copy(
                        update={"profile": bundle.profile.model_copy(update={"coverage": coverage})}
                    )
                self._closed_bundle = bundle
        except BaseException as error:
            with self._lock:
                self._close_error = error
        finally:
            with self._lock:
                self._closing = False
            self._close_done.set()


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
