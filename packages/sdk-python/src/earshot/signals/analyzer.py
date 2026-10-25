"""One recording's analysis: decode once, scan every channel, write the result.

``analyze_file`` streams the recording block by block. At the source rate it finds
each channel's exact-zero runs and the peaks; at the analysis rate (16 kHz, or 8 kHz
for 8 kHz telephony) it runs Silero and the 10 ms energy frames. At the end each
channel's runs are segmented and refined (``signals.vad``). All times in the result
are integer milliseconds from the recording's first sample.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import ALGORITHM_VERSION, MODEL_NAME, MODEL_SHA256
from .audio import DecodeError, Peaks, Resampler, Source, ZeroRuns
from .peaks import read_header
from .vad import (
    ZERO_RUN_MIN_MS,
    EnergyFrames,
    SileroModel,
    SpeechProbabilities,
    analysis_rate,
    noise_floor,
    parameters,
    refine,
    refine_threshold,
    segment,
    zero_frame_mask,
)

ANALYSIS_CHANNEL_MISMATCH = "EARSHOT_ANALYSIS_CHANNEL_MISMATCH"


def _ms(samples: int, rate: int) -> int:
    """``samples`` at ``rate`` in milliseconds, to the nearest (halves up)."""

    return (samples * 2000 + rate) // (2 * rate)


def analyze_file(
    path: Path,
    *,
    speakers: list[str],
    peaks_path: Path,
    content_type: str | None = None,
    model: SileroModel | None = None,
) -> dict[str, Any]:
    """Analyse the recording at ``path``; write its peaks to ``peaks_path``."""

    model = model or SileroModel()
    with Source(path, content_type) as source:
        if len(speakers) != source.channels:
            raise DecodeError(ANALYSIS_CHANNEL_MISMATCH)
        rate = analysis_rate(source.rate)
        channels = range(source.channels)
        resamplers = (
            None if source.rate == rate else [Resampler(source.rate, rate) for _ in channels]
        )
        zeros = [ZeroRuns(source.rate, ZERO_RUN_MIN_MS) for _ in channels]
        speech = [SpeechProbabilities(model, rate) for _ in channels]
        energy = [EnergyFrames(rate) for _ in channels]
        analysed = [0 for _ in channels]
        peaks = Peaks(source.rate, source.channels)
        frames = 0

        def scan(channel: int, signal: np.ndarray) -> None:
            speech[channel].feed(signal)
            energy[channel].feed(signal)
            analysed[channel] += len(signal)

        for block in source.blocks():
            frames += len(block)
            peaks.feed(block)
            for channel in channels:
                column = np.ascontiguousarray(block[:, channel])
                zeros[channel].feed(column)
                scan(
                    channel,
                    column.astype(np.float32) / 32768.0
                    if resamplers is None
                    else resamplers[channel].feed(column),
                )
        if resamplers is not None:
            for channel in channels:
                scan(channel, resamplers[channel].flush())
        source_rate, source_channels = source.rate, source.channels
    if frames == 0:
        raise DecodeError("EARSHOT_ANALYSIS_NO_AUDIO")
    peaks_file = peaks.finish()
    peaks_path.write_bytes(peaks_file)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "vad": {"model": MODEL_NAME, "model_sha256": MODEL_SHA256, **parameters()},
        "source": {
            "sample_rate": source_rate,
            "channels": source_channels,
            "frames": frames,
            "duration_ms": round(frames * 1000 / source_rate),
            "analysis_rate": rate,
        },
        "channels": [
            _channel(
                index,
                speakers[index],
                rate=rate,
                source_rate=source_rate,
                total_samples=analysed[index],
                probabilities=speech[index].finish(),
                levels=energy[index].finish(),
                zero_runs=zeros[index].finish(),
            )
            for index in channels
        ],
        "peaks": peaks.describe(read_header(peaks_file).length),
    }


def _channel(
    index: int,
    speaker: str,
    *,
    rate: int,
    source_rate: int,
    total_samples: int,
    probabilities: list[float],
    levels: np.ndarray,
    zero_runs: list[tuple[int, int]],
) -> dict[str, Any]:
    zero_ms = [(_ms(start, source_rate), _ms(end, source_rate)) for start, end in zero_runs]
    floor = noise_floor(levels, zero_frame_mask(len(levels), zero_ms))
    threshold = refine_threshold(floor)
    vad_runs = [
        (_ms(start, rate), _ms(end, rate))
        for start, end in segment(probabilities, rate=rate, total_samples=total_samples)
    ]
    runs = refine(vad_runs, levels, threshold)
    return {
        "index": index,
        "speaker": speaker,
        "noise_floor_dbfs": None if floor is None else round(floor, 1),
        "threshold_dbfs": round(threshold, 1),
        "speech_ms": sum(run.end_ms - run.start_ms for run in runs),
        "runs": [run.document() for run in runs],
        "zero_runs": [{"start_ms": start, "end_ms": end} for start, end in zero_ms],
    }
