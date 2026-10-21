"""A 64 MiB ElevenLabs ``post_call_audio`` is authenticated as it streams, never held.

ElevenLabs sends a call's audio as one base64 MP3 body, chunked, often far over the
hook's body cap. The server streams it into the HMAC, keeps its first 4 KiB to read
the event type, and answers 200 ``ignored`` (the audio comes from the API instead).
A real server process takes a 64 MiB chunked upload here while its resident memory
is sampled: it must grow by less than 8 MiB. The server first takes one streamed
16 MiB delivery: the first body past the cap grows the allocator's arenas once
(about 8 to 12 MiB on macOS, whatever the size); a buffered body would grow it by
its size every time.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import subprocess
import threading
import time

import pytest

from connector_support import SECRETS
from server_process import ServerProcess
from store_support import make_store

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SECRET_ENV = "ELEVENLABS_STREAM_TEST_SECRET"
ENDPOINT = "elevenlabs_stream_0001"
TOTAL = 64 * 1024 * 1024
CHUNK = 64 * 1024
LIMIT_KIB = 8 * 1024


def _chunks(at: int, total: int = TOTAL) -> list[bytes]:
    head = json.dumps({"type": "post_call_audio", "event_timestamp": at})[:-1].encode()
    head += b',"data":{"agent_id":"agent_x","conversation_id":"conv_x","full_audio":"'
    tail = b'"}}'
    filler = total - len(head) - len(tail)
    body = [head]
    while filler > 0:
        size = min(CHUNK, filler)
        body.append(b"A" * size)
        filler -= size
    body.append(tail)
    return body


def _rss_kib(pid: int) -> int:
    output = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    return int(output.stdout.strip())


def _post(port: int, chunks: list[bytes], at: int) -> tuple[int, dict]:
    mac = hmac.new(SECRETS["elevenlabs"].encode(), f"{at}.".encode(), hashlib.sha256)
    for chunk in chunks:
        mac.update(chunk)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            "POST",
            f"/hooks/v1/connectors/{ENDPOINT}",
            body=iter(chunks),
            headers={
                "Content-Type": "application/json",
                "ElevenLabs-Signature": f"t={at},v0={mac.hexdigest()}",
                "Transfer-Encoding": "chunked",
            },
            encode_chunked=True,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_a_64_mib_audio_delivery_is_ignored_with_bounded_memory(tmp_path) -> None:
    path = tmp_path / "server"
    store = make_store(path)
    store.create_connector(
        "default", provider="elevenlabs", secret_ref=f"env:{SECRET_ENV}", endpoint_id=ENDPOINT
    )
    store.close()
    server = ServerProcess.start(
        path, tmp_path / "server.log", **{SECRET_ENV: SECRETS["elevenlabs"]}
    )
    try:
        at = int(time.time())
        warm = _post(server.port, _chunks(at, 16 * 1024 * 1024), at)
        assert warm == (200, {"receipt_id": None, "disposition": "ignored", "call_id": None})
        baseline = _rss_kib(server.process.pid)
        peak = baseline
        done = threading.Event()

        def sample() -> None:
            nonlocal peak
            while not done.is_set():
                peak = max(peak, _rss_kib(server.process.pid))
                time.sleep(0.005)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        chunks = _chunks(at)
        try:
            status, body = _post(server.port, chunks, at)
        finally:
            done.set()
            sampler.join(timeout=10)
        peak = max(peak, _rss_kib(server.process.pid))
    finally:
        server.stop()

    assert sum(len(chunk) for chunk in chunks) == TOTAL
    assert (status, body) == (200, {"receipt_id": None, "disposition": "ignored", "call_id": None})
    assert peak - baseline < LIMIT_KIB, (baseline, peak)
