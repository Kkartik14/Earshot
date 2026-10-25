"""The example call's virtual microphone and speaker for a LiveKit ``AgentSession``.

``CallerAudioInput`` streams the caller WAV in at real time, then silence, like a
microphone. ``PacedAudioOutput`` plays agent audio out at 1x real time into nothing and
reports playback the way a real device does: ``playback_started`` at the first sample,
``playback_progressed`` for each contiguous run, and ``playback_finished`` with the
position actually reached (cut short on ``clear_buffer``). LiveKit's ``RecorderIO``
places the agent channel from those reports, and ``agent_speaking``,
``lk.e2e_latency`` and ``lk.playout.position`` come from them too, so an output that
"plays" instantly would compress every one of them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from livekit import rtc
from livekit.agents.voice import io as vio

FRAME_S = 0.02
# A frame that arrives later than this after the previous one finished playing starts a
# new run (the speaker ran dry and waited), instead of being placed back-to-back.
UNDERRUN_S = 0.005


class CallerAudioInput(vio.AudioInput):
    """A virtual microphone: 20 ms mono PCM16 frames on a monotonic real-time schedule.

    Each frame is returned when its last sample has "arrived", as a capture device
    would. ``started_at`` is the wall time of the caller WAV's sample 0.
    """

    def __init__(self, pcm: bytes, *, sample_rate: int) -> None:
        super().__init__(label="earshot-caller-wav")
        self._pcm = pcm
        self._sample_rate = sample_rate
        self._chunk = int(sample_rate * FRAME_S) * 2
        self._offset = 0
        self._deadline: float | None = None
        self.started_at: float | None = None
        self.script_done = asyncio.Event()

    def __aiter__(self) -> CallerAudioInput:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        loop = asyncio.get_running_loop()
        if self._deadline is None:
            self._deadline, self.started_at = loop.time(), time.time()
        self._deadline += FRAME_S
        await asyncio.sleep(max(0.0, self._deadline - loop.time()))
        chunk = self._pcm[self._offset : self._offset + self._chunk].ljust(self._chunk, b"\0")
        self._offset += self._chunk
        if self._offset >= len(self._pcm):
            self.script_done.set()
        return rtc.AudioFrame(
            data=chunk,
            sample_rate=self._sample_rate,
            num_channels=1,
            samples_per_channel=self._chunk // 2,
        )


@dataclass
class PlayedSegment:
    """One playback segment as the speaker played it (wall-clock seconds)."""

    started_at: float
    played_s: float = 0.0
    captured_s: float = 0.0
    interrupted: bool = False
    runs: list[tuple[float, float, float]] = field(default_factory=list)  # (wall, offset, s)


_END, _CLEAR = object(), object()


class PacedAudioOutput(vio.AudioOutput):
    """A virtual speaker: plays agent audio at 1x real time into nothing, reporting honestly.

    Segments follow LiveKit's contract: a segment opens on the first ``capture_frame``
    after a ``flush`` and must get exactly one ``playback_finished``. ``capture_frame``
    never blocks; a playout task drains the queue at real time.
    """

    def __init__(self) -> None:
        super().__init__(
            label="earshot-paced-speaker",
            capabilities=vio.AudioOutputCapabilities(pause=False),
            sample_rate=None,  # any rate: play TTS frames as they come
        )
        self._queue: asyncio.Queue[tuple[int, object]] = asyncio.Queue()
        self._interrupt = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._capturing = False
        self._segment_id = 0  # producer side: id of the newest segment
        self._finished_id = 0  # playout side: every id <= this got playback_finished
        self._current: PlayedSegment | None = None
        self._run: tuple[float, float, float] | None = None  # (mono start, wall start, offset)
        self._run_s = 0.0
        self.segments: list[PlayedSegment] = []

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if not self._capturing:
            self._capturing = True
            self._segment_id += 1
        if self._task is None:
            self._task = asyncio.create_task(self._playout(), name="earshot_paced_playout")
        self._queue.put_nowait((self._segment_id, frame))

    def flush(self) -> None:
        super().flush()
        if self._capturing:
            self._capturing = False
            self._queue.put_nowait((self._segment_id, _END))

    def clear_buffer(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._interrupt.set()
        self._queue.put_nowait((self._segment_id, _CLEAR))

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _playout(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            segment_id, item = await self._queue.get()
            if item is _CLEAR:
                self._interrupt.clear()
                # every segment up to this one ends here, played or not
                while self._finished_id < segment_id:
                    self._finish(self._finished_id + 1, interrupted=True)
                continue
            if segment_id <= self._finished_id:
                continue  # a late frame of a segment that was already cut off
            if item is _END:
                self._finish(segment_id, interrupted=False)
                continue
            assert isinstance(item, rtc.AudioFrame)
            now = loop.time()
            if self._current is None:
                self._current = PlayedSegment(started_at=time.time())
                self.segments.append(self._current)
                self.on_playback_started(created_at=self._current.started_at)
            self._current.captured_s += item.duration
            if self._run is None or now > self._run[0] + self._run_s + UNDERRUN_S:
                self._close_run()
                self._run = (now, time.time(), self._current.played_s)
            start = self._run[0] + self._run_s
            try:
                await asyncio.wait_for(
                    self._interrupt.wait(), timeout=max(0.0, start + item.duration - now)
                )
            except TimeoutError:
                self._advance(item.duration)
            else:  # clear_buffer while this frame played: keep what was heard
                self._advance(min(item.duration, max(0.0, loop.time() - start)))
                self._interrupt.clear()
                while self._finished_id < self._segment_id:
                    self._finish(self._finished_id + 1, interrupted=True)

    def _advance(self, seconds: float) -> None:
        assert self._current is not None
        self._run_s += seconds
        self._current.played_s += seconds

    def _close_run(self) -> None:
        if self._run is not None and self._run_s > 0 and self._current is not None:
            _, wall, offset = self._run
            self._current.runs.append((wall, offset, self._run_s))
            self.on_playback_progressed(started_at=wall, offset=offset, duration=self._run_s)
        self._run, self._run_s = None, 0.0

    def _finish(self, segment_id: int, *, interrupted: bool) -> None:
        """Report ``segment_id`` finished; segments play in order, so it is ``_current``."""
        self._finished_id = segment_id
        current = self._current
        if current is None:  # cleared before a single frame played
            self.segments.append(PlayedSegment(started_at=time.time(), interrupted=True))
            self.on_playback_finished(playback_position=0.0, interrupted=True)
            return
        self._close_run()
        self._current = None
        current.interrupted = interrupted
        self.on_playback_finished(playback_position=current.played_s, interrupted=interrupted)
