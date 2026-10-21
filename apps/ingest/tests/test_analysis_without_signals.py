"""A server without the ``signals`` extra runs, and analyses once the extra is there (AC2.6, M10).

A real ``earshot serve`` from a fresh virtual environment holding only the
``server`` extra (no numpy, onnxruntime or soundfile) stores an uploaded recording
and marks its analysis ``unavailable`` with ``EARSHOT_SIGNALS_EXTRA_REQUIRED``,
while it keeps serving. The same data directory served from this checkout's
environment, which has the extra, requeues that job at start and stores its peaks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

from otlp_support import request
from server_process import ServerProcess
from store_support import catalog_rows, make_store

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.sqlite_only]

ROOT = Path(__file__).resolve().parents[3]
USER_WAV = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio" / "user.wav"


def _server_only_python(workspace: Path) -> Path:
    """A fresh environment with this checkout's ``server`` extra and nothing else."""

    environment_dir = workspace / "server-venv"
    python = environment_dir / "bin" / "python"
    uv = shutil.which("uv")
    if uv is not None:
        subprocess.run(
            [uv, "venv", "--quiet", "--python", sys.executable, str(environment_dir)],
            check=True,
            timeout=120,
        )
        subprocess.run(
            [uv, "pip", "install", "--quiet", "--python", str(python), "-e", f"{ROOT}[server]"],
            check=True,
            timeout=600,
        )
    else:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_dir)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "-e", f"{ROOT}[server]"],
            check=True,
            timeout=900,
        )
    return python


def _job(path: Path) -> tuple | None:
    rows = catalog_rows(path, "SELECT state, error_code FROM audio_analysis")
    return rows[0] if rows else None


def test_without_the_extra_analyses_are_unavailable_and_requeued_once_it_is_installed(
    tmp_path,
) -> None:
    python = _server_only_python(tmp_path)
    probe = subprocess.run(
        [str(python), "-c", "import earshot.signals as s; print(s.extra_available())"],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"), "PATH": "/usr/bin"},
    )
    assert probe.stdout.strip() == "False"
    data = tmp_path / "data"
    make_store(data).close()

    server = ServerProcess.start(
        data,
        tmp_path / "server-only.log",
        command=[str(python), "-m", "earshot.cli", "serve", "--log-level", "warning", "--port"],
    )
    try:
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
            USER_WAV.read_bytes(),
            headers={"Content-Type": "audio/wav"},
        )
        deadline = time.monotonic() + 60
        while _job(data) != ("unavailable", "EARSHOT_SIGNALS_EXTRA_REQUIRED"):
            assert time.monotonic() < deadline, _job(data)
            time.sleep(0.1)
        ready = request(server.port, "GET", "/readyz")
        peaks = request(server.port, "GET", "/v1/recordings/call-1.user/peaks")
    finally:
        server.stop()

    assert uploaded.status == 200, uploaded.body
    assert ready.status == 200
    assert (peaks.status, peaks.problem_code()) == (404, "EARSHOT_PEAKS_NOT_FOUND")

    # The same data directory, served by an environment with the extra.
    server = ServerProcess.start(data, tmp_path / "with-extra.log", entry_point="serve")
    try:
        deadline = time.monotonic() + 90
        while (
            peaks := request(server.port, "GET", "/v1/recordings/call-1.user/peaks")
        ).status != 200:
            assert time.monotonic() < deadline, peaks.body
            time.sleep(0.1)
    finally:
        server.stop()

    assert _job(data) == ("stored", None)
