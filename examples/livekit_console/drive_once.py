"""Headless text-turn driver — exercises real LLM/TTS + LiveKit + Earshot.

Unlike ``agent.py console`` (which needs a live mic), this runs a single real
text turn through LiveKit's eval harness. OpenAI gpt-4o-mini and tts-1 run,
LiveKit emits their real spans and metrics, and Earshot builds, validates, and
analyzes the incident. Text input does not exercise STT, VAD, or endpointing;
use ``drive_audio.py`` for that pipeline without a microphone.

    set -a && . ./.env && set +a
    python examples/livekit_console/drive_once.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

from _runtime import LIVEKIT_AGENTS_VERSION, NullAudioOutput
from livekit.agents import Agent, AgentSession, telemetry
from livekit.plugins import openai
from opentelemetry.sdk.trace import TracerProvider

import earshot
from earshot.adapters import LiveKitAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, encode_incident_json
from earshot.validation import validate_incident

OUTPUT_PATH = pathlib.Path(".earshot/livekit_console/text_incident.json")


async def main() -> int:
    earshot.configure()
    recorder = earshot.session(session_id="headless-eval")
    provider: TracerProvider | None = None
    routing_handle = None
    utterance = "What is the capital of France?"
    session: AgentSession | None = None
    sink = NullAudioOutput()
    lifecycle_status = "failed"
    reply = ""
    bundle = None
    artifact_written = False

    try:
        provider = TracerProvider()
        telemetry.set_tracer_provider(provider)
        adapter = LiveKitAdapter(recorder, framework_version=LIVEKIT_AGENTS_VERSION)
        routing_handle = adapter.attach_span_processor(provider)
        session = AgentSession(
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=openai.TTS(model="tts-1", voice="alloy"),
        )
        adapter.attach_session_listeners(session)

        with routing_handle.session_scope():
            agent = Agent(
                instructions="You are a friendly assistant. Answer in one short sentence."
            )
            await session.start(agent=agent)
            # A roomless session only runs TTS when an audio output is attached.
            # This sink discards samples but completes LiveKit's playout protocol.
            session.output.audio = sink
            result = await session.run(user_input=utterance, input_modality="text")
            for event in reversed(result.events):
                item = getattr(event, "item", None)
                text_content = getattr(item, "text_content", None)
                if text_content:
                    reply = text_content
                    break
            if not reply:
                raise RuntimeError("LiveKit completed the turn without an assistant reply")
            if not sink.saw_audio:
                raise RuntimeError("LiveKit completed the turn without sending TTS audio")
            await asyncio.wait_for(session.wait_for_idle(), timeout=15)
        lifecycle_status = "completed"
    except TimeoutError as error:
        lifecycle_status = "timed_out"
        print(f"[driver] text pipeline timed out: {error!r}", file=sys.stderr)
    except Exception as error:
        print(f"[driver] text pipeline failed: {error!r}", file=sys.stderr)
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

        if routing_handle is not None:
            routing_handle.close()

        try:
            bundle = recorder.close(lifecycle_status)
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_bytes(encode_incident_json(bundle, indent=2))
            artifact_written = True
        except Exception as error:
            print(f"[driver] incident finalization failed: {error!r}", file=sys.stderr)
        finally:
            earshot.shutdown()

    if bundle is None:
        return 1

    if lifecycle_status == "completed":
        print("[driver] ran 1 text turn (real LLM+TTS; no STT)")
    else:
        print(f"[driver] text turn did not complete ({lifecycle_status})")
    print(f"[driver] USER : {utterance}")
    print(f"[driver] AGENT: {reply or '(no reply)'}")

    report = validate_incident(bundle)
    profile = bundle.profile
    print("\n" + "=" * 70)
    print("REAL INCIDENT FROM LIVEKIT + OPENAI")
    print("=" * 70)
    print(f"  lifecycle status          : {profile.session.status}")
    print(f"  valid against v1 contract : {report.ok}  (errors={len(report.errors)})")
    if not report.ok:
        for issue in report.errors[:8]:
            print(f"     - {issue.code} at {'.'.join(str(p) for p in issue.path)}")

    print(f"  operations                : {len(profile.operations)}")
    print(f"  events                    : {len(profile.events)}")
    print(f"  quality_samples           : {len(profile.quality_samples)}")
    op_names = sorted({op.operation_name for op in profile.operations})
    print(f"  real stages captured      : {', '.join(op_names) or '(none)'}")
    expected_stages = {"llm", "tts"}
    missing_stages = expected_stages.difference(op_names)
    if missing_stages:
        print(f"  missing expected stages   : {', '.join(sorted(missing_stages))}")

    digest = analysis_input_sha256(bundle)
    analysis = analyze_incident(bundle, input_sha256=digest, generated_at_unix_nano=time.time_ns())
    for turn in analysis.projections.turns:
        rl = turn.metrics.response_latency
        ft = turn.metrics.first_token_latency
        rlv = f"{rl.value:.0f}{rl.unit}" if rl.value is not None else rl.availability
        ftv = f"{ft.value:.0f}{ft.unit}" if ft.value is not None else ft.availability
        print(f"  turn {turn.turn_id[:14]:14}  response={rlv:<12} first_token={ftv}")
    if analysis.projections.limitations:
        print(f"  limitations               : {', '.join(analysis.projections.limitations)}")

    print(f"  full artifact             : {OUTPUT_PATH}")
    print("=" * 70)
    succeeded = (
        lifecycle_status == "completed" and artifact_written and report.ok and not missing_stages
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
