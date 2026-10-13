"""Run and verify the live Pipecat provider Cartesian product.

Each cell runs in a fresh process because OpenTelemetry's global tracer provider
is intentionally one-shot. The runner never prints credentials or provider
payloads. Provider combinations without configured credentials are enumerated as
unavailable instead of being silently omitted.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

import earshot

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "pipecat_headless" / "drive.py"
ARTIFACT_DIR = ROOT / ".earshot" / "pipecat-matrix"
REPORT_PATH = ARTIFACT_DIR / "report.json"


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def _load_drive() -> Any:
    sys.path.insert(0, str(EXAMPLE.parent))
    spec = importlib.util.spec_from_file_location("earshot_pipecat_headless_drive", EXAMPLE)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Pipecat headless driver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class Cell:
    stt: str
    llm: str
    tts: str

    @property
    def name(self) -> str:
        return f"{self.stt}__{self.llm}__{self.tts}"


STT_PROVIDERS = ("deepgram", "cartesia", "sarvam", "elevenlabs", "assemblyai", "soniox")
LLM_PROVIDERS = ("groq", "openai")
TTS_PROVIDERS = ("cartesia", "elevenlabs")

CELLS = tuple(
    Cell(stt, llm, tts) for stt in STT_PROVIDERS for llm in LLM_PROVIDERS for tts in TTS_PROVIDERS
)

KEYS = {
    "deepgram": "DEEPGRAM_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "sarvam": "SARVAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "soniox": "SONIOX_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _key(provider: str) -> str:
    value = os.environ.get(KEYS[provider], "")
    if not value:
        raise RuntimeError(f"{KEYS[provider]} is missing")
    return value


def _missing_credentials(cell: Cell) -> tuple[str, ...]:
    names = [KEYS[cell.stt], KEYS[cell.llm], KEYS[cell.tts]]
    if cell.tts == "cartesia":
        names.append("CARTESIA_VOICE_ID")
    if cell.tts == "elevenlabs":
        names.append("ELEVENLABS_VOICE_ID")
    return tuple(dict.fromkeys(name for name in names if not os.environ.get(name)))


def _http_probe(
    request: urllib.request.Request,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Return status-only evidence; never retain or print provider response bodies."""

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            return {"status_code": response.status}
    except urllib.error.HTTPError as error:
        error.close()
        return {"status_code": error.code}
    except (TimeoutError, OSError, urllib.error.URLError) as error:
        return {"error_type": type(error).__name__}


