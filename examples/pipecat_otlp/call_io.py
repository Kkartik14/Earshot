"""The example call's virtual microphone and speaker (no provider imports).

``WavCallerInput`` streams the caller WAV in at real time; ``PacedDiscardOutput``
plays the bot out at real time into nothing.

**Recording time zero.** ``AudioBufferProcessor``'s sample 0 is the first audio
frame the input transport produced: the recorder writes its first frame without a
gap fill and packs frames that were held up (behind a service's StartFrame
handling, say) contiguously. So the input stamps ``started_at_unix_nano`` as it
produces its first frame. A stamp taken where frames reach the recorder is late
by that hold. The call projection accounts for recording time relative to span time.

**Known Pipecat behaviour, not corrected here.** When frames reach the recorder
more than 200 ms late and then in a burst, ``AudioBufferProcessor`` pads the gap
with silence and still appends the burst, so the tracks gain time from that point
on. Earshot measures recording and span timing when it builds the call projection.
"""

from __future__ import annotations

import asyncio
import time

from make_caller_audio import SAMPLE_RATE
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

FRAME_S = 0.02


class WavCallerInput(BaseInputTransport):
    """A virtual microphone: streams caller PCM in 20 ms frames at real time, then silence."""

    def __init__(self, pcm: bytes) -> None:
        super().__init__(TransportParams(audio_in_enabled=True, audio_in_sample_rate=SAMPLE_RATE))
        self._pcm = pcm
        self._mic_task: asyncio.Task | None = None
        self.script_done = asyncio.Event()
        self.bot_speaking = False
        self.bot_stopped_at = 0.0
        # Wall time of the first frame produced: sample 0 of the recorded tracks.
        self.started_at_unix_nano: int | None = None
        self.first_audio = asyncio.Event()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        if self._mic_task is None:
            self._mic_task = self.create_task(self._stream())

    async def stop(self, frame: EndFrame) -> None:
        await self._stop_mic()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._stop_mic()
        await super().cancel(frame)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking, self.bot_stopped_at = False, time.monotonic()

    async def _stop_mic(self) -> None:
        if self._mic_task is not None:
            await self.cancel_task(self._mic_task)
            self._mic_task = None

    async def _stream(self) -> None:
        chunk = int(SAMPLE_RATE * FRAME_S) * 2
        loop = asyncio.get_running_loop()
        deadline, offset = loop.time(), 0
        while True:
            audio = self._pcm[offset : offset + chunk].ljust(chunk, b"\0")
            offset += chunk
            if offset >= len(self._pcm) and not self.script_done.is_set():
                self.script_done.set()
            frame = InputAudioRawFrame(audio=audio, sample_rate=SAMPLE_RATE, num_channels=1)
            if self.started_at_unix_nano is None:
                self.started_at_unix_nano = time.time_ns()
                self.first_audio.set()
            await self.push_audio_frame(frame)
            deadline += FRAME_S
            await asyncio.sleep(max(0.0, deadline - loop.time()))


class PacedDiscardOutput(BaseOutputTransport):
    """A virtual speaker: plays bot audio out at 1x real time and discards it."""

    def __init__(self) -> None:
        super().__init__(TransportParams(audio_out_enabled=True, audio_out_end_silence_secs=0))
        self._playout_end = 0.0

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        now = time.monotonic()
        seconds = len(frame.audio) / (frame.sample_rate * 2 * frame.num_channels)
        self._playout_end = max(self._playout_end, now) + seconds
        await asyncio.sleep(self._playout_end - now)
        return True
