"""A 1 GiB recording upload through a real server process stays small.

The server is ``uvicorn`` serving ``apps.ingest.app:app`` in a subprocess, through
the whole HTTP path: the authentication ``BaseHTTPMiddleware``, the recordings
route and the blob store (local disk, and MinIO). The test's launcher adds one
thing to that process: on ``SIGUSR1`` it appends its peak RSS
(``getrusage().ru_maxrss``) to a file. The baseline is taken after a 16 MiB
warm-up upload, so the threadpool, the S3 client and its part buffer already
exist; the 1 GiB upload must then grow the peak by at most 64 MiB, so nothing on
the path (middleware included) holds the body.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import signal
import struct
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from otlp_support import request
from server_process import ServerProcess
from store_support import make_store

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

MIB = 1024 * 1024
RATE = 16_000
RSS_SERVER = """
import os, resource, signal, sys
import uvicorn

def report(_signum, _frame):
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB
    with open(os.environ["RSS_REPORT_PATH"], "a") as handle:
        handle.write(f"{peak}\\n")

signal.signal(signal.SIGUSR1, report)
uvicorn.run("apps.ingest.app:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


def _wav_header(data_bytes: int) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


def _wav_body(data_mib: int, digest: hashlib._Hash) -> Iterator[bytes]:
    """A mono WAV of ``data_mib`` MiB of audio, generated as it is sent."""

    block = os.urandom(MIB)
    header = _wav_header(data_mib * MIB)
    digest.update(header)
    yield header
    for _ in range(data_mib):
        digest.update(block)
        yield block


def _create(port: int, recording_id: str) -> None:
    reply = request(
        port,
        "POST",
        "/v1/recordings",
        json.dumps(
            {
                "recording_id": recording_id,
                "session_key": "memory-call",
                "speaker": "mixed",
                "content_type": "audio/wav",
                "started_at_unix_nano": "1800000000000000000",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert reply.status == 201, reply.body


def _upload(port: int, recording_id: str, data_mib: int) -> tuple[dict, str, float]:
    digest = hashlib.sha256()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    started = time.monotonic()
    try:
        connection.request(
            "PUT",
            f"/v1/recordings/{recording_id}/content",
            body=_wav_body(data_mib, digest),
            headers={
                "Content-Type": "audio/wav",
                "Content-Length": str(44 + data_mib * MIB),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
    finally:
        connection.close()
    return body, digest.hexdigest(), time.monotonic() - started


def _peak_rss(server: ServerProcess, report: Path) -> int:
    before = len(report.read_text().splitlines()) if report.exists() else 0
    os.kill(server.process.pid, signal.SIGUSR1)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        lines = report.read_text().splitlines() if report.exists() else []
        if len(lines) > before:
            return int(lines[-1])
        time.sleep(0.02)
    raise AssertionError("the server did not report its peak RSS")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGUSR1 and getrusage are POSIX")
def test_a_one_gib_upload_grows_the_server_peak_rss_by_at_most_64_mib(
    tmp_path, blob_backend
) -> None:
    make_store(tmp_path).close()
    report = tmp_path / "rss.txt"
    server = ServerProcess.start(
        tmp_path,
        tmp_path / "server.log",
        command=[sys.executable, "-c", RSS_SERVER],
        EARSHOT_BLOB_URL=blob_backend.url,
        # 1 GiB is over the 512 MiB default; an operator raises the bound like this.
        EARSHOT_RECORDING_MAX_BYTES=str(2 * 1024 * MIB),
        RSS_REPORT_PATH=str(report),
    )
    try:
        _create(server.port, "warm-up")
        _upload(server.port, "warm-up", 16)
        baseline = _peak_rss(server, report)
        _create(server.port, "one-gib")
        stored, sha256, seconds = _upload(server.port, "one-gib", 1024)
        peak = _peak_rss(server, report)
    finally:
        server.stop()

    growth = peak - baseline
    print(
        f"[{blob_backend.name}] 1 GiB PUT in {seconds:.1f} s; server peak RSS "
        f"{baseline / MIB:.1f} -> {peak / MIB:.1f} MiB (+{growth / MIB:.1f} MiB)"
    )
    assert (stored["state"], stored["size"], stored["sha256"]) == (
        "stored",
        44 + 1024 * MIB,
        sha256,
    )
    assert stored["duration_ms"] == 1024 * MIB * 1000 // (RATE * 2)
    assert growth <= 64 * MIB
