"""Generate the scripted caller WAV (16 kHz mono PCM16) with real Cartesia TTS, and cache it.

    python examples/pipecat_otlp/make_caller_audio.py [--force]

Needs CARTESIA_API_KEY and CARTESIA_VOICE_ID (CARTESIA_MODEL optional). Each line of
CALLER_SCRIPT is followed by a silence gap long enough for the bot to answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
import wave
from pathlib import Path

# Tests check that these words come back in the call's `stt` transcripts.
CALLER_SCRIPT = (
    "Hi, I would like to book a table for two people tomorrow evening.",
    "Seven thirty works for us. Could we sit somewhere quiet near the window?",
    "Perfect, that is everything I needed. Thank you very much, goodbye.",
)
SAMPLE_RATE = 16_000
LEAD_IN_S = 1.0
GAP_S = 7.0  # silence after each line, while the bot replies
CALLER_WAV = Path(".earshot/pipecat_otlp/caller.wav")
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2026-03-01"
DEFAULT_CARTESIA_MODEL = "sonic-3.5"


def _synthesize(text: str) -> bytes:
    payload = {
        "model_id": os.environ.get("CARTESIA_MODEL") or DEFAULT_CARTESIA_MODEL,
        "transcript": text,
        "voice": {"mode": "id", "id": os.environ["CARTESIA_VOICE_ID"]},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
        "language": "en",
    }
    request = urllib.request.Request(
        CARTESIA_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Cartesia-Version": CARTESIA_VERSION,
            "X-API-Key": os.environ["CARTESIA_API_KEY"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _script_digest() -> str:
    layout = {"script": CALLER_SCRIPT, "rate": SAMPLE_RATE, "lead": LEAD_IN_S, "gap": GAP_S}
    return hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()


def ensure_caller_wav(path: Path = CALLER_WAV, *, force: bool = False) -> Path:
    """Return the cached caller WAV, synthesizing it first if missing or stale."""
    sidecar = path.with_suffix(".json")
    cached = not force and path.exists() and sidecar.exists()
    if cached and json.loads(sidecar.read_text()).get("script_sha256") == _script_digest():
        return path

    def silence(seconds: float) -> bytes:
        return bytes(int(SAMPLE_RATE * seconds) * 2)

    pcm, offsets = bytearray(silence(LEAD_IN_S)), []
    for line in CALLER_SCRIPT:
        speech = _synthesize(line)
        offsets.append({"text": line, "start_s": len(pcm) / (2 * SAMPLE_RATE)})
        offsets[-1]["duration_s"] = len(speech) / (2 * SAMPLE_RATE)
        pcm += speech + silence(GAP_S)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    meta = {"script_sha256": _script_digest(), "duration_s": len(pcm) / (2 * SAMPLE_RATE)}
    sidecar.write_text(json.dumps({**meta, "lines": offsets}, indent=2) + "\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the scripted caller WAV.")
    parser.add_argument("--force", action="store_true", help="re-synthesize even if cached")
    parser.add_argument("--out", type=Path, default=CALLER_WAV)
    args = parser.parse_args()
    print(ensure_caller_wav(args.out, force=args.force))
