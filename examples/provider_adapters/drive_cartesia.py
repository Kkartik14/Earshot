"""Drive the Cartesia adapter against the REAL Cartesia TTS WebSocket.

Synthesizes one utterance with a real voice id, feeds every native chunk into
`CartesiaAdapter`, and builds a contract-valid incident from live provider bytes.
No provider SDK: we speak the raw WebSocket and hand the messages to the adapter,
exactly as a production pipeline would.

    set -a && . ./.env && set +a
    .venv2/bin/python examples/provider_adapters/drive_cartesia.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time

import websockets

import earshot
from earshot.adapters.providers import CartesiaAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256

API_KEY = os.environ["CARTESIA_API_KEY"]
VOICE_ID = os.environ.get("EARSHOT_CARTESIA_VOICE_ID") or os.environ["CARTESIA_VOICE_ID"]
MODEL = os.environ.get("EARSHOT_CARTESIA_MODEL", "sonic-2")
VERSION = os.environ.get("EARSHOT_CARTESIA_VERSION", "2024-11-13")
TRANSCRIPT = "The quick brown fox jumps over the lazy dog."
OUTPUT = pathlib.Path(".earshot/provider_adapters/cartesia_incident.json")


async def _synthesize() -> list:
    epoch = time.monotonic()

    def now_ms() -> float:
        return (time.monotonic() - epoch) * 1000

    adapter = CartesiaAdapter(model=MODEL, voice=VOICE_ID)
    url = f"wss://api.cartesia.ai/tts/websocket?cartesia_version={VERSION}"
    updates = []
    chunk_count = 0
    async with websockets.connect(
        url, additional_headers={"X-API-Key": API_KEY}, max_size=16 * 1024 * 1024
    ) as ws:
        request = {
            "model_id": MODEL,
            "transcript": TRANSCRIPT,
            "voice": {"mode": "id", "id": VOICE_ID},
            "language": "en",
            "context_id": "earshot-cartesia-real",
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 44100},
            "add_timestamps": True,
            "continue": False,
        }
        sent_ms = now_ms()
        await ws.send(json.dumps(request))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            message = json.loads(raw)
            received_ms = now_ms()
            kind = message.get("type")
            if kind == "chunk":
                chunk_count += 1
                updates.append(
                    adapter.adapt(message, received_at_ms=received_ms, request_sent_at_ms=sent_ms)
                )
            elif kind in {"timestamps", "phoneme_timestamps", "flush_done"}:
                updates.append(adapter.adapt(message, received_at_ms=received_ms))
            elif kind == "done":
                updates.append(adapter.adapt(message, received_at_ms=received_ms))
                break
            elif kind == "error":
                # Surface the provider error class without leaking its message.
                print(f"[cartesia] provider error status={message.get('status_code')} "
                      f"code={message.get('error_code')!r}", file=sys.stderr)
                updates.append(adapter.adapt(message, received_at_ms=received_ms))
                break
    print(f"[cartesia] received {chunk_count} real audio chunks")
    return updates


def main() -> int:
    updates = asyncio.run(_synthesize())
    session = earshot.pipeline(session_id="cartesia-real")
    with session.turn() as turn:
        for update in updates:
            update.apply(turn)
    bundle = session.close()

    report = earshot.validate_incident(bundle)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(earshot.encode_incident_json(bundle, indent=2))

    measurements = {
        measurement.name: measurement
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
    }
    step_times = [
        measurement.value
        for sample in bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name == "cartesia.tts.step_time"
    ]
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=time.time_ns()
    )
    generated = analysis.projections.turns[0].metrics.generated_response_latency

    print("\n" + "=" * 68)
    print("REAL CARTESIA TTS INCIDENT")
    print("=" * 68)
    print(f"  voice id (real)       : {VOICE_ID}")
    print(f"  model                 : {MODEL}")
    print(f"  valid v1 contract     : {report.ok}  (errors={len(report.errors)})")
    print(f"  operations            : {[op.operation_name for op in bundle.profile.operations]}")
    leaked = TRANSCRIPT.encode() in earshot.encode_incident_protobuf(bundle)
    print(f"  step_time samples      : {len(step_times)} chunks "
          f"(min={min(step_times) if step_times else '-'} "
          f"max={max(step_times) if step_times else '-'} ms)")
    if "earshot.tts.ttfb" in measurements:
        print(f"  app TTFB (estimated)   : {measurements['earshot.tts.ttfb'].value:.0f} ms")
    print(f"  generated_response     : {generated.value} "
          f"({generated.availability}, {generated.confidence})")
    print(f"  transcript retained?   : {'YES (LEAK!)' if leaked else 'no'}")
    print(f"  artifact               : {OUTPUT}")
    print("=" * 68)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
