"""Drive the Deepgram adapter against the REAL Deepgram streaming STT WebSocket.

Synthesizes an utterance with macOS `say`, streams the PCM to Deepgram in real
time, and feeds every native `Results`/`SpeechStarted`/`UtteranceEnd` message into
`DeepgramAdapter` to build a contract-valid incident from live transcription.

    set -a && . ./.env && set +a
    .venv2/bin/python examples/provider_adapters/drive_deepgram.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time
import wave

import websockets

import earshot
from earshot.adapters.providers import DeepgramAdapter

API_KEY = os.environ["DEEPGRAM_API_KEY"]
MODEL = os.environ.get("EARSHOT_DEEPGRAM_MODEL", "nova-3")
SR = 24000
UTTERANCE = "What is the capital of France? Please answer in one short word."
WAV = pathlib.Path(".earshot/provider_adapters/deepgram_input.wav")
OUTPUT = pathlib.Path(".earshot/provider_adapters/deepgram_incident.json")


def _synth_wav() -> bytes:
    WAV.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["say", "-o", str(WAV), f"--data-format=LEI16@{SR}", UTTERANCE],
        check=True, capture_output=True,
    )
    with wave.open(str(WAV)) as handle:
        return handle.readframes(handle.getnframes())


async def _transcribe() -> list:
    pcm = _synth_wav()
    epoch = time.monotonic()

    def now_ms() -> float:
        return (time.monotonic() - epoch) * 1000

    adapter = DeepgramAdapter(model=MODEL)
    query = (
        f"model={MODEL}&encoding=linear16&sample_rate={SR}&channels=1"
        "&interim_results=true&punctuate=true&vad_events=true"
        "&utterance_end_ms=1000&endpointing=300"
    )
    url = f"wss://api.deepgram.com/v1/listen?{query}"
    updates = []
    counts: dict[str, int] = {}

    async with websockets.connect(
        url, additional_headers={"Authorization": f"Token {API_KEY}"}, max_size=8 * 1024 * 1024
    ) as ws:
        async def send_audio() -> None:
            frame = int(SR * 0.1) * 2  # 100 ms of s16le mono
            for offset in range(0, len(pcm), frame):
                await ws.send(pcm[offset : offset + frame])
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "CloseStream"}))

        sender = asyncio.create_task(send_audio())
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                message = json.loads(raw)
                received_ms = now_ms()
                kind = message.get("type")
                counts[kind] = counts.get(kind, 0) + 1
                if kind in {"Results", "SpeechStarted", "UtteranceEnd"}:
                    updates.append(adapter.adapt(message, received_at_ms=received_ms))
                elif kind == "Metadata":
                    break
        except (TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            sender.cancel()
    print(f"[deepgram] native messages: {counts}")
    return updates


def main() -> int:
    updates = asyncio.run(_transcribe())
    session = earshot.pipeline(session_id="deepgram-real")
    applied = 0
    with session.turn() as turn:
        for update in updates:
            try:
                if update.apply(turn):
                    applied += 1
            except ValueError as error:  # e.g. a Flux-ordering guard on a non-Flux stream
                print(f"[deepgram] skipped an update: {error}", file=sys.stderr)
    bundle = session.close()

    report = earshot.validate_incident(bundle)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(earshot.encode_incident_json(bundle, indent=2))

    event_names = [event.event_name for event in bundle.profile.events]
    measurement_names = sorted({
        measurement.name
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    })
    leaked = UTTERANCE.encode() in earshot.encode_incident_protobuf(bundle)

    print("\n" + "=" * 68)
    print("REAL DEEPGRAM STT INCIDENT")
    print("=" * 68)
    print(f"  model                 : {MODEL}")
    print(f"  valid v1 contract     : {report.ok}  (errors={len(report.errors)})")
    print(f"  updates applied       : {applied}")
    print(f"  operations            : {[op.operation_name for op in bundle.profile.operations]}")
    print(f"  events                : {event_names}")
    print(f"  measurements          : {measurement_names}")
    print(f"  transcript retained?  : {'YES (LEAK!)' if leaked else 'no'}")
    print(f"  artifact              : {OUTPUT}")
    print("=" * 68)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
