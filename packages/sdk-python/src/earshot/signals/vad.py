"""Speech runs from Silero VAD, refined to 10 ms energy frames, with their uncertainty.

**Silero.** The vendored ONNX model (``MODEL_PATH``, its sha256 checked at load) runs
on the CPU execution provider with one thread, on 512-sample frames at 16 kHz (256 at
8 kHz) plus the model's 64- (32-) sample context, with its state carried across the
whole track: the same input on the same platform gives the same probabilities.

**Segmentation** follows Silero's ``get_speech_timestamps`` without its padding: a
run starts at the first frame at or above ``ON_THRESHOLD`` and ends at the first frame
below ``OFF_THRESHOLD`` that starts at least ``MIN_SILENCE_MS`` of quiet; a run of
``MIN_SPEECH_MS`` or less is dropped. No pad and no hangover: either would bias every
gap measured from these runs by its length, which is a bias, not an uncertainty.

**Refinement.** Silero's boundaries sit on its 32 ms frames and lag the audio by a
frame or two. Each is snapped to the 10 ms energy frames within ``REFINE_WINDOW_MS``:
an onset to the first frame louder than the threshold, an end to the end of the last
such frame. The threshold is ``max(REFINE_FLOOR_DBFS, noise floor +
REFINE_ABOVE_NOISE_DB)``, where the noise floor is the 10th percentile of the track's
10 ms frames outside its exact-zero runs (recorder padding is not room noise).

**Uncertainty.** A boundary's sigma is ``max(SIGMA_FLOOR_MS, |Silero - refined|)``:
the two estimates disagree by about that much. A boundary with no frame above the
threshold in its window keeps Silero's value with sigma ``UNREFINED_SIGMA_MS``: the
energy edge, if any, lies outside the window, so the two disagree by at least that.
A gap between two runs has the uncertainty ``sqrt(sigma_a**2 + sigma_b**2)``.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import MODEL_PATH, MODEL_SHA256

ANALYSIS_RATES = (8000, 16000)
FRAME_SAMPLES = {16000: 512, 8000: 256}
CONTEXT_SAMPLES = {16000: 64, 8000: 32}
ON_THRESHOLD = 0.5
OFF_THRESHOLD = 0.35
MIN_SPEECH_MS = 250
MIN_SILENCE_MS = 200
REFINE_FRAME_MS = 10
REFINE_WINDOW_MS = 100
REFINE_FLOOR_DBFS = -50.0
REFINE_ABOVE_NOISE_DB = 15.0
NOISE_FLOOR_PERCENTILE = 10
SIGMA_FLOOR_MS = 10
UNREFINED_SIGMA_MS = REFINE_WINDOW_MS
# Exact-zero runs this long are recorder padding (and audio anchors).
ZERO_RUN_MIN_MS = 50


class ModelMismatchError(Exception):
    """The vendored model is not the file this release was built and tested with."""


def analysis_rate(source_rate: int) -> int:
    """The rate the VAD runs at: 8 kHz telephony natively, everything else at 16 kHz."""

    return 8000 if source_rate == 8000 else 16000


def parameters() -> dict[str, object]:
    """The VAD settings a result records."""

    return {
        "on_threshold": ON_THRESHOLD,
        "off_threshold": OFF_THRESHOLD,
        "min_speech_ms": MIN_SPEECH_MS,
        "min_silence_ms": MIN_SILENCE_MS,
        "pad_ms": 0,
        "refine_frame_ms": REFINE_FRAME_MS,
        "refine_window_ms": REFINE_WINDOW_MS,
        "refine_floor_dbfs": REFINE_FLOOR_DBFS,
        "refine_above_noise_db": REFINE_ABOVE_NOISE_DB,
        "noise_floor_percentile": NOISE_FLOOR_PERCENTILE,
        "sigma_floor_ms": SIGMA_FLOOR_MS,
        "unrefined_sigma_ms": UNREFINED_SIGMA_MS,
        "zero_run_min_ms": ZERO_RUN_MIN_MS,
    }


class SileroModel:
    """The Silero ONNX session: CPU only, one thread, sha256-checked."""

    def __init__(self, path: Path = MODEL_PATH, expected_sha256: str = MODEL_SHA256) -> None:
        # A self-hosted server sends nothing anywhere it was not configured to. Some
        # onnxruntime builds (1.31 on Linux) start a usage-telemetry client on import
        # unless this is set, and read it only then.
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        import onnxruntime

        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ModelMismatchError("the vendored Silero model does not match its sha256")
        onnxruntime.disable_telemetry_events()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        self.session = onnxruntime.InferenceSession(
            data, sess_options=options, providers=["CPUExecutionProvider"]
        )


class SpeechProbabilities:
    """One channel's Silero speech probability per frame, fed block by block."""

    def __init__(self, model: SileroModel, rate: int) -> None:
        self.session = model.session
        self.rate = rate
        self.frame = FRAME_SAMPLES[rate]
        self._rate = np.array(rate, dtype=np.int64)
        self._context = np.zeros((1, CONTEXT_SAMPLES[rate]), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self.probabilities: list[float] = []

    def _run(self, frame: np.ndarray) -> None:
        window = np.concatenate((self._context, frame[None, :]), axis=1)
        output, self._state = self.session.run(
            None, {"input": window, "state": self._state, "sr": self._rate}
        )
        self.probabilities.append(float(output[0, 0]))
        self._context = window[:, -self._context.shape[1] :]

    def feed(self, samples: np.ndarray) -> None:
        data = np.concatenate((self._pending, samples)) if len(self._pending) else samples
        whole = len(data) // self.frame * self.frame
        for start in range(0, whole, self.frame):
            self._run(np.ascontiguousarray(data[start : start + self.frame], dtype=np.float32))
        self._pending = data[whole:].astype(np.float32, copy=True)

    def finish(self) -> list[float]:
        if len(self._pending):
            last = np.zeros(self.frame, dtype=np.float32)
            last[: len(self._pending)] = self._pending
            self._run(last)
            self._pending = self._pending[:0]
        return self.probabilities


class EnergyFrames:
    """One channel's 10 ms frames' level in dBFS (``-inf`` for digital silence)."""

    def __init__(self, rate: int) -> None:
        self.size = rate * REFINE_FRAME_MS // 1000
        self._parts: list[np.ndarray] = []
        self._pending = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> None:
        data = np.concatenate((self._pending, samples)) if len(self._pending) else samples
        whole = len(data) // self.size * self.size
        if whole:
            frames = data[:whole].astype(np.float64).reshape(-1, self.size)
            self._parts.append(np.mean(frames * frames, axis=1))
        self._pending = data[whole:].astype(np.float32, copy=True)

    def finish(self) -> np.ndarray:
        if len(self._pending):
            rest = self._pending.astype(np.float64)
            self._parts.append(np.array([np.mean(rest * rest)]))
            self._pending = self._pending[:0]
        power = np.concatenate(self._parts) if self._parts else np.zeros(0)
        with np.errstate(divide="ignore"):
            return 10 * np.log10(power)


def segment(probabilities: list[float], *, rate: int, total_samples: int) -> list[tuple[int, int]]:
    """Speech runs ``[start, end)`` in samples at ``rate``, from Silero's probabilities."""

    frame = FRAME_SAMPLES[rate]
    min_speech = rate * MIN_SPEECH_MS // 1000
    min_silence = rate * MIN_SILENCE_MS // 1000
    runs: list[tuple[int, int]] = []
    triggered = False
    start = quiet_from = 0
    for index, probability in enumerate(probabilities):
        position = index * frame
        if probability >= ON_THRESHOLD and quiet_from:
            quiet_from = 0
        if probability >= ON_THRESHOLD and not triggered:
            triggered = True
            start = position
            continue
        if probability < OFF_THRESHOLD and triggered:
            if not quiet_from:
                quiet_from = position
            if position - quiet_from < min_silence:
                continue
            if quiet_from - start > min_speech:
                runs.append((start, quiet_from))
            triggered = False
            quiet_from = 0
    if triggered and total_samples - start > min_speech:
        runs.append((start, total_samples))
    return runs


def zero_frame_mask(frame_count: int, zero_runs_ms: list[tuple[int, int]]) -> np.ndarray:
    """Which 10 ms frames overlap an exact-zero run."""

    marks = np.zeros(frame_count + 1, dtype=np.int64)
    for start, end in zero_runs_ms:
        first = max(0, start // REFINE_FRAME_MS)
        last = min(frame_count, -(-end // REFINE_FRAME_MS))
        if first < last:
            marks[first] += 1
            marks[last] -= 1
    return np.cumsum(marks[:-1]) > 0


def noise_floor(levels: np.ndarray, padding: np.ndarray) -> float | None:
    """The track's noise floor in dBFS, or ``None`` when it holds no non-zero frame
    outside its padding."""

    usable = levels[np.isfinite(levels) & ~padding]
    if usable.size == 0:
        return None
    return float(np.percentile(usable, NOISE_FLOOR_PERCENTILE))


def refine_threshold(floor: float | None) -> float:
    return (
        REFINE_FLOOR_DBFS
        if floor is None
        else max(REFINE_FLOOR_DBFS, floor + REFINE_ABOVE_NOISE_DB)
    )


@dataclass(frozen=True, slots=True)
class SpeechRun:
    """One speech run in milliseconds from the recording's first sample."""

    start_ms: int
    end_ms: int
    start_sigma_ms: int
    end_sigma_ms: int
    vad_start_ms: int
    vad_end_ms: int

    def document(self) -> dict[str, int]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_sigma_ms": self.start_sigma_ms,
            "end_sigma_ms": self.end_sigma_ms,
            "vad_start_ms": self.vad_start_ms,
            "vad_end_ms": self.vad_end_ms,
        }


def _sigma(vad_ms: int, refined_ms: int | None) -> int:
    if refined_ms is None:
        return UNREFINED_SIGMA_MS
    return max(SIGMA_FLOOR_MS, abs(vad_ms - refined_ms))


def refine(runs_ms: list[tuple[int, int]], levels: np.ndarray, threshold: float) -> list[SpeechRun]:
    """Snap each run's boundaries to the energy frames around them (module docstring)."""

    count = len(levels)
    loud = levels > threshold
    refined: list[SpeechRun] = []
    for vad_start, vad_end in runs_ms:
        first = max(0, math.ceil((vad_start - REFINE_WINDOW_MS) / REFINE_FRAME_MS))
        last = min(count - 1, (vad_start + REFINE_WINDOW_MS) // REFINE_FRAME_MS)
        hits = np.flatnonzero(loud[first : last + 1]) if first <= last else np.zeros(0)
        start = None if hits.size == 0 else int(first + hits[0]) * REFINE_FRAME_MS
        first = max(0, math.ceil((vad_end - REFINE_WINDOW_MS) / REFINE_FRAME_MS) - 1)
        last = min(count - 1, (vad_end + REFINE_WINDOW_MS) // REFINE_FRAME_MS - 1)
        hits = np.flatnonzero(loud[first : last + 1]) if first <= last else np.zeros(0)
        end = None if hits.size == 0 else int(first + hits[-1] + 1) * REFINE_FRAME_MS
        begin, finish = vad_start if start is None else start, vad_end if end is None else end
        if begin >= finish:
            begin, finish, start, end = vad_start, vad_end, None, None
        refined.append(
            SpeechRun(
                start_ms=begin,
                end_ms=finish,
                start_sigma_ms=_sigma(vad_start, start),
                end_sigma_ms=_sigma(vad_end, end),
                vad_start_ms=vad_start,
                vad_end_ms=vad_end,
            )
        )
    # Refinement moves each edge by at most the window; keep runs apart and in order.
    for index in range(len(refined) - 1):
        current, following = refined[index], refined[index + 1]
        if current.end_ms > following.start_ms:
            refined[index] = SpeechRun(
                start_ms=current.start_ms,
                end_ms=max(current.start_ms + 1, following.start_ms),
                start_sigma_ms=current.start_sigma_ms,
                end_sigma_ms=current.end_sigma_ms,
                vad_start_ms=current.vad_start_ms,
                vad_end_ms=current.vad_end_ms,
            )
    return refined
