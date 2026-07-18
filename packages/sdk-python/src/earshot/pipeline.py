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
from collections.abc import Iterator, Mapping
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
from .recorder import IncidentRecorder, RecorderConfig

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

    Stages are placed on the turn clock in call order; each stage occupies its own
    interval so the waterfall is readable, while provider-reported latencies
    (TTFT/TTFB) are attached as governed measurements the analyzer consumes.
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

        duration = self._duration(final_ms, ttfb_ms)
        operation_id = self._operation(
            "stt", provider, model=model, duration_ms=duration, source="pipeline.stt",
            confidence=confidence, attributes=attributes,
        )
        if ttfb_ms is not None:
            self._measurement(
                "earshot.stt.ttfb", ttfb_ms, operation_id, confidence, "pipeline.stt.ttfb"
            )
        if final_ms is not None and transcript_final:
            self._event("earshot.transcript.final", self._cursor_ms + final_ms, _AGENT, confidence)
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

        duration = self._duration(completion_ms, ttft_ms)
        operation_id = self._operation(
            "llm", provider, model=model, duration_ms=duration, source="pipeline.llm",
            confidence=confidence, attributes=attributes,
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

        stage_attributes = dict(attributes or {})
        if voice is not None:
            stage_attributes["earshot.tts.voice"] = voice
        duration = self._duration(first_audio_ms, ttfb_ms)
        operation_id = self._operation(
            "tts", provider, model=model, duration_ms=duration, source="pipeline.tts",
            confidence=confidence, attributes=stage_attributes,
        )
        if ttfb_ms is not None:
            self._measurement(
                "earshot.tts.ttfb", ttfb_ms, operation_id, confidence, "pipeline.tts.ttfb"
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

        if speech_start_ms is not None:
            self._event(
                "earshot.speech.started", self._cursor_ms + speech_start_ms, _USER, confidence
            )
        if speech_end_ms is not None:
            self._event("earshot.speech.ended", self._cursor_ms + speech_end_ms, _USER, confidence)
            self._advance(speech_end_ms)
        return self

    def barge_in(
        self,
        *,
        at_ms: float,
        accepted: bool = True,
        confidence: str = "inferred",
    ) -> TurnRecorder:
        """Record a caller barge-in (``accepted`` = real interruption vs ignored)."""

        name = "earshot.interruption.accepted" if accepted else "earshot.interruption.ignored"
        self._event(name, self._cursor_ms + at_ms, _USER, confidence)
        self._advance(at_ms)
        return self

    # -- internals -----------------------------------------------------------

    def _operation(
        self,
        operation_name: str,
        provider: str,
        *,
        model: str | None,
        duration_ms: float,
        source: str,
        confidence: str,
        attributes: Mapping[str, Any] | None,
    ) -> str:
        self._sequence += 1
        operation_id = f"operation-{operation_name}-{self._turn_index}-{self._sequence}"
        stage_attributes: dict[str, Any] = {"gen_ai.provider.name": provider}
        if model is not None:
            stage_attributes["gen_ai.request.model"] = model
        stage_attributes.update(attributes or {})
        self._session.recorder.record_operation(
            operation_id=operation_id,
            operation_name=operation_name,
            status="ok",
            started_at=self._point(self._cursor_ms),
            ended_at=self._point(self._cursor_ms + duration_ms),
            participant_id=_AGENT,
            turn_id=self._turn_id,
            evidence=self._evidence(confidence, source),
            attributes=stage_attributes,
        )
        return operation_id

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
        self._sequence += 1
        self._session.recorder.record_event(
            name,
            event_id=f"event-{self._turn_index}-{self._sequence}",
            time=self._point(at_ms),
            participant_id=participant_id,
            turn_id=self._turn_id,
            evidence=self._evidence(confidence, "pipeline.event"),
        )
        self._max_ms = max(self._max_ms, at_ms)

    def _evidence(self, confidence: str, source_field: str) -> Evidence:
        return Evidence(
            source=_confidence_source(confidence),
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
                if candidate < 0:
                    raise ValueError("stage duration must be non-negative")
                return float(candidate)
        return 0.0

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
        recorder_config = config or RecorderConfig(
            producer_name=producer_name,
            producer_version=PIPELINE_ADAPTER_VERSION,
            adapters=(
                Adapter(
                    name="earshot.pipeline",
                    version=PIPELINE_ADAPTER_VERSION,
                    framework=framework,
                ),
            ),
        )
        self.recorder = IncidentRecorder(
            session_id=session_id,
            bundle_id=bundle_id,
            clock=self._clock,
            config=recorder_config,
        )
        self.recorder.add_participant(_USER, role="user", endpoint_kind="app")
        self.recorder.add_participant(_AGENT, role="agent", endpoint_kind="app")
        # A self-instrumented server pipeline cannot see the client's playout.
        self.recorder.record_coverage(
            "client.render", "not_observed", "server_cannot_observe_client_render"
        )
        self._turn_origins_nano: list[int] = []
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
        turn_index = len(self._turn_origins_nano)
        self._turn_origins_nano.append(self._cursor_nano)
        recorder = TurnRecorder(self, turn_id or f"turn-{turn_index}", turn_index)
        yield recorder
        turn_span_nano = int(recorder._max_ms * _MS_TO_NANO) + int(gap_ms * _MS_TO_NANO)
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
