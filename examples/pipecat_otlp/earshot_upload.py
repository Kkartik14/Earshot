"""Upload a call's per-speaker tracks to Earshot's recordings API (standard library only).

``run_call.py --upload`` uses it in two steps, so the call stays held open in
Earshot until its audio arrives:

1. at the call's first audio, ``create_tracks`` creates one pending recording per
   speaker (``POST /v1/recordings``), keyed by the same session key the spans carry;
2. after the run and the tracer's ``force_flush``, ``upload_tracks`` streams each
   WAV to its ``upload_path`` (``PUT``), with a ``Content-Digest``.

Recording IDs come from the conversation ID and the speaker, so a retried create
or upload is idempotent. The API key is ``EARSHOT_API_KEY``, or the
``Authorization`` entry of ``OTEL_EXPORTER_OTLP_HEADERS`` (the one the exporter
already sends); without either, requests are anonymous (a loopback server).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

SPEAKERS = ("user", "agent")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_RECORDING_ID_MAX = 128


class UploadError(RuntimeError):
    """Earshot refused a request; the message carries the status and the error code."""


def recording_id_for(conversation_id: str, speaker: str) -> str:
    """A stable recording ID for one speaker's track of a conversation.

    Characters a recording ID cannot hold become ``-``; an ID too long to fit
    keeps a prefix plus a hash of the whole conversation ID, so distinct
    conversations never share one.
    """

    suffix = f".{speaker}"
    base = _UNSAFE.sub("-", conversation_id).lstrip("._-") or "call"
    room = _RECORDING_ID_MAX - len(suffix)
    if len(base) > room or base != conversation_id:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:16]
        base = f"{base[: room - len(digest) - 1]}-{digest}"
    return base + suffix


def authorization_header(environment: Mapping[str, str]) -> str | None:
    """``Bearer <key>`` from ``EARSHOT_API_KEY`` or the OTLP exporter's headers."""

    key = environment.get("EARSHOT_API_KEY", "").strip()
    if key:
        return f"Bearer {key}"
    for entry in environment.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
        name, separator, value = entry.partition("=")
        if separator and name.strip().lower() == "authorization":
            return unquote(value.strip()) or None
    return None


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return f"sha-256=:{base64.b64encode(hasher.digest()).decode('ascii')}:"


class EarshotRecordings:
    """A minimal client of ``/v1/recordings`` on one Earshot server."""

    def __init__(
        self, base_url: str, *, authorization: str | None = None, timeout: float = 120.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._authorization = authorization
        self._timeout = timeout

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        sent = dict(headers or {})
        if self._authorization:
            sent["Authorization"] = self._authorization
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=sent
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as reply:
                return json.loads(reply.read())
        except urllib.error.HTTPError as error:
            try:
                code = json.loads(error.read())["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "no error code"
            raise UploadError(f"{method} {path}: HTTP {error.code} {code}") from None

    def create(
        self,
        recording_id: str,
        *,
        session_key: str,
        speaker: str,
        started_at_unix_nano: int,
        content_type: str = "audio/wav",
    ) -> dict[str, Any]:
        manifest = {
            "recording_id": recording_id,
            "session_key": session_key,
            "speaker": speaker,
            "content_type": content_type,
            "started_at_unix_nano": str(started_at_unix_nano),
        }
        return self._send(
            "POST",
            "/v1/recordings",
            body=json.dumps(manifest).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def upload(self, upload_path: str, path: Path, *, content_type: str = "audio/wav") -> dict:
        size = path.stat().st_size
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(size),
            "Content-Digest": _digest(path),
        }
        with path.open("rb") as body:
            return self._send("PUT", upload_path, body=body, headers=headers)

    def create_tracks(
        self,
        conversation_id: str,
        *,
        started_at_unix_nano: int,
        speakers: Iterable[str] = SPEAKERS,
    ) -> dict[str, dict[str, Any]]:
        """Create one pending recording per speaker (at the call's first audio)."""

        return {
            speaker: self.create(
                recording_id_for(conversation_id, speaker),
                session_key=conversation_id,
                speaker=speaker,
                started_at_unix_nano=started_at_unix_nano,
            )
            for speaker in speakers
        }

    def upload_tracks(
        self, conversation_id: str, tracks: Mapping[str, Path]
    ) -> dict[str, dict[str, Any]]:
        """Upload each speaker's WAV to the recording ``create_tracks`` made for it."""

        return {
            speaker: self.upload(
                f"/v1/recordings/{recording_id_for(conversation_id, speaker)}/content", path
            )
            for speaker, path in tracks.items()
        }
