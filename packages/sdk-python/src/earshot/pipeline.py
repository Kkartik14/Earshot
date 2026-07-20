"""Provider-neutral pipeline capture facade.

For teams wiring raw STT / LLM / TTS providers into their own pipeline instead of
using an instrumented framework (LiveKit, Pipecat). One ergonomic API records each
conversational turn's stages and barge-ins as an evidence-qualified Earshot
incident, so a home-grown pipeline is observed exactly like a framework one and
its turns analyze identically (first-token, generated-response, interruptions).

    sess = earshot.pipeline(session_id="call-42")
    with sess.turn() as t:
        t.stt("deepgram", model="nova-3", ttfb_ms=180, final_ms=420)
        t.llm("openai", model="gpt-4o", ttft_ms=350)
        t.tts("cartesia", model="sonic-3", ttfb_ms=90, first_audio_ms=140)
        t.barge_in(at_ms=1600, accepted=True)
    incident = sess.close()

Latency arguments are provider-reported by default (``confidence="measured"``); pass
``confidence="estimated"`` for a value the pipeline timed itself, or ``"inferred"``
for one it deduced. The metric names emitted here (``earshot.llm.ttft``,
``earshot.tts.ttfb``) are the same governed names the analyzer already derives
first-token and generated-response latency from, so no analyzer change is needed.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from .clock import ManualClock
from .contract import (
    Adapter,
    Evidence,
    IncidentBundle,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from .privacy import CaptureClass
from .recorder import IncidentRecorder, RecorderConfig
from .sdk import _runtime_snapshot

PIPELINE_ADAPTER_VERSION = "1.0.0"
_MS_TO_NANO = 1_000_000
_DEFAULT_TURN_GAP_MS = 500.0
_USER = "participant-user"
_AGENT = "participant-agent"


def _confidence_source(confidence: str) -> str:
    # A measured latency is the provider's own reported number; anything the
    # pipeline timed or deduced is an application-observed value.
    return "provider" if confidence == "measured" else "app"


class TurnRecorder:
    """Records one conversational turn's stages and barge-ins.

    Stages are placed on the turn clock in call order. A scalar latency does not
    prove a stage interval, so stage operations are points with app-inferred
    evidence while provider-reported latencies are separate governed measurements.
    """

    def __init__(self, session: PipelineSession, turn_id: str, turn_index: int) -> None:
        self._session = session
        self._turn_id = turn_id
        self._turn_index = turn_index
        self._cursor_ms = 0.0
        self._max_ms = 0.0
        self._sequence = 0

    @property
    def turn_id(self) -> str:
        return self._turn_id

    def stt(
        self,
        provider: str,
        *,
        model: str | None = None,
        ttfb_ms: float | None = None,
        final_ms: float | None = None,
        transcript_final: bool = True,
        confidence: str = "measured",
        attributes: Mapping[str, Any] | None = None,
    ) -> TurnRecorder:
        """Record the speech-to-text stage. ``final_ms`` is audio-stop to final transcript."""

        provider = self._label(provider, "provider")
        model = self._optional_label(model, "model")
        confidence = self._confidence(confidence)
        ttfb_ms = self._optional_ms(ttfb_ms, "earshot.stt.ttfb")
        final_ms = self._optional_ms(final_ms, "stt final")
        duration = self._duration(final_ms, ttfb_ms)
        operation_id = self._operation(
            "stt", provider, model=model, attributes=attributes,
        )
        if ttfb_ms is not None:
            self._measurement(
                "earshot.stt.ttfb", ttfb_ms, operation_id, confidence, "pipeline.stt.ttfb"
            )
        if final_ms is not None and transcript_final:
            self._event("earshot.transcript.final", self._cursor_ms + final_ms, _USER, confidence)
        self._advance(duration)
        return self

    def llm(
        self,
        provider: str,
        *,
        model: str | None = None,
        ttft_ms: float | None = None,
        completion_ms: float | None = None,
        confidence: str = "measured",
        attributes: Mapping[str, Any] | None = None,
    ) -> TurnRecorder:
        """Record the LLM stage. ``ttft_ms`` feeds the derived first-token latency."""

        provider = self._label(provider, "provider")
        model = self._optional_label(model, "model")
        confidence = self._confidence(confidence)
        ttft_ms = self._optional_ms(ttft_ms, "earshot.llm.ttft")
        completion_ms = self._optional_ms(completion_ms, "llm completion")
        duration = self._duration(completion_ms, ttft_ms)
        operation_id = self._operation(
            "llm", provider, model=model, attributes=attributes,
        )
        if ttft_ms is not None:
            self._measurement(
                "earshot.llm.ttft", ttft_ms, operation_id, confidence, "pipeline.llm.ttft"
            )
        self._advance(duration)
        return self

    def tts(
        self,
        provider: str,
        *,
        model: str | None = None,
        voice: str | None = None,
        ttfb_ms: float | None = None,
        first_audio_ms: float | None = None,
        confidence: str = "measured",
        attributes: Mapping[str, Any] | None = None,
    ) -> TurnRecorder:
        """Record the text-to-speech stage. ``ttfb_ms`` feeds generated-response latency."""

        provider = self._label(provider, "provider")
        model = self._optional_label(model, "model")
        voice = self._optional_label(voice, "voice")
        confidence = self._confidence(confidence)
        ttfb_ms = self._optional_ms(ttfb_ms, "earshot.tts.ttfb")
        first_audio_ms = self._optional_ms(first_audio_ms, "tts first audio")
        stage_attributes = dict(attributes or {})
        if voice is not None:
            stage_attributes["earshot.tts.voice"] = voice
        duration = self._duration(first_audio_ms, ttfb_ms)
        operation_id = self._operation(
            "tts", provider, model=model, attributes=stage_attributes,
        )
        if ttfb_ms is not None:
            self._measurement(
                "earshot.tts.ttfb", ttfb_ms, operation_id, confidence, "pipeline.tts.ttfb"
            )
        if first_audio_ms is not None and ttfb_ms is None:
            self._event(
                "earshot.response.first_audio_generated",
                self._cursor_ms + first_audio_ms,
                _AGENT,
                confidence,
            )
        self._advance(duration)
        return self

    def vad(
        self,
        *,
        speech_start_ms: float | None = None,
        speech_end_ms: float | None = None,
        confidence: str = "measured",
    ) -> TurnRecorder:
        """Mark user speech boundaries; paired with STT they yield finalization latency."""

        confidence = self._confidence(confidence)
        speech_start_ms = self._optional_ms(speech_start_ms, "speech start")
        speech_end_ms = self._optional_ms(speech_end_ms, "speech end")
        if speech_start_ms is not None:
            self._event("earshot.speech.started", speech_start_ms, _USER, confidence)
        if speech_end_ms is not None:
            self._event("earshot.speech.ended", speech_end_ms, _USER, confidence)
            self._cursor_ms = max(self._cursor_ms, speech_end_ms)
        return self

    def barge_in(
        self,
        *,
        at_ms: float,
        accepted: bool = True,
        confidence: str = "inferred",
    ) -> TurnRecorder:
        """Record a caller barge-in (``accepted`` = real interruption vs ignored)."""

        confidence = self._confidence(confidence)
        at_ms = self._optional_ms(at_ms, "barge-in offset")
        assert at_ms is not None
        name = "earshot.interruption.accepted" if accepted else "earshot.interruption.ignored"
        self._event(name, at_ms, _USER, confidence)
        return self

    def record_stage(
        self,
        operation_name: str,
        provider: str,
        *,
        model: str | None = None,
        status: str = "ok",
        at_ms: float | None = None,
        ended_at_ms: float | None = None,
        source: str = "app",
        confidence: str = "inferred",
        source_field: str = "pipeline.stage",
        attributes: Mapping[str, Any] | None = None,
    ) -> str:
        """Author a provider stage without inventing boundaries it did not expose."""

        operation_name = self._label(operation_name, "operation name")
        provider = self._label(provider, "provider")
        model = self._optional_label(model, "model")
        status = self._label(status, "status")
        source = self._label(source, "evidence source")
        confidence = self._confidence(confidence)
        source_field = self._label(source_field, "source field")
        start = self._cursor_ms if at_ms is None else self._required_ms(at_ms, "stage offset")
        end = self._optional_ms(ended_at_ms, "stage end offset")
        if end is not None and end < start:
            raise ValueError("stage end offset must not precede its start")

        stage_attributes: dict[str, Any] = dict(attributes or {})
        stage_attributes["gen_ai.provider.name"] = provider
        if model is not None:
            stage_attributes["gen_ai.request.model"] = model
        self._sequence += 1
        operation_id = f"operation-{operation_name}-{self._turn_index}-{self._sequence}"
        self._session.recorder.record_operation(
            operation_id=operation_id,
            operation_name=operation_name,
            status=status,
            started_at=self._point(start),
            ended_at=None if end is None else self._point(end),
            participant_id=_AGENT,
            turn_id=self._turn_id,
            evidence=self._fact_evidence(source, confidence, source_field),
            attributes=stage_attributes,
        )
        boundary = start if end is None else end
        self._cursor_ms = max(self._cursor_ms, boundary)
        self._max_ms = max(self._max_ms, boundary)
        return operation_id

    def record_event(
        self,
        name: str,
        *,
        at_ms: float,
        participant: str | None = None,
        source: str = "app",
        confidence: str = "estimated",
        source_field: str = "pipeline.event",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Author a turn-relative point event with fact-specific evidence."""

        name = self._label(name, "event name")
        offset = self._required_ms(at_ms, "event offset")
        participant_id = self._participant(participant)
        source = self._label(source, "evidence source")
        confidence = self._confidence(confidence)
        source_field = self._label(source_field, "source field")
        self._sequence += 1
        self._session.recorder.record_event(
            name,
            event_id=f"event-{self._turn_index}-{self._sequence}",
            time=self._point(offset),
            participant_id=participant_id,
            turn_id=self._turn_id,
            evidence=self._fact_evidence(source, confidence, source_field),
            attributes=attributes,
        )
        self._max_ms = max(self._max_ms, offset)

    def record_measurement(
        self,
        name: str,
        value: float,
        *,
        unit: str,
        operation_id: str | None = None,
        source: str,
        confidence: str,
        source_field: str | None = None,
        basis: str | None = None,
        at_ms: float | None = None,
        quality_kind: str = "provider_metric",
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Author a provider-native or standard scalar without relabeling its meaning."""

        name = self._label(name, "measurement name")
        normalized_value = self._finite_number(value, name)
        unit = self._label(unit, "measurement unit")
        operation_id = self._optional_label(operation_id, "operation id")
        source = self._label(source, "evidence source")
        confidence = self._confidence(confidence)
        source_field = self._label(source_field or name, "source field")
        basis = self._optional_label(basis, "measurement basis")
        quality_kind = self._label(quality_kind, "quality kind")
        offset = self._cursor_ms if at_ms is None else self._required_ms(
            at_ms, "measurement offset"
        )

        sample_attributes: dict[str, Any] = dict(attributes or {})
        sample_attributes["earshot.turn.id"] = self._turn_id
        sample_attributes["earshot.correlation"] = "provider_turn_scalar"
        sample_attributes["earshot.chronology"] = "not_exposed"
        if operation_id is not None:
            sample_attributes["earshot.operation.id"] = operation_id
        if basis is not None:
            sample_attributes["earshot.metric.basis"] = basis
        self._sequence += 1
        point = self._point(offset)
        self._session.recorder.record_quality_sample(
            QualitySample(
                sample_id=f"quality-{self._turn_index}-{self._sequence}",
                session_id=self._session.session_id,
                quality_kind=quality_kind,
                sample_window=TimeRange(start=point, end=point),
                measurements=(
                    QualityMeasurement(name=name, value=normalized_value, unit=unit),
                ),
                evidence=self._fact_evidence(source, confidence, source_field),
                participant_id=_AGENT,
                attributes=sample_attributes,
            )
        )
        self._max_ms = max(self._max_ms, offset)

    def record_omission(
        self,
        field_name: str,
        *,
        capture_class: str | CaptureClass,
        reason: str = "adapter_payload_omitted",
    ) -> None:
        """Ledger a discarded provider field without retaining its value."""

        self._session.recorder.record_omission(
            field_name,
            capture_class=capture_class,
            reason=reason,
        )

    # -- internals -----------------------------------------------------------

    def _operation(
        self,
        operation_name: str,
        provider: str,
        *,
        model: str | None,
        attributes: Mapping[str, Any] | None,
    ) -> str:
        return self.record_stage(
            operation_name,
            provider,
            model=model,
            source="app",
            confidence="inferred",
            source_field="pipeline.stage_order",
            attributes=attributes,
        )

    def _measurement(
        self,
        name: str,
        value_ms: float,
        operation_id: str,
        confidence: str,
        source_field: str,
    ) -> None:
        if value_ms < 0:
            raise ValueError(f"{name} latency must be non-negative")
        self._sequence += 1
        point = self._point(self._cursor_ms)
        self._session.recorder.record_quality_sample(
            QualitySample(
                sample_id=f"quality-{self._turn_index}-{self._sequence}",
                session_id=self._session.session_id,
                quality_kind="provider_latency",
                sample_window=TimeRange(start=point, end=point),
                measurements=(QualityMeasurement(name=name, value=float(value_ms), unit="ms"),),
                evidence=self._evidence(confidence, source_field),
                participant_id=_AGENT,
                attributes={
                    "earshot.turn.id": self._turn_id,
                    "earshot.operation.id": operation_id,
                    "earshot.correlation": "provider_turn_scalar",
                    "earshot.chronology": "not_exposed",
                },
            )
        )

    def _event(self, name: str, at_ms: float, participant_id: str, confidence: str) -> None:
        participant = "user" if participant_id == _USER else "agent"
        self.record_event(
            name,
            at_ms=at_ms,
            participant=participant,
            source=_confidence_source(confidence),
            confidence=confidence,
        )

    def _evidence(self, confidence: str, source_field: str) -> Evidence:
        return self._fact_evidence(_confidence_source(confidence), confidence, source_field)

    @staticmethod
    def _fact_evidence(source: str, confidence: str, source_field: str) -> Evidence:
        return Evidence(
            source=source,
            observer="server",
            method="pipeline_capture",
            method_version=PIPELINE_ADAPTER_VERSION,
            source_field=source_field,
            confidence=confidence,
            availability="available",
        )

    def _point(self, offset_ms: float) -> TimePoint:
        absolute_nano = self._session.turn_origin_nano(self._turn_index) + int(
            offset_ms * _MS_TO_NANO
        )
        return TimePoint(
            source_time_unix_nano=str(self._session.start_wall_nano + absolute_nano),
            monotonic_time_nano=str(absolute_nano),
            clock_domain_id=self._session.clock_domain_id,
            uncertainty_nano="1000000",
        )

    @staticmethod
    def _duration(primary_ms: float | None, secondary_ms: float | None) -> float:
        for candidate in (primary_ms, secondary_ms):
            if candidate is not None:
                return candidate
        return 0.0

    @staticmethod
    def _optional_ms(value: float | None, label: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{label} must be a number")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{label} must be finite and non-negative")
        return normalized

    @classmethod
    def _required_ms(cls, value: float, label: str) -> float:
        normalized = cls._optional_ms(value, label)
        assert normalized is not None
        return normalized

    @staticmethod
    def _finite_number(value: float, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{label} must be a number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{label} must be finite")
        return normalized

    @staticmethod
    def _participant(value: str | None) -> str | None:
        if value is None:
            return None
        if value in {"user", _USER}:
            return _USER
        if value in {"agent", _AGENT}:
            return _AGENT
        raise ValueError("participant must be user, agent, or None")

    @staticmethod
    def _label(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value

    @classmethod
    def _optional_label(cls, value: str | None, label: str) -> str | None:
        return None if value is None else cls._label(value, label)

    @staticmethod
    def _confidence(value: str) -> str:
        if value not in {"measured", "estimated", "inferred"}:
            raise ValueError("confidence must be measured, estimated, or inferred")
        return value

    def _advance(self, span_ms: float) -> None:
        self._cursor_ms += span_ms
        self._max_ms = max(self._max_ms, self._cursor_ms)


class PipelineSession:
    """A provider-neutral capture session for one voice conversation."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        bundle_id: str | None = None,
        framework: str = "custom_pipeline",
        started_at_unix_nano: int | None = None,
        producer_name: str = "earshot.pipeline",
        config: RecorderConfig | None = None,
    ) -> None:
        import time as _time

        self.start_wall_nano = (
            started_at_unix_nano if started_at_unix_nano is not None else _time.time_ns()
        )
        self._clock = ManualClock(wall=self.start_wall_nano, monotonic=0)
        runtime_config, exporter = _runtime_snapshot()
        pipeline_adapter = Adapter(
            name="earshot.pipeline",
            version=PIPELINE_ADAPTER_VERSION,
            framework=framework,
        )
        if config is None:
            recorder_config = RecorderConfig(
                producer_name=producer_name,
                producer_version=PIPELINE_ADAPTER_VERSION,
                capture_policy=runtime_config.capture_policy,
                adapters=(pipeline_adapter,),
            )
        else:
            adapters = config.adapters
            if not any(adapter.name == pipeline_adapter.name for adapter in adapters):
                adapters = (*adapters, pipeline_adapter)
            recorder_config = replace(config, adapters=adapters)
        self.recorder = IncidentRecorder(
            session_id=session_id,
            bundle_id=bundle_id,
            clock=self._clock,
            config=recorder_config,
            exporter=exporter,
        )
        self.recorder.add_participant(_USER, role="user", endpoint_kind="app")
        self.recorder.add_participant(_AGENT, role="agent", endpoint_kind="app")
        # A self-instrumented server pipeline cannot see the client's playout.
        self.recorder.record_coverage(
            "client.render", "not_observed", "server_cannot_observe_client_render"
        )
        self._turn_origins_nano: list[int] = []
        self._turn_ids: set[str] = set()
        self._cursor_nano = 0
        self._closed = False

    @property
    def session_id(self) -> str:
        return self.recorder.session_id

    @property
    def bundle_id(self) -> str:
        return self.recorder.bundle_id

    @property
    def clock_domain_id(self) -> str:
        return self.recorder.clock_domain_id

    def turn_origin_nano(self, turn_index: int) -> int:
        return self._turn_origins_nano[turn_index]

    @contextlib.contextmanager
    def turn(
        self,
        turn_id: str | None = None,
        *,
        gap_ms: float = _DEFAULT_TURN_GAP_MS,
    ) -> Iterator[TurnRecorder]:
        """Open a conversational turn; stages recorded on it share one turn id."""

        if self._closed:
            raise RuntimeError("pipeline session is closed")
        normalized_gap = TurnRecorder._optional_ms(gap_ms, "turn gap")
        assert normalized_gap is not None
        turn_index = len(self._turn_origins_nano)
        resolved_turn_id = turn_id or f"turn-{turn_index}"
        resolved_turn_id = TurnRecorder._label(resolved_turn_id, "turn id")
        if resolved_turn_id in self._turn_ids:
            raise ValueError("turn id must be unique within a pipeline session")
        self._turn_ids.add(resolved_turn_id)
        self._turn_origins_nano.append(self._cursor_nano)
        recorder = TurnRecorder(self, resolved_turn_id, turn_index)
        try:
            yield recorder
        finally:
            turn_span_nano = int(recorder._max_ms * _MS_TO_NANO) + int(
                normalized_gap * _MS_TO_NANO
            )
            self._cursor_nano += turn_span_nano

    def close(self, status: str = "completed") -> IncidentBundle:
        """Finalize and return the immutable incident for ingestion or analysis."""

        if not self._closed:
            self._clock.advance(self._cursor_nano)
            self._closed = True
        return self.recorder.close(status=status)


def pipeline(
    session_id: str | None = None,
    *,
    bundle_id: str | None = None,
    framework: str = "custom_pipeline",
    started_at_unix_nano: int | None = None,
    producer_name: str = "earshot.pipeline",
    config: RecorderConfig | None = None,
) -> PipelineSession:
    """Start a provider-neutral pipeline capture session.

    Use this when you wire raw STT/LLM/TTS providers into your own pipeline. Record
    each turn's stages and barge-ins, then ``close()`` to obtain a contract-valid,
    evidence-qualified incident that analyzes like a framework-instrumented session.
    """

    return PipelineSession(
        session_id=session_id,
        bundle_id=bundle_id,
        framework=framework,
        started_at_unix_nano=started_at_unix_nano,
        producer_name=producer_name,
        config=config,
    )
