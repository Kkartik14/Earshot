"""A mono pair's stereo rendition: one FLAC a browser plays on one clock.

Two mono recordings on one timeline (equal ``started_at``, durations within the
tolerance) become one 16-bit FLAC, channel 0 user and channel 1 agent, at their
common rate; the shorter is padded with silence at its end. Pairs at different
rates, or with more than one channel each, get no rendition.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile

from .audio import Source


def write_rendition(
    sources: list[Path],
    content_types: list[str | None],
    output: Path,
    *,
    tolerance_ms: int,
) -> bool:
    """Interleave the two mono ``sources`` into the stereo FLAC ``output``; whether it
    was written."""

    first, second = (Source(path, kind) for path, kind in zip(sources, content_types, strict=True))
    with first, second:
        if first.rate != second.rate or first.channels != 1 or second.channels != 1:
            return False
        if abs(first.frames - second.frames) * 1000 > tolerance_ms * first.rate:
            return False
        blocks = (first.blocks(), second.blocks())
        with soundfile.SoundFile(
            str(output), "w", samplerate=first.rate, channels=2, format="FLAC", subtype="PCM_16"
        ) as rendition:
            while True:
                parts = [next(stream, None) for stream in blocks]
                if all(part is None for part in parts):
                    break
                frames = max(0 if part is None else len(part) for part in parts)
                block = np.zeros((frames, 2), dtype=np.int16)
                for channel, part in enumerate(parts):
                    if part is not None:
                        block[: len(part), channel] = part[:, 0]
                rendition.write(block)
    return True
