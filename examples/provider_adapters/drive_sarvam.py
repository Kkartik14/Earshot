"""Drive the Sarvam adapter against the REAL Sarvam streaming STT WebSocket.

Sends real Hindi audio (macOS `say -v Lekha`) so we can empirically answer the
open question about `saaras:v3` + `mode="transcribe"`: does it return Devanagari
(true transcription) or English (translation)? The driver classifies only the
SCRIPT of the returned transcript — it never stores the text — and confirms the
adapter builds a contract-valid incident from live Sarvam bytes.

    set -a && . ./.env && set +a
    .venv2/bin/python examples/provider_adapters/drive_sarvam.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
import wave

import websockets

import earshot
from earshot.adapters.providers import SarvamAdapter

API_KEY = os.environ["SARVAM_API_KEY"]
MODEL = os.environ.get("EARSHOT_SARVAM_MODEL", "saaras:v3")
MODE = os.environ.get("EARSHOT_SARVAM_MODE", "transcribe")
VOICE = os.environ.get("EARSHOT_SAY_VOICE", "Lekha")  # hi_IN
SR = 16000
UTTERANCE = os.environ.get("EARSHOT_SARVAM_TEXT", "नमस्ते, आप कैसे हैं? मेरा नाम अर्जुन है।")
WAV = pathlib.Path(".earshot/provider_adapters/sarvam_input.wav")
OUTPUT = pathlib.Path(".earshot/provider_adapters/sarvam_incident.json")


def _synth_pcm() -> bytes:
    WAV.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["say", "-v", VOICE, "-o", str(WAV), f"--data-format=LEI16@{SR}", UTTERANCE],
        check=True, capture_output=True,
    )
    with wave.open(str(WAV)) as handle:
        return handle.readframes(handle.getnframes())


def _script_of(text: str) -> str:
    if any("ऀ" <= ch <= "ॿ" for ch in text):
        return "devanagari (transcribed)"
    if any(ch.isascii() and ch.isalpha() for ch in text):
        return "latin/english (LIKELY TRANSLATED)"
    return "other/empty"


async def _transcribe() -> list:
    pcm = _synth_pcm()
    epoch = time.monotonic()

    def now_ms() -> float:
        return (time.monotonic() - epoch) * 1000

    adapter = SarvamAdapter(model=MODEL, mode=MODE, language_code="unknown")
    query = urllib.parse.urlencode({
        "language-code": "unknown",
        "model": MODEL,
        "mode": MODE,
        "sample_rate": SR,
        "input_audio_codec": "pcm_s16le",
    })
    url = f"wss://api.sarvam.ai/speech-to-text/ws?{query}"
    updates = []
    counts: dict[str, int] = {}

    async with websockets.connect(
        url, additional_headers={"Api-Subscription-Key": API_KEY}, max_size=8 * 1024 * 1024
    ) as ws:
        # Stream ~1s PCM frames as base64, then flush to finalize.
        frame = SR * 2  # 1 s of s16le mono
        for offset in range(0, len(pcm), frame):
            chunk = pcm[offset : offset + frame]
            await ws.send(json.dumps({
                "audio": {
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "sample_rate": str(SR),
                    "encoding": "audio/wav",
                }
            }))
            await asyncio.sleep(0.2)
        await ws.send(json.dumps({"type": "flush"}))

        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                message = json.loads(raw)
                received_ms = now_ms()
                kind = message.get("type")
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "data":
                    data = message.get("data", {})
                    transcript = data.get("transcript") or ""
                    # Driver-side observation ONLY — never stored in the incident.
                    print(f"[sarvam] mode={MODE!r} lang={data.get('language_code')} "
                          f"p={data.get('language_probability')} script={_script_of(transcript)}")
                    updates.append(adapter.adapt(message, received_at_ms=received_ms))
                elif kind == "events":
                    updates.append(adapter.adapt(message, received_at_ms=received_ms))
                elif kind == "error":
                    print(f"[sarvam] provider error: {message.get('data', {}).get('code')}",
                          file=sys.stderr)
                    updates.append(adapter.adapt(message, received_at_ms=received_ms))
                    break
        except (TimeoutError, websockets.ConnectionClosed):
            pass
    print(f"[sarvam] native messages: {counts}")
    return updates


def main() -> int:
    updates = asyncio.run(_transcribe())
    if not updates:
        print("[sarvam] no updates produced — check message format against the live server",
              file=sys.stderr)
        return 1
    session = earshot.pipeline(session_id="sarvam-real")
    with session.turn() as turn:
        for update in updates:
            update.apply(turn)
    bundle = session.close()

    report = earshot.validate_incident(bundle)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(earshot.encode_incident_json(bundle, indent=2))

    stage = next((op for op in bundle.profile.operations if op.operation_name == "stt"), None)
    stage_attrs = stage.attributes if stage else {}
    measurements = {
        measurement.name: measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    leaked = UTTERANCE.encode() in earshot.encode_incident_protobuf(bundle)

    print("\n" + "=" * 68)
    print("REAL SARVAM STT INCIDENT")
    print("=" * 68)
    print(f"  model / mode          : {MODEL} / {MODE}")
    print(f"  valid v1 contract     : {report.ok}  (errors={len(report.errors)})")
    print(f"  stt language.code     : {stage_attrs.get('earshot.language.code', '-')}")
    print(f"  stt mode label        : {stage_attrs.get('earshot.stt.mode', '-')}")
    print(f"  processing_latency ms : {measurements.get('sarvam.stt.processing_latency')}")
    print(f"  audio_duration s      : {measurements.get('sarvam.stt.audio_duration')}")
    print(f"  transcript retained?  : {'YES (LEAK!)' if leaked else 'no'}")
    print(f"  artifact              : {OUTPUT}")
    print("=" * 68)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
