"""Real tools for the install tests: Docker, Compose, Helm and kubeconform.

The install tests run the shipped deployment files (``compose.yaml``,
``compose.dev.yaml``, ``compose.full.yaml``, ``deploy/helm/earshot`` and
``deploy/railway``) with the tools an operator uses. Nothing is simulated.

They are opt-in, because they need those tools, pull images and take minutes:
set ``EARSHOT_TEST_INSTALL=1``. Once opted in, a missing tool fails the test
instead of skipping it, so a CI job that opts in cannot pass vacuously.

The image under test is the one ``compose.yaml`` names (the GHCR release tag),
built locally from this tree, for example::

    docker build --tag ghcr.io/kkartik14/earshot:0.4 .

Compose stacks run from a fresh directory holding copies of the Compose files,
as a user who downloaded them would, and the steps an install page marks with
``<!-- earshot-step: NAME -->`` run verbatim, so the docs and the tests cannot
drift. Each test names everything it creates with its own random
``earshot-install-<hex>`` prefix (containers, networks, volumes and Compose
projects) and removes all of it when it ends, whatever the outcome. Secret
values reach ``docker`` through the environment of the CLI process, never its
arguments, and are never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from call_done_when import Earshot, SentCall, script_words, wav_seconds
from captured_otlp import FIXTURE, captured_manifest

ROOT = Path(__file__).resolve().parents[4]
OPT_IN_ENVIRONMENT = "EARSHOT_TEST_INSTALL"
QUICK_START = ROOT / "compose.yaml"
DEV_OVERRIDE = ROOT / "compose.dev.yaml"
FULL_STACK = ROOT / "compose.full.yaml"
CHART = ROOT / "deploy" / "helm" / "earshot"
RAILWAY_CONFIG = ROOT / "deploy" / "railway" / "railway.toml"
COMPOSE_PAGE = ROOT / "docs" / "install-compose.md"
SETTLE_TIMEOUT_S = 120.0
LOSS_COUNTERS = ("rejected_spans", "span_conflicts", "parked_traces_discarded")
# The variables a tool process keeps from the test's environment: what finds the
# daemon and the CLI's own configuration, and nothing of the developer's Earshot,
# Postgres or provider settings.
_KEPT_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


def require_install_tests() -> None:
    """Skip unless the install tests were asked for."""

    if os.environ.get(OPT_IN_ENVIRONMENT) != "1":
        pytest.skip(f"install tests are opt-in: set {OPT_IN_ENVIRONMENT}=1 (install_support.py)")


def require_tool(name: str) -> str:
    require_install_tests()
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{OPT_IN_ENVIRONMENT}=1 needs {name!r} on PATH")
    return path


def tool_environment(**extra: str) -> dict[str, str]:
    environment = {key: os.environ[key] for key in _KEPT_ENVIRONMENT if key in os.environ}
    environment.update(extra)
    return environment


def run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin: str | None = None,
    timeout: float = 300.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one tool; on an unexpected failure, fail with its output (never its env)."""

    completed = subprocess.run(
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=dict(environment) if environment is not None else tool_environment(),
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        pytest.fail(
            f"{' '.join(command)[:300]} exited {completed.returncode}:\n"
            f"{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}"
        )
    return completed


def random_prefix() -> str:
    return f"earshot-install-{secrets.token_hex(4)}"


def quick_start_image() -> str:
    """The image ``compose.yaml`` runs; it must exist locally (built from this tree)."""

    require_tool("docker")
    image = yaml.safe_load(QUICK_START.read_text(encoding="utf-8"))["services"]["earshot"]["image"]
    if run(["docker", "image", "inspect", image], check=False).returncode != 0:
        pytest.fail(f"build the image under test first: docker build --tag {image} .")
    return image


def wait_for(description: str, probe: Any, *, timeout: float, interval: float = 0.5) -> Any:
    """Poll ``probe`` until it returns a truthy value; fail after ``timeout`` seconds."""

    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout} s waiting for {description}")
        time.sleep(interval)


