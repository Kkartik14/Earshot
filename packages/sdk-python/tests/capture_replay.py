"""Replay the two real captures into a running Earshot, as their examples sent them.

``fixtures/captured/pipecat-otlp-1.5`` and ``fixtures/captured/livekit-otlp-1.8`` each
hold one real call: the OTLP/HTTP requests its exporter sent, in arrival order, and
its real audio. A replay sends them to a server through ``send`` in the order the
example does:

- **Pipecat** (the quick start's order): a mono ``user`` and a mono ``agent`` WAV
  recording are created at the call's first audio, so the call is held for them;
  the exporter's requests follow; both tracks are uploaded after the call.
- **LiveKit** (``examples/livekit_otlp``): one ``mixed`` Ogg Opus recording with
  ``channel_speakers`` ``["user", "agent"]`` is created when the session starts;
  the requests follow; the file is uploaded from ``on_session_end``.

The standard library only: ``scripts/viewer_smoke.py`` runs it against an installed
artifact from a Python that has no Earshot. ``send(method, path, body, headers)``
makes one HTTP request and returns its status and body.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_FIXTURE = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5"
LIVEKIT_FIXTURE = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8"

Send = Callable[[str, str, "bytes | None", Mapping[str, str]], tuple[int, bytes]]


@dataclass(frozen=True)
class Replayed:
    """One replayed call: what its client side sent."""

    session_key: str
    exported_span_count: int
    started_at_unix_nano: int
    # recording ID -> (speaker, the bytes uploaded for it)
    recordings: Mapping[str, tuple[str, bytes]]
    # the fixture's providers per operation: {"stt": {"provider": …, "model": …}, …}
    providers: Mapping[str, Mapping[str, str]]


def _manifest(fixture: Path) -> dict[str, Any]:
    return json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))


def _json_body(value: Any) -> tuple[bytes, dict[str, str]]:
    return json.dumps(value).encode("utf-8"), {"Content-Type": "application/json"}


def _create(send: Send, manifest: Mapping[str, Any]) -> None:
    body, headers = _json_body(manifest)
    status, reply = send("POST", "/v1/recordings", body, headers)
    assert status == 201, ("POST /v1/recordings", status, reply[:500])
    assert json.loads(reply)["state"] == "pending", reply[:500]


def _post_requests(send: Send, fixture: Path, manifest: Mapping[str, Any]) -> None:
    for entry in manifest["requests"]:
        payload = (fixture / entry["file"]).read_bytes()
        headers = {"Content-Type": entry["wire"]["content_type"]}
        status, reply = send("POST", "/v1/traces", payload, headers)
        assert (status, reply) == (200, b""), (entry["file"], status, reply[:500])


def _upload(send: Send, recording_id: str, content_type: str, body: bytes) -> None:
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    headers = {"Content-Type": content_type, "Content-Digest": f"sha-256=:{digest}:"}
    status, reply = send("PUT", f"/v1/recordings/{recording_id}/content", body, headers)
    assert status == 200, (recording_id, status, reply[:500])


def replay_pipecat(send: Send) -> Replayed:
    """The Pipecat 1.5 capture, with its two mono tracks (the quick start's order)."""

    manifest = _manifest(PIPECAT_FIXTURE)
    session_key = manifest["provenance"]["conversation_id"]
    audio = manifest["audio"]
    started = int(audio["started_at_unix_nano"])
    tracks = {
        f"{session_key}.{speaker}": (
            speaker,
            (PIPECAT_FIXTURE / audio[speaker]["file"]).read_bytes(),
        )
        for speaker in ("user", "agent")
    }
    for recording_id, (speaker, _) in tracks.items():
        _create(
            send,
            {
                "recording_id": recording_id,
                "session_key": session_key,
                "speaker": speaker,
                "content_type": "audio/wav",
                "started_at_unix_nano": str(started),
            },
        )
    _post_requests(send, PIPECAT_FIXTURE, manifest)
    for recording_id, (_, body) in tracks.items():
        _upload(send, recording_id, "audio/wav", body)
    return Replayed(
        session_key=session_key,
        exported_span_count=manifest["provenance"]["exported_span_count_tee"],
        started_at_unix_nano=started,
        recordings=tracks,
        providers=manifest["provenance"]["providers"],
    )


def replay_livekit(send: Send) -> Replayed:
    """The LiveKit 1.8 capture, with its stereo Ogg Opus recording (the example's order)."""

    manifest = _manifest(LIVEKIT_FIXTURE)
    session_key = manifest["provenance"]["session_key"]
    audio = manifest["audio"]
    recording_id = f"{session_key}.stereo"
    body = (LIVEKIT_FIXTURE / audio["file"]).read_bytes()
    _create(
        send,
        {
            "recording_id": recording_id,
            "session_key": session_key,
            "speaker": "mixed",
            "channel_speakers": audio["channel_speakers"],
            "content_type": "audio/ogg",
            "started_at_unix_nano": audio["started_at_unix_nano"],
        },
    )
    _post_requests(send, LIVEKIT_FIXTURE, manifest)
    _upload(send, recording_id, "audio/ogg", body)
    return Replayed(
        session_key=session_key,
        exported_span_count=manifest["provenance"]["exported_span_count_tee"],
        started_at_unix_nano=int(audio["started_at_unix_nano"]),
        recordings={recording_id: ("mixed", body)},
        providers={
            operation: entry
            for operation, entry in manifest["provenance"]["providers"].items()
            if operation in ("stt", "llm", "tts")
        },
    )
