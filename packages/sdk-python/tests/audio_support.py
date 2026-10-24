"""Real audio bodies for recordings tests: WAV from ``wave``, FLAC and Ogg Opus from
PyAV's encoders, and the real LiveKit 1.8 stereo recording.

``encoded`` skips the calling test when PyAV (or numpy) is not installed; both come
with the ``livekit`` extra the CI job installs.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# Ogg Opus, 2 channels (ch0 user, ch1 agent), 48 kHz, 34.0 s: LiveKit's own RecorderIO.
LIVEKIT_RECORDING = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"


def wav(*, channels: int = 1, seconds: float = 1.5, rate: int = 16_000, tone: int = 1) -> bytes:
    """A PCM16 WAV of a constant sample value."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes([tone, 0]) * int(rate * seconds) * channels)
    return buffer.getvalue()


def encoded(container: str, codec: str, *, rate: int, channels: int, seconds: float) -> bytes:
    """A real ``codec`` file in ``container`` (``"flac"``/``"flac"`` or
    ``"ogg"``/``"libopus"``): a 440 Hz tone, from PyAV's encoder."""

    av = pytest.importorskip("av")
    numpy = pytest.importorskip("numpy")
    layout = "mono" if channels == 1 else "stereo"
    total = round(rate * seconds)
    tone = (numpy.sin(numpy.arange(total) / rate * 2 * numpy.pi * 440) * 8000).astype("int16")
    planes = numpy.repeat(tone[None, :], channels, axis=0)
    buffer = io.BytesIO()
    with av.open(buffer, "w", format=container) as output:
        stream = output.add_stream(codec, rate=rate, layout=layout)
        size = stream.codec_context.frame_size or 4096
        for start in range(0, total, size):
            frame = av.AudioFrame.from_ndarray(
                numpy.ascontiguousarray(planes[:, start : start + size]),
                format="s16p",
                layout=layout,
            )
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    return buffer.getvalue()
