"""Run one real Pipecat 1.5 voice call and export its traces over OTLP/HTTP.

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318   # Earshot, or any OTLP receiver
    python examples/pipecat_otlp/run_call.py --out .earshot/pipecat_otlp/run [--upload]

A scripted caller WAV (see make_caller_audio.py) streams in at real time through
Silero VAD, Deepgram STT, Groq LLM and Cartesia TTS; bot audio plays out at real
time into nothing. Spans go to the HTTP ``OTLPSpanExporter()``, configured only by
the standard ``OTEL_EXPORTER_OTLP_*`` env vars. At the end the script writes the
per-speaker mono WAVs, one stereo ``call.wav`` built from them (channel 0 user,
channel 1 agent), and ``manifest.json`` (conversation id, recording
``started_at_unix_nano``, exported span count, versions, providers) to ``--out``.

``started_at_unix_nano`` is when the input produced its first audio frame, which is
sample 0 of both tracks (``call_io.WavCallerInput``). With ``--upload [BASE_URL]``
(default: the OTLP endpoint, which is Earshot's base URL; the API key from
``EARSHOT_API_KEY`` or the OTLP headers) the stereo recording is created with
``earshot.client`` at that first audio, with ``channel_speakers`` ``["user",
"agent"]``, and uploaded after the run and the tracer's flush (``call_recording.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import wave
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from call_io import PacedDiscardOutput, WavCallerInput
from call_recording import create_call_recording, stereo_wav
from loguru import logger
from make_caller_audio import CALLER_SCRIPT, DEFAULT_CARTESIA_MODEL, SAMPLE_RATE, ensure_caller_wav
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.utils.tracing.setup import setup_tracing
from pipecat.workers.runner import WorkerRunner

from earshot.client import EarshotClient, authorization_from_environment

DEEPGRAM_MODEL = "nova-3-general"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
SYSTEM_PROMPT = (
    "You are the host at a small restaurant, taking a booking on the phone. "
    "Answer in one or two short spoken sentences, without lists or emojis."
)
REQUIRED_ENV = (
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "CARTESIA_API_KEY",
    "CARTESIA_VOICE_ID",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)
SECRET_ENV = (
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "CARTESIA_API_KEY",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "EARSHOT_API_KEY",
)
# A call where any of these never produced a span did not really run: Pipecat 1.5
# only logs a warning and retries when, for example, Deepgram refuses the key (401).
PROVIDER_SPANS = ("stt", "llm", "tts")
QUIET_BEFORE_END_S = 2.0
END_WAIT_S = 20.0
DRAIN_TIMEOUT_S = 30.0


class SpanCounter(SpanProcessor):
    """Tee processor: counts every span handed to the exporter pipeline, by name."""

    def __init__(self) -> None:
        self.names: Counter[str] = Counter()

    def on_end(self, span: ReadableSpan) -> None:
        if span.context is not None and span.context.trace_flags.sampled:
            self.names[span.name] += 1


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def _redacting_log_sink(message: str) -> None:
    text = str(message)
    for key in SECRET_ENV:
        if os.environ.get(key):
            text = text.replace(os.environ[key], f"[{key}]")
    sys.stderr.write(text)


async def run_call(out: Path, conversation_id: str, upload_url: str | None = None) -> dict:
    caller_pcm = _read_pcm(ensure_caller_wav())
    setup_tracing(service_name="earshot-pipecat-otlp-example", exporter=OTLPSpanExporter())
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    counter = SpanCounter()
    provider.add_span_processor(counter)

    groq_model = os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    cartesia_model = os.environ.get("CARTESIA_MODEL") or DEFAULT_CARTESIA_MODEL
    caller, speaker = WavCallerInput(caller_pcm), PacedDiscardOutput()
    recorder = AudioBufferProcessor(sample_rate=SAMPLE_RATE, auto_start_recording=True)
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(model=DEEPGRAM_MODEL),
    )
    llm = GroqLLMService(
        api_key=os.environ["GROQ_API_KEY"],
        settings=GroqLLMService.Settings(model=groq_model, system_instruction=SYSTEM_PROMPT),
    )
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            model=cartesia_model, voice=os.environ["CARTESIA_VOICE_ID"]
        ),
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(
        LLMContext(), user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    pipeline = Pipeline([caller, stt, user_agg, llm, tts, speaker, recorder, assistant_agg])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE, enable_metrics=True, enable_usage_metrics=True
        ),
        enable_tracing=True,
        enable_turn_tracking=True,
        conversation_id=conversation_id,
        additional_span_attributes={"earshot.session.id": conversation_id},
        enable_rtvi=False,
        idle_timeout_secs=None,
    )
    tracks: dict[str, bytes] = {}
    errors: list[str] = []

    @recorder.event_handler("on_track_audio_data")
    async def on_tracks(_buffer, user_pcm: bytes, bot_pcm: bytes, _rate: int, _channels: int):
        tracks.update(user=user_pcm, agent=bot_pcm)

    @worker.event_handler("on_pipeline_error")
    async def on_error(_worker, frame) -> None:
        errors.append(str(frame.error))

    uploader = (
        None
        if upload_url is None
        else EarshotClient(upload_url, authorization=authorization_from_environment())
    )

    async def create_recording_at_first_audio() -> dict:
        # A pending recording holds the call open in Earshot until its audio arrives.
        await caller.first_audio.wait()
        assert uploader is not None and caller.started_at_unix_nano is not None
        return await asyncio.to_thread(
            create_call_recording, uploader, conversation_id, caller.started_at_unix_nano
        )

    created = None if uploader is None else asyncio.create_task(create_recording_at_first_audio())
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    script_seconds = len(caller_pcm) / (2 * SAMPLE_RATE)
    await asyncio.wait_for(caller.script_done.wait(), timeout=script_seconds + 30)
    quiet_deadline = time.monotonic() + END_WAIT_S
    while time.monotonic() < quiet_deadline and (
        caller.bot_speaking or time.monotonic() - caller.bot_stopped_at < QUIET_BEFORE_END_S
    ):
        await asyncio.sleep(0.1)
    await worker.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(run_task, timeout=DRAIN_TIMEOUT_S)
    finally:
        if created is not None and not caller.first_audio.is_set():
            created.cancel()  # no audio, so no recording to create
    flushed = await asyncio.to_thread(provider.force_flush, 10_000)
    provider.shutdown()

    if errors:
        raise RuntimeError(f"pipeline reported {len(errors)} error(s): {errors}")
    silent = [name for name in PROVIDER_SPANS if not counter.names[name]]
    if silent:
        raise RuntimeError(f"no {'/'.join(silent)} span: a provider never answered (see the log)")
    if not flushed or caller.started_at_unix_nano is None or set(tracks) != {"user", "agent"}:
        raise RuntimeError("call ended without a flush, a first audio frame, or both tracks")
    out.mkdir(parents=True, exist_ok=True)
    for name, pcm in tracks.items():
        _write_wav(out / f"{name}.wav", pcm, SAMPLE_RATE)
    (out / "call.wav").write_bytes(stereo_wav(tracks["user"], tracks["agent"], SAMPLE_RATE))
    uploaded = {}
    if created is not None and uploader is not None:
        with uploader:
            recording = await created  # it exists (or this raises the server's refusal)
            uploaded["call"] = await asyncio.to_thread(
                uploader.upload_recording, recording, out / "call.wav"
            )
    manifest = {
        "conversation_id": conversation_id,
        "started_at_unix_nano": str(caller.started_at_unix_nano),
        "exported_span_count": sum(counter.names.values()),
        "exported_span_names": dict(sorted(counter.names.items())),
        "pipecat_version": version("pipecat-ai"),
        "opentelemetry_sdk_version": version("opentelemetry-sdk"),
        "providers": {
            "stt": {"provider": "deepgram", "model": DEEPGRAM_MODEL},
            "llm": {"provider": "groq", "model": groq_model},
            "tts": {"provider": "cartesia", "model": cartesia_model},
        },
        "caller_script": list(CALLER_SCRIPT),
        "tracks": {
            "user": "user.wav",
            "agent": "agent.wav",
            "stereo": "call.wav",
            "channel_speakers": ["user", "agent"],
            "sample_rate": SAMPLE_RATE,
        },
        "recordings": {
            name: {key: body[key] for key in ("recording_id", "call_id", "sha256", "size")}
            for name, body in uploaded.items()
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"{path} must be {SAMPLE_RATE} Hz mono PCM16")
        return wav.readframes(wav.getnframes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one real Pipecat call with OTLP export.")
    parser.add_argument("--out", type=Path, default=Path(".earshot/pipecat_otlp/run"))
    parser.add_argument("--conversation-id", default=f"pipecat-otlp-{uuid.uuid4()}")
    parser.add_argument(
        "--upload",
        nargs="?",
        const="",
        metavar="BASE_URL",
        help="upload the stereo recording to Earshot (default: the OTLP endpoint)",
    )
    args = parser.parse_args()
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2
    logger.remove()
    logger.add(_redacting_log_sink, level=os.environ.get("LOG_LEVEL", "INFO"))
    upload_url = None
    if args.upload is not None:
        upload_url = args.upload or os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    manifest = asyncio.run(run_call(args.out, args.conversation_id, upload_url))
    summary = ("conversation_id", "exported_span_count", "recordings")
    print(json.dumps({key: manifest[key] for key in summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
