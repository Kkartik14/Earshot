"""Recording analysis: speech runs, noise floor, exact-zero runs, peaks.

Each stored recording gets one analysis job. The server leases it from the catalog
(``storage_analysis``) and runs it in a child process of its own
(``signals.runner``, ``python -m earshot.signals.child``), never in the serving
interpreter. The child decodes the recording with libsndfile, runs the vendored
Silero VAD (ONNX, CPU, one thread) and writes a result document, an audiowaveform
``.dat`` peaks file and, for a mono pair on one timeline, a stereo FLAC rendition.

The analysis needs the ``signals`` extra (numpy, onnxruntime, soundfile). This
package's top level imports only the standard library, so a server without the
extra runs and marks each analysis ``unavailable`` with
``EARSHOT_SIGNALS_EXTRA_REQUIRED``; installing the extra requeues those at the next
start.

The VAD parameters are fixed (``signals.vad``): Silero's 512-sample frames at 16 kHz
(256 at 8 kHz) with its state carried across the whole track, on 0.5 / off 0.35,
minimum speech 250 ms, minimum silence 200 ms, no padding and no hangover. Each
boundary is then refined to 10 ms energy frames within 100 ms, at
``max(-50 dBFS, noise floor + 15 dB)``; its uncertainty is the distance it moved,
at least 10 ms. A change to any of them is a new ``ALGORITHM_VERSION``, and every
analysis computed with an older one is requeued when the server starts.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

# The analysis algorithm a result was computed with. Bump it with any change to
# the VAD, the refinement or the result's shape: a server start requeues older
# results (``storage_analysis.AnalysisStore.requeue``).
ALGORITHM_VERSION = 1

# The Silero VAD model, vendored (MIT, ``model/LICENSE``) from
# https://github.com/snakers4/silero-vad, release v6.2.3,
# ``src/silero_vad/data/silero_vad.onnx``. It is checked at load.
MODEL_PATH = Path(__file__).resolve().parent / "model" / "silero_vad.onnx"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_NAME = "silero-vad v6.2.3"

# What the ``signals`` extra provides.
EXTRA_MODULES = ("numpy", "onnxruntime", "soundfile")

# Stable ``audio_analysis.error_code`` values.
SIGNALS_EXTRA_REQUIRED = "EARSHOT_SIGNALS_EXTRA_REQUIRED"
ANALYSIS_DECODE_FAILED = "EARSHOT_ANALYSIS_DECODE_FAILED"
ANALYSIS_UNSUPPORTED_CODEC = "EARSHOT_ANALYSIS_UNSUPPORTED_CODEC"
ANALYSIS_NO_AUDIO = "EARSHOT_ANALYSIS_NO_AUDIO"
ANALYSIS_SOURCE_MISSING = "EARSHOT_ANALYSIS_SOURCE_MISSING"
ANALYSIS_SOURCE_UNREADABLE = "EARSHOT_ANALYSIS_SOURCE_UNREADABLE"
ANALYSIS_CRASHED = "EARSHOT_ANALYSIS_CRASHED"
ANALYSIS_OUT_OF_MEMORY = "EARSHOT_ANALYSIS_OUT_OF_MEMORY"
ANALYSIS_TIMEOUT = "EARSHOT_ANALYSIS_TIMEOUT"
ANALYSIS_STORE_FAILED = "EARSHOT_ANALYSIS_STORE_FAILED"
ANALYSIS_SPOOL_FAILED = "EARSHOT_ANALYSIS_SPOOL_FAILED"
ANALYSIS_LEASE_EXPIRED = "EARSHOT_ANALYSIS_LEASE_EXPIRED"
ANALYSIS_MODEL_MISMATCH = "EARSHOT_ANALYSIS_MODEL_MISMATCH"


def extra_available() -> bool:
    """Whether the ``signals`` extra is installed (found, not imported)."""

    return all(importlib.util.find_spec(name) is not None for name in EXTRA_MODULES)


def model_sha256(path: Path = MODEL_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(path: str | Path, *, speakers: list[str], peaks_path: str | Path) -> dict[str, Any]:
    """Analyse the recording at ``path`` (one speaker per channel) and write its peaks.

    Needs the ``signals`` extra. Returns the result document ``audio_analysis``
    stores; raises ``signals.audio.DecodeError`` for a body it cannot decode.
    """

    from .analyzer import analyze_file

    return analyze_file(Path(path), speakers=speakers, peaks_path=Path(peaks_path))
