from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import subprocess
import sys
import time
from types import ModuleType
from typing import Any

import pytest

import earshot
from earshot.codec import decode_incident_json

ROOT = pathlib.Path(__file__).resolve().parents[3]
DRIVER = ROOT / "examples" / "pipecat_headless" / "drive.py"
pytest.importorskip("pipecat")
pytest.importorskip("groq")
pytestmark = pytest.mark.integration


def load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("earshot_pipecat_headless_driver", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_returns_usage_error_when_groq_key_is_missing() -> None:
    environment = os.environ.copy()
    environment.pop("GROQ_API_KEY", None)

    result = subprocess.run(
        [sys.executable, str(DRIVER)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "Set GROQ_API_KEY before" in result.stderr


def test_real_runtime_fails_fast_when_global_tracing_is_already_owned() -> None:
    script = f"""
import importlib.util
import sys
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

trace.set_tracer_provider(TracerProvider())
spec = importlib.util.spec_from_file_location('earshot_pipecat_owned_provider', {str(DRIVER)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    module.PipecatRuntime('test-key', object())
except RuntimeError as error:
    assert str(error) == 'the headless Pipecat driver requires a fresh one-shot process'
else:
    raise AssertionError('runtime accepted a rejected tracer-provider replacement')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_setup_failure_writes_failed_incident(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"

    def fail_setup(api_key: str, recorder: object) -> object:
        del api_key, recorder
        raise RuntimeError("pipeline setup failed")

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=fail_setup,
            output_path=output_path,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"


class SuccessfulRuntime:
    def __init__(
        self,
        recorder: earshot.IncidentRecorder,
        *,
        recorded_stages: tuple[str, ...] = ("stt", "llm", "tts"),
        flush_result: bool = True,
    ) -> None:
        self.recorder = recorder
        self.recorded_stages = recorded_stages
        self._saw_tts_audio = True
        self.flush_result = flush_result
        self.closed = False
        self.shutdown_called = False

    @property
    def saw_tts_audio(self) -> bool:
        return self._saw_tts_audio

    async def run(self) -> None:
        for stage in self.recorded_stages:
            with self.recorder.operation(stage, operation_id=f"{stage}-operation"):
                pass

    async def aclose(self) -> None:
        self.closed = True

    async def force_flush(self) -> bool:
        return self.flush_result

    async def shutdown(self) -> None:
        self.shutdown_called = True


def test_success_requires_real_stages_and_closes_runtime(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        assert api_key == "test-key"
        runtime = SuccessfulRuntime(recorder)
        created.append(runtime)
        return runtime

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    assert exit_code == 0
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "completed"
    assert {operation.operation_name for operation in bundle.profile.operations} == {
        "stt",
        "llm",
        "tts",
    }
    assert created[0].closed
    assert created[0].shutdown_called


def test_missing_captured_stage_marks_incident_failed(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return SuccessfulRuntime(recorder, recorded_stages=("llm", "tts"))

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"


@pytest.mark.parametrize("stage_status", ["error", "cancelled", "timed_out"])
def test_failed_captured_stage_cannot_certify_success(
    tmp_path: pathlib.Path,
    stage_status: str,
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"

    class FailedStageRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            started_at = earshot.TimePoint(
                monotonic_time_nano=str(time.monotonic_ns()),
                clock_domain_id=self.recorder.clock_domain_id,
            )
            ended_at = earshot.TimePoint(
                monotonic_time_nano=str(time.monotonic_ns()),
                clock_domain_id=self.recorder.clock_domain_id,
            )
            self.recorder.record_operation(
                operation_id="stt-operation",
                operation_name="stt",
                status=stage_status,
                started_at=started_at,
                ended_at=ended_at,
            )
            for stage in ("llm", "tts"):
                with self.recorder.operation(stage, operation_id=f"{stage}-operation"):
                    pass

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return FailedStageRuntime(recorder)

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"


def test_pipeline_timeout_is_bounded_and_finalized(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    class HangingRuntime(SuccessfulRuntime):
        run_cancelled = False

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.run_cancelled = True
                raise

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = HangingRuntime(recorder)
        created.append(runtime)
        return runtime

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
            run_timeout=0.01,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "timed_out"
    assert created[0].run_cancelled
    assert created[0].closed
    assert created[0].shutdown_called


def test_run_deadline_cannot_be_suppressed_into_false_success(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    class CancellationSuppressingRuntime(SuccessfulRuntime):
        cancellation_seen = False

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_seen = True
                await asyncio.sleep(0.08)
                await super().run()

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = CancellationSuppressingRuntime(recorder)
        created.append(runtime)
        return runtime

    started = time.monotonic()
    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
            run_timeout=0.001,
        )
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert elapsed < 0.05
    assert created[0].cancellation_seen
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "timed_out"


def test_late_failure_after_deadline_does_not_leak_task_exception_payload(
    tmp_path: pathlib.Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    sentinel = "LATE_TASK_SECRET_SENTINEL_c8a712"

    class LateFailureRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError(sentinel) from None

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return LateFailureRuntime(recorder)

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
            run_timeout=0.001,
        )
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert decode_incident_json(output_path.read_bytes()).profile.session.status == "timed_out"
    assert "Task exception was never retrieved" not in captured.err
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_caller_cancellation_propagates_after_single_failed_artifact(
    tmp_path: pathlib.Path,
) -> None:
    driver = load_driver()
    artifact_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    class HangingRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            await asyncio.Event().wait()

    write_count = 0

    def count_artifact_write(path: pathlib.Path, payload: bytes) -> None:
        nonlocal write_count
        write_count += 1
        driver._write_artifact_atomic(path, payload)

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = HangingRuntime(recorder)
        created.append(runtime)
        return runtime

    async def cancel_driver() -> None:
        task = asyncio.create_task(
            driver.run_driver(
                "test-key",
                runtime_factory=create_runtime,
                output_path=artifact_path,
                artifact_writer=count_artifact_write,
            )
        )
        while not created:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_driver())

    assert write_count == 1
    bundle = decode_incident_json(artifact_path.read_bytes())
    assert bundle.profile.session.status == "failed"
    assert created[0].closed
    assert created[0].shutdown_called


@pytest.mark.parametrize("cancel_stage", ["close", "flush", "shutdown"])
def test_cleanup_cancellation_does_not_skip_later_finalization(
    tmp_path: pathlib.Path,
    cancel_stage: str,
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []
    sdk_shutdown_calls = 0

    class CancellingCleanupRuntime(SuccessfulRuntime):
        flush_called = False

        async def aclose(self) -> None:
            self.closed = True
            if cancel_stage == "close":
                raise asyncio.CancelledError

        async def force_flush(self) -> bool:
            self.flush_called = True
            if cancel_stage == "flush":
                raise asyncio.CancelledError
            return True

        async def shutdown(self) -> None:
            self.shutdown_called = True
            if cancel_stage == "shutdown":
                raise asyncio.CancelledError

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = CancellingCleanupRuntime(recorder)
        created.append(runtime)
        return runtime

    def shutdown_sdk() -> bool:
        nonlocal sdk_shutdown_calls
        sdk_shutdown_calls += 1
        return True

    async def run_cancelled_cleanup() -> None:
        with pytest.raises(asyncio.CancelledError):
            await driver.run_driver(
                "test-key",
                runtime_factory=create_runtime,
                output_path=output_path,
                sdk_shutdown=shutdown_sdk,
            )

    asyncio.run(run_cancelled_cleanup())

    assert created[0].closed
    assert created[0].flush_called
    assert created[0].shutdown_called
    assert sdk_shutdown_calls == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"


def test_driver_filters_sensitive_payloads_from_artifact_and_console(
    tmp_path: pathlib.Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    sentinel = "GROQ_SECRET_SENTINEL_e4c91d"

    class SensitiveRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            from loguru import logger

            logger.debug("provider payload: {}", sentinel)
            attributes = {
                "stt": {"speech.text": sentinel, "audio.chunk": sentinel},
                "llm": {"gen_ai.input.messages": [{"content": sentinel}]},
                "tts": {"speech.text": sentinel, "model.output": sentinel},
            }
            for stage, stage_attributes in attributes.items():
                with self.recorder.operation(
                    stage,
                    operation_id=f"{stage}-operation",
                    attributes=stage_attributes,
                ):
                    pass

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        assert api_key == sentinel
        return SensitiveRuntime(recorder)

    exit_code = asyncio.run(
        driver.run_driver(
            sentinel,
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    captured = capfd.readouterr()
    assert exit_code == 0
    assert sentinel.encode() not in output_path.read_bytes()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_driver_does_not_echo_exception_payloads(
    tmp_path: pathlib.Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    sentinel = "GROQ_EXCEPTION_SENTINEL_77c5"

    class LeakyFailureRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            raise RuntimeError(sentinel)

        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError(sentinel)

        async def force_flush(self) -> bool:
            raise RuntimeError(sentinel)

        async def shutdown(self) -> None:
            self.shutdown_called = True
            raise RuntimeError(sentinel)

        @property
        def saw_tts_audio(self) -> bool:
            raise RuntimeError(sentinel)

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return LeakyFailureRuntime(recorder)

    def fail_sdk_shutdown() -> bool:
        raise RuntimeError(sentinel)

    exit_code = asyncio.run(
        driver.run_driver(
            sentinel,
            runtime_factory=create_runtime,
            output_path=output_path,
            sdk_shutdown=fail_sdk_shutdown,
        )
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert sentinel.encode() not in output_path.read_bytes()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_cancellation_payload_is_not_rethrown_or_logged(
    tmp_path: pathlib.Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    sentinel = "CANCEL_SECRET_SENTINEL_91af"

    class PayloadCancellationRuntime(SuccessfulRuntime):
        async def run(self) -> None:
            raise asyncio.CancelledError(sentinel)

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return PayloadCancellationRuntime(recorder)

    async def run_cancelled() -> None:
        with pytest.raises(asyncio.CancelledError) as raised:
            await driver.run_driver(
                sentinel,
                runtime_factory=create_runtime,
                output_path=output_path,
            )
        assert raised.value.args == ()

    asyncio.run(run_cancelled())

    captured = capfd.readouterr()
    assert sentinel.encode() not in output_path.read_bytes()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_trace_flush_failure_prevents_completed_status(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = SuccessfulRuntime(recorder, flush_result=False)
        created.append(runtime)
        return runtime

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"
    assert created[0].closed
    assert created[0].shutdown_called


def test_sdk_shutdown_failure_returns_nonzero(tmp_path: pathlib.Path) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    shutdown_calls = 0

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        return SuccessfulRuntime(recorder)

    def fail_shutdown() -> bool:
        nonlocal shutdown_calls
        shutdown_calls += 1
        return False

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
            sdk_shutdown=fail_shutdown,
        )
    )

    assert exit_code == 1
    assert shutdown_calls == 1
    assert output_path.is_file()
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"


@pytest.mark.parametrize("failure", ["close", "flush", "shutdown", "tts_evidence"])
def test_runtime_boundary_failure_still_finalizes_and_shuts_down(
    tmp_path: pathlib.Path,
    failure: str,
) -> None:
    driver = load_driver()
    output_path = tmp_path / "incident.json"
    created: list[SuccessfulRuntime] = []

    class BoundaryFailureRuntime(SuccessfulRuntime):
        @property
        def saw_tts_audio(self) -> bool:
            if failure == "tts_evidence":
                raise RuntimeError("TTS evidence unavailable")
            return super().saw_tts_audio

        async def aclose(self) -> None:
            self.closed = True
            if failure == "close":
                raise RuntimeError("pipeline close failed")

        async def force_flush(self) -> bool:
            if failure == "flush":
                raise RuntimeError("trace flush failed")
            return True

        async def shutdown(self) -> None:
            self.shutdown_called = True
            if failure == "shutdown":
                raise RuntimeError("trace provider shutdown failed")

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = BoundaryFailureRuntime(recorder)
        created.append(runtime)
        return runtime

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    assert exit_code == 1
    bundle = decode_incident_json(output_path.read_bytes())
    assert bundle.profile.session.status == "failed"
    assert created[0].closed
    assert created[0].shutdown_called


def test_synth_user_pcm_kills_a_wedged_say_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = load_driver()

    class WedgedProcess:
        returncode: int | None = None
        killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            assert self.killed
            return -9

    process = WedgedProcess()

    async def create_process(*args: object, **kwargs: object) -> WedgedProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(driver.asyncio, "create_subprocess_exec", create_process)

    async def synthesize() -> None:
        with pytest.raises(TimeoutError, match="timed out"):
            await driver.synth_user_pcm(timeout=0.001)

    asyncio.run(synthesize())
    assert process.killed


def test_synth_user_pcm_kills_say_when_the_driver_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = load_driver()
    process_created = asyncio.Event()

    class WedgedProcess:
        returncode: int | None = None
        killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            assert self.killed
            return -9

    process = WedgedProcess()

    async def create_process(*args: object, **kwargs: object) -> WedgedProcess:
        del args, kwargs
        process_created.set()
        return process

    monkeypatch.setattr(driver.asyncio, "create_subprocess_exec", create_process)

    async def cancel_synthesis() -> None:
        task = asyncio.create_task(driver.synth_user_pcm(timeout=60))
        await process_created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_synthesis())
    assert process.killed


def test_artifact_write_failure_returns_nonzero_after_closing_recorder(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = load_driver()
    created: list[SuccessfulRuntime] = []
    output_path = tmp_path / "incident.json"
    prior_artifact = b"previous durable incident"
    output_path.write_bytes(prior_artifact)

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(driver.os, "replace", fail_replace)

    def create_runtime(api_key: str, recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = SuccessfulRuntime(recorder)
        created.append(runtime)
        return runtime

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=output_path,
        )
    )

    captured = capfd.readouterr()
    assert exit_code == 1
    assert output_path.read_bytes() == prior_artifact
    assert list(tmp_path.glob(".incident.json.*.tmp")) == []
    assert "REAL PIPECAT INCIDENT" not in captured.out
    assert "lifecycle status" not in captured.out
    assert "full artifact" not in captured.out
    assert created[0].closed
    assert created[0].shutdown_called
    with pytest.raises(RuntimeError, match="closed"), created[0].recorder.operation("tool"):
        pass


def test_recorder_close_failure_does_not_skip_runtime_or_sdk_shutdown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = load_driver()
    created: list[SuccessfulRuntime] = []
    sdk_shutdown_calls = 0

    class CloseFailureRecorder(earshot.IncidentRecorder):
        close_calls = 0

        def close(self, status: str = "completed") -> earshot.IncidentBundle:
            del status
            self.close_calls += 1
            raise RuntimeError("recorder close failed")

    recorder = CloseFailureRecorder()
    monkeypatch.setattr(driver.earshot, "session", lambda **kwargs: recorder)

    def create_runtime(api_key: str, runtime_recorder: earshot.IncidentRecorder) -> Any:
        del api_key
        runtime = SuccessfulRuntime(runtime_recorder)
        created.append(runtime)
        return runtime

    def shutdown_sdk() -> bool:
        nonlocal sdk_shutdown_calls
        sdk_shutdown_calls += 1
        return True

    exit_code = asyncio.run(
        driver.run_driver(
            "test-key",
            runtime_factory=create_runtime,
            output_path=tmp_path / "incident.json",
            sdk_shutdown=shutdown_sdk,
        )
    )

    assert exit_code == 1
    assert recorder.close_calls == 1
    assert sdk_shutdown_calls == 1
    assert created[0].closed
    assert created[0].shutdown_called


def test_discard_output_transport_observes_real_tts_audio() -> None:
    frames = __import__("pipecat.frames.frames", fromlist=["OutputAudioRawFrame"])
    driver = load_driver()
    sink = driver.DiscardOutputTransport()

    accepted = asyncio.run(
        sink.write_audio_frame(
            frames.OutputAudioRawFrame(
                audio=b"\x01\x02\x03\x04",
                sample_rate=48_000,
                num_channels=1,
            )
        )
    )

    assert accepted
    assert sink.saw_tts_audio
    assert sink.audio_bytes_written == 4
