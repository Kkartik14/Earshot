"""Peaks and renditions over HTTP, from real servers whose maintenance
loop runs the analysis job.

Both entry points (``earshot serve`` and ``uvicorn apps.ingest.app:app``) analyse
an uploaded recording in a child process and serve its audiowaveform peaks; an
in-process server (``create_app`` under uvicorn) shows the cache headers, the
ETag revalidation, the 404s, and a mono pair's stereo rendition with its paired
peaks, on local disk and MinIO. The recordings are the real Pipecat fixture tracks.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
soundfile = pytest.importorskip("soundfile")

from earshot.api import create_app  # noqa: E402
from earshot.signals import analyze  # noqa: E402
from earshot.signals.peaks import interleave, read_header  # noqa: E402
from otlp_support import Reply, request, serve  # noqa: E402
from server_process import ServerProcess  # noqa: E402
from store_support import make_store  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
STARTED_AT = "1800000000000000000"
JSON_HEADERS = {"Content-Type": "application/json"}
ANALYSIS_DEADLINE_SECONDS = 90


def _upload(port: int, recording_id: str, speaker: str) -> None:
    created = request(
        port,
        "POST",
        "/v1/recordings",
        json.dumps(
            {
                "recording_id": recording_id,
                "session_key": "call-1",
                "speaker": speaker,
                "content_type": "audio/wav",
                "started_at_unix_nano": STARTED_AT,
            }
        ).encode(),
        headers=JSON_HEADERS,
    )
    assert created.status == 201, created.body
    uploaded = request(
        port,
        "PUT",
        created.json()["upload_path"],
        (PIPECAT_AUDIO / f"{speaker}.wav").read_bytes(),
        headers={"Content-Type": "audio/wav"},
    )
    assert uploaded.status == 200, uploaded.body


def _until_ok(port: int, path: str) -> Reply:
    """GET ``path`` until it answers 200 (the analysis job runs on its own cadence)."""

    deadline = time.monotonic() + ANALYSIS_DEADLINE_SECONDS
    while True:
        reply = request(port, "GET", path)
        if reply.status == 200 or time.monotonic() > deadline:
            return reply
        assert reply.status == 404, reply.body
        assert reply.problem_code() in {"EARSHOT_PEAKS_NOT_FOUND", "EARSHOT_RENDITION_NOT_FOUND"}
        time.sleep(0.1)


def _expected_peaks(tmp_path: Path, speaker: str) -> bytes:
    path = tmp_path / f"expected-{speaker}.dat"
    analyze(PIPECAT_AUDIO / f"{speaker}.wav", speakers=[speaker], peaks_path=path)
    return path.read_bytes()


@pytest.mark.e2e
@pytest.mark.parametrize("entry_point", ["serve", "uvicorn"])
def test_each_entry_point_analyses_an_uploaded_recording_and_serves_its_peaks(
    tmp_path, entry_point
) -> None:
    make_store(tmp_path).close()  # migrate before the server starts
    server = ServerProcess.start(tmp_path, tmp_path / f"{entry_point}.log", entry_point=entry_point)
    try:
        _upload(server.port, "call-1.user", "user")
        peaks = _until_ok(server.port, "/v1/recordings/call-1.user/peaks")
    finally:
        server.stop()

    assert peaks.status == 200, peaks.body
    assert peaks.body == _expected_peaks(tmp_path, "user")
    assert peaks.headers["cache-control"] == "private, no-cache"
    assert peaks.headers["content-type"] == "application/octet-stream"
    assert not list((tmp_path / "analysis-tmp").iterdir())


def test_peaks_revalidate_with_their_etag_and_are_404_until_stored(tmp_path) -> None:
    store = make_store(tmp_path)
    with serve(create_app(store=store)) as port:
        unknown = request(port, "GET", "/v1/recordings/nope/peaks")
        created = request(
            port,
            "POST",
            "/v1/recordings",
            json.dumps(
                {
                    "recording_id": "call-1.user",
                    "session_key": "call-1",
                    "speaker": "user",
                    "content_type": "audio/wav",
                    "started_at_unix_nano": STARTED_AT,
                }
            ).encode(),
            headers=JSON_HEADERS,
        )
        not_uploaded = request(port, "GET", "/v1/recordings/call-1.user/peaks")
        request(
            port,
            "PUT",
            created.json()["upload_path"],
            (PIPECAT_AUDIO / "user.wav").read_bytes(),
            headers={"Content-Type": "audio/wav"},
        )
        first = _until_ok(port, "/v1/recordings/call-1.user/peaks")
        etag = first.headers["etag"]
        again = request(
            port, "GET", "/v1/recordings/call-1.user/peaks", headers={"If-None-Match": etag}
        )
        other = request(
            port, "GET", "/v1/recordings/call-1.user/peaks", headers={"If-None-Match": '"x"'}
        )

    assert (unknown.status, unknown.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND")
    assert (not_uploaded.status, not_uploaded.problem_code()) == (404, "EARSHOT_PEAKS_NOT_FOUND")
    assert first.status == 200 and etag.startswith('"') and etag.endswith('"')
    assert (again.status, again.body) == (304, b"")
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "private, no-cache"
    assert other.status == 200 and other.body == first.body


def test_a_mono_pair_plays_as_its_stereo_rendition_with_paired_peaks(
    tmp_path, blob_backend
) -> None:
    store = make_store(tmp_path)
    with serve(create_app(store=store, blob_store=blob_backend.store)) as port:
        _upload(port, "call-1.user", "user")
        _upload(port, "call-1.agent", "agent")
        rendition = _until_ok(port, "/v1/recordings/call-1.user/rendition")
        peaks = _until_ok(port, "/v1/recordings/call-1.user/rendition/peaks")
        user_peaks = _until_ok(port, "/v1/recordings/call-1.user/peaks")
        agent_peaks = _until_ok(port, "/v1/recordings/call-1.agent/peaks")
        ranged = request(
            port, "GET", "/v1/recordings/call-1.user/rendition", headers={"Range": "bytes=0-99"}
        )
        partner = request(port, "GET", "/v1/recordings/call-1.agent/rendition")

    assert rendition.status == 200
    assert rendition.headers["content-type"] == "audio/flac"
    assert rendition.headers["cache-control"] == "no-store"
    samples, rate = soundfile.read(io.BytesIO(rendition.body), dtype="int16", always_2d=True)
    user, _ = soundfile.read(PIPECAT_AUDIO / "user.wav", dtype="int16")
    agent, _ = soundfile.read(PIPECAT_AUDIO / "agent.wav", dtype="int16")
    assert rate == 16000 and (samples[:, 0] == user).all() and (samples[:, 1] == agent).all()
    assert ranged.status == 206 and ranged.body == rendition.body[:100]
    assert peaks.body == interleave(user_peaks.body, agent_peaks.body)
    assert read_header(peaks.body).channels == 2
    assert peaks.headers["cache-control"] == "private, no-cache" and peaks.headers["etag"]
    assert (partner.status, partner.problem_code()) == (404, "EARSHOT_RENDITION_NOT_FOUND")
