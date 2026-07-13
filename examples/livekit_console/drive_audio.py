"""Headless FULL-pipeline driver — real STT + LLM + TTS + turn detection.

Unlike drive_once.py (text-only LLM/TTS harness) and unlike agent.py console
(needs a mic), this pushes a real synthesized user utterance through a roomless
AgentSession so LiveKit runs — and meters — the whole pipeline: VAD -> STT ->
end-of-utterance -> LLM -> TTS. Our adapter records the real metrics/spans and we
build + validate + analyze a real incident. No microphone required.

    set -a && . ./.env && set +a
    python examples/livekit_console/drive_audio.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

from _runtime import LIVEKIT_AGENTS_VERSION, NullAudioOutput
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    UserInputTranscribedEvent,
    telemetry,
)
from livekit.agents.utils.audio import audio_frames_from_file, silence_frame
from livekit.agents.voice import io as vio
from livekit.plugins import openai, silero
from openai import OpenAI
from opentelemetry.sdk.trace import TracerProvider

import earshot
from earshot.adapters import LiveKitAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, encode_incident_json
from earshot.validation import validate_incident

SR = 24000
UTTERANCE = "What is the capital of France? Answer in one word."
OUTPUT_DIR = pathlib.Path(".earshot/livekit_console")
USER_WAV = OUTPUT_DIR / "user_utterance.wav"
INCIDENT_PATH = OUTPUT_DIR / "audio_incident.json"


def synth_user_utterance(path: pathlib.Path) -> None:
    """One real OpenAI TTS call to create the user's spoken audio input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    with client.audio.speech.with_streaming_response.create(
        model="tts-1", voice="echo", input=UTTERANCE, response_format="wav"
    ) as response:
        response.stream_to_file(str(path))


class WavAudioInput(vio.AudioInput):
    """Feeds pre-built frames into the session, paced ~real-time so VAD segments."""

    def __init__(self, frames: list[rtc.AudioFrame]) -> None:
        super().__init__(label="earshot-wav")
        self._frames = frames
        self._i = 0

    def __aiter__(self) -> WavAudioInput:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        if self._i < len(self._frames):
            frame = self._frames[self._i]
            self._i += 1
            await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)
            return frame
        # Keep the input stream alive with silence after the utterance.
        await asyncio.sleep(0.1)
        return silence_frame(0.1, SR)


async def build_frames() -> list[rtc.AudioFrame]:
    synth_user_utterance(USER_WAV)
    lead = [silence_frame(0.1, SR) for _ in range(5)]  # 0.5s so VAD sees onset
    speech = [frame async for frame in audio_frames_from_file(str(USER_WAV), sample_rate=SR)]
    trail = [silence_frame(0.1, SR) for _ in range(25)]  # 2.5s so EOU fires
    return lead + speech + trail


