"""``earshot.client`` against a real Earshot server.

Every test that talks to a server runs the real app on a real uvicorn server, with
a real catalog (SQLite, or Postgres under ``--db postgresql``), a real project API
key and a real blob store on local disk, through both of the client's transports:
the standard library's and httpx's. Audio is the real LiveKit 1.8 stereo Ogg Opus
recording and real WAV bytes. What an Earshot server never answers (a redirect, a
proxy's page, an answer cut short) comes from real local HTTP servers, a
standard-library one and a raw socket.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from audio_support import LIVEKIT_RECORDING, wav
from earshot.api import create_app
from earshot.blobstore import LocalBlobStore
from earshot.client import (
    EarshotClient,
    EarshotError,
    authorization_from_environment,
    recording_id_for,
    tracer_metadata,
)
from otlp_support import free_port, serve
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

STARTED_AT = 1_792_407_986_846_424_000
RECORDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TRANSPORTS = ["urllib", "httpx"]


@pytest.fixture
def earshot(tmp_path):
    """A served app with a project API key: ``(base_url, key)``."""

    store = make_store(tmp_path)
    key = store.issue_api_key("default", label="client").credential
    app = create_app(store=store, blob_store=LocalBlobStore(tmp_path / "blobs"))
    with serve(app) as port:
        yield f"http://127.0.0.1:{port}", key


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_stereo_recording_is_created_streamed_and_found_through_its_call(
    tmp_path, earshot, transport
) -> None:
    base_url, key = earshot
    body = LIVEKIT_RECORDING.read_bytes()
    recording_id = recording_id_for("conv-1", "call")

    with EarshotClient(base_url + "/", api_key=key, transport=transport) as client:
        created = client.create_recording(
            session_key="conv-1",
            speaker="mixed",
            content_type="audio/ogg",
            started_at_unix_nano=STARTED_AT,
            channel_speakers=["user", "agent"],
            recording_id=recording_id,
        )
        retried = client.create_recording(
            session_key="conv-1",
            speaker="mixed",
            content_type="audio/ogg",
            started_at_unix_nano=str(STARTED_AT),
            channel_speakers=("user", "agent"),
            recording_id=recording_id,
        )
        uploaded = client.upload_recording(created, LIVEKIT_RECORDING)
        again = client.upload_recording(recording_id, str(LIVEKIT_RECORDING))
        call_id = client.resolve_call("conv-1")["call_id"]
        call = client.get_call(call_id)

    assert (created["state"], created["channel_speakers"], retried) == (
        "pending",
        ["user", "agent"],
        created,
    )
    assert {key: uploaded[key] for key in ("state", "sha256", "size", "duration_ms")} == {
        "state": "stored",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "duration_ms": 34_010,
    }
    assert again["sha256"] == uploaded["sha256"]
    assert call_id == created["call_id"] == call["call_id"]
    assert [(item["recording_id"], item["channel_speakers"]) for item in call["recordings"]] == [
        (recording_id, ["user", "agent"])
    ]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(1,)]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_an_upload_streams_from_an_open_file_and_is_checked_against_its_digest(
    earshot, transport
) -> None:
    base_url, key = earshot
    body = wav(seconds=0.75)

    with EarshotClient(base_url, api_key=key, transport=transport) as client:
        created = client.create_recording(
            session_key="conv-2",
            speaker="user",
            content_type="audio/wav",
            started_at_unix_nano=STARTED_AT,
        )
        uploaded = client.upload_recording(created, io.BytesIO(body))
        recording = client.get_recording(created["recording_id"])

    assert RECORDING_ID.fullmatch(created["recording_id"])
    assert (uploaded["sha256"], uploaded["duration_ms"]) == (hashlib.sha256(body).hexdigest(), 750)
    assert recording == uploaded


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_refused_request_raises_its_status_and_code_and_never_the_key(earshot, transport) -> None:
    base_url, key = earshot

    bad = EarshotClient(base_url, api_key="earshot_sk_not-a-real-key", transport=transport)
    with bad, pytest.raises(EarshotError) as unauthorized:
        bad.resolve_call("conv-1")
    with EarshotClient(base_url, api_key=key, transport=transport) as client:
        with pytest.raises(EarshotError) as missing:
            client.resolve_call("never-seen")
        with pytest.raises(EarshotError) as mismatch:
            created = client.create_recording(
                session_key="conv-3",
                speaker="agent",
                content_type="audio/ogg",
                started_at_unix_nano=STARTED_AT,
            )
            client.upload_recording(created, LIVEKIT_RECORDING)

    assert (unauthorized.value.status, unauthorized.value.code) == (401, "EARSHOT_UNAUTHORIZED")
    assert "not-a-real-key" not in str(unauthorized.value)
    assert (missing.value.status, missing.value.code) == (404, "EARSHOT_CALL_NOT_FOUND")
    assert "never-seen" not in str(missing.value)
    assert (mismatch.value.status, mismatch.value.code) == (
        422,
        "EARSHOT_RECORDING_CHANNEL_MISMATCH",
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_server_that_does_not_answer_is_an_error_with_status_0(transport) -> None:
    closed = f"http://127.0.0.1:{free_port()}"  # nothing listens there

    client = EarshotClient(closed, transport=transport, timeout=5)
    with client, pytest.raises(EarshotError) as unanswered:
        client.get_call("call_1")

    assert (unanswered.value.status, unanswered.value.code) == (0, None)


def test_the_client_reads_its_server_and_key_from_the_environment(earshot) -> None:
    base_url, key = earshot
    environment = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": base_url,
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer%20{key}",
    }

    with EarshotClient.from_environment(environment) as client:
        created = client.create_recording(
            session_key="conv-4",
            speaker="user",
            content_type="audio/wav",
            started_at_unix_nano=STARTED_AT,
        )

    assert client.base_url == base_url
    assert created["state"] == "pending"


def test_tracer_metadata_keys_every_span_with_the_session_key() -> None:
    assert tracer_metadata("room-7f1c") == {"earshot.session.id": "room-7f1c"}
    for invalid in ("", "   ", "x" * 257):
        with pytest.raises(ValueError):
            tracer_metadata(invalid)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"EARSHOT_API_KEY": "earshot_sk_a"}, "Bearer earshot_sk_a"),
        (
            {"OTEL_EXPORTER_OTLP_HEADERS": "x-tenant=blue, Authorization=Bearer%20earshot_sk_b"},
            "Bearer earshot_sk_b",
        ),
        (
            {
                "EARSHOT_API_KEY": "earshot_sk_a",
                "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20earshot_sk_b",
            },
            "Bearer earshot_sk_a",
        ),
        ({"OTEL_EXPORTER_OTLP_HEADERS": "x-tenant=blue"}, None),
        ({}, None),
    ],
)
def test_the_authorization_comes_from_earshot_api_key_or_the_otlp_headers(
    environment, expected
) -> None:
    assert authorization_from_environment(environment) == expected


@pytest.mark.parametrize(
    ("session_key", "name", "expected"),
    [
        ("pipecat-otlp-7f1c0d6e", "call", "pipecat-otlp-7f1c0d6e.call"),
        ("pipecat-otlp-7f1c0d6e", "user", "pipecat-otlp-7f1c0d6e.user"),
        ("call 42/é", "call", None),
        ("-leading", "agent", None),
        ("x" * 300, "user", None),
    ],
)
def test_a_recording_id_is_derived_from_the_session_key_and_a_name(
    session_key, name, expected
) -> None:
    derived = recording_id_for(session_key, name)

    assert RECORDING_ID.fullmatch(derived)
    assert derived.endswith(f".{name}")
    assert derived == recording_id_for(session_key, name)  # stable across retries
    if expected is not None:
        assert derived == expected
    assert recording_id_for("x" * 300, "user") != recording_id_for("x" * 299 + "y", "user")


# --- answers an Earshot server never gives --------------------------------------------------------

_Answer = tuple[int, dict[str, str], bytes]
_JSON = {"Content-Type": "application/json"}


class _Answering(BaseHTTPRequestHandler):
    """Records each request (method, path, ``Authorization``) and answers it with the
    server's ``answer(path)``."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return None

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        status, headers, body = self.server.answer(self.path)
        self.send_response(status)
        for name, value in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = _answer


