"""Both server entry points serve recordings from ``EARSHOT_BLOB_URL`` and sweep it at startup.

Real server processes (``earshot serve`` and ``uvicorn apps.ingest.app:app``)
configured only through the environment, on a real catalog and a real blob store
(local disk and MinIO). Before each start a crashed upload is left behind (a spool
file, or an S3 multipart upload with a part in it); the server's startup sweep
removes it, and a recording uploaded through the process lands in that store.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import time
import uuid
import wave

import pytest

from earshot.blobstore import PART_SIZE
from otlp_support import request
from server_process import ServerProcess
from store_support import make_store

pytestmark = pytest.mark.e2e


def _wav(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8_000)
        handle.writeframes(b"\x05\x00" * int(8_000 * seconds))
    return buffer.getvalue()


@pytest.mark.parametrize("entry_point", ["serve", "uvicorn"])
def test_each_entry_point_sweeps_its_blob_store_at_startup_and_stores_recordings_there(
    tmp_path, blob_backend, entry_point
) -> None:
    make_store(tmp_path).close()  # migrate before the server starts
    interrupted = blob_backend.store.begin_put("recordings/default/interrupted")
    interrupted.write(os.urandom(PART_SIZE + 1))  # S3: an upload in progress, one part in
    left_behind = blob_backend.leftovers()
    body = _wav(0.75)

    server = ServerProcess.start(
        tmp_path,
        tmp_path / f"{entry_point}.log",
        entry_point=entry_point,
        EARSHOT_BLOB_URL=blob_backend.url,
    )
    try:
        deadline = time.monotonic() + 15
        while blob_backend.leftovers() and time.monotonic() < deadline:
            time.sleep(0.05)
        swept = blob_backend.leftovers()
        created = request(
            server.port,
            "POST",
            "/v1/recordings",
            json.dumps(
                {
                    "recording_id": "call-1.user",
                    "session_key": "call-1",
                    "speaker": "user",
                    "content_type": "audio/wav",
                    "started_at_unix_nano": "1800000000000000000",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        uploaded = request(
            server.port,
            "PUT",
            created.json()["upload_path"],
            body,
            headers={"Content-Type": "audio/wav"},
        )
        read = request(server.port, "GET", "/v1/recordings/call-1.user/content")
    finally:
        server.stop()
        with contextlib.suppress(Exception):
            interrupted.abort()

    assert len(left_behind) == 1, left_behind
    assert swept == []
    assert created.status == 201 and uploaded.status == 200, uploaded.body
    assert uploaded.json()["sha256"] == hashlib.sha256(body).hexdigest()
    assert uploaded.json()["duration_ms"] == 750
    assert read.body == body
    (key,) = blob_backend.objects("recordings/")  # the server's analysis may add peaks
    assert key.startswith("recordings/default/") and uuid.UUID(key.rsplit("/", 1)[1])