async def main() -> int:
    earshot.configure()
    recorder = earshot.session(session_id="headless-full-pipeline")
    provider: TracerProvider | None = None
    session: AgentSession | None = None
    sink = NullAudioOutput(sample_rate=SR)
    reply_done = asyncio.Event()
    transcript: dict[str, str] = {}
    lifecycle_status = "failed"
    bundle = None
    artifact_written = False
    input_synthesized = False

    try:
        provider = TracerProvider()
        telemetry.set_tracer_provider(provider)
        adapter = LiveKitAdapter(recorder, framework_version=LIVEKIT_AGENTS_VERSION)
        adapter.attach_span_processor(provider)
        frames = await build_frames()
        input_synthesized = True
        session = AgentSession(
            vad=silero.VAD.load(),
            stt=openai.STT(model="whisper-1"),
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=openai.TTS(model="tts-1", voice="alloy"),
        )
        adapter.attach_session_listeners(session)

        @session.on("user_input_transcribed")
        def _on_stt(ev: UserInputTranscribedEvent) -> None:
            if getattr(ev, "is_final", False):
                transcript["user"] = getattr(ev, "transcript", "")

        @session.on("conversation_item_added")
        def _on_item(ev: ConversationItemAddedEvent) -> None:
            item = getattr(ev, "item", None)
            if getattr(item, "role", None) == "assistant":
                transcript["agent"] = getattr(item, "text_content", "") or ""
                reply_done.set()

        await session.start(agent=Agent(instructions="Answer in one short word."))
        session.output.audio = sink
        session.input.audio = WavAudioInput(frames)
        session.input.set_audio_enabled(True)

        try:
            await asyncio.wait_for(reply_done.wait(), timeout=45)
        except TimeoutError:
            lifecycle_status = "timed_out"
            print("[driver] timed out waiting for the agent reply", file=sys.stderr)

        if lifecycle_status != "timed_out":
            try:
                await asyncio.wait_for(session.wait_for_idle(), timeout=15)
            except TimeoutError:
                lifecycle_status = "timed_out"
                print("[driver] timed out waiting for session idle", file=sys.stderr)

        if lifecycle_status != "timed_out":
            if not transcript.get("user"):
                raise RuntimeError("the audio pipeline produced no final STT transcript")
            if not transcript.get("agent"):
                raise RuntimeError("the audio pipeline produced no assistant reply")
            if not sink.saw_audio:
                raise RuntimeError("the audio pipeline produced no TTS audio")
            lifecycle_status = "completed"
    except Exception as error:
        lifecycle_status = "failed"
        print(f"[driver] audio pipeline failed: {error!r}", file=sys.stderr)
    finally:
        if session is not None:
            try:
                await session.aclose()
            except Exception as error:
                lifecycle_status = "failed"
                print(f"[driver] session close failed: {error!r}", file=sys.stderr)

        try:
            if provider is None:
                raise RuntimeError("trace provider was not initialized")
            flushed = await asyncio.to_thread(provider.force_flush, timeout_millis=5_000)
        except Exception as error:
            lifecycle_status = "failed"
            print(f"[driver] trace-provider flush failed: {error!r}", file=sys.stderr)
        else:
            if not flushed:
                lifecycle_status = "failed"
                print("[driver] trace-provider flush timed out", file=sys.stderr)

        try:
            bundle = recorder.close(lifecycle_status)
            INCIDENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            INCIDENT_PATH.write_bytes(encode_incident_json(bundle, indent=2))
            artifact_written = True
        except Exception as error:
            print(f"[driver] incident finalization failed: {error!r}", file=sys.stderr)
        finally:
            earshot.shutdown()

    print(f"[driver] USER heard-as : {transcript.get('user', '(no STT result)')!r}")
    print(f"[driver] AGENT reply   : {transcript.get('agent', '(no reply)')!r}")

    if bundle is None:
        return 1

    report = validate_incident(bundle)
    profile = bundle.profile

    print("\n" + "=" * 72)
    print("REAL FULL-PIPELINE INCIDENT (LiveKit + OpenAI, headless)")
    print("=" * 72)
    print(f"  lifecycle status          : {profile.session.status}")
    print(f"  valid against v1 contract : {report.ok}  (errors={len(report.errors)})")
    for issue in report.errors[:8]:
        print(f"     - {issue.code} at {'.'.join(str(p) for p in issue.path)}")
    op_names = sorted({op.operation_name for op in profile.operations})
    print(f"  operations                : {len(profile.operations)}")
    print(f"  real stages captured      : {', '.join(op_names) or '(none)'}")
    print(f"  events                    : {len(profile.events)}")
    print(f"  quality_samples           : {len(profile.quality_samples)}")
    expected_stages = {"llm", "stt", "tts", "turn_detection"}
    missing_stages = expected_stages.difference(op_names)
    vad_seen = any(
        measurement.name == "earshot.metric.inference.count"
        for sample in profile.quality_samples
        for measurement in sample.measurements
    )
    if missing_stages:
        print(f"  missing expected stages   : {', '.join(sorted(missing_stages))}")
    if not vad_seen:
        print("  missing expected signal   : VAD inference metric")

    digest = analysis_input_sha256(bundle)
    analysis = analyze_incident(bundle, input_sha256=digest, generated_at_unix_nano=time.time_ns())
    for turn in analysis.projections.turns:
        m = turn.metrics

        def show(metric: object) -> str:
            return (
                f"{metric.value:.0f}{metric.unit}"
                if getattr(metric, "value", None) is not None
                else metric.availability
            )

        print(f"  turn {turn.turn_id[:16]:16}")
        print(f"     first_token      : {show(m.first_token_latency)}")
        print(f"     generated (TTS)  : {show(m.generated_response_latency)}")
        print(f"     response         : {show(m.response_latency)}")
    if analysis.projections.limitations:
        print(f"  limitations               : {', '.join(analysis.projections.limitations)}")

    input_path = str(USER_WAV) if input_synthesized else "(not produced in this run)"
    print(f"  synthesized input         : {input_path}")
    print(f"  full artifact             : {INCIDENT_PATH}")
    print("=" * 72)
    succeeded = (
        lifecycle_status == "completed"
        and artifact_written
        and report.ok
        and not missing_stages
        and vad_seen
        and bool(transcript.get("user"))
        and bool(transcript.get("agent"))
        and sink.saw_audio
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
