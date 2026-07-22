"""Install one release artifact in isolation and exercise its public surfaces."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
from pathlib import Path


def _venv_executable(root: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return root / directory / f"{name}{suffix}"


def _run(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read_when_ready(url: str, process: subprocess.Popen[str], *, timeout: float = 15) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"server exited before {url} was ready\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status != 200:
                    raise RuntimeError(f"{url} returned HTTP {response.status}")
                return response.read()
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"server did not make {url} ready: {last_error}")


def _smoke_server(
    earshot: Path,
    *,
    workspace: Path,
    environment: dict[str, str],
) -> None:
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            str(earshot),
            "serve",
            "--data-dir",
            str(workspace / "server-data"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=workspace,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        readiness = json.loads(_read_when_ready(f"{base_url}/readyz", process))
        if readiness != {"status": "ready"}:
            raise RuntimeError(f"unexpected readiness response: {readiness!r}")
        incidents = json.loads(_read_when_ready(f"{base_url}/v1/incidents", process))
        if incidents.get("items") != []:
            raise RuntimeError(f"unexpected incident list response: {incidents!r}")

        index = _read_when_ready(f"{base_url}/", process)
        if b'<div id="root"></div>' not in index:
            raise RuntimeError("installed server did not serve the bundled viewer index")
        match = re.search(rb'(?:src|href)="([^"]*/assets/[^"]+\.(?:js|css))"', index)
        if match is None:
            raise RuntimeError("bundled viewer index did not reference a compiled asset")
        asset_url = urllib.parse.urljoin(f"{base_url}/", match.group(1).decode("utf-8"))
        if not _read_when_ready(asset_url, process):
            raise RuntimeError("bundled viewer asset was empty")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def smoke_artifact(path: Path) -> None:
    artifact = path.resolve()
    if not artifact.is_file() or not (
        artifact.suffix == ".whl" or artifact.name.endswith(".tar.gz")
    ):
        raise SystemExit(f"{artifact}: expected a wheel or source distribution")

    with tempfile.TemporaryDirectory(prefix="earshot-artifact-") as temporary:
        workspace = Path(temporary)
        virtual_environment = workspace / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(virtual_environment)
        python = _venv_executable(virtual_environment, "python")
        earshot = _venv_executable(virtual_environment, "earshot")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        _run(
            [str(python), "-m", "pip", "install", "--no-cache-dir", str(artifact)],
            cwd=workspace,
            environment=environment,
        )
        _run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, earshot; "
                    "assert earshot.Client is not None; "
                    "client = earshot.Client(); client.status(); client.shutdown(); "
                    "assert importlib.util.find_spec('fastapi') is None; "
                    "assert importlib.util.find_spec('uvicorn') is None"
                ),
            ],
            cwd=workspace,
            environment=environment,
        )
        _run([str(earshot), "--help"], cwd=workspace, environment=environment)
        listed = _run(
            [str(earshot), "list", "--data-dir", str(workspace / "cli-data")],
            cwd=workspace,
            environment=environment,
        )
        if json.loads(listed.stdout).get("items") != []:
            raise RuntimeError("base CLI did not return an empty incident list")

        server_requirement = f"earshot-observability[server] @ {artifact.as_uri()}"
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                server_requirement,
            ],
            cwd=workspace,
            environment=environment,
        )
        _run(
            [
                str(python),
                "-c",
                (
                    "import fastapi, uvicorn; "
                    "from earshot.api import ApiConfig, create_app; "
                    "assert create_app and ApiConfig"
                ),
            ],
            cwd=workspace,
            environment=environment,
        )
        _smoke_server(earshot, workspace=workspace, environment=environment)


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise SystemExit("usage: smoke_artifact.py PATH_TO_WHEEL_OR_SDIST")
    path = Path(arguments[0])
    smoke_artifact(path)
    print(f"clean artifact smoke passed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
