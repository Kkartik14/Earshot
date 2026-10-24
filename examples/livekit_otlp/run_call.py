"""Run one real LiveKit Agents 1.8 voice call headless and export its traces over OTLP/HTTP.

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318   # Earshot, or any OTLP receiver
    python examples/livekit_otlp/run_call.py --out .earshot/livekit_otlp/run

No LiveKit server and no WebRTC. ``AgentServer.simulate_job(fake_job=True)`` runs the
entrypoint as an in-process job (the way ``console`` mode does), so the call has a
real ``JobContext``: LiveKit's own ``RecorderIO`` records it, and the job's shutdown
spans follow ``session_close`` exactly as they do in production. The caller WAV
(``examples/pipecat_otlp/make_caller_audio.py``, real Cartesia) streams in at real
time through Silero VAD, Deepgram STT, Groq LLM and Cartesia TTS; agent audio plays
out at real time into nothing (``call_io.py``).

Spans go to the HTTP ``OTLPSpanExporter()``, configured only by the standard
``OTEL_EXPORTER_OTLP_*`` env vars, through
``telemetry.set_tracer_provider(provider, metadata=tracer_metadata(key))``, which
stamps ``earshot.session.id`` on every span.
``--out`` gets ``recording.ogg`` (RecorderIO: stereo Ogg/Opus, channel 0 = user,
channel 1 = agent, sample 0 = ``recording_started_at``), ``session_report.json`` and
``manifest.json``. The manifest is written when the session starts and completed
after the tracer's flush.

With ``--upload [BASE_URL]`` (default: the OTLP endpoint, which is Earshot's base
URL; the API key from ``EARSHOT_API_KEY`` or the OTLP headers), ``earshot.client``
creates the call's recording when the session starts (``mixed``, with
``channel_speakers`` ``["user", "agent"]``), so Earshot holds the call open for its
audio instead of closing it without, and ``on_session_end`` uploads
``recording.ogg`` to it: where a LiveKit host has the file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
import wave
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any

from call_io import CallerAudioInput, PacedAudioOutput
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobExecutorType, telemetry
from livekit.plugins import cartesia, deepgram, groq, silero
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from earshot.client import (
    EarshotClient,
    authorization_from_environment,
    recording_id_for,
    tracer_metadata,
)

# The caller script and audio are shared with the Pipecat example (stdlib only).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipecat_otlp"))
from make_caller_audio import (
    CALLER_SCRIPT,
    DEFAULT_CARTESIA_MODEL,
    SAMPLE_RATE,
    ensure_caller_wav,
)

SERVICE_NAME = "earshot-livekit-otlp-example"
ROOM_NAME = "earshot-livekit-otlp"
DEEPGRAM_MODEL = "nova-3"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
SYSTEM_PROMPT = (
    "You are the host at a small restaurant, taking a booking on the phone. "
    "Answer in one or two short spoken sentences, without lists or emojis."
)
# RecorderIO only; nothing is uploaded to LiveKit Cloud (no LIVEKIT_URL is set).
RECORD = {"audio": True, "traces": False, "logs": False, "transcript": False}
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
# RecorderIO's file: channel 0 is the user (the session's input), channel 1 the agent.
CHANNEL_SPEAKERS = ("user", "agent")
QUIET_BEFORE_END_S = 2.0
END_WAIT_S = 20.0
JOB_END_WAIT_S = 60.0
PACKAGES = (
    "livekit-agents",
    "livekit-plugins-deepgram",
    "livekit-plugins-groq",
    "livekit-plugins-cartesia",
    "livekit-plugins-silero",
    "livekit",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "av",
)


class SpanCounter(SpanProcessor):
    """Tee processor: counts every span handed to the exporter pipeline, by name."""

    def __init__(self) -> None:
        self.names: Counter[str] = Counter()

    def on_end(self, span: ReadableSpan) -> None:
        if span.context is not None and span.context.trace_flags.sampled:
            self.names[span.name] += 1


class RedactingFormatter(logging.Formatter):
    """Log lines (tracebacks included) never carry a secret env value."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for key in SECRET_ENV:
            if os.environ.get(key):
                text = text.replace(os.environ[key], f"[{key}]")
        return text


