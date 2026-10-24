"""The Compose installs, run as a user runs them, against the image built from this tree.

Each test copies the Compose file into an empty directory (what the download step
gives a user), runs the install page's marked steps verbatim, replays the real
Pipecat 1.5 capture (its OTLP requests and both recordings), and checks the call
through the API as a user would see it.

- The quick start (``compose.yaml``) keeps the call across ``docker compose down``
  and ``up``, picks up an optional ``.env``, and deletes everything on
  ``down --volumes``.
- The full stack (``compose.full.yaml``) refuses to start without its secrets,
  requires the token, and stores the call in Postgres and its recordings in MinIO.
- Every container runs read-only, without capabilities or privilege escalation,
  and publishes only on host loopback.
"""

from __future__ import annotations

import json
import secrets

import pytest
from install_support import (
    DEV_OVERRIDE,
    FULL_STACK,
    QUICK_START,
    assert_call_served,
    assert_hardened_on_loopback,
    compose_project,
    quick_start_image,
    replay_captured_call,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

FULL_STACK_SECRETS = (
    "EARSHOT_TOKEN",
    "EARSHOT_CORRELATION_KEY",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
)


def test_the_quick_start_serves_a_replayed_call_and_keeps_it_across_a_restart(tmp_path) -> None:
    quick_start_image()
    with compose_project(tmp_path / "earshot", QUICK_START) as project:
        assert not (project.directory / ".env").exists()
        project.step("quickstart-up")
        assert_hardened_on_loopback(project)
        anonymous = project.client()
        sent = replay_captured_call(anonymous)
        first = assert_call_served(anonymous, sent)

        # Down and up again: the named volume keeps the call, and a token put in
        # the optional .env reaches the server.
        project.step("quickstart-stop")
        assert project.containers() == []
        token = secrets.token_hex(32)
        (project.directory / ".env").write_text(f"EARSHOT_TOKEN={token}\n", encoding="utf-8")
        project.step("quickstart-up")
        assert anonymous.call("GET", "/v1/calls").status == 401
        again = assert_call_served(project.client(token), sent)
        assert again["call_id"] == first["call_id"]
        assert again["revisions"] == first["revisions"]

        project.step("quickstart-delete")
        assert project.containers() == []
        assert project.volumes() == []


def test_the_full_stack_serves_a_replayed_call_from_postgres_and_minio(tmp_path) -> None:
    quick_start_image()
    with compose_project(tmp_path / "earshot", FULL_STACK) as project:
        project.step("full-env")
        generated = (project.directory / ".env").read_text(encoding="utf-8")
        assert sorted(project.dotenv()) == sorted(FULL_STACK_SECRETS)

        # Without any one of its secrets the stack does not start at all.
        for missing in FULL_STACK_SECRETS:
            (project.directory / ".env").write_text(
                "".join(
                    line + "\n"
                    for line in generated.splitlines()
                    if not line.startswith(missing + "=")
                ),
                encoding="utf-8",
            )
            refused = project.compose("up", "--detach", check=False)
            assert refused.returncode != 0
            assert f"set {missing} in .env" in refused.stderr
            assert project.containers() == []
        (project.directory / ".env").write_text(generated, encoding="utf-8")

        project.step("full-up")
        assert_hardened_on_loopback(project)
        published = {
            container["Config"]["Labels"]["com.docker.compose.service"]
            for container in project.containers()
            if container["HostConfig"]["PortBindings"]
        }
        assert published == {"earshot"}

        assert project.client().call("GET", "/v1/calls").status == 401
        earshot = project.client(project.dotenv()["EARSHOT_TOKEN"])
        sent = replay_captured_call(earshot)
        detail = assert_call_served(earshot, sent)

        # The call is in Postgres, and its two recordings are objects in MinIO.
        catalog = project.compose(
            "exec", "-T", "postgres",
            "psql", "--username", "earshot", "--dbname", "earshot", "--tuples-only",
            "--no-align", "--command", "SELECT call_id FROM calls",
        )  # fmt: skip
        assert catalog.stdout.split() == [detail["call_id"]]
        listing = project.compose(
            "exec", "-T", "minio", "sh", "-c",
            'mc alias set check http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"'
            " >/dev/null && mc ls --recursive --json check/earshot",
        )  # fmt: skip
        objects = [json.loads(line) for line in listing.stdout.splitlines() if line.strip()]
        assert sorted(item["size"] for item in objects) == sorted(
            len(track) for track in sent.tracks.values()
        )
        assert all(item["key"].startswith("recordings/") for item in objects), objects

        project.step("full-backup-catalog")
        assert (project.directory / "earshot.dump").read_bytes()[:5] == b"PGDMP"


def test_the_dev_override_builds_this_checkout_for_either_install(tmp_path) -> None:
    with compose_project(tmp_path / "earshot", QUICK_START, FULL_STACK, DEV_OVERRIDE) as project:
        placeholders = (f"{name}=placeholder" for name in FULL_STACK_SECRETS)
        (project.directory / ".env").write_text("\n".join(placeholders), encoding="utf-8")
        for base in (QUICK_START.name, FULL_STACK.name):
            shown = project.compose(
                "config", "--format", "json", files=[base, DEV_OVERRIDE.name]
            ).stdout
            earshot = json.loads(shown)["services"]["earshot"]
            assert earshot["build"]["context"] == str(project.directory.resolve())
            assert earshot["build"]["dockerfile"] == "Dockerfile"
            assert earshot["image"] == "earshot:dev"
