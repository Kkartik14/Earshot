"""Generate the synthesized caller recording used by both real-call examples.

Each scripted sentence is followed by a silence long enough for the bot to answer.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import tempfile
import urllib.request
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CALLER_SCRIPT = (
    "Hi, I would like to book a table for two people tomorrow evening.",
    "Seven thirty works for us. Could we sit somewhere quiet near the window?",
    "Perfect, that is everything I needed. Thank you very much, goodbye.",
)
SAMPLE_RATE = 16_000
LEAD_IN_S = 1.0
GAP_S = 7.0
CALLER_WAV = Path(".earshot/pipecat_otlp/caller.wav")
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2026-03-01"
DEFAULT_CARTESIA_MODEL = "sonic-3.6"
CARTESIA_VOICE_MODE = "id"


def _synthesize(text: str) -> bytes:
    payload = {
        "model_id": os.environ.get("CARTESIA_MODEL") or DEFAULT_CARTESIA_MODEL,
        "transcript": text,
        "voice": {"mode": CARTESIA_VOICE_MODE, "id": os.environ["CARTESIA_VOICE_ID"]},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "language": "en",
        "voice_mode": CARTESIA_VOICE_MODE,
    }
    request = urllib.request.Request(
        CARTESIA_URL,
        data=json.dumps(payload).encode(),
        headers={
            "X-API-Key": os.environ["CARTESIA_API_KEY"],
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _cache_digest() -> str:
    settings = {
        "script": CALLER_SCRIPT,
        "sample_rate": SAMPLE_RATE,
        "lead_in_s": LEAD_IN_S,
        "gap_s": GAP_S,
        "url": CARTESIA_URL,
        "version": CARTESIA_VERSION,
        "language": "en",
        "model": os.environ.get("CARTESIA_MODEL") or DEFAULT_CARTESIA_MODEL,
        "voice_id": os.environ.get("CARTESIA_VOICE_ID"),
        "voice_mode": CARTESIA_VOICE_MODE,
        "format": "raw-pcm-s16le-mono",
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def caller_wav_metadata_path(path: Path) -> Path:
    """Return the timeline sidecar path for a generated caller WAV."""
    return path.with_suffix(".json")


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_cached_wav(path: Path, metadata_path: Path, digest: str) -> bool:
    try:
        metadata = json.loads(metadata_path.read_text())
        lines = metadata.get("lines") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("cache_sha256") != digest
            or not isinstance(metadata.get("wav_sha256"), str)
            or not _finite_number(metadata.get("duration_s"))
            or metadata["duration_s"] <= 0
            or not isinstance(lines, list)
            or len(lines) != len(CALLER_SCRIPT)
            or not all(
                isinstance(line, dict)
                and line.get("text") == text
                and _finite_number(line.get("start_s"))
                and _finite_number(line.get("duration_s"))
                and line["start_s"] >= 0
                and line["duration_s"] > 0
                for text, line in zip(CALLER_SCRIPT, lines, strict=True)
            )
        ):
            return False
        expected_start = LEAD_IN_S
        tolerance = 1 / SAMPLE_RATE
        for line in lines:
            if not math.isclose(line["start_s"], expected_start, rel_tol=0, abs_tol=tolerance):
                return False
            expected_start = line["start_s"] + line["duration_s"] + GAP_S

        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            if not (
                wav.getnchannels() == 1
                and wav.getsampwidth() == 2
                and wav.getframerate() == SAMPLE_RATE
                and frames > 0
                and path.stat().st_size == 44 + frames * 2
            ):
                return False
        wav_duration = frames / SAMPLE_RATE
        return (
            math.isclose(metadata["duration_s"], wav_duration, rel_tol=0, abs_tol=tolerance)
            and math.isclose(expected_start, wav_duration, rel_tol=0, abs_tol=tolerance)
            and hashlib.sha256(path.read_bytes()).hexdigest() == metadata["wav_sha256"]
        )
    except (EOFError, OSError, UnicodeError, ValueError, wave.Error):
        return False


def _cache_output_path(path: Path | None, digest: str) -> Path:
    if path is None:
        path = CALLER_WAV.with_name(f"{CALLER_WAV.stem}-{digest}{CALLER_WAV.suffix}")
    if path.suffix != ".wav":
        raise ValueError("caller WAV output path must have a .wav extension")
    return path


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    """Serialize cache users across processes while keeping each pair in sync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ensure_caller_wav_unlocked(path: Path, metadata_path: Path, digest: str, force: bool) -> Path:
    if (
        not force
        and path.is_file()
        and metadata_path.is_file()
        and _valid_cached_wav(path, metadata_path, digest)
    ):
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    # A failed regeneration must not leave metadata that blesses stale audio.
    metadata_path.unlink(missing_ok=True)
    pcm = bytearray(bytes(int(SAMPLE_RATE * LEAD_IN_S) * 2))
    lines = []
    silence = bytes(int(SAMPLE_RATE * GAP_S) * 2)
    for text in CALLER_SCRIPT:
        speech = _synthesize(text)
        if not speech or len(speech) % 2:
            raise ValueError("Cartesia returned empty or incomplete 16-bit PCM audio")
        lines.append(
            {
                "text": text,
                "start_s": len(pcm) / (2 * SAMPLE_RATE),
                "duration_s": len(speech) / (2 * SAMPLE_RATE),
            }
        )
        pcm.extend(speech)
        pcm.extend(silence)

    temporary_wav: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=path.parent, delete=False) as temporary:
            temporary_wav = Path(temporary.name)
        with wave.open(str(temporary_wav), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        wav_sha256 = hashlib.sha256(temporary_wav.read_bytes()).hexdigest()
        os.replace(temporary_wav, path)
    finally:
        if temporary_wav is not None:
            temporary_wav.unlink(missing_ok=True)

    metadata = {
        "cache_sha256": digest,
        "wav_sha256": wav_sha256,
        "duration_s": len(pcm) / (2 * SAMPLE_RATE),
        "lines": lines,
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=metadata_path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(json.dumps(metadata, indent=2) + "\n")
        os.replace(temporary_path, metadata_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def ensure_caller_wav(path: Path | None = None, *, force: bool = False) -> Path:
    """Return a valid cached caller WAV, synthesizing it when absent or stale."""
    digest = _cache_digest()
    wav_path = _cache_output_path(path, digest)
    metadata_path = caller_wav_metadata_path(wav_path)
    with _cache_lock(wav_path):
        return _ensure_caller_wav_unlocked(wav_path, metadata_path, digest, force)


def load_caller_wav_snapshot(
    path: Path | None = None, *, force: bool = False
) -> tuple[bytes, dict[str, object]]:
    """Return audio bytes and timeline metadata from one locked cache generation."""
    digest = _cache_digest()
    wav_path = _cache_output_path(path, digest)
    metadata_path = caller_wav_metadata_path(wav_path)
    with _cache_lock(wav_path):
        _ensure_caller_wav_unlocked(wav_path, metadata_path, digest, force)
        return wav_path.read_bytes(), json.loads(metadata_path.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the scripted caller WAV.")
    parser.add_argument("--force", action="store_true", help="re-synthesize even if cached")
    parser.add_argument("--out", type=Path, help="WAV output path")
    args = parser.parse_args()
    print(ensure_caller_wav(args.out, force=args.force))