class Call:
    """What the job thread and the main thread share about the one call."""

    def __init__(
        self, key: str, out: Path, caller_pcm: bytes, earshot: EarshotClient | None = None
    ) -> None:
        self.key, self.out, self.caller_pcm, self.earshot = key, out, caller_pcm, earshot
        self.recording: dict[str, Any] | None = None  # created in Earshot at session start
        self.groq_model = os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
        self.cartesia_model = os.environ.get("CARTESIA_MODEL") or DEFAULT_CARTESIA_MODEL
        self.session_ended = threading.Event()
        self.errors: list[str] = []
        self.caller: CallerAudioInput | None = None
        self.speaker: PacedAudioOutput | None = None
        self.manifest: dict[str, Any] = {
            "session_key": key,
            "status": "in_progress",
            "versions": {package: version(package) for package in PACKAGES}
            | {"python": sys.version.split()[0]},
            "providers": {
                "stt": {"provider": "deepgram", "model": DEEPGRAM_MODEL},
                "llm": {"provider": "groq", "model": self.groq_model},
                "tts": {"provider": "cartesia", "model": self.cartesia_model},
                "vad": {"provider": "silero"},
            },
            "caller_script": list(CALLER_SCRIPT),
            "caller_audio": "examples/pipecat_otlp/make_caller_audio.py",
            "recording": {"file": "recording.ogg", "channels": list(CHANNEL_SPEAKERS)},
        }

    def write_manifest(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")


CALL: Call | None = None  # set by main() before the job starts; read by the job thread


async def entrypoint(ctx: JobContext) -> None:
    call = CALL
    assert call is not None
    try:
        await _run_session(ctx, call)
    except Exception as error:
        call.errors.append(repr(error))
        raise
    finally:
        ctx.shutdown(reason="caller hung up")


async def _run_session(ctx: JobContext, call: Call) -> None:
    caller = call.caller = CallerAudioInput(call.caller_pcm, sample_rate=SAMPLE_RATE)
    speaker = call.speaker = PacedAudioOutput()
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model=DEEPGRAM_MODEL),
        llm=groq.LLM(model=call.groq_model),
        tts=cartesia.TTS(model=call.cartesia_model, voice=os.environ["CARTESIA_VOICE_ID"]),
    )
    # Set before start(): with a job context and both audio I/Os present,
    # start(record=...) wraps them in RecorderIO, as it does for RoomIO in production.
    session.input.audio, session.output.audio = caller, speaker
    last_spoke = {"at": time.monotonic()}

    @session.on("agent_state_changed")
    def _on_agent_state(event: Any) -> None:
        if event.old_state == "speaking":
            last_spoke["at"] = time.monotonic()

    await session.start(agent=Agent(instructions=SYSTEM_PROMPT), record=RECORD)
    # 1.8.8 has no public accessor before the session report; the host needs the
    # recording's time zero now, to create its recording before the session closes.
    recorder = session._recorder_io
    if recorder is None or recorder.recording_started_at is None:
        raise RuntimeError("RecorderIO did not start (is there a job context?)")
    call.manifest["recording"]["started_at"] = recorder.recording_started_at
    call.manifest["recording"]["started_at_unix_nano"] = _unix_nano(recorder.recording_started_at)
    call.manifest["room_sid"] = ctx.job.room.sid
    if call.earshot is not None:
        call.recording = await asyncio.to_thread(
            call.earshot.create_recording,
            session_key=call.key,
            speaker="mixed",
            channel_speakers=CHANNEL_SPEAKERS,
            content_type="audio/ogg",
            started_at_unix_nano=call.manifest["recording"]["started_at_unix_nano"],
            recording_id=recording_id_for(call.key, "call"),
        )
        call.manifest["recording"]["earshot"] = {
            key: call.recording[key] for key in ("recording_id", "call_id", "state")
        }
    call.write_manifest()

    script_s = len(call.caller_pcm) / (2 * SAMPLE_RATE)
    await asyncio.wait_for(caller.script_done.wait(), timeout=script_s + 30)
    deadline = time.monotonic() + END_WAIT_S
    while time.monotonic() < deadline and (
        session.agent_state in ("thinking", "speaking")
        or time.monotonic() - last_spoke["at"] < QUIET_BEFORE_END_S
    ):
        await asyncio.sleep(0.1)


async def on_session_end(ctx: JobContext) -> None:
    """Runs after session_close, inside the job's shutdown; where a host uploads."""
    call = CALL
    assert call is not None
    try:
        report = ctx.make_session_report()
        if report.audio_recording_path is None or report.audio_recording_started_at is None:
            raise RuntimeError("the session report has no audio recording")
        shutil.copyfile(report.audio_recording_path, call.out / "recording.ogg")
        if report.audio_recording_started_at != call.manifest["recording"]["started_at"]:
            raise RuntimeError("recording_started_at changed between start and report")
        (call.out / "session_report.json").write_text(
            json.dumps(report.to_dict(), indent=2, default=str) + "\n"
        )
        if call.earshot is not None and call.recording is not None:
            uploaded = await asyncio.to_thread(
                call.earshot.upload_recording, call.recording, call.out / "recording.ogg"
            )
            call.manifest["recording"]["earshot"] = {
                key: uploaded[key]
                for key in ("recording_id", "call_id", "state", "sha256", "size", "duration_ms")
            }
    except Exception as error:
        call.errors.append(repr(error))
        raise
    finally:
        call.session_ended.set()