def doc_step(page: Path, name: str) -> str:
    """The fenced block right after ``<!-- earshot-step: NAME -->`` on a docs page."""

    pattern = re.compile(
        r"<!-- earshot-step: " + re.escape(name) + r" -->\s*```[a-z]*\n(.*?)\n```", re.DOTALL
    )
    found = pattern.findall(page.read_text(encoding="utf-8"))
    assert len(found) == 1, f"{page.name} must mark exactly one block as {name!r}"
    return found[0]


# ---------------------------------------------------------------------------
# Plain containers


@dataclass
class DockerScope:
    """Containers and networks one test created; all removed by ``close``."""

    prefix: str = field(default_factory=random_prefix)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)

    def docker(self, *arguments: str, check: bool = True, timeout: float = 300.0):
        return run(
            ["docker", *arguments],
            environment=tool_environment(**self.secrets),
            check=check,
            timeout=timeout,
        )

    def network(self, name: str) -> str:
        full = f"{self.prefix}-{name}"
        self.docker("network", "create", full)
        self.networks.append(full)
        return full

    def start(
        self,
        name: str,
        image: str,
        *arguments: str,
        network: str | None = None,
        alias: str | None = None,
        environment: Mapping[str, str] | None = None,
        secret_names: Sequence[str] = (),
        options: Sequence[str] = (),
        entrypoint: str | None = None,
    ) -> str:
        """``docker run --detach``; ``secret_names`` pass from ``self.secrets`` by name."""

        full = f"{self.prefix}-{name}"
        command = ["run", "--detach", "--name", full]
        if network is not None:
            command += ["--network", network]
        if alias is not None:
            command += ["--network-alias", alias]
        for key, value in (environment or {}).items():
            command += ["--env", f"{key}={value}"]
        for key in secret_names:
            command += ["--env", key]
        if entrypoint is not None:
            command += ["--entrypoint", entrypoint]
        self.containers.append(full)
        self.docker(*command, *options, image, *arguments)
        return full

    def remove(self, container: str) -> None:
        self.docker("rm", "--force", "--volumes", container, check=False)
        if container in self.containers:
            self.containers.remove(container)

    def state(self, container: str) -> tuple[str, int]:
        """The container's status (``running``, ``exited``…) and exit code."""

        shown = self.docker(
            "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container
        ).stdout.split()
        return shown[0], int(shown[1])

    def logs(self, container: str) -> str:
        shown = self.docker("logs", container, check=False)
        return shown.stdout + shown.stderr

    def close(self) -> None:
        for container in reversed(self.containers):
            self.docker("rm", "--force", "--volumes", container, check=False)
        for network in self.networks:
            self.docker("network", "rm", network, check=False)


@contextmanager
def docker_scope() -> Iterator[DockerScope]:
    require_tool("docker")
    scope = DockerScope()
    try:
        yield scope
    finally:
        scope.close()


def start_postgres(scope: DockerScope, network: str, *, publish: bool = False) -> str:
    """A throwaway Postgres 17 on ``network`` (alias ``postgres``), accepting connections."""

    scope.secrets.setdefault("POSTGRES_PASSWORD", secrets.token_hex(16))
    container = scope.start(
        "postgres",
        "postgres:17",
        network=network,
        alias="postgres",
        environment={"POSTGRES_USER": "earshot", "POSTGRES_DB": "earshot"},
        secret_names=["POSTGRES_PASSWORD"],
        options=["--publish", "127.0.0.1::5432"] if publish else [],
    )
    ready = ("pg_isready", "--host", "127.0.0.1", "--username", "earshot")
    wait_for(
        "Postgres to accept connections",
        lambda: scope.docker("exec", container, *ready, check=False).returncode == 0,
        timeout=60,
    )
    return container


def published_port(scope: DockerScope, container: str, port: int) -> int:
    shown = scope.docker("port", container, f"{port}/tcp").stdout.splitlines()[0]
    return int(shown.rsplit(":", 1)[1])


def server_ready_inside(scope: DockerScope, container: str) -> bool:
    """Whether the Earshot server in ``container`` answers ``/readyz`` with 200."""

    probe = (
        "import urllib.request;"
        "urllib.request.urlopen('http://127.0.0.1:4319/readyz', timeout=2).read()"
    )
    return scope.docker("exec", container, "python", "-c", probe, check=False).returncode == 0


# ---------------------------------------------------------------------------
# Compose projects


