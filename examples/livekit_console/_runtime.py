"""Small runtime helpers shared by the executable LiveKit examples."""

from __future__ import annotations

import os
import pathlib
import subprocess
import time
import wave
from dataclasses import dataclass
from importlib.metadata import version

from livekit import rtc
from livekit.agents.voice import io as vio

LIVEKIT_AGENTS_VERSION = version("livekit-agents")
DEFAULT_SAMPLE_RATE = 24_000
_AUDIO_OUTPUT_CAPABILITIES = vio.AudioOutputCapabilities(pause=False)


# ---------------------------------------------------------------------------
# Model-agnostic provider selection
#
# The examples default to Groq's free tier (no credit card) but are not wired to
# any one vendor. Each stage is chosen from the environment, so the same driver
# proves the adapter on whatever STT/LLM/TTS a deployment actually runs:
#
#   EARSHOT_STT_PROVIDER / EARSHOT_STT_MODEL
#   EARSHOT_LLM_PROVIDER / EARSHOT_LLM_MODEL
#   EARSHOT_TTS_PROVIDER / EARSHOT_TTS_MODEL / EARSHOT_TTS_VOICE
#
# Adapters normalize provider-neutral facts, so switching vendors must not change
# the shape of the incident -- only the numbers inside it.
# ---------------------------------------------------------------------------

_STAGE_DEFAULTS: dict[str, dict[str, str]] = {
    "STT": {"provider": "groq", "model": "whisper-large-v3-turbo"},
    "LLM": {"provider": "groq", "model": "llama-3.1-8b-instant"},
    "TTS": {"provider": "groq", "model": "canopylabs/orpheus-v1-english", "voice": "autumn"},
}


@dataclass(frozen=True)
class StageChoice:
    """A stage's provider + model, resolved from the environment."""

    stage: str
    provider: str
    model: str
    voice: str | None = None

    def label(self) -> str:
        return f"{self.provider}:{self.model}" + (f"/{self.voice}" if self.voice else "")


def _stage_choice(stage: str) -> StageChoice:
    defaults = _STAGE_DEFAULTS[stage]
    return StageChoice(
        stage=stage,
        provider=os.environ.get(f"EARSHOT_{stage}_PROVIDER", defaults["provider"]).lower(),
        model=os.environ.get(f"EARSHOT_{stage}_MODEL", defaults["model"]),
        voice=os.environ.get(f"EARSHOT_{stage}_VOICE", defaults.get("voice")),
    )


def _livekit_plugin(provider: str) -> object:
    # livekit.plugins is a namespace package; each vendor plugin must be imported
    # explicitly rather than read as an attribute.
    import importlib

    try:
        return importlib.import_module(f"livekit.plugins.{provider}")
    except ImportError as error:
        raise ValueError(
            f"unsupported provider {provider!r}; install livekit-plugins-{provider} "
            f"or set the EARSHOT_*_PROVIDER variables to an installed one"
        ) from error


def voice_stack() -> tuple[object, object, object, str]:
    """Build (stt, llm, tts, label) for a LiveKit AgentSession from the environment.

    Providers read their own key from the environment (e.g. ``GROQ_API_KEY``,
    ``OPENAI_API_KEY``). Returns a human-readable label of what was selected.
    """
    stt_choice = _stage_choice("STT")
    llm_choice = _stage_choice("LLM")
    tts_choice = _stage_choice("TTS")

    stt = _livekit_plugin(stt_choice.provider).STT(model=stt_choice.model)
    llm = _livekit_plugin(llm_choice.provider).LLM(model=llm_choice.model)
    tts_module = _livekit_plugin(tts_choice.provider)
    tts = (
        tts_module.TTS(model=tts_choice.model, voice=tts_choice.voice)
        if tts_choice.voice
        else tts_module.TTS(model=tts_choice.model)
    )
    label = " / ".join(choice.label() for choice in (stt_choice, llm_choice, tts_choice))
    return stt, llm, tts, label


def synth_utterance_wav(
    path: pathlib.Path, text: str, *, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> None:
    """Synthesize the user's utterance locally with macOS ``say`` -- no API, no cost.

    Writes a mono s16le WAV at ``sample_rate``, which ``audio_frames_from_file`` can
    ingest directly as if it were microphone audio.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["say", "-o", str(path), f"--data-format=LEI16@{sample_rate}", text],
        check=True,
        capture_output=True,
    )
    with wave.open(str(path)) as handle:
        if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (
            sample_rate,
            1,
            2,
        ):
            raise RuntimeError("`say` did not produce the expected mono s16le WAV")


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
