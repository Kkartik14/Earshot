"""Capture a real ElevenLabs Agents conversation for the integrity fixtures.

Subcommands (raw output goes to ``.earshot/raw-captures/elevenlabs/``, git-ignored):

``survey``
    Free GETs only: ``/v1/convai/agents`` and ``/v1/convai/conversations``. Prints
    counts, statuses and durations, never IDs, names or keys.

``fetch --index N``
    ``GET /v1/convai/conversations/{id}`` plus ``/audio`` for the N-th surveyed
    conversation. Its audio may hold human speech, so it stays raw-only.

``converse``
    The one owner-approved conversation: creates a dedicated test agent, then drives
    it over the official conversation websocket with a synthetic (Cartesia TTS) caller
    read from the cache ``.earshot/pipecat_otlp/caller.wav``. Both sides are
    synthetic. It then waits for the conversation to finish processing and fetches
    the conversation JSON and ``/audio``.

Keys come from ``--env`` (default ``../TVIC/.env``): ``ELEVENLABS_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import urllib.parse
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_capture import (
    DEFAULT_ENV,
    RAW_ROOT,
    ROOT,
    HttpError,
    load_keys,
    redact,
    request,
    write_json,
    write_response,
)

API = "https://api.elevenlabs.io"
RAW = RAW_ROOT / "elevenlabs"
CALLER_CACHE = ROOT / ".earshot" / "pipecat_otlp"
AGENT_NAME = "earshot-fixture-agent"
AGENT_PROMPT = (
    "You are a restaurant booking line used for an automated test. Reply in one short "
    "sentence of at most twelve words. Never ask for names, phone numbers or emails."
)
FIRST_MESSAGE = "Hello, thanks for calling the restaurant. How can I help?"
MAX_DURATION_S = 90
SAMPLE_RATE = 16_000
AUDIO_FORMAT = "pcm_16000"
CHUNK_S = 0.1


def _get(path: str, key: str) -> tuple[int, dict[str, str], bytes]:
    return request("GET", API + path, headers={"xi-api-key": key})


def _get_json(path: str, key: str) -> Any:
    return json.loads(_get(path, key)[2])


def survey(key: str) -> None:
    out = RAW / "survey"
    status, headers, body = _get("/v1/convai/agents?page_size=100", key)
    write_response(out, "agents.json", status, headers, body)
    agents = json.loads(body).get("agents", [])
    status, headers, body = _get("/v1/convai/conversations?page_size=100", key)
    write_response(out, "conversations.json", status, headers, body)
    listing = json.loads(body)
    conversations = listing.get("conversations", [])
    agent_index = {a.get("agent_id"): i for i, a in enumerate(agents)}
    print(f"agents: {len(agents)}")
    print(f"conversations: {len(conversations)} (has_more={listing.get('has_more')})")
    print(f"conversation list item keys: {sorted(conversations[0]) if conversations else []}")
    for i, conv in enumerate(conversations):
        print(
            f"  [{i}] status={conv.get('status')} duration_s={conv.get('call_duration_secs')} "
            f"messages={conv.get('message_count')} "
            f"agent_index={agent_index.get(conv.get('agent_id'))}"
        )


def _fetch_conversation(key: str, conversation_id: str, out: Path) -> dict[str, Any]:
    cid = urllib.parse.quote(conversation_id, safe="")
    status, headers, body = _get(f"/v1/convai/conversations/{cid}", key)
    write_response(out, "conversation.json", status, headers, body)
    conv = json.loads(body)
    if conv.get("has_audio"):
        status, headers, body = _get(f"/v1/convai/conversations/{cid}/audio", key)
        write_response(out, "audio.bin", status, headers, body)
        print(f"audio: {len(body)} bytes, content-type={headers.get('content-type')}")
    try:
        status, headers, body = _get(f"/v1/convai/conversations/{cid}?format=opentelemetry", key)
        write_response(out, "conversation.otel.json", status, headers, body)
    except HttpError as exc:
        print(f"opentelemetry format unavailable: {exc}")
    print(
        f"conversation: status={conv.get('status')} has_audio={conv.get('has_audio')} "
        f"turns={len(conv.get('transcript') or [])}"
    )
    return conv


def fetch(key: str, index: int) -> None:
    listing = json.loads((RAW / "survey" / "conversations.json").read_text())
    conversation_id = listing["conversations"][index]["conversation_id"]
    _fetch_conversation(key, conversation_id, RAW / f"existing-{index}")


def _caller_lines() -> list[bytes]:
    """PCM16 mono 16 kHz bytes of each line of the cached synthetic caller WAV."""
    meta = json.loads((CALLER_CACHE / "caller.json").read_text())
    with wave.open(str(CALLER_CACHE / "caller.wav"), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise SystemExit("caller.wav is not mono PCM16 16 kHz")
        pcm = wav.readframes(wav.getnframes())
    lines = []
    for line in meta["lines"]:
        start = int(line["start_s"] * SAMPLE_RATE) * 2
        end = start + int((line["duration_s"] + 0.15) * SAMPLE_RATE) * 2
        lines.append(pcm[start:end])
    return lines


def _create_agent(key: str) -> str:
    payload = {
        "name": AGENT_NAME,
        "tags": ["earshot-fixture"],
        "conversation_config": {
            "agent": {
                "first_message": FIRST_MESSAGE,
                "language": "en",
                "prompt": {"prompt": AGENT_PROMPT},
            },
            "asr": {"user_input_audio_format": AUDIO_FORMAT},
            "tts": {"agent_output_audio_format": AUDIO_FORMAT},
            "conversation": {"max_duration_seconds": MAX_DURATION_S},
        },
    }
    _, _, body = request(
        "POST",
        API + "/v1/convai/agents/create",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        body=json.dumps(payload).encode(),
    )
    return json.loads(body)["agent_id"]


async def _drive(signed_url: str, lines: list[bytes], log: list[dict[str, Any]]) -> str:
    import websockets

    state: dict[str, Any] = {"conversation_id": "", "audio_until": 0.0, "audio_events": 0}
    state["last_agent_event"] = time.monotonic()
    chunk = int(SAMPLE_RATE * CHUNK_S) * 2
    silence = b"\x00" * chunk
    t0 = time.monotonic()
    deadline = t0 + 75.0

    async with websockets.connect(signed_url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))

        async def receive() -> None:
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")
                now = time.monotonic()
                entry: dict[str, Any] = {"t": round(now - t0, 3), "type": kind}
                if kind == "conversation_initiation_metadata":
                    meta = event.get("conversation_initiation_metadata_event", {})
                    state["conversation_id"] = meta.get("conversation_id", "")
                    entry["formats"] = {
                        k: meta.get(k)
                        for k in ("agent_output_audio_format", "user_input_audio_format")
                    }
                elif kind == "audio":
                    audio = base64.b64decode(event.get("audio_event", {}).get("audio_base_64", ""))
                    seconds = len(audio) / 2 / SAMPLE_RATE
                    state["audio_until"] = max(state["audio_until"], now) + seconds
                    state["audio_events"] += 1
                    state["last_agent_event"] = now
                    entry["bytes"] = len(audio)
                elif kind == "ping":
                    ping = event.get("ping_event", {})
                    await ws.send(json.dumps({"type": "pong", "event_id": ping.get("event_id")}))
                else:
                    entry["keys"] = sorted(event)
                    if kind in ("agent_response", "interruption"):
                        state["last_agent_event"] = now
                log.append(entry)

        async def stream(pcm: bytes, seconds_of_silence: float) -> None:
            padded = pcm + silence * round(seconds_of_silence / CHUNK_S)
            for offset in range(0, len(padded), chunk):
                piece = padded[offset : offset + chunk]
                await ws.send(json.dumps({"user_audio_chunk": base64.b64encode(piece).decode()}))
                await asyncio.sleep(CHUNK_S)

        async def wait_for_agent_turn(since_events: int) -> None:
            # The agent's turn is over once new audio arrived, played out, and 1.2 s passed.
            while time.monotonic() < deadline:
                now = time.monotonic()
                if (
                    state["audio_events"] > since_events
                    and now > state["audio_until"] + 1.2
                    and now - state["last_agent_event"] > 1.2
                ):
                    return
                await stream(b"", CHUNK_S)
            raise TimeoutError

        async def send() -> None:
            seen = 0
            for index, pcm in enumerate(lines):
                await wait_for_agent_turn(seen)
                seen = state["audio_events"]
                log.append({"t": round(time.monotonic() - t0, 3), "type": f"caller_line_{index}"})
                await stream(pcm, 0.0)
            await wait_for_agent_turn(seen)
            await stream(b"", 1.0)

        receiver = asyncio.create_task(receive())
        try:
            await send()
        except (TimeoutError, websockets.ConnectionClosed) as exc:
            log.append({"t": round(time.monotonic() - t0, 3), "type": type(exc).__name__})
        finally:
            receiver.cancel()
    return state["conversation_id"]


def _credits(key: str) -> dict[str, Any] | None:
    """Free read of the subscription counters, for cost-as-reported before and after."""
    try:
        sub = _get_json("/v1/user/subscription", key)
    except HttpError as exc:
        print(f"subscription read unavailable: {exc}")
        return None
    return {
        k: sub.get(k)
        for k in ("character_count", "character_limit", "tier", "next_character_count_reset_unix")
    }


def converse(key: str) -> None:
    out = RAW / "conversation-run"
    if (out / "conversation.json").exists():
        raise SystemExit("a conversation was already captured; the owner approved exactly one")
    lines = _caller_lines()
    before = _credits(key)
    agent_id = _create_agent(key)
    out.mkdir(parents=True, exist_ok=True)
    write_json(
        out / "run.json",
        {"agent_id": agent_id, "started_unix": time.time(), "credits_before": before},
    )
    aid = urllib.parse.quote(agent_id, safe="")
    signed = _get_json(f"/v1/convai/conversation/get-signed-url?agent_id={aid}", key)["signed_url"]
    log: list[dict[str, Any]] = []
    try:
        conversation_id = asyncio.run(_drive(signed, lines, log))
    finally:
        write_json(out / "ws_events.json", log)
    run = json.loads((out / "run.json").read_text())
    run.update({"conversation_id": conversation_id, "ended_unix": time.time()})
    write_json(out / "run.json", run)
    print(f"websocket events: {len(log)}; conversation id received: {bool(conversation_id)}")
    cid = urllib.parse.quote(conversation_id, safe="")
    for _ in range(40):
        conv = _get_json(f"/v1/convai/conversations/{cid}", key)
        if conv.get("status") in ("done", "failed"):
            break
        time.sleep(5)
    _fetch_conversation(key, conversation_id, out)
    run = json.loads((out / "run.json").read_text())
    run["credits_after"] = _credits(key)
    write_json(out / "run.json", run)
    if before and run["credits_after"]:
        used = run["credits_after"]["character_count"] - before["character_count"]
        print(f"subscription character_count delta: {used}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("survey")
    fetch_parser = sub.add_parser("fetch")
    fetch_parser.add_argument("--index", type=int, required=True)
    sub.add_parser("converse")
    args = parser.parse_args()
    key = load_keys(args.env, ["ELEVENLABS_API_KEY"])["ELEVENLABS_API_KEY"]
    try:
        if args.command == "survey":
            survey(key)
        elif args.command == "fetch":
            fetch(key, args.index)
        else:
            converse(key)
    except Exception as exc:
        raise SystemExit(
            f"{type(exc).__name__}: {redact(str(exc)).replace(key, '[KEY]')}"
        ) from None


if __name__ == "__main__":
    main()
