"""Headless real Pipecat run, instrumented with Earshot.

Mirrors the LiveKit headless driver but for the OTHER runtime, to prove the same
portable incident comes out of a genuinely different stack. It builds a real
Pipecat `stt -> llm -> tts` pipeline, feeds a synthesized user utterance as audio
frames, and attaches our adapter to Pipecat's own OpenTelemetry tracing. No
microphone, no transport hardware.

Uses Groq-hosted models plus macOS `say`:

  * user audio  -- macOS `say`, local, no API call at all
  * STT         -- Groq whisper-large-v3-turbo
  * LLM         -- Groq llama-3.1-8b-instant
  * TTS         -- Groq canopylabs/orpheus-v1-english

Set a Groq API key, then:

    echo 'GROQ_API_KEY=gsk_...' >> .env
    set -a && . ./.env && set +a
    python examples/pipecat_headless/drive.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import sys
import tempfile
import time
import wave
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from typing import Any, Protocol

from loguru import logger as framework_logger
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.groq.tts import GroqTTSService
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

import earshot
from earshot.adapters import PipecatAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256, encode_incident_json
from earshot.validation import validate_incident

SR = 24000  # macOS `say` LEI16@24000 -> the mic-side audio we feed in
TTS_SR = 48000  # Groq TTS is fixed at 48 kHz; matching it avoids a resample
UTTERANCE = "What is the capital of France? Answer in one word."
FRAME_BYTES = 960  # 20 ms @ 24 kHz s16le mono
RESPONSE_TIMEOUT_S = 45.0  # wait for observed end of real TTS playout
DRAIN_TIMEOUT_S = 15.0  # bounded wait for a clean pipeline shutdown
RUN_TIMEOUT_S = 90.0  # evidence deadline; late completion can never certify success
SYNTH_TIMEOUT_S = 10.0  # local `say` is a child process and must also be bounded
# Explicit model names keep this reproducible; any Groq-supported model works.
STT_MODEL = "whisper-large-v3-turbo"
LLM_MODEL = "llama-3.1-8b-instant"
TTS_MODEL = "canopylabs/orpheus-v1-english"
TTS_VOICE = "autumn"
# Generated artifacts live under the gitignored .earshot/ tree, matching the
# LiveKit examples. Never write them to the repo root: they are nondeterministic
# and would land in git status and prettier's format check.
OUTPUT_PATH = pathlib.Path(".earshot/pipecat_headless/incident.json")
_FAILED_STAGE_STATUSES = {
    "error",
    "failed",
    "cancelled",
    "canceled",
    "timed_out",
    "timeout",
}


class DiscardOutputTransport(BaseOutputTransport):
    """Drain Pipecat TTS audio while retaining proof that audio was emitted."""

    def __init__(self) -> None:
        super().__init__(
            TransportParams(
                audio_out_enabled=True,
                audio_out_sample_rate=TTS_SR,
                audio_out_end_silence_secs=0,
            )
        )
        self.audio_bytes_written = 0
        self._tts_finished = asyncio.Event()

    @property
    def saw_tts_audio(self) -> bool:
        return self.audio_bytes_written > 0

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        self.audio_bytes_written += len(frame.audio)
        return True

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        await super().push_frame(frame, direction)
        if (
            direction is FrameDirection.DOWNSTREAM
            and isinstance(frame, BotStoppedSpeakingFrame)
            and self.saw_tts_audio
        ):
            self._tts_finished.set()

    async def wait_for_tts_completion(self) -> None:
        """Wait until Pipecat reports that emitted TTS audio finished playout."""

        await self._tts_finished.wait()


class StageTrackingRecorder:
    """Delegate to a recorder while exposing stages that were actually accepted."""

    def __init__(self, recorder: earshot.IncidentRecorder) -> None:
        self._recorder = recorder
        self.observed_stages: set[str] = set()
        self.failed_stages: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._recorder, name)

    def record_operation(self, *args: Any, **kwargs: Any) -> Any:
        operation = self._recorder.record_operation(*args, **kwargs)
        self.observed_stages.add(operation.operation_name)
        if operation.status in _FAILED_STAGE_STATUSES:
            self.failed_stages.add(operation.operation_name)
        return operation

    @contextlib.contextmanager
    def operation(self, operation_name: str, **kwargs: Any) -> Any:
        with self._recorder.operation(operation_name, **kwargs) as context:
            yield context
        self.observed_stages.add(operation_name)


class DriverRuntime(Protocol):
    @property
    def saw_tts_audio(self) -> bool: ...

    async def run(self) -> None: ...

    async def aclose(self) -> None: ...

    async def force_flush(self) -> bool: ...

    async def shutdown(self) -> None: ...


RuntimeFactory = Callable[[str, StageTrackingRecorder], DriverRuntime]
ArtifactWriter = Callable[[pathlib.Path, bytes], None]


def _write_artifact_atomic(output_path: pathlib.Path, payload: bytes) -> None:
    """Durably replace an artifact without exposing a partially written file."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = pathlib.Path(temporary_file.name)
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, output_path)
        temporary_path = None
        directory_fd = os.open(output_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


async def _terminate_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def _wait_until_deadline(task: asyncio.Task[None], timeout: float) -> None:
    """Fail at the observed deadline without waiting for cancellation acknowledgement."""

    if timeout <= 0:
        raise ValueError("run timeout must be positive")
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        raise TimeoutError("Pipecat runtime exceeded its run deadline")
    await task


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """Retrieve late task failures without rendering provider exception payloads."""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def synth_user_pcm(*, timeout: float = SYNTH_TIMEOUT_S) -> bytes:
    """Synthesize the user's utterance locally with macOS `say` -- no API, no cost.

    Returns raw s16le mono PCM at SR, exactly what the pipeline ingests as mic audio.
    """
    if timeout <= 0:
        raise ValueError("synthesis timeout must be positive")
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "user.wav"
        process = await asyncio.create_subprocess_exec(
            "say",
            "-o",
            str(path),
            f"--data-format=LEI16@{SR}",
            UTTERANCE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as error:
            await _terminate_child(process)
            raise TimeoutError(f"local speech synthesis timed out after {timeout:g}s") from error
        except BaseException:
            await _terminate_child(process)
            raise
        if process.returncode != 0:
            raise RuntimeError(f"macOS `say` failed with exit code {process.returncode}")
        with wave.open(str(path)) as handle:
            if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (
                SR,
                1,
                2,
            ):
                raise RuntimeError("`say` did not produce 24 kHz s16le mono audio")
            return handle.readframes(handle.getnframes())


def _pipeline_params() -> PipelineParams:
    # Pin the input rate to our synthesized PCM: the segmented STT wraps its buffer
    # as WAV at the pipeline rate, not the frame rate, so a mismatch garbles audio.
    return PipelineParams(
        enable_metrics=True,
        enable_usage_metrics=True,
        audio_in_sample_rate=SR,
        audio_out_sample_rate=TTS_SR,
    )


class PipecatRuntime:
    """Own one real Pipecat worker, runner, output sink, and trace provider."""

    def __init__(self, api_key: str, recorder: StageTrackingRecorder) -> None:
        self._provider = TracerProvider()
        self._adapter: PipecatAdapter | None = None
        self._routing_handle = None
        self._runner_task: asyncio.Task[None] | None = None
        self._pipeline_started = asyncio.Event()
        self._pipeline_failed = False
        self._pipeline_finished_normally = False
        try:
            otel_trace.set_tracer_provider(self._provider)
            if otel_trace.get_tracer_provider() is not self._provider:
                raise RuntimeError("the headless Pipecat driver requires a fresh one-shot process")
            self._adapter = PipecatAdapter(
                recorder,
                framework_version=version("pipecat-ai"),
            )
            self._routing_handle = self._adapter.attach(self._provider)

            stt = GroqSTTService(
                api_key=api_key,
                settings=GroqSTTService.Settings(model=STT_MODEL),
            )
            llm = GroqLLMService(
                api_key=api_key,
                settings=GroqLLMService.Settings(model=LLM_MODEL),
            )
            tts = GroqTTSService(
                api_key=api_key,
                settings=GroqTTSService.Settings(model=TTS_MODEL, voice=TTS_VOICE),
            )
            context = LLMContext(
                messages=[{"role": "system", "content": "Answer in one short word."}]
            )
            aggregator = LLMContextAggregatorPair(context)
            self._sink = DiscardOutputTransport()
            pipeline = Pipeline(
                [stt, aggregator.user(), llm, tts, aggregator.assistant(), self._sink]
            )
            self._worker = PipelineWorker(
                pipeline,
                params=_pipeline_params(),
                enable_tracing=True,
                enable_turn_tracking=True,
                enable_rtvi=False,
                cancel_on_idle_timeout=False,
                conversation_id="earshot-pipecat",
                observers=[self._adapter.create_observer()],
            )
            self._runner = WorkerRunner(handle_sigint=False)

            @self._worker.event_handler("on_pipeline_started")
            async def on_pipeline_started(worker: object, frame: object) -> None:
                del worker, frame
                self._pipeline_started.set()

            @self._worker.event_handler("on_pipeline_error")
            async def on_pipeline_error(worker: object, frame: object) -> None:
                del worker, frame
                self._pipeline_failed = True

            @self._worker.event_handler("on_pipeline_finished")
            async def on_pipeline_finished(worker: object, frame: object) -> None:
                del worker
                if isinstance(frame, EndFrame):
                    self._pipeline_finished_normally = True
        except BaseException:
            try:
                with contextlib.suppress(Exception):
                    self._provider.shutdown()
            finally:
                if self._adapter is not None:
                    self._adapter.detach()
            raise

    @property
    def saw_tts_audio(self) -> bool:
        return self._sink.saw_tts_audio

    async def _wait_for_completion_or_runner(
        self,
        completion: Awaitable[object],
        *,
        timeout: float,
        description: str,
    ) -> None:
        if self._runner_task is None:
            raise RuntimeError("Pipecat runner was not started")
        completion_task = asyncio.ensure_future(completion)
        try:
            done, _ = await asyncio.wait(
                {completion_task, self._runner_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if completion_task in done:
                await completion_task
                return
            if self._runner_task in done:
                await self._runner_task
                raise RuntimeError(f"Pipecat runner ended before {description}")
            raise TimeoutError(f"timed out waiting for {description}")
        finally:
            if not completion_task.done():
                completion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await completion_task

    async def run(self) -> None:
        if self._routing_handle is None:
            raise RuntimeError("Pipecat span routing was not initialized")
        with self._routing_handle.session_scope():
            await self._run_scoped()

    async def _run_scoped(self) -> None:
        pcm = await synth_user_pcm()
        audio_frames = [
            InputAudioRawFrame(
                audio=pcm[index : index + FRAME_BYTES],
                sample_rate=SR,
                num_channels=1,
            )
            for index in range(0, len(pcm), FRAME_BYTES)
        ]

        await self._runner.add_workers(self._worker)
        self._runner_task = asyncio.create_task(self._runner.run())
        await self._wait_for_completion_or_runner(
            self._pipeline_started.wait(),
            timeout=DRAIN_TIMEOUT_S,
            description="pipeline start",
        )

        await self._worker.queue_frames([UserStartedSpeakingFrame(), VADUserStartedSpeakingFrame()])
        for frame in audio_frames:
            await self._worker.queue_frame(frame)
            await asyncio.sleep(0.02)
        await self._worker.queue_frames([VADUserStoppedSpeakingFrame(), UserStoppedSpeakingFrame()])
        await self._wait_for_completion_or_runner(
            self._sink.wait_for_tts_completion(),
            timeout=RESPONSE_TIMEOUT_S,
            description="TTS completion",
        )
        await self._worker.stop_when_done()
        await asyncio.wait_for(
            asyncio.shield(self._runner_task),
            timeout=DRAIN_TIMEOUT_S,
        )
        if self._pipeline_failed:
            raise RuntimeError("Pipecat emitted a pipeline error")
        if not self._pipeline_finished_normally:
            raise RuntimeError("Pipecat pipeline did not finish with EndFrame")

    async def aclose(self) -> None:
        if self._runner_task is None:
            return
        if self._runner_task.done():
            if self._runner_task.cancelled():
                return
            error = self._runner_task.exception()
            if error is not None:
                raise RuntimeError("Pipecat runner failed during cleanup") from error
            return
        await self._runner.cancel("Earshot driver finalization")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._runner_task),
                timeout=DRAIN_TIMEOUT_S,
            )
        except TimeoutError as error:
            self._runner_task.cancel()
            try:
                await asyncio.wait_for(self._runner_task, timeout=5)
            except asyncio.CancelledError:
                pass
            except TimeoutError as cancel_error:
                raise RuntimeError("Pipecat runner did not cancel") from cancel_error
            raise RuntimeError("Pipecat runner did not close cleanly") from error

    async def force_flush(self) -> bool:
        return await asyncio.to_thread(self._provider.force_flush, timeout_millis=5_000)

    async def shutdown(self) -> None:
        try:
            await asyncio.to_thread(self._provider.shutdown)
        finally:
            if self._adapter is not None:
                self._adapter.detach()


def create_pipecat_runtime(
    api_key: str,
    recorder: StageTrackingRecorder,
) -> DriverRuntime:
    return PipecatRuntime(api_key, recorder)


def _print_summary(bundle: earshot.IncidentBundle, output_path: pathlib.Path) -> None:
    report = validate_incident(bundle)
    profile = bundle.profile
    print("\n" + "=" * 72)
    print("REAL PIPECAT INCIDENT (headless, Groq services)")
    print("=" * 72)
    print(f"  lifecycle status          : {profile.session.status}")
    print(f"  valid against v1 contract : {report.ok}  (errors={len(report.errors)})")
    for issue in report.errors[:8]:
        print(f"     - {issue.code} at {'.'.join(str(part) for part in issue.path)}")
    operation_names = sorted(operation.operation_name for operation in profile.operations)
    print(
        f"  operations ({len(operation_names):2})           : "
        f"{', '.join(operation_names) or '(none)'}"
    )
    print(f"  events                    : {len(profile.events)}")
    print(f"  quality_samples           : {len(profile.quality_samples)}")
    measurements = sorted(
        {
            measurement.name
            for sample in profile.quality_samples
            for measurement in sample.measurements
        }
    )
    print(f"  provider measurements     : {', '.join(measurements) or '(none)'}")

    digest = analysis_input_sha256(bundle)
    analysis = analyze_incident(
        bundle,
        input_sha256=digest,
        generated_at_unix_nano=time.time_ns(),
    )
    for turn in analysis.projections.turns:
        metrics = turn.metrics

        def show(metric: object) -> str:
            return (
                f"{metric.value:.0f}{metric.unit}"
                if getattr(metric, "value", None) is not None
                else str(getattr(metric, "availability", "unknown"))
            )

        print(f"  turn {turn.turn_id[:20]:20}")
        print(f"     first_token      : {show(metrics.first_token_latency)}")
        print(f"     generated (TTS)  : {show(metrics.generated_response_latency)}")
        print(f"     response         : {show(metrics.response_latency)}")
    if analysis.projections.limitations:
        print(f"  limitations               : {', '.join(analysis.projections.limitations)}")
    print(f"  full artifact             : {output_path}")
    print("=" * 72)


async def run_driver(
    api_key: str,
    *,
    runtime_factory: RuntimeFactory = create_pipecat_runtime,
    output_path: pathlib.Path = OUTPUT_PATH,
    run_timeout: float = RUN_TIMEOUT_S,
    sdk_shutdown: Callable[[], bool] = earshot.shutdown,
    artifact_writer: ArtifactWriter = _write_artifact_atomic,
) -> int:
    """Run one headless turn and always finalize initialized Earshot state."""

    recorder: earshot.IncidentRecorder | None = None
    tracking_recorder: StageTrackingRecorder | None = None
    runtime: DriverRuntime | None = None
    configured = False
    run_completed = False
    lifecycle_status = "failed"
    bundle = None
    artifact_written = False
    sdk_shutdown_ok = True
    pending_cancellation = False
    run_task: asyncio.Task[None] | None = None
    # Pipecat's DEBUG messages contain transcripts, prompts, and generated text.
    # This standalone evidence harness is metadata-only, so framework logging is
    # disabled before any provider service is constructed.
    framework_logger.remove()
    try:
        earshot.configure()
        configured = True
        recorder = earshot.session(session_id="headless-pipecat")
        tracking_recorder = StageTrackingRecorder(recorder)
        runtime = runtime_factory(api_key, tracking_recorder)
        run_task = asyncio.create_task(runtime.run())
        run_task.add_done_callback(_consume_task_result)
        await _wait_until_deadline(run_task, run_timeout)
        run_completed = True
    except asyncio.CancelledError:
        pending_cancellation = True
        print("[driver] pipeline cancelled", file=sys.stderr)
    except TimeoutError:
        lifecycle_status = "timed_out"
        print("[driver] pipeline timed out", file=sys.stderr)
    except Exception:
        print("[driver] pipeline failed", file=sys.stderr)
    finally:
        runtime_clean = runtime is not None
        if runtime is not None:
            try:
                await runtime.aclose()
            except asyncio.CancelledError:
                pending_cancellation = True
                runtime_clean = False
                print("[driver] pipeline close cancelled", file=sys.stderr)
            except Exception:
                runtime_clean = False
                print("[driver] pipeline close failed", file=sys.stderr)
            try:
                flushed = await runtime.force_flush()
            except asyncio.CancelledError:
                pending_cancellation = True
                runtime_clean = False
                print("[driver] trace-provider flush cancelled", file=sys.stderr)
            except Exception:
                runtime_clean = False
                print("[driver] trace-provider flush failed", file=sys.stderr)
            else:
                if not flushed:
                    runtime_clean = False
                    print("[driver] trace-provider flush timed out", file=sys.stderr)
            try:
                await runtime.shutdown()
            except asyncio.CancelledError:
                pending_cancellation = True
                runtime_clean = False
                print("[driver] pipeline shutdown cancelled", file=sys.stderr)
            except Exception:
                runtime_clean = False
                print("[driver] pipeline shutdown failed", file=sys.stderr)

            observed_stages = (
                tracking_recorder.observed_stages if tracking_recorder is not None else set()
            )
            missing_stages = {"stt", "llm", "tts"}.difference(observed_stages)
            failed_stages = (
                {"stt", "llm", "tts"}.intersection(tracking_recorder.failed_stages)
                if tracking_recorder is not None
                else set()
            )
            try:
                saw_tts_audio = runtime.saw_tts_audio
            except asyncio.CancelledError:
                pending_cancellation = True
                runtime_clean = False
                saw_tts_audio = False
                print("[driver] TTS evidence check cancelled", file=sys.stderr)
            except Exception:
                runtime_clean = False
                saw_tts_audio = False
                print("[driver] TTS evidence check failed", file=sys.stderr)
            if missing_stages:
                print(
                    f"[driver] missing expected stages: {', '.join(sorted(missing_stages))}",
                    file=sys.stderr,
                )
            if failed_stages:
                print(
                    f"[driver] failed expected stages: {', '.join(sorted(failed_stages))}",
                    file=sys.stderr,
                )
            if not saw_tts_audio:
                print("[driver] pipeline produced no TTS audio", file=sys.stderr)
            if (
                run_completed
                and runtime_clean
                and not missing_stages
                and not failed_stages
                and saw_tts_audio
            ):
                lifecycle_status = "completed"

        if configured:
            try:
                sdk_shutdown_ok = sdk_shutdown()
            except asyncio.CancelledError:
                pending_cancellation = True
                sdk_shutdown_ok = False
                print("[driver] Earshot shutdown cancelled", file=sys.stderr)
            except Exception:
                sdk_shutdown_ok = False
                print("[driver] Earshot shutdown failed", file=sys.stderr)
            else:
                if not sdk_shutdown_ok:
                    print("[driver] Earshot shutdown timed out", file=sys.stderr)
            if not sdk_shutdown_ok:
                lifecycle_status = "failed"

        if recorder is not None:
            try:
                # This driver configures no exporter, so the process-level SDK
                # can shut down before sealing the local artifact. That lets a
                # shutdown failure participate in the recorded lifecycle status.
                bundle = recorder.close(lifecycle_status)
                artifact_writer(output_path, encode_incident_json(bundle, indent=2))
                artifact_written = True
            except asyncio.CancelledError:
                pending_cancellation = True
                print("[driver] incident finalization cancelled", file=sys.stderr)
            except Exception:
                print("[driver] incident finalization failed", file=sys.stderr)

        if run_task is not None and not run_task.done():
            run_task.cancel()

    if pending_cancellation:
        raise asyncio.CancelledError from None

    if bundle is None or not artifact_written:
        return 1
    report = validate_incident(bundle)
    summary_ok = True
    try:
        _print_summary(bundle, output_path)
    except asyncio.CancelledError:
        print("[driver] incident analysis cancelled", file=sys.stderr)
        raise asyncio.CancelledError from None
    except Exception:
        summary_ok = False
        print("[driver] incident analysis failed", file=sys.stderr)
    succeeded = (
        lifecycle_status == "completed"
        and artifact_written
        and report.ok
        and summary_ok
        and sdk_shutdown_ok
        and bundle.profile.session.status == "completed"
    )
    return 0 if succeeded else 1


async def main() -> int:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        print(
            "Set GROQ_API_KEY before running the Pipecat example.\n"
            "For example: export GROQ_API_KEY='gsk_...'\n",
            file=sys.stderr,
        )
        return 2
    return await run_driver(key)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
