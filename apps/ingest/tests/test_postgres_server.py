"""One server per Postgres database (``K_server``), with real server processes.

Both entry points are covered, ``earshot serve`` and ``uvicorn apps.ingest.app:app``:
a second server or worker is refused with ``EARSHOT_DATABASE_IN_USE``, and a server
whose lock session is terminated exits with status 70 within two maintenance
ticks, logging ``EARSHOT_DATABASE_LOCK_LOST``. A CLI writer (``K_write``) works
while the server runs. No output ever carries the database password.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest
from sqlalchemy.engine import make_url

from store_support import TEST_CORRELATION_KEY_BASE64, postgres_cluster

pytestmark = [pytest.mark.e2e, pytest.mark.postgres_only]

ROOT = Path(__file__).resolve().parents[3]
TICK_SECONDS = 1.0


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _environment(database_url: str, data_dir: Path, **extra: str) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment.update(
        {
            "EARSHOT_CORRELATION_KEY": TEST_CORRELATION_KEY_BASE64,
            "EARSHOT_DATABASE_URL": database_url,
            "EARSHOT_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
            "PYTHONUNBUFFERED": "1",
            **extra,
        }
    )
    return environment


class _Server:
    """A server process whose combined output goes to a file (never a full pipe)."""

    def __init__(self, command: list[str], environment: dict[str, str], log: Path) -> None:
        self.log = log
        self._handle: IO[bytes] = log.open("wb")
        self.process = subprocess.Popen(
            command, cwd=ROOT, env=environment, stdout=self._handle, stderr=subprocess.STDOUT
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
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as reply:
                    if reply.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
        raise AssertionError(f"server did not become ready:\n{self.output}")

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


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[list[_Server]]:
    started: list[_Server] = []
    yield started
    for server in started:
        server.stop()


def _start(
    servers: list[_Server], command: list[str], url: str, log: Path, **extra: str
) -> _Server:
    server = _Server(command, _environment(url, log.parent / "data", **extra), log)
    servers.append(server)
    return server


def _server_lock_backend(postgres_admin, database_name: str) -> int:
    rows = postgres_admin.execute(
        "SELECT pid FROM pg_stat_activity WHERE datname = %s "
        "AND application_name = 'earshot-server-lock'",
        (database_name,),
    ).fetchall()
    assert len(rows) == 1, rows
    return int(rows[0][0])


def _cli(url: str, data_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "earshot.cli", *arguments],
        cwd=ROOT,
        env=_environment(url, data_dir),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _status(port: int, path: str, *, credential: str | None = None) -> int:
    headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return int(reply.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def test_a_second_server_on_the_same_database_is_refused(tmp_path, servers) -> None:
    url = postgres_cluster().database_for(tmp_path)
    first_port, second_port = _free_port(), _free_port()
    first = _start(servers, _serve_command(first_port), url, tmp_path / "first.log")
    first.wait_ready(first_port)

    second = _start(servers, _serve_command(second_port), url, tmp_path / "second.log")
    returncode = second.process.wait(timeout=30)

    assert returncode != 0
    assert "EARSHOT_DATABASE_IN_USE" in second.output
    assert _status(first_port, "/readyz") == 200


def test_uvicorn_with_two_workers_is_refused_and_stops(tmp_path, servers) -> None:
    url = postgres_cluster().database_for(tmp_path)
    port = _free_port()
    server = _start(servers, _uvicorn_command(port, "--workers", "2"), url, tmp_path / "w.log")

    server.process.wait(timeout=60)

    output = server.output
    assert "EARSHOT_DATABASE_IN_USE" in output
    # The refused worker exits with uvicorn's startup-failure status, which makes
    # the supervisor stop the whole server instead of restarting that worker.
    assert "failed to start, stopping the parent process" in output
    assert _status_or_none(port) is None


def _status_or_none(port: int) -> int | None:
    try:
        return _status(port, "/readyz")
    except (urllib.error.URLError, OSError):
        return None


@pytest.mark.parametrize("entry_point", ["earshot-serve", "uvicorn"])
def test_losing_the_server_lock_exits_within_two_ticks(
    tmp_path, servers, postgres_admin, entry_point: str
) -> None:
    url = postgres_cluster().database_for(tmp_path)
    port = _free_port()
    command = _serve_command(port) if entry_point == "earshot-serve" else _uvicorn_command(port)
    server = _start(servers, command, url, tmp_path / "server.log")
    server.wait_ready(port)
    backend = _server_lock_backend(postgres_admin, postgres_cluster().database_name(tmp_path))

    terminated_at = time.monotonic()
    postgres_admin.execute("SELECT pg_terminate_backend(%s)", (backend,))
    returncode = server.process.wait(timeout=10)
    elapsed = time.monotonic() - terminated_at

    assert returncode == 70
    assert elapsed <= 2 * TICK_SECONDS, elapsed
    assert "EARSHOT_DATABASE_LOCK_LOST" in server.output


def test_cli_revocation_takes_effect_while_the_server_runs(tmp_path, servers) -> None:
    url = postgres_cluster().database_for(tmp_path)
    data_dir = tmp_path / "cli"
    port = _free_port()
    server = _start(servers, _serve_command(port), url, tmp_path / "server.log")
    server.wait_ready(port)

    issued = _cli(url, data_dir, "api-key", "issue", "--project", "default", "--label", "ops")
    assert issued.returncode == 0, issued.stderr
    key = json.loads(issued.stdout)
    assert _status(port, "/v1/incidents", credential=key["credential"]) == 200

    revoked = _cli(
        url, data_dir, "api-key", "revoke", "--project", "default", "--key-id", key["key_id"]
    )

    assert revoked.returncode == 0, revoked.stderr
    assert json.loads(revoked.stdout)["revoked"] is True
    assert _status(port, "/v1/incidents", credential=key["credential"]) == 401
    assert _status(port, "/readyz") == 200


def test_no_output_carries_the_database_password_or_an_s3_secret(
    tmp_path, servers, postgres_admin
) -> None:
    role = f"earshot_pw_{secrets.token_hex(4)}"
    password = f"pw-{secrets.token_urlsafe(18)}"
    s3_key_id = f"AKIA{secrets.token_hex(8).upper()}"
    s3_secret = f"s3-{secrets.token_urlsafe(24)}"
    blob_store = {
        "EARSHOT_BLOB_URL": "s3://earshot-logs/server?endpoint=http://127.0.0.1:59000&path_style=1",
        "AWS_ACCESS_KEY_ID": s3_key_id,
        "AWS_SECRET_ACCESS_KEY": s3_secret,
    }
    # Credentials pasted into the URL are refused, and the refusal must not echo them.
    leaky_blob_store = {"EARSHOT_BLOB_URL": f"s3://{s3_key_id}:{s3_secret}@earshot-logs/server"}
    postgres_admin.execute(f"CREATE ROLE {role} LOGIN SUPERUSER PASSWORD '{password}'")
    database_name = None
    try:
        admin_url = postgres_cluster().database_for(tmp_path)
        database_name = postgres_cluster().database_name(tmp_path)
        url = make_url(admin_url).set(username=role, password=password)
        url = url.render_as_string(hide_password=False)
        wrong = make_url(url).set(password=password[::-1]).render_as_string(hide_password=False)
        outputs: dict[str, str] = {}
        for entry_point, command in (("serve", _serve_command), ("uvicorn", _uvicorn_command)):
            port = _free_port()
            server = _start(
                servers, command(port), url, tmp_path / f"{entry_point}.log", **blob_store
            )
            server.wait_ready(port)
            for path in ("/readyz", "/v1/incidents", "/v1/incidents/missing"):
                _status(port, path)
            _status(port, "/v1/incidents", credential="earshot_sk_0000000000000000.wrong")
            refused = {
                "second": _start(
                    servers,
                    command(_free_port()),
                    url,
                    tmp_path / f"{entry_point}-second.log",
                    **blob_store,
                ),
                "wrong password": _start(
                    servers,
                    command(_free_port()),
                    wrong,
                    tmp_path / f"{entry_point}-wrong.log",
                    **blob_store,
                ),
                "credentials in the blob URL": _start(
                    servers,
                    command(_free_port()),
                    url,
                    tmp_path / f"{entry_point}-leaky.log",
                    **leaky_blob_store,
                ),
            }
            for name, process in refused.items():
                assert process.process.wait(timeout=60) != 0, f"{entry_point}: {name} started"
                outputs[f"{entry_point} ({name})"] = process.output
            server.stop()
            outputs[entry_point] = server.output
            assert "EARSHOT_DATABASE_IN_USE" in refused["second"].output, entry_point
            assert "EARSHOT_BLOB_URL" in refused["credentials in the blob URL"].output
        cli_outputs = [
            _cli(url, tmp_path / "cli", "db", "current"),
            _cli(url, tmp_path / "cli", "list"),
            _cli(wrong, tmp_path / "cli", "list"),
        ]

        for name, result in zip(("db current", "list", "list (wrong)"), cli_outputs, strict=True):
            outputs[name] = result.stdout + result.stderr
        # Failure messages must never echo the output: it is what might leak.
        leaked = sorted(
            name
            for name, output in outputs.items()
            if any(secret in output for secret in (password, password[::-1], s3_secret))
        )
        assert leaked == []
        banner = [line for line in outputs["serve"].splitlines() if "Earshot database:" in line]
        assert len(banner) == 1 and "***@" in banner[0] and password not in banner[0], "banner"
        assert cli_outputs[2].returncode == 2
    finally:
        if database_name is not None:
            postgres_admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                (role,),
            )
        postgres_admin.execute(f"DROP ROLE IF EXISTS {role}")
