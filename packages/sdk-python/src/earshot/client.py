"""A small client for an Earshot server's recordings and calls API.

For a voice agent (or anything beside it) that sends its call's spans to Earshot
over OTLP and uploads the call's audio::

    from earshot.client import EarshotClient, tracer_metadata

    # LiveKit: every span carries the session key that names the call.
    telemetry.set_tracer_provider(provider, metadata=tracer_metadata(session_key))

    with EarshotClient.from_environment() as earshot:
        # At the call's first audio: a pending recording holds the call open.
        recording = earshot.create_recording(
            session_key=session_key,
            speaker="mixed",
            channel_speakers=["user", "agent"],
            content_type="audio/ogg",
            started_at_unix_nano=first_sample_unix_nano,
        )
        ...  # the call runs
        earshot.upload_recording(recording, "call.ogg")  # streamed from disk
        call = earshot.get_call(earshot.resolve_call(session_key)["call_id"])

``EarshotClient.from_environment`` reads the server from ``EARSHOT_URL``, else the
standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` (Earshot's OTLP receiver is at its base
URL), and the API key from ``EARSHOT_API_KEY``, else the ``Authorization`` entry of
``OTEL_EXPORTER_OTLP_HEADERS``: the one the OTLP exporter already sends.

It needs only the standard library; when httpx is installed it is used instead
(``transport="auto"``), which keeps one connection pool per client. A refused
request raises ``EarshotError`` with the HTTP status and Earshot's stable error
code; its message never holds the API key or a session key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Protocol
from urllib.parse import quote, unquote

from .call_capture import valid_session_key

SESSION_KEY_ATTRIBUTE = "earshot.session.id"
DEFAULT_TIMEOUT_SECONDS = 120.0
# An upload is read and sent in blocks of this size, never whole.
_BLOCK_BYTES = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_RECORDING_ID_MAX = 128


class EarshotError(RuntimeError):
    """A request Earshot refused (or that never got an answer: ``status`` 0)."""

    def __init__(
        self, method: str, path: str, status: int, code: str | None, message: str | None
    ) -> None:
        self.method, self.path, self.status, self.code = method, path, status, code
        self.message = message
        super().__init__(f"{method} {path}: HTTP {status} {code or 'without an error code'}")


def tracer_metadata(session_key: str) -> dict[str, str]:
    """The span attributes that key a call: what LiveKit's
    ``telemetry.set_tracer_provider(provider, metadata=…)`` stamps on every span.
    The key is 1 to 256 bytes of UTF-8 after trimming, as the server reads it."""

    if valid_session_key(session_key) is None:
        raise ValueError("a session key is 1 to 256 bytes of UTF-8 after trimming")
    return {SESSION_KEY_ATTRIBUTE: session_key}


def authorization_from_environment(environment: Mapping[str, str] | None = None) -> str | None:
    """``Bearer <key>`` from ``EARSHOT_API_KEY``, else the ``Authorization`` entry of
    ``OTEL_EXPORTER_OTLP_HEADERS``; ``None`` without either (an anonymous loopback
    server)."""

    environment = os.environ if environment is None else environment
    key = environment.get("EARSHOT_API_KEY", "").strip()
    if key:
        return f"Bearer {key}"
    for entry in environment.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
        name, separator, value = entry.partition("=")
        if separator and name.strip().lower() == "authorization":
            return unquote(value.strip()) or None
    return None


def recording_id_for(session_key: str, name: str) -> str:
    """A stable recording ID for the recording ``name`` of a session key's call, so a
    retried create or upload names the same recording.

    Characters a recording ID cannot hold become ``-``; an ID that changed, or is
    too long, keeps a prefix plus a hash of the whole key, so distinct keys never
    share one.
    """

    suffix = f".{name}"
    base = _UNSAFE.sub("-", session_key).lstrip("._-") or "call"
    room = _RECORDING_ID_MAX - len(suffix)
    if len(base) > room or base != session_key:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:16]
        base = f"{base[: room - len(digest) - 1]}-{digest}"
    return base + suffix


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    body: bytes


class _Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Iterator[bytes] | None,
    ) -> _Reply: ...

    def close(self) -> None: ...


class _UrllibTransport:
    """The standard library's HTTP: one connection per request."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Iterator[bytes] | None,
    ) -> _Reply:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as reply:
                return _Reply(reply.status, reply.read())
        except urllib.error.HTTPError as error:
            with error:
                return _Reply(error.code, error.read())

    def close(self) -> None:
        return None


class _HttpxTransport:
    """httpx: one connection pool for the client's life. A request that gets no answer
    raises ``OSError``, as the standard library's does."""

    def __init__(self, timeout: float) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout)
        self._transport_error = httpx.TransportError

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Iterator[bytes] | None,
    ) -> _Reply:
        try:
            reply = self._client.request(method, url, headers=dict(headers), content=body)
        except self._transport_error as error:
            raise ConnectionError(type(error).__name__) from error
        return _Reply(reply.status_code, reply.content)

    def close(self) -> None:
        self._client.close()


def _transport(name: str, timeout: float) -> _Transport:
    if name == "auto":
        try:
            return _HttpxTransport(timeout)
        except ImportError:
            return _UrllibTransport(timeout)
    if name == "httpx":
        return _HttpxTransport(timeout)
    if name == "urllib":
        return _UrllibTransport(timeout)
    raise ValueError(f"unknown transport {name!r}: auto, httpx or urllib")