def _provider_probes() -> dict[str, dict[str, Any]]:
    """Probe the two provider boundaries that failed in the live matrix."""

    results: dict[str, dict[str, Any]] = {}
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        body = json.dumps(
            {
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": "Reply with one word."}],
                "max_tokens": 1,
            }
        ).encode()
        results["openai"] = _http_probe(
            urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        )

    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    elevenlabs_voice = os.environ.get("ELEVENLABS_VOICE_ID")
    if elevenlabs_key and elevenlabs_voice:
        body = json.dumps(
            {
                "text": "ok",
                "model_id": "eleven_flash_v2_5",
                "output_format": "pcm_48000",
            }
        ).encode()
        results["elevenlabs"] = _http_probe(
            urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{elevenlabs_voice}/stream",
                data=body,
                headers={
                    "xi-api-key": elevenlabs_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        )
    return results


def _failure_hint(cell: Cell, probes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Choose the first known failed boundary in pipeline order."""

    for provider, stage in ((cell.llm, "llm"), (cell.tts, "tts")):
        probe = probes.get(provider, {})
        status_code = probe.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            return {
                "provider": provider,
                "stage": stage,
                "error_type": f"provider_http_{status_code}",
                "status_code": status_code,
                "source": "direct_provider_probe",
            }
    return {}


def _stt(drive: Any, provider: str) -> Any:
    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        return DeepgramSTTService(
            api_key=_key(provider),
            sample_rate=drive.SR,
            settings=DeepgramSTTService.Settings(
                model="nova-3",
                language="en-US",
                interim_results=True,
                endpointing=300,
                punctuate=True,
                smart_format=True,
            ),
        )
    if provider == "cartesia":
        from pipecat.services.cartesia.stt import CartesiaSTTService

        return CartesiaSTTService(
            api_key=_key(provider),
            encoding="pcm_s16le",
            sample_rate=drive.SR,
            settings=CartesiaSTTService.Settings(model="ink-2", language="en"),
        )
    if provider == "sarvam":
        from pipecat.services.sarvam.stt import SarvamSTTService

        return SarvamSTTService(
            api_key=_key(provider),
            mode="transcribe",
            sample_rate=16000,
            input_audio_codec="wav",
            settings=SarvamSTTService.Settings(model="saaras:v3"),
        )
    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.stt import (
            CommitStrategy,
            ElevenLabsRealtimeSTTService,
        )

        return ElevenLabsRealtimeSTTService(
            api_key=_key(provider),
            commit_strategy=CommitStrategy.MANUAL,
            sample_rate=drive.SR,
            settings=ElevenLabsRealtimeSTTService.Settings(model="scribe_v2_realtime"),
        )
    if provider == "assemblyai":
        from pipecat.services.assemblyai.stt import AssemblyAISTTService

        return AssemblyAISTTService(
            api_key=_key(provider),
            sample_rate=16000,
            encoding="pcm_s16le",
            settings=AssemblyAISTTService.Settings(model="u3-rt-pro", language="en"),
        )
    if provider == "soniox":
        from pipecat.services.soniox.stt import SonioxSTTService

        return SonioxSTTService(
            api_key=_key(provider),
            sample_rate=16000,
            audio_format="pcm_s16le",
            settings=SonioxSTTService.Settings(model="stt-rt-v5"),
        )
    raise ValueError(f"unsupported STT provider: {provider}")


def _llm(drive: Any, provider: str) -> Any:
    if provider == "groq":
        from pipecat.services.groq.llm import GroqLLMService

        return GroqLLMService(
            api_key=_key(provider),
            settings=GroqLLMService.Settings(model="openai/gpt-oss-20b"),
        )
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=_key(provider),
            settings=OpenAILLMService.Settings(model="gpt-4.1-mini"),
        )
    raise ValueError(f"unsupported LLM provider: {provider}")


def _tts(drive: Any, provider: str) -> Any:
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        voice = os.environ.get("CARTESIA_VOICE_ID")
        if not voice:
            raise RuntimeError("CARTESIA_VOICE_ID is missing")
        return CartesiaTTSService(
            api_key=_key(provider),
            sample_rate=drive.TTS_SR,
            cartesia_version="2026-08-14",
            settings=CartesiaTTSService.Settings(model="sonic-3.6", voice=voice),
        )
    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        voice = os.environ.get("ELEVENLABS_VOICE_ID")
        if not voice:
            raise RuntimeError("ELEVENLABS_VOICE_ID is missing")
        return ElevenLabsTTSService(
            api_key=_key(provider),
            sample_rate=drive.TTS_SR,
            settings=ElevenLabsTTSService.Settings(model="eleven_flash_v2_5", voice=voice),
        )
    raise ValueError(f"unsupported TTS provider: {provider}")


class MatrixRuntime:
    def __init__(
        self,
        drive: Any,
        cell: Cell,
        recorder: Any,
        failure_hint: Mapping[str, Any] | None = None,
    ) -> None:
        self._drive = drive
        self._cell = cell
        self._recorder = recorder
        self._failure_hint = dict(failure_hint or {})
        self._failure_recorded = False
        self._provider = TracerProvider()
        self._adapter = None
        self._routing_handle = None
        self._runner_task: asyncio.Task[None] | None = None
        self._pipeline_started = asyncio.Event()
        self._pipeline_failed = False
        self._pipeline_finished_normally = False
        self._sink = None
        try:
            otel_trace.set_tracer_provider(self._provider)
            if otel_trace.get_tracer_provider() is not self._provider:
                raise RuntimeError("matrix cell requires a fresh one-shot process")
            from earshot.adapters import PipecatAdapter

            self._adapter = PipecatAdapter(recorder, framework_version=version("pipecat-ai"))
            self._routing_handle = self._adapter.attach(self._provider)
            stt = _stt(drive, cell.stt)
            llm = _llm(drive, cell.llm)
            tts = _tts(drive, cell.tts)
            from pipecat.pipeline.pipeline import Pipeline
            from pipecat.pipeline.worker import PipelineWorker
            from pipecat.processors.aggregators.llm_context import LLMContext
            from pipecat.processors.aggregators.llm_response_universal import (
                LLMContextAggregatorPair,
            )
            from pipecat.workers.runner import WorkerRunner

            context = LLMContext(
                messages=[{"role": "system", "content": "Answer in one short word."}]
            )
            aggregator = LLMContextAggregatorPair(context)
            self._sink = drive.DiscardOutputTransport()
            pipeline = Pipeline(
                [stt, aggregator.user(), llm, tts, aggregator.assistant(), self._sink]
            )
            self._worker = PipelineWorker(
                pipeline,
                params=drive._pipeline_params(),
                enable_tracing=True,
                enable_turn_tracking=True,
                enable_rtvi=False,
                cancel_on_idle_timeout=False,
                conversation_id=f"earshot-pipecat-{cell.name}",
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
                if isinstance(frame, drive.EndFrame):
                    self._pipeline_finished_normally = True
        except BaseException:
            with contextlib.suppress(Exception):
                self._provider.shutdown()
            if self._adapter is not None:
                self._adapter.detach()
            raise

    def _record_failure(self) -> None:
        """Retain status-only provider-boundary evidence when a stage never closes."""

        if self._failure_recorded:
            return
        self._failure_recorded = True
        hint = self._failure_hint
        provider = str(hint.get("provider") or self._cell.llm)
        stage = str(hint.get("stage") or "runtime")
        error_type = str(hint.get("error_type") or "provider_runtime_failure")
        attributes: dict[str, Any] = {
            "error.type": error_type,
            "gen_ai.provider.name": provider,
            "gen_ai.operation.name": stage,
            "earshot.framework.operation.name": "provider_failure",
        }
        now = earshot.TimePoint(
            monotonic_time_nano=str(time.monotonic_ns()),
            clock_domain_id=self._recorder.clock_domain_id,
        )
        try:
            self._recorder.record_operation(
                operation_id=f"matrix-provider-failure-{self._cell.name}",
                operation_name="provider_failure",
                status="error",
                started_at=now,
                ended_at=now,
                attributes=attributes,
                error=earshot.ErrorRecord(
                    code=error_type,
                    category="provider",
                    message=None,
                    capture_class="metadata",
                    attributes=attributes,
                ),
                capture_class="metadata",
            )
        except Exception:
            # The original runtime failure must remain the primary result even if
            # a best-effort diagnostic cannot be admitted by the recorder.
            self._failure_recorded = False

    @property
    def saw_tts_audio(self) -> bool:
        return bool(self._sink and self._sink.saw_tts_audio)

    async def _wait_for(self, awaitable: Any, *, timeout: float, description: str) -> None:
        if self._runner_task is None:
            raise RuntimeError("Pipecat runner was not started")
        target = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {target, self._runner_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if target in done:
                await target
                return
            if self._runner_task in done:
                await self._runner_task
                raise RuntimeError(f"runner ended before {description}")
            raise TimeoutError(f"timed out waiting for {description}")
        finally:
            if not target.done():
                target.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await target

    async def run(self) -> None:
        if self._routing_handle is None:
            raise RuntimeError("Pipecat span routing was not initialized")
        try:
            with self._routing_handle.session_scope():
                pcm = await self._drive.synth_user_pcm()
                input_sample_rate = self._drive.SR
                frame_bytes = self._drive.FRAME_BYTES
                if self._cell.stt in {"sarvam", "assemblyai", "soniox"}:
                    pcm, _ = audioop.ratecv(pcm, 2, 1, self._drive.SR, 16000, None)
                    input_sample_rate = 16000
                    frame_bytes = int(input_sample_rate * 0.02 * 2)
                frames = [
                    self._drive.InputAudioRawFrame(
                        audio=pcm[index : index + frame_bytes],
                        sample_rate=input_sample_rate,
                        num_channels=1,
                    )
                    for index in range(0, len(pcm), frame_bytes)
                ]
                await self._runner.add_workers(self._worker)
                self._runner_task = asyncio.create_task(self._runner.run())
                await self._wait_for(
                    self._pipeline_started.wait(),
                    timeout=self._drive.DRAIN_TIMEOUT_S,
                    description="pipeline start",
                )
                await self._worker.queue_frames(
                    [
                        self._drive.UserStartedSpeakingFrame(),
                        self._drive.VADUserStartedSpeakingFrame(),
                    ]
                )
                for frame in frames:
                    await self._worker.queue_frame(frame)
                    await asyncio.sleep(0.02)
                await self._worker.queue_frames(
                    [
                        self._drive.VADUserStoppedSpeakingFrame(),
                        self._drive.UserStoppedSpeakingFrame(),
                    ]
                )
                await self._wait_for(
                    self._sink.wait_for_tts_completion(),
                    timeout=self._drive.RESPONSE_TIMEOUT_S,
                    description="TTS completion",
                )
                await self._worker.stop_when_done()
                await asyncio.wait_for(
                    asyncio.shield(self._runner_task), timeout=self._drive.DRAIN_TIMEOUT_S
                )
                if self._pipeline_failed:
                    raise RuntimeError("Pipecat emitted a pipeline error")
                if not self._pipeline_finished_normally:
                    raise RuntimeError("Pipecat pipeline did not finish with EndFrame")
        except BaseException:
            self._record_failure()
            raise

    async def aclose(self) -> None:
        if self._runner_task is None or self._runner_task.done():
            return
        await self._runner.cancel("Earshot matrix finalization")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._runner_task), timeout=self._drive.DRAIN_TIMEOUT_S
            )
        finally:
            if not self._runner_task.done():
                self._runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._runner_task

    async def force_flush(self) -> bool:
        return await asyncio.to_thread(self._provider.force_flush, timeout_millis=5_000)

    async def shutdown(self) -> None:
        try:
            await asyncio.to_thread(self._provider.shutdown)
        finally:
            if self._adapter is not None:
                self._adapter.detach()


def _child(cell: Cell) -> int:
    _load_dotenv()
    drive = _load_drive()
    output = ARTIFACT_DIR / f"{cell.name}.json"
    raw_hint = os.environ.get("EARSHOT_MATRIX_FAILURE_HINT", "{}")
    try:
        failure_hint = json.loads(raw_hint)
    except json.JSONDecodeError:
        failure_hint = {}

    def factory(_api_key: str, recorder: Any) -> MatrixRuntime:
        return MatrixRuntime(drive, cell, recorder, failure_hint=failure_hint)

    key = os.environ.get(KEYS[cell.llm], "")
    return asyncio.run(
        drive.run_driver(
            key,
            runtime_factory=factory,
            output_path=output,
            run_timeout=35.0,
        )
    )


def _summary(cell: Cell, output: str, returncode: int) -> None:
    path = ARTIFACT_DIR / f"{cell.name}.json"
    status = "no-artifact"
    operations = ""
    if path.exists():
        payload = json.loads(path.read_text())
        profile = payload.get("profile", payload)
        status = profile.get("session", {}).get("status", "unknown")
        operations = ",".join(
            f"{item.get('operation_name')}:{item.get('status')}"
            for item in sorted(
                profile.get("operations", []),
                key=lambda item: item.get("operation_name", ""),
            )
        )
    print(f"CELL {cell.name} exit={returncode} status={status} operations={operations}")
    if returncode:
        lines = [line for line in output.splitlines() if line.strip()]
        for line in lines[-8:]:
            print(f"  {line}")


def _artifact_summary(cell: Cell) -> dict[str, Any]:
    path = ARTIFACT_DIR / f"{cell.name}.json"
    if not path.exists():
        return {"cell": cell.name, "status": "no_artifact", "valid": False}
    payload = json.loads(path.read_text())
    bundle = earshot.decode_incident_json(path.read_bytes(), validate=False)
    report = earshot.validate_incident(bundle)
    operations = {
        operation.operation_name: operation.status for operation in bundle.profile.operations
    }
    measurements_ms: dict[str, list[float]] = {}
    for sample in bundle.profile.quality_samples:
        for measurement in sample.measurements:
            if measurement.name in {
                "pipecat.llm.ttfb",
                "pipecat.tts.ttfb",
                "pipecat.turn.user_bot_latency",
            }:
                measurements_ms.setdefault(measurement.name, []).append(
                    float(measurement.value) * 1000
                )
    measurement_summary = {
        name: {
            "count": len(values),
            "min_ms": round(min(values), 1),
            "max_ms": round(max(values), 1),
        }
        for name, values in measurements_ms.items()
    }
    return {
        "cell": cell.name,
        "status": bundle.profile.session.status,
        "valid": report.ok,
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "operations": operations,
        "quality_samples": len(bundle.profile.quality_samples),
        "measurements_ms": measurement_summary,
        "raw_otlp_chunks": len(payload.get("raw_otlp_chunks", [])),
    }


def _parent(*, force: bool) -> int:
    _load_dotenv()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    probes = _provider_probes()
    failures = 0
    unavailable: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for cell in CELLS:
        missing = _missing_credentials(cell)
        if missing:
            unavailable.append(
                {
                    "cell": cell.name,
                    "status": "unavailable",
                    "missing_environment": list(missing),
                }
            )
            print(
                f"CELL {cell.name} unavailable=missing:{','.join(missing)}",
                flush=True,
            )
            continue
        if not force and (ARTIFACT_DIR / f"{cell.name}.json").exists():
            print(f"CELL {cell.name} reused=existing-artifact", flush=True)
            cells.append(_artifact_summary(cell))
            continue
        child_env = os.environ.copy()
        child_env["EARSHOT_MATRIX_FAILURE_HINT"] = json.dumps(_failure_hint(cell, probes))
        process = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "--cell", cell.name],
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            text=True,
        )
        output = f"{process.stdout}\n{process.stderr}"
        _summary(cell, output, process.returncode)
        sys.stdout.flush()
        failures += process.returncode != 0
        cells.append(_artifact_summary(cell))
    report = {
        "matrix_version": 1,
        "runner": "scripts/run_pipecat_matrix.py",
        "framework": "pipecat-ai==1.5.0",
        "dimensions": {
            "stt": list(STT_PROVIDERS),
            "llm": list(LLM_PROVIDERS),
            "tts": list(TTS_PROVIDERS),
            "total_cells": len(CELLS),
        },
        "provider_probes": probes,
        "cells": cells,
        "unavailable": unavailable,
        "latency_policy": {
            "observer_budget_ms": 250,
            "remote_provider_ttfb": "advisory_not_pass_fail",
            "reason": "Provider network latency is evidence, not Earshot observer overhead.",
        },
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"MATRIX total={len(CELLS)} live={len(cells)} "
        f"unavailable={len(unavailable)} failures={failures}"
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cell")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.cell:
        match = next((cell for cell in CELLS if cell.name == args.cell), None)
        if match is None:
            raise SystemExit(f"unknown cell: {args.cell}")
        return _child(match)
    return _parent(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
