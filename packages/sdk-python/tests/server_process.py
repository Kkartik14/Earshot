"""Real server processes for tests: ``uvicorn apps.ingest.app:app`` or ``earshot serve``.

A ``ServerProcess`` runs one of Earshot's two entry points (or a test-supplied
command that serves the same app) on the test's store, configured only through
the environment as an operator would: the data directory, and under
``--db postgresql`` the test database and the correlation key (unless the test
names its own ``EARSHOT_DATABASE_URL``). A variable passed as ``None`` is removed
from the inherited environment. Its combined output goes to a log file, never a
pipe that could fill up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from otlp_support import free_port
from store_support import TEST_CORRELATION_KEY_BASE64, current_dialect, postgres_cluster

ROOT = Path(__file__).resolve().parents[3]


def entry_point_command(entry_point: str, port: int) -> list[str]:
    """The command line of one of the two server entry points, listening on ``port``."""

    if entry_point == "uvicorn":
        return [
            *(sys.executable, "-m", "uvicorn", "apps.ingest.app:app"),
            *("--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"),
        ]
    if entry_point == "serve":
        return [
            *(sys.executable, "-m", "earshot.cli", "serve"),
            *("--port", str(port), "--log-level", "warning"),
        ]
    raise ValueError(f"unknown entry point {entry_point!r}")


class ServerProcess:
    """One server process on the test's store, output to ``log``."""

    def __init__(
        self,
        path: Path,
        log: Path,
        *,
        entry_point: str = "uvicorn",
        command: Sequence[str] | None = None,
        **environment: str | None,
    ) -> None:
        self.port = free_port()
        child = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EARSHOT_") or key == "EARSHOT_TEST_POSTGRES_URL"
        }
        child.update(
            {
                "EARSHOT_DATA_DIR": str(path),
                "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        for key, value in environment.items():
            if value is None:
                child.pop(key, None)
            else:
                child[key] = value
        if current_dialect() == "postgresql" and "EARSHOT_DATABASE_URL" not in environment:
            child["EARSHOT_DATABASE_URL"] = postgres_cluster().database_for(path)
            child["EARSHOT_CORRELATION_KEY"] = TEST_CORRELATION_KEY_BASE64
        self.log = log
        self._handle: IO[bytes] = log.open("ab")
        argv = (
            entry_point_command(entry_point, self.port)
            if command is None
            else [*command, str(self.port)]
        )
        self.process = subprocess.Popen(
            argv, cwd=ROOT, env=child, stdout=self._handle, stderr=subprocess.STDOUT
        )

    @classmethod
    def start(
        cls,
        path: Path,
        log: Path,
        *,
        entry_point: str = "uvicorn",
        command: Sequence[str] | None = None,
        **environment: str | None,
    ) -> ServerProcess:
        # After a SIGKILL, Postgres may hold the dead server's K_server session for
        # a moment; a refused start is retried.
        deadline = time.monotonic() + 30
        while True:
            server = cls(path, log, entry_point=entry_point, command=command, **environment)
            if server.wait_ready(deadline):
                return server
            server.stop()
            assert time.monotonic() < deadline, log.read_text(errors="replace")
            time.sleep(0.2)

    def wait_ready(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                url = f"http://127.0.0.1:{self.port}/readyz"
                with urllib.request.urlopen(url, timeout=1) as reply:
                    if reply.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
        return False

    def kill(self) -> None:
        self.process.kill()  # SIGKILL: no cleanup, no flush, no shutdown
        self.process.wait(timeout=10)
        self._handle.close()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._handle.close()
