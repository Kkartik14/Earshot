"""Small runtime helpers shared by the executable LiveKit examples."""

from __future__ import annotations

import time
from importlib.metadata import version

from livekit import rtc
from livekit.agents.voice import io as vio

LIVEKIT_AGENTS_VERSION = version("livekit-agents")
DEFAULT_SAMPLE_RATE = 24_000
_AUDIO_OUTPUT_CAPABILITIES = vio.AudioOutputCapabilities(pause=False)


class NullAudioOutput(vio.AudioOutput):
    """Discard TTS audio while honoring LiveKit's playout bookkeeping contract."""

    def __init__(self, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        super().__init__(
            label="earshot-null",
            capabilities=_AUDIO_OUTPUT_CAPABILITIES,
            sample_rate=sample_rate,
        )
        self._segment_duration = 0.0
        self._saw_audio = False

    @property
    def saw_audio(self) -> bool:
        """Whether LiveKit sent at least one synthesized audio frame."""
        return self._saw_audio

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        # The base implementation opens a segment and increments the count used
        # by wait_for_playout(). A sink must call it for every captured frame.
        await super().capture_frame(frame)
        if self._segment_duration == 0.0:
            self.on_playback_started(created_at=time.time())
        self._segment_duration += frame.duration
        self._saw_audio = True

    def _finish_segment(self, *, interrupted: bool) -> None:
        # The base implementation closes the active capture segment. Match it
        # with exactly one playback-finished notification when audio was seen.
        super().flush()
        if self._segment_duration == 0.0:
            return
        playback_position = self._segment_duration
        self._segment_duration = 0.0
        self.on_playback_finished(
            playback_position=playback_position,
            interrupted=interrupted,
        )

    def flush(self) -> None:
        self._finish_segment(interrupted=False)

    def clear_buffer(self) -> None:
        self._finish_segment(interrupted=True)
