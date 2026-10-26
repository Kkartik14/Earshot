"""One server per SQLite data directory (``.server.lock``), with real server processes.

Both entry points are covered, ``earshot serve`` and ``uvicorn apps.ingest.app:app``:
a second server or worker on the same data directory is refused with
``EARSHOT_DATABASE_IN_USE``, as on Postgres (``test_postgres_server.py``). A CLI
writer still works while the server runs: it takes the per-mutation store lock,
never the server lock.

The last test covers two overlapping server processes: a second server
started beside a busy first one used to drain the first one's journaled upload
keys and sweep its spool files, losing acknowledged recordings. It must now be
refused, and every acknowledged upload must read back.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.sqlite_only]

ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _environment(data_dir: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment.update(
        {
            "EARSHOT_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


class _Server:
    """A server process whose combined output goes to a file (never a full pipe)."""

    def __init__(self, command: list[str], data_dir: Path, log: Path) -> None:
        self.log = log
        self._handle: IO[bytes] = log.open("wb")
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=_environment(data_dir),
            stdout=self._handle,
            stderr=subprocess.STDOUT,
        )

    @property
    def output(self) -> str:
        if not self._handle.closed:
            self._handle.flush()
        return self.log.read_text(encoding="utf-8", errors="replace")

    def wait_ready(self, port: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"server exited early:\n{self.output}")
            if _status_or_none(port) == 200:
                return
            time.sleep(0.05)
        raise AssertionError(f"server did not become ready:\n{self.output}")

    def wait_exit(self, timeout: float = 30.0) -> int | None:
        """The exit status, or ``None`` if the server is still running at ``timeout``."""

        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._handle.close()


def _serve_command(port: int) -> list[str]:
    return [
        *(sys.executable, "-m", "earshot.cli", "serve"),
        *("--port", str(port), "--log-level", "info"),
    ]


def _uvicorn_command(port: int, *extra: str) -> list[str]:
    return [
        *(sys.executable, "-m", "uvicorn", "apps.ingest.app:app"),
        *("--host", "127.0.0.1", "--port", str(port), "--log-level", "info", *extra),
    ]


_COMMANDS = {"earshot-serve": _serve_command, "uvicorn": _uvicorn_command}
# What each entry point exits with when it refuses to start (docs/production.md).
_REFUSED_STATUS = {"earshot-serve": 2, "uvicorn": 3}


@pytest.fixture
def servers() -> Iterator[list[_Server]]:
    started: list[_Server] = []
    yield started
    for server in started:
        server.stop()


def _start(servers: list[_Server], command: list[str], data_dir: Path, log: Path) -> _Server:
    server = _Server(command, data_dir, log)
    servers.append(server)
    return server


def _status_or_none(port: int, path: str = "/readyz", credential: str | None = None) -> int | None:
    headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        with urllib.request.urlopen(request, timeout=5) as reply:
            return int(reply.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError):
        return None


def _cli(data_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "earshot.cli", *arguments],
        cwd=ROOT,
        env=_environment(data_dir),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("entry_point", ["earshot-serve", "uvicorn"])
def test_a_second_server_on_the_same_data_directory_is_refused(
    tmp_path, servers, entry_point: str
) -> None:
    data_dir = tmp_path / "data"
    command = _COMMANDS[entry_point]
    first_port, second_port = _free_port(), _free_port()
    first = _start(servers, command(first_port), data_dir, tmp_path / "first.log")
    first.wait_ready(first_port)

    second = _start(servers, command(second_port), data_dir, tmp_path / "second.log")
    returncode = second.wait_exit()

    assert returncode == _REFUSED_STATUS[entry_point], second.output
    assert "EARSHOT_DATABASE_IN_USE" in second.output
    assert _status_or_none(first_port) == 200


def test_uvicorn_with_two_workers_is_refused_and_stops(tmp_path, servers) -> None:
    port = _free_port()
    server = _start(
        servers, _uvicorn_command(port, "--workers", "2"), tmp_path / "data", tmp_path / "w.log"
    )

    returncode = server.wait_exit(timeout=60)

    output = server.output
    assert returncode is not None, output
    assert "EARSHOT_DATABASE_IN_USE" in output
    # The refused worker exits with uvicorn's startup-failure status, which makes
    # the supervisor stop the whole server instead of restarting that worker.
    assert "failed to start, stopping the parent process" in output
    assert _status_or_none(port) is None


def test_a_server_can_start_again_once_the_first_one_stopped(tmp_path, servers) -> None:
    data_dir = tmp_path / "data"
    port = _free_port()
    first = _start(servers, _serve_command(port), data_dir, tmp_path / "first.log")
    first.wait_ready(port)
    first.stop()

    again = _start(servers, _serve_command(port), data_dir, tmp_path / "again.log")

    again.wait_ready(port)


def test_cli_revocation_takes_effect_while_the_server_runs(tmp_path, servers) -> None:
    data_dir = tmp_path / "data"
    port = _free_port()
    server = _start(servers, _serve_command(port), data_dir, tmp_path / "server.log")
    server.wait_ready(port)

    issued = _cli(data_dir, "api-key", "issue", "--project", "default", "--label", "ops")
    assert issued.returncode == 0, issued.stderr
    key = json.loads(issued.stdout)
    assert _status_or_none(port, "/v1/incidents", key["credential"]) == 200

    revoked = _cli(data_dir, "api-key", "revoke", "--project", "default", "--key-id", key["key_id"])

    assert revoked.returncode == 0, revoked.stderr
    assert json.loads(revoked.stdout)["revoked"] is True
    assert _status_or_none(port, "/v1/incidents", key["credential"]) == 401
    assert _status_or_none(port) == 200


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x05\x00" * 400)
    return buffer.getvalue()


def _call(
    connection: http.client.HTTPConnection, method: str, path: str, body: bytes | None, kind: str
) -> tuple[int, bytes]:
    connection.request(method, path, body=body, headers={"Content-Type": kind})
    reply = connection.getresponse()
    return reply.status, reply.read()


def test_a_restart_overlap_is_refused_and_loses_no_acknowledged_upload(tmp_path, servers) -> None:
    """Server B cannot open A's data directory while A holds the lifetime lock.
    Every upload acknowledged by A must still be readable afterward."""

    data_dir = tmp_path / "data"
    port = _free_port()
    first = _start(servers, _uvicorn_command(port), data_dir, tmp_path / "a.log")
    first.wait_ready(port)
    overlap = _start(servers, _uvicorn_command(_free_port()), data_dir, tmp_path / "b.log")
    body = _wav()
    acknowledged: list[str] = []
    failures: list[str] = []

    def upload(worker: int, count: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            for index in range(count):
                recording_id = f"r{worker}-{index}"
                manifest = {
                    "recording_id": recording_id,
                    "session_key": f"s{worker}-{index}",
                    "speaker": "user",
                    "content_type": "audio/wav",
                    "started_at_unix_nano": "1800000000000000000",
                }
                status, created = _call(
                    connection,
                    "POST",
                    "/v1/recordings",
                    json.dumps(manifest).encode(),
                    "application/json",
                )
                if status != 201:
                    failures.append(f"POST {recording_id}: {status}")
                    continue
                path = json.loads(created)["upload_path"]
                status, _ = _call(connection, "PUT", path, body, "audio/wav")
                if status == 200:
                    acknowledged.append(recording_id)
                else:
                    failures.append(f"PUT {recording_id}: {status}")
        finally:
            connection.close()

    workers = [threading.Thread(target=upload, args=(worker, 100)) for worker in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    refused = overlap.wait_exit(timeout=30)

    assert refused == 3, overlap.output
    assert "EARSHOT_DATABASE_IN_USE" in overlap.output
    assert failures == []
    assert len(acknowledged) == 400
    reader = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        lost = []
        for recording_id in acknowledged:
            status, content = _call(
                reader, "GET", f"/v1/recordings/{recording_id}/content", None, "audio/wav"
            )
            if (status, content) != (200, body):
                lost.append((recording_id, status))
    finally:
        reader.close()
    assert lost == []