def _source_digest(source: Path | BinaryIO) -> tuple[str, int]:
    """The ``Content-Digest`` (sha-256) and size of an upload, read in blocks."""

    hasher, size = hashlib.sha256(), 0
    for block in _blocks(source):
        hasher.update(block)
        size += len(block)
    return f"sha-256=:{base64.b64encode(hasher.digest()).decode('ascii')}:", size


def _blocks(source: Path | BinaryIO) -> Iterator[bytes]:
    if isinstance(source, Path):
        with source.open("rb") as handle:
            yield from iter(lambda: handle.read(_BLOCK_BYTES), b"")
        return
    source.seek(0)
    yield from iter(lambda: source.read(_BLOCK_BYTES), b"")


class EarshotClient:
    """The recordings and calls API of one Earshot server, as one project's API key.

    ``base_url`` is the server's base URL (the OTLP endpoint). ``api_key`` sends
    ``Authorization: Bearer <key>``; ``authorization`` sends a whole header value
    instead. Use it as a context manager, or ``close()`` it.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        authorization: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: str = "auto",
    ) -> None:
        if api_key is not None and authorization is not None:
            raise ValueError("pass api_key or authorization, not both")
        self.base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {api_key}" if api_key is not None else authorization
        self._transport = _transport(transport, timeout)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None, **options: Any
    ) -> EarshotClient:
        """A client for ``EARSHOT_URL`` (else ``OTEL_EXPORTER_OTLP_ENDPOINT``) with the
        key ``authorization_from_environment`` finds."""

        environment = os.environ if environment is None else environment
        base_url = environment.get("EARSHOT_URL") or environment.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not base_url:
            raise ValueError("set EARSHOT_URL or OTEL_EXPORTER_OTLP_ENDPOINT")
        return cls(base_url, authorization=authorization_from_environment(environment), **options)

    def __enter__(self) -> EarshotClient:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    # --- recordings ------------------------------------------------------------------------

    def create_recording(
        self,
        *,
        session_key: str,
        speaker: str,
        content_type: str,
        started_at_unix_nano: int | str,
        channel_speakers: Iterable[str] | None = None,
        recording_id: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Create (or find again) a pending recording of the call ``session_key``
        names. ``speaker`` is ``user``, ``agent`` or ``mixed``; a ``mixed`` stereo
        recording names its channels' speakers in ``channel_speakers``. With a
        ``recording_id`` the request is idempotent; without one the server mints one.
        """

        manifest: dict[str, Any] = {
            "session_key": session_key,
            "speaker": speaker,
            "content_type": content_type,
            "started_at_unix_nano": str(started_at_unix_nano),
        }
        if channel_speakers is not None:
            manifest["channel_speakers"] = list(channel_speakers)
        if recording_id is not None:
            manifest["recording_id"] = recording_id
        if label is not None:
            manifest["label"] = label
        return self._json("POST", "/v1/recordings", manifest)

    def get_recording(self, recording_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/recordings/{quote(recording_id, safe='')}")

    def upload_recording(
        self,
        recording: str | Mapping[str, Any],
        source: str | os.PathLike[str] | BinaryIO,
        *,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a pending recording's bytes from a file path or a seekable binary
        file, streamed in blocks with their ``Content-Digest``; the stored recording.

        ``recording`` is the recording ``create_recording`` returned (its upload path
        and content type), or its ID (the content type is then read from the server
        unless given). An upload of the same bytes again answers the stored recording.
        """

        if isinstance(recording, Mapping):
            path = str(recording["upload_path"])
            content_type = content_type or str(recording["content_type"])
        else:
            path = f"/v1/recordings/{quote(recording, safe='')}/content"
            if content_type is None:
                content_type = str(self.get_recording(recording)["content_type"])
        body = Path(source) if isinstance(source, str | os.PathLike) else source
        digest, size = _source_digest(body)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(size),
            "Content-Digest": digest,
        }
        return self._json("PUT", path, None, headers=headers, stream=_blocks(body))

    # --- calls ------------------------------------------------------------------------------

    def resolve_call(self, session_key: str) -> dict[str, Any]:
        """``{"call_id": …}`` of the call ``session_key`` names now."""

        return self._json("POST", "/v1/calls:resolve", {"session_key": session_key})

    def get_call(self, call_id: str) -> dict[str, Any]:
        """The call's detail: its revisions, recordings and transcript."""

        return self._json("GET", f"/v1/calls/{quote(call_id, safe='')}")

    # --- HTTP -------------------------------------------------------------------------------

    def _json(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
        stream: Iterator[bytes] | None = None,
    ) -> dict[str, Any]:
        sent = {"Accept": "application/json", **(headers or {})}
        body: bytes | Iterator[bytes] | None = stream
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            sent["Content-Type"] = "application/json"
        if self._authorization:
            sent["Authorization"] = self._authorization
        try:
            reply = self._transport.send(method, self.base_url + path, sent, body)
        except OSError as error:  # no answer: refused, reset, timed out
            raise EarshotError(method, path, 0, None, type(error).__name__) from None
        if reply.status >= 400:
            code, message = None, None
            try:
                problem = json.loads(reply.body)["error"]
                code, message = str(problem["code"]), str(problem.get("message", ""))
            except (ValueError, KeyError, TypeError):
                pass
            raise EarshotError(method, path, reply.status, code, message)
        return json.loads(reply.body)