@dataclass
class ComposeProject:
    """One Compose project, run from ``directory`` as its user would run it.

    ``files`` are copied into ``directory``; commands run there with the project
    name and the published Earshot port set through the environment, so the
    docs' own commands (``docker compose up -d --wait``) work unchanged.
    """

    directory: Path
    files: tuple[str, ...]
    name: str = field(default_factory=random_prefix)
    port: int = field(default_factory=lambda: _free_port())

    @property
    def environment(self) -> dict[str, str]:
        return tool_environment(
            COMPOSE_PROJECT_NAME=self.name, EARSHOT_PUBLISHED_PORT=str(self.port)
        )

    def compose(self, *arguments: str, files: Sequence[str] | None = None, check: bool = True):
        flags = [part for file in (files or self.files) for part in ("-f", file)]
        return run(
            ["docker", "compose", *flags, *arguments],
            environment=self.environment,
            cwd=self.directory,
            timeout=600,
            check=check,
        )

    def step(self, name: str, page: Path = COMPOSE_PAGE) -> subprocess.CompletedProcess[str]:
        """Run one marked step of an install page, verbatim, in the project directory."""

        return run(
            ["bash", "-e", "-c", doc_step(page, name)],
            environment=self.environment,
            cwd=self.directory,
            timeout=600,
        )

    def dotenv(self) -> dict[str, str]:
        lines = (self.directory / ".env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def client(self, credential: str | None = None) -> Earshot:
        """An HTTP client of the published server. Its catalog and blobs live in
        containers, so only the API is read (``catalog`` and ``read_blob`` unused)."""

        def unavailable(key: str) -> bytes:
            raise AssertionError("blobs are read through the API in the install tests")

        return Earshot(
            port=self.port,
            project_id="default",
            catalog="",
            read_blob=unavailable,
            credential=credential,
        )

    @property
    def label(self) -> str:
        return f"label=com.docker.compose.project={self.name}"

    def containers(self) -> list[dict[str, Any]]:
        shown = run(
            ["docker", "ps", "--all", "--quiet", "--filter", self.label],
            environment=self.environment,
        ).stdout.split()
        if not shown:
            return []
        inspected = run(["docker", "inspect", *shown], environment=self.environment).stdout
        return json.loads(inspected)

    def volumes(self) -> list[str]:
        listed = run(
            ["docker", "volume", "ls", "--quiet", "--filter", self.label],
            environment=self.environment,
        )
        return listed.stdout.split()

    def close(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "5", check=False)
        for kind in ("volume", "network"):
            listed = run(
                ["docker", kind, "ls", "--quiet", "--filter", self.label],
                environment=self.environment,
                check=False,
            ).stdout.split()
            for name in listed:
                run(["docker", kind, "rm", name], environment=self.environment, check=False)


@contextmanager
def compose_project(directory: Path, *sources: Path) -> Iterator[ComposeProject]:
    """A project in ``directory`` holding copies of ``sources``; removed at the end."""

    require_tool("docker")
    directory.mkdir(parents=True, exist_ok=True)
    for source in sources:
        shutil.copyfile(source, directory / source.name)
    project = ComposeProject(directory, tuple(source.name for source in sources))
    try:
        yield project
    finally:
        project.close()


def _free_port() -> int:
    from otlp_support import free_port

    return free_port()


def assert_hardened_on_loopback(project: ComposeProject) -> None:
    """Every container: read-only root, no capabilities, no privilege escalation, and
    every published port on host loopback only."""

    containers = project.containers()
    assert containers
    for container in containers:
        name, host = container["Name"], container["HostConfig"]
        assert host["ReadonlyRootfs"] is True, name
        assert host["CapDrop"] == ["ALL"], name
        assert "no-new-privileges:true" in host["SecurityOpt"], name
        bindings = [
            binding for listed in (host["PortBindings"] or {}).values() for binding in listed
        ]
        assert all(binding["HostIp"] == "127.0.0.1" for binding in bindings), (name, bindings)


# ---------------------------------------------------------------------------
# One replayed call, checked through the API


def replay_captured_call(earshot: Earshot) -> SentCall:
    """Send the real Pipecat 1.5 capture as its example did: both recordings
    created at the call's first audio, the exporter's requests in arrival order,
    then both tracks uploaded."""

    manifest = captured_manifest()
    audio = manifest["audio"]
    sent = SentCall(
        session_key=manifest["provenance"]["conversation_id"],
        exported_span_count=manifest["provenance"]["exported_span_count_tee"],
        started_at_unix_nano=int(audio["started_at_unix_nano"]),
        tracks={
            speaker: (FIXTURE / audio[speaker]["file"]).read_bytes()
            for speaker in ("user", "agent")
        },
    )
    for speaker in sent.tracks:
        created = earshot.json(
            "POST",
            "/v1/recordings",
            {
                "recording_id": f"{sent.session_key}.{speaker}",
                "session_key": sent.session_key,
                "speaker": speaker,
                "content_type": "audio/wav",
                "started_at_unix_nano": str(sent.started_at_unix_nano),
            },
        )
        assert created["state"] == "pending"
    for entry in manifest["requests"]:
        reply = earshot.call(
            "POST",
            "/v1/traces",
            (FIXTURE / entry["file"]).read_bytes(),
            {"Content-Type": entry["wire"]["content_type"]},
        )
        assert (reply.status, reply.body) == (200, b""), entry["file"]
    for speaker, track in sent.tracks.items():
        reply = earshot.call(
            "PUT",
            f"/v1/recordings/{sent.session_key}.{speaker}/content",
            track,
            {"Content-Type": "audio/wav"},
        )
        assert reply.status == 200, reply.body
    return sent


def assert_call_served(earshot: Earshot, sent: SentCall) -> dict[str, Any]:
    """The replayed call, as a user sees it through the API: one closed, final call
    with its transcript, every span, and both recordings byte for byte."""

    call_id = earshot.json("POST", "/v1/calls:resolve", {"session_key": sent.session_key})[
        "call_id"
    ]

    def settled() -> dict[str, Any] | None:
        detail = earshot.json("GET", f"/v1/calls/{call_id}")
        latest = detail["revisions"][-1] if detail["revisions"] else None
        if (
            detail["state"] == "closed"
            and all(item["state"] != "pending" for item in detail["recordings"])
            and latest is not None
            and latest["recording_count"] == len(sent.tracks)
        ):
            return detail
        return None

    detail = wait_for(f"call {call_id} to settle", settled, timeout=SETTLE_TIMEOUT_S)
    listed = earshot.json("GET", "/v1/calls")["items"]
    assert [(item["call_id"], item["session_key"]) for item in listed] == [
        (call_id, sent.session_key)
    ]
    latest = detail["revisions"][-1]
    assert (latest["close_reason"], latest["finality"]) == ("end_signal", "final")
    assert detail["materialize_error"] is None
    assert (detail["span_count"], latest["span_count"]) == (sent.exported_span_count,) * 2

    # The transcript: both speakers, and the caller's scripted words.
    assert detail["transcript_coverage"] == "available"
    by_speaker: dict[str, str] = {}
    for line in detail["transcript"]:
        by_speaker[line["speaker"]] = by_speaker.get(line["speaker"], "") + " " + line["text"]
    assert set(by_speaker) == {"user", "agent"}, by_speaker
    caller_script = " ".join(captured_manifest()["provenance"]["caller_script"])
    assert script_words(caller_script) <= script_words(by_speaker["user"])

    # The recordings, byte for byte.
    recordings = {item["speaker"]: item for item in detail["recordings"]}
    assert sorted(recordings) == sorted(sent.tracks)
    for speaker, uploaded in sent.tracks.items():
        recording, digest = recordings[speaker], hashlib.sha256(uploaded).hexdigest()
        assert (recording["state"], recording["sha256"], recording["size"]) == (
            "stored",
            digest,
            len(uploaded),
        ), speaker
        content = earshot.call("GET", f"/v1/recordings/{recording['recording_id']}/content")
        assert (content.status, content.media_type) == (200, "audio/wav"), speaker
        assert hashlib.sha256(content.body).hexdigest() == digest, speaker
        assert wav_seconds(content.body) > 1.0, speaker

    stats = earshot.json("GET", "/v1/otlp/stats")
    assert {name: stats[name] for name in LOSS_COUNTERS} == dict.fromkeys(LOSS_COUNTERS, 0)
    return detail