@contextlib.contextmanager
def _answering(answer: Callable[[str], _Answer]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Answering)
    server.seen, server.answer = [], answer
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("where", ["another-origin", "the-same-origin"])
def test_a_redirect_is_an_error_and_the_key_never_follows_it(transport, status, where) -> None:
    key = "earshot_sk_redirect-canary"
    with _answering(lambda path: (200, _JSON, b'{"call_id": "call_x"}')) as other:

        def redirect(path: str) -> _Answer:
            if path.startswith("/moved"):
                return 200, _JSON, b'{"call_id": "call_x"}'
            origin = _url(other) if where == "another-origin" else ""
            return status, {"Location": f"{origin}/moved{path}"}, b""

        with (
            _answering(redirect) as server,
            EarshotClient(_url(server), api_key=key, transport=transport, timeout=10) as client,
        ):
            with pytest.raises(EarshotError) as read:
                client.get_call("call_1")
            with pytest.raises(EarshotError) as upload:
                client.upload_recording(
                    {"upload_path": "/v1/recordings/r1/content", "content_type": "audio/wav"},
                    io.BytesIO(b"RIFF" + bytes(64)),
                )
            followed = [seen for seen in server.seen + other.seen if seen[1].startswith("/moved")]

    assert (read.value.status, read.value.code) == (status, None)
    assert (upload.value.status, upload.value.code) == (status, None)
    assert followed == []
    assert key not in f"{read.value} {read.value.message}"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_success_whose_answer_is_not_json_is_an_earshot_error(transport) -> None:
    page = (200, {"Content-Type": "text/html"}, b"<html>a proxy's sign-in page</html>")
    with (
        _answering(lambda path: page) as server,
        EarshotClient(_url(server), api_key="earshot_sk_x", transport=transport) as client,
        pytest.raises(EarshotError) as error,
    ):
        client.get_call("call_1")

    assert (error.value.status, error.value.code) == (200, None)


@contextlib.contextmanager
def _raw_answer(answer: bytes) -> Iterator[str]:
    """A server that reads a request, writes ``answer`` as it is, and hangs up."""

    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        with contextlib.suppress(OSError):
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(65536)
                    connection.sendall(answer)

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(
    "answer",
    [
        b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"ca',
        b"this is not HTTP\r\n\r\n",
    ],
    ids=["cut-short", "not-http"],
)
def test_an_answer_cut_short_or_not_http_is_no_answer(transport, answer) -> None:
    with (
        _raw_answer(answer) as url,
        EarshotClient(url, api_key="earshot_sk_x", transport=transport, timeout=10) as client,
        pytest.raises(EarshotError) as error,
    ):
        client.get_call("call_1")

    assert (error.value.status, error.value.code) == (0, None)
