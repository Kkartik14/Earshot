"""Decoding and the scans that run at the source rate: exact-zero runs, peaks, resampling.

Everything here streams: a recording is read in blocks, so a 60-minute stereo call
never sits in memory as samples. Needs the ``signals`` extra (numpy, soundfile).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import soundfile

from . import (
    ANALYSIS_DECODE_FAILED,
    ANALYSIS_NO_AUDIO,
    ANALYSIS_UNSUPPORTED_CODEC,
)
from .peaks import SAMPLES_PER_PIXEL as PEAKS_SAMPLES_PER_PIXEL
from .peaks import VERSION as PEAKS_VERSION
from .peaks import PeaksHeader

# libsndfile reads WAV, FLAC, Ogg (Vorbis, Opus) and MP3; it has no WebM/Matroska
# demuxer, so a WebM recording is unavailable without trying.
UNSUPPORTED_CONTENT_TYPES = frozenset({"audio/webm"})
# Blocks of this many seconds, rounded up to whole peaks pixels.
BLOCK_SECONDS = 10


class DecodeError(Exception):
    """A recording the analysis cannot read; ``code`` is the stable error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Source:
    """A recording opened for decoding: its rate, channels and length, and its blocks
    of int16 samples, ``(frames, channels)``."""

    def __init__(self, path: Path, content_type: str | None = None) -> None:
        if content_type in UNSUPPORTED_CONTENT_TYPES:
            raise DecodeError(ANALYSIS_UNSUPPORTED_CODEC)
        try:
            self._file = soundfile.SoundFile(str(path))
        except (RuntimeError, TypeError, ValueError) as error:
            # libsndfile's message can quote the path; the code says enough.
            raise DecodeError(ANALYSIS_DECODE_FAILED) from error
        self.rate = int(self._file.samplerate)
        self.channels = int(self._file.channels)
        self.frames = int(self._file.frames)
        if self.rate <= 0 or self.channels <= 0 or self.frames <= 0:
            self._file.close()
            raise DecodeError(ANALYSIS_NO_AUDIO)
        pixels = math.ceil(self.rate * BLOCK_SECONDS / PEAKS_SAMPLES_PER_PIXEL)
        self.block_frames = pixels * PEAKS_SAMPLES_PER_PIXEL

    def blocks(self) -> Iterator[np.ndarray]:
        try:
            yield from self._file.blocks(blocksize=self.block_frames, dtype="int16", always_2d=True)
        except (RuntimeError, ValueError) as error:
            raise DecodeError(ANALYSIS_DECODE_FAILED) from error

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class Resampler:
    """A streaming rational resampler: a Kaiser-windowed sinc low-pass, evaluated per
    output sample (polyphase), with the input history carried between blocks.

    ``feed`` takes int16 samples and returns float32 samples in [-1, 1] at the new
    rate; ``flush`` returns the rest. The output has ``ceil(n * rate_out / rate_in)``
    samples for ``n`` input samples, and output sample ``i`` is at input time
    ``i * rate_in / rate_out``: no delay.
    """

    ZERO_CROSSINGS = 16
    ROLLOFF = 0.94
    KAISER_BETA = 8.6
    CHUNK = 16384

    def __init__(self, rate_in: int, rate_out: int) -> None:
        divisor = math.gcd(rate_in, rate_out)
        self.up, self.down = rate_out // divisor, rate_in // divisor
        cutoff = 0.5 * min(1.0, self.up / self.down) * self.ROLLOFF  # cycles per input sample
        half = math.ceil(self.ZERO_CROSSINGS / (2 * cutoff))
        self._offsets = np.arange(-half + 1, half + 1, dtype=np.int64)
        distance = np.arange(self.up)[:, None] / self.up - self._offsets[None, :]
        window = np.i0(
            self.KAISER_BETA * np.sqrt(np.clip(1 - (distance / half) ** 2, 0, None))
        ) / np.i0(self.KAISER_BETA)
        taps = 2 * cutoff * np.sinc(2 * cutoff * distance) * window
        self._taps = (taps / taps.sum(axis=1, keepdims=True)).astype(np.float32)
        self._half = half
        # The buffer holds input samples from absolute index ``_start``; before the
        # first sample it is silence.
        self._buffer = np.zeros(half - 1, dtype=np.float32)
        self._start = -(half - 1)
        self._received = 0
        self._next = 0

    def _base(self, index: int) -> int:
        return index * self.down // self.up

    def _produce(self, end: int) -> np.ndarray:
        outputs = []
        for first in range(self._next, end, self.CHUNK):
            index = np.arange(first, min(end, first + self.CHUNK), dtype=np.int64)
            base = index * self.down // self.up
            positions = (base - self._start)[:, None] + self._offsets[None, :]
            window = self._buffer[positions]
            if self.up == 1:
                outputs.append(window @ self._taps[0])
            else:
                outputs.append(
                    np.einsum("ij,ij->i", window, self._taps[index * self.down % self.up])
                )
        if end > self._next:
            keep_from = self._base(end) + int(self._offsets[0]) - self._start
            self._buffer = self._buffer[max(0, keep_from) :]
            self._start += max(0, keep_from)
            self._next = end
        return np.concatenate(outputs) if outputs else np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> np.ndarray:
        self._buffer = np.concatenate((self._buffer, samples.astype(np.float32) / 32768.0))
        self._received += len(samples)
        last_complete = self._received - 1 - self._half  # the last base with all its taps
        if last_complete < 0:
            return np.zeros(0, dtype=np.float32)
        end = -(-(last_complete + 1) * self.up // self.down)
        return self._produce(end)

    def flush(self) -> np.ndarray:
        self._buffer = np.concatenate((self._buffer, np.zeros(self._half, dtype=np.float32)))
        return self._produce(-(-self._received * self.up // self.down))


class ZeroRuns:
    """Runs of exact-zero samples at least ``minimum_ms`` long, in one channel.

    A recorder pads with digital zero where it had no audio; a microphone, a codec
    or a room never gives exact zeros for this long. The runs keep padding out of
    the noise floor and give the analysis its sample-exact audio anchors.
    """

    def __init__(self, rate: int, minimum_ms: int) -> None:
        self.rate = rate
        self.minimum = math.ceil(rate * minimum_ms / 1000)
        self.runs: list[tuple[int, int]] = []  # [start, end) in samples
        self._position = 0
        self._open: int | None = None  # the start of a run reaching the last block's end

    def feed(self, samples: np.ndarray) -> None:
        count = len(samples)
        if count == 0:
            return
        edges = np.diff(np.concatenate(([0], (samples == 0).view(np.int8), [0])))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        if self._open is not None:
            if starts.size and starts[0] == 0:
                starts = starts.copy()
                starts[0] = self._open - self._position
            else:
                self._close(self._open, self._position)
            self._open = None
        if ends.size and ends[-1] == count:
            self._open = self._position + int(starts[-1])
            starts, ends = starts[:-1], ends[:-1]
        long_enough = (ends - starts) >= self.minimum
        for start, end in zip(starts[long_enough], ends[long_enough], strict=True):
            self.runs.append((self._position + int(start), self._position + int(end)))
        self._position += count

    def _close(self, start: int, end: int) -> None:
        if end - start >= self.minimum:
            self.runs.append((start, end))

    def finish(self) -> list[tuple[int, int]]:
        if self._open is not None:
            self._close(self._open, self._position)
            self._open = None
        return self.runs


class Peaks:
    """An audiowaveform ``.dat`` version 2 file (``signals.peaks``), 8-bit, built block
    by block.

    Each pixel holds every channel's minimum and maximum over
    ``PEAKS_SAMPLES_PER_PIXEL`` source samples, as the sample's high byte
    (``value >> 8``); the last pixel covers what is left.
    """

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = rate
        self.channels = channels
        self._parts: list[np.ndarray] = []
        self._pending = np.zeros((0, channels), dtype=np.int16)

    def _pixels(self, data: np.ndarray) -> np.ndarray:
        shaped = data.reshape(-1, PEAKS_SAMPLES_PER_PIXEL, self.channels)
        pairs = np.stack((shaped.min(axis=1), shaped.max(axis=1)), axis=-1)
        return (pairs >> 8).astype(np.int8)

    def feed(self, block: np.ndarray) -> None:
        data = np.concatenate((self._pending, block)) if len(self._pending) else block
        whole = len(data) // PEAKS_SAMPLES_PER_PIXEL * PEAKS_SAMPLES_PER_PIXEL
        if whole:
            self._parts.append(self._pixels(data[:whole]))
        self._pending = data[whole:].copy()

    def finish(self) -> bytes:
        if len(self._pending):
            rest = self._pending
            pairs = np.stack((rest.min(axis=0), rest.max(axis=0)), axis=-1)[None]
            self._parts.append((pairs >> 8).astype(np.int8))
            self._pending = self._pending[:0]
        body = (
            np.concatenate(self._parts)
            if self._parts
            else np.zeros((0, self.channels, 2), dtype=np.int8)
        )
        header = PeaksHeader(self.rate, PEAKS_SAMPLES_PER_PIXEL, len(body), self.channels)
        return header.pack() + body.tobytes()

    def describe(self, length: int) -> dict[str, object]:
        return {
            "format": "audiowaveform-dat",
            "version": PEAKS_VERSION,
            "bits": 8,
            "sample_rate": self.rate,
            "samples_per_pixel": PEAKS_SAMPLES_PER_PIXEL,
            "channels": self.channels,
            "length": length,
        }
