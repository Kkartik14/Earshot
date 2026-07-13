"""A real LiveKit Agents console voice agent, instrumented with Earshot.

This is a genuine mic -> STT -> LLM -> TTS -> speaker call. Earshot attaches to
LiveKit's *own* OpenTelemetry tracing + metrics (it does not re-instrument), and
on hangup it writes a real incident bundle and prints the derived analysis.

Run it:

    export OPENAI_API_KEY=sk-...
    python examples/livekit_console/agent.py download-files   # one-time: VAD + turn model
    python examples/livekit_console/agent.py console           # talk to it, then Ctrl-C

Console mode is fully local (terminal mic/speaker); it needs no LiveKit server
credentials. The only external dependency is the OpenAI key (STT+LLM+TTS).

After you hang up, look at ./.earshot/livekit_console/console_incident.json and
the printed summary.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time
from dataclasses import dataclass

from _runtime import LIVEKIT_AGENTS_VERSION
from livekit.agents import (
    Agent,
    AgentSession,
    CloseEvent,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    telemetry,
)
from livekit.plugins import openai, silero
from opentelemetry.sdk.trace import TracerProvider

import earshot
from earshot.adapters import LiveKitAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, encode_incident_json

try:  # The turn detector is optional; VAD endpointing still works without it.
    from livekit.plugins.turn_detector.multilingual import MultilingualModel

    _TURN_DETECTOR = MultilingualModel
except Exception:  # pragma: no cover - environment dependent
    _TURN_DETECTOR = None

OUTPUT_PATH = pathlib.Path(".earshot/livekit_console/console_incident.json")

# One process-wide OpenTelemetry provider. Earshot installs a *span processor* on
# it; it never becomes the trace root. LiveKit publishes its session spans here.
_provider = TracerProvider()
telemetry.set_tracer_provider(_provider)
earshot.configure()  # metadata-only by default; no payload capture, no endpoint.


@dataclass
class _LifecycleState:
    status: str = "starting"


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


def _write_incident(recorder: earshot.IncidentRecorder, status: str) -> None:
    """Finalize the recorder into a real incident, then analyze + summarize it."""
    try:
        bundle = recorder.close(status)
    except Exception as error:
        print(f"\n[earshot] could not build incident: {error!r}", file=sys.stderr)
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(encode_incident_json(bundle, indent=2))
    digest = analysis_input_sha256(bundle)
    analysis = analyze_incident(bundle, input_sha256=digest, generated_at_unix_nano=time.time_ns())
    profile = bundle.profile
    projections = analysis.projections

    print("\n" + "=" * 68)
    print("EARSHOT INCIDENT (real LiveKit call)")
    print("=" * 68)
    print(f"  session_id     : {profile.session.session_id}")
    print(f"  session_status : {profile.session.status}")
    print(f"  operations     : {len(profile.operations)}")
    print(f"  events         : {len(profile.events)}")
    print(f"  quality_samples: {len(profile.quality_samples)}")
    print(f"  turns          : {len(projections.turns)}")
    op_names = sorted({op.operation_name for op in profile.operations})
    print(f"  stages seen    : {', '.join(op_names) or '(none)'}")
    for turn in projections.turns:
        rl = turn.metrics.response_latency
        ft = turn.metrics.first_token_latency
        value = f"{rl.value:.0f} {rl.unit}" if rl.value is not None else rl.availability
        first = f"{ft.value:.0f} {ft.unit}" if ft.value is not None else ft.availability
        print(f"  turn {turn.turn_id[:16]:16}  response={value:<14} first_token={first}")
    if analysis.diagnoses:
        print("  diagnoses      : " + ", ".join(d.code for d in analysis.diagnoses))
    if projections.limitations:
        print("  limitations    : " + ", ".join(projections.limitations))
    print("=" * 68)
    print(f"  full artifact written to: {OUTPUT_PATH.resolve()}")
    print("=" * 68 + "\n")


async def _finalize_incident(
    session: AgentSession,
    recorder: earshot.IncidentRecorder,
    lifecycle: _LifecycleState,
) -> None:
    """Close LiveKit and flush its spans before Earshot snapshots the incident."""
    if lifecycle.status == "starting":
        lifecycle.status = "failed"
    try:
        # LiveKit also closes the session during job shutdown. Its close lock makes
        # this safe when both callbacks start together and lets us wait for all
        # final session spans and metric callbacks before closing the recorder.
        await session.aclose()
    except Exception as error:
        lifecycle.status = "failed"
        print(f"[earshot] LiveKit session close failed: {error!r}", file=sys.stderr)

    try:
        flushed = await asyncio.to_thread(_provider.force_flush, timeout_millis=5_000)
    except Exception as error:
        lifecycle.status = "failed"
        print(f"[earshot] trace-provider flush failed: {error!r}", file=sys.stderr)
    else:
        if not flushed:
            lifecycle.status = "failed"
            print("[earshot] trace-provider flush timed out", file=sys.stderr)

    try:
        _write_incident(recorder, lifecycle.status)
    finally:
        earshot.shutdown()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    session_id = getattr(getattr(ctx, "room", None), "name", None) or "console-session"
    recorder = earshot.session(session_id=session_id)
    adapter = LiveKitAdapter(recorder, framework_version=LIVEKIT_AGENTS_VERSION)
    # Additive: attach to the existing provider + session; do not replace anything.
    adapter.attach_span_processor(_provider)

    turn_detection = _TURN_DETECTOR() if _TURN_DETECTOR is not None else None
    session = AgentSession(
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
        # Explicit model names keep this reproducible; use any LiveKit-supported
        # STT, LLM, and TTS models that your deployment has enabled.
        stt=openai.STT(model="whisper-1"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(model="tts-1", voice="alloy"),
        turn_detection=turn_detection,
    )
    adapter.attach_session_listeners(session)
    lifecycle = _LifecycleState()

    @session.on("close")
    def on_session_close(event: CloseEvent) -> None:
        reason = getattr(event.reason, "value", event.reason)
        if event.error is not None or reason == "error":
            lifecycle.status = "failed"

    # Emit the incident after LiveKit has ended its session spans. This callback
    # must be async because JobContext awaits every shutdown callback.
    async def finalize(reason: str) -> None:
        if reason == "job crashed":
            lifecycle.status = "failed"
        await _finalize_incident(session, recorder, lifecycle)

    ctx.add_shutdown_callback(finalize)

    await session.start(
        agent=Agent(
            instructions=(
                "You are a friendly voice assistant demoing Earshot. "
                "Keep answers to one or two short sentences."
            )
        ),
        room=ctx.room,
    )
    await session.generate_reply(instructions="Greet the user in one short sentence.")
    if lifecycle.status != "failed":
        lifecycle.status = "completed"


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "Set OPENAI_API_KEY first (used for STT + LLM + TTS):\n"
            "    export OPENAI_API_KEY=sk-...\n",
            file=sys.stderr,
        )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