async def run_call(call: Call) -> dict[str, Any]:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    counter = SpanCounter()
    provider.add_span_processor(counter)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    telemetry.set_tracer_provider(provider, metadata=tracer_metadata(call.key))

    server = AgentServer(
        job_executor_type=JobExecutorType.THREAD, host="127.0.0.1", num_idle_processes=0
    )
    server.rtc_session(entrypoint, on_session_end=on_session_end)

    dispatched: list[asyncio.Future[None]] = []

    @server.once("worker_started")
    def _start_job() -> None:
        dispatched.append(asyncio.ensure_future(server.simulate_job(ROOM_NAME, fake_job=True)))

    run_task = asyncio.create_task(server.run(devmode=True, unregistered=True))
    script_s = len(call.caller_pcm) / (2 * SAMPLE_RATE)
    ended = await asyncio.to_thread(call.session_ended.wait, script_s + END_WAIT_S + 90)
    await asyncio.gather(*dispatched)  # surfaces a failed dispatch
    # the job tail (session_end_upload, room_disconnect, job_shutdown, job_entrypoint)
    # follows on_session_end; let it finish before the server stops the job
    deadline = time.monotonic() + JOB_END_WAIT_S
    while server.active_jobs and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    await server.aclose()
    await asyncio.wait_for(run_task, timeout=JOB_END_WAIT_S)
    flushed = await asyncio.to_thread(provider.force_flush, 10_000)
    provider.shutdown()
    if call.earshot is not None:
        call.earshot.close()

    if not ended or call.errors:
        raise RuntimeError(f"call failed: session_ended={ended}, errors={call.errors}")
    if not flushed:
        raise RuntimeError("the tracer provider did not flush")
    assert call.caller is not None and call.caller.started_at is not None
    assert call.speaker is not None
    call.manifest |= {
        "status": "completed",
        "caller_started_at": call.caller.started_at,
        "caller_started_at_unix_nano": _unix_nano(call.caller.started_at),
        "exported_span_count": sum(counter.names.values()),
        "exported_span_names": dict(sorted(counter.names.items())),
        "speaker_segments": [
            {
                "started_at": segment.started_at,
                "played_s": round(segment.played_s, 6),
                "captured_s": round(segment.captured_s, 6),
                "interrupted": segment.interrupted,
                "runs": [list(run) for run in segment.runs],
            }
            for segment in call.speaker.segments
        ],
    }
    call.manifest["recording"]["size"] = (call.out / "recording.ogg").stat().st_size
    call.write_manifest()
    return call.manifest


def _unix_nano(seconds: float) -> str:
    """A ``time.time()`` float as decimal unix nanoseconds, at its microsecond precision."""
    return str(round(seconds * 1e6) * 1000)


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"{path} must be {SAMPLE_RATE} Hz mono PCM16")
        return wav.readframes(wav.getnframes())


def main() -> int:
    global CALL
    parser = argparse.ArgumentParser(description="Run one real LiveKit call with OTLP export.")
    parser.add_argument("--out", type=Path, default=Path(".earshot/livekit_otlp/run"))
    parser.add_argument("--session-key", default=f"livekit-otlp-{uuid.uuid4()}")
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
    cloud = [key for key in ("LIVEKIT_URL", "LIVEKIT_OBSERVABILITY_URL") if os.environ.get(key)]
    if cloud:  # a job with an observability URL would also upload the call to LiveKit Cloud
        print(f"unset {', '.join(cloud)}: this example runs without LiveKit", file=sys.stderr)
        return 2
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=[handler])

    if args.out.exists():
        shutil.rmtree(args.out)
    caller_wav = ensure_caller_wav()
    earshot = None
    if args.upload is not None:
        base_url = args.upload or os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        earshot = EarshotClient(base_url, authorization=authorization_from_environment())
    CALL = Call(args.session_key, args.out, _read_pcm(caller_wav), earshot)
    CALL.manifest["caller_lines"] = json.loads(caller_wav.with_suffix(".json").read_text())["lines"]
    manifest = asyncio.run(run_call(CALL))
    summary = ("session_key", "exported_span_count", "exported_span_names")
    print(
        json.dumps(
            {key: manifest[key] for key in summary}
            | {"earshot": manifest["recording"].get("earshot")}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
