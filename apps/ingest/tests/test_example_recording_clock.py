"""The Pipecat example's recording time zero is the input's first audio frame.

``AudioBufferProcessor``'s sample 0 is the first audio frame the recorder receives,
which is the first frame the input transport produced: the recorder writes its
first frame without a gap fill and packs a delayed burst contiguously. A service's
slow StartFrame handling (a websocket connect) holds the frames on their way down,
so a stamp taken where frames *reach* the recorder is late by that hold. This runs a real
Pipecat pipeline, without providers, whose middle processor holds the StartFrame
for 0.3 s, as a connect does, and checks the example's stamp against the
production time of the frame that became sample 0.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import EndFrame, Frame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "pipecat_otlp"
sys.path.insert(0, str(EXAMPLE))

from call_io import WavCallerInput  # noqa: E402

pytestmark = pytest.mark.integration

RATE = 16_000
HOLD_SECONDS = 0.3
FRAME_BYTES = int(RATE * 0.02) * 2


class _TimedCaller(WavCallerInput):
    """The example's input, also noting when it produced each frame."""

    def __init__(self, pcm: bytes) -> None:
        super().__init__(pcm)
        self.produced: list[int] = []

    async def push_audio_frame(self, frame) -> None:
        self.produced.append(time.time_ns())
        await super().push_audio_frame(frame)


class _HeldStart(FrameProcessor):
    """Holds the StartFrame for a while, as a service connecting its websocket does."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await asyncio.sleep(HOLD_SECONDS)
        await self.push_frame(frame, direction)


async def _record(pcm: bytes) -> tuple[_TimedCaller, bytes]:
    caller = _TimedCaller(pcm)
    recorder = AudioBufferProcessor(sample_rate=RATE, auto_start_recording=True)
    worker = PipelineWorker(
        Pipeline([caller, _HeldStart(), recorder]),
        params=PipelineParams(audio_in_sample_rate=RATE),
        enable_rtvi=False,
        idle_timeout_secs=None,
    )
    tracks: dict[str, bytes] = {}

    @recorder.event_handler("on_track_audio_data")
    async def on_tracks(_buffer, user_pcm: bytes, _bot_pcm: bytes, _rate: int, _channels: int):
        tracks["user"] = user_pcm

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run = asyncio.create_task(runner.run())
    await asyncio.wait_for(caller.script_done.wait(), timeout=10)
    await worker.queue_frame(EndFrame())
    await asyncio.wait_for(run, timeout=10)
    return caller, tracks["user"]


def test_the_recording_starts_at_the_first_frame_the_input_produced() -> None:
    # A distinct first frame, then a ramp, so sample 0 can be recognised.
    pcm = bytes(range(256)) * (FRAME_BYTES // 256) + bytes(FRAME_BYTES * 30)

    caller, user = asyncio.run(_record(pcm))

    assert user[:FRAME_BYTES] == pcm[:FRAME_BYTES]  # sample 0 is the first frame produced
    assert caller.started_at_unix_nano is not None
    late_by = (caller.started_at_unix_nano - caller.produced[0]) / 1e9
    assert abs(late_by) < 0.02, f"started_at is {late_by:.3f} s off sample 0"
