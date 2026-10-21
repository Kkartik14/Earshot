"""The Helm chart (``deploy/helm/earshot``): lint, schemas, its rules, and a boot gate.

- ``helm lint --strict`` passes for every variant: SQLite on a claim, behind an
  Ingress, and Postgres with S3 and no claim.
- Every rendered variant validates with ``kubeconform -strict`` against the
  Kubernetes 1.30 and 1.36 schemas.
- The operator token is required, Ingress or not: the chart refuses to render
  without it, and ``EARSHOT_TRUST_LOCAL_NETWORK`` never appears without it.
- Exactly one replica, replaced with ``Recreate``, hardened like the Compose files.
- Settings that would keep the catalog or recordings on the pod's own disk are
  refused.
- The boot gate: the chart's rendered environment and pod settings, run under
  Compose against the full stack's Postgres and MinIO, reach ``/readyz`` 200.
  No cluster is involved; ``helm lint`` and schemas alone cannot show that the
  rendered configuration starts a server.
"""

from __future__ import annotations

import base64
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from install_support import (
    CHART,
    FULL_STACK,
    compose_project,
    quick_start_image,
    require_tool,
    run,
    tool_environment,
    wait_for,
)

from captured_otlp import FIXTURE, captured_manifest

pytestmark = [pytest.mark.e2e]

KUBERNETES_VERSIONS = ("1.30.0", "1.36.0")
TOKEN_SECRET = {"auth": {"existingSecret": "earshot"}}
VARIANTS: dict[str, dict[str, Any]] = {
    "sqlite-on-a-claim": TOKEN_SECRET,
    "behind-an-ingress": {
        **TOKEN_SECRET,
        "ingress": {
            "enabled": True,
            "className": "nginx",
            "tls": [{"secretName": "earshot-tls", "hosts": ["earshot.example.com"]}],
        },
    },
    "postgres-and-s3": {
        **TOKEN_SECRET,
        "database": {"existingSecret": "earshot-db"},
        "blobs": {"url": "s3://bucket/earshot?region=eu-west-1", "existingSecret": "earshot-s3"},
        "persistence": {"enabled": False},
    },
}
Helm = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="module")
def helm(tmp_path_factory: pytest.TempPathFactory) -> Helm:
    """``helm`` with its configuration, cache and data kept in a temporary home."""

    require_tool("helm")
    home = tmp_path_factory.mktemp("helm-home")
    environment = tool_environment(
        HELM_CONFIG_HOME=str(home / "config"),
        HELM_CACHE_HOME=str(home / "cache"),
        HELM_DATA_HOME=str(home / "data"),
    )

    def helm(*arguments: str, values: dict[str, Any] | None = None, check: bool = True):
        command = ["helm", *arguments]
        if values is not None:
            path = home / f"values-{secrets.token_hex(4)}.yaml"
            path.write_text(yaml.safe_dump(values), encoding="utf-8")
            command += ["--values", str(path)]
        return run(command, environment=environment, check=check)

    return helm


def _render(helm: Helm, values: dict[str, Any]) -> list[dict[str, Any]]:
    shown = helm("template", "earshot", str(CHART), "--namespace", "earshot", values=values)
    return [document for document in yaml.safe_load_all(shown.stdout) if document]


def _deployment(documents: list[dict[str, Any]]) -> dict[str, Any]:
    [deployment] = [document for document in documents if document["kind"] == "Deployment"]
    return deployment


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in container["env"]}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_helm_lint_passes_strict(helm: Helm, variant: str) -> None:
    linted = helm("lint", "--strict", str(CHART), values=VARIANTS[variant])
    assert "0 chart(s) failed" in linted.stdout


def test_every_rendered_variant_is_valid_kubernetes(helm: Helm, tmp_path: Path) -> None:
    require_tool("kubeconform")
    rendered = "---\n".join(
        helm("template", "earshot", str(CHART), values=VARIANTS[variant]).stdout
        for variant in sorted(VARIANTS)
    )
    cache = tmp_path / "schemas"
    cache.mkdir()
    for version in KUBERNETES_VERSIONS:
        checked = run(
            [
                "kubeconform",
                "-strict",
                "-summary",
                "-cache",
                str(cache),
                "-kubernetes-version",
                version,
            ],
            stdin=rendered,
            timeout=600,
        )
        assert "Invalid: 0, Errors: 0, Skipped: 0" in checked.stdout, (version, checked.stdout)


@pytest.mark.parametrize("ingress", [False, True])
def test_the_chart_refuses_to_render_without_the_token(helm: Helm, ingress: bool) -> None:
    values = {"ingress": {"enabled": ingress}}
    refused = helm("template", "earshot", str(CHART), values=values, check=False)
    assert refused.returncode != 0
    assert "auth.existingSecret is required" in refused.stderr


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({**TOKEN_SECRET, "persistence": {"enabled": False}}, "needs database.existingSecret"),
        (
            {
                **TOKEN_SECRET,
                "database": {"existingSecret": "earshot-db"},
                "persistence": {"enabled": False},
            },
            "needs blobs.url",
        ),
    ],
)
def test_the_chart_refuses_data_on_the_pods_own_disk(
    helm: Helm, values: dict[str, Any], reason: str
) -> None:
    refused = helm("template", "earshot", str(CHART), values=values, check=False)
    assert refused.returncode != 0
    assert reason in refused.stderr


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_every_variant_runs_one_hardened_server_that_requires_the_token(
    helm: Helm, variant: str
) -> None:
    values = VARIANTS[variant]
    # A replica count in values changes nothing: there is no such knob.
    deployment = _deployment(_render(helm, {**values, "replicas": 3, "replicaCount": 3}))
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}

    pod = deployment["spec"]["template"]["spec"]
    [container] = pod["containers"]
    environment = _environment(container)
    assert environment["EARSHOT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "earshot",
        "key": "token",
    }
    if values.get("ingress", {}).get("enabled"):
        assert environment["EARSHOT_BEHIND_TLS_PROXY"]["value"] == "true"
        assert "EARSHOT_TRUST_LOCAL_NETWORK" not in environment
    else:
        assert environment["EARSHOT_TRUST_LOCAL_NETWORK"]["value"] == "true"
        assert "EARSHOT_BEHIND_TLS_PROXY" not in environment

    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"


def _compose_service(
    pod: dict[str, Any], container: dict[str, Any], secret_values: dict[tuple[str, str], str]
) -> dict[str, Any]:
    """The pod's container as a Compose service: its image, arguments and
    environment (secret references resolved), user, hardening and emptyDir mounts."""

    environment = {}
    for entry in container["env"]:
        if "value" in entry:
            environment[entry["name"]] = entry["value"]
        else:
            reference = entry["valueFrom"]["secretKeyRef"]
            environment[entry["name"]] = secret_values[(reference["name"], reference["key"])]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    tmpfs = []
    for mount in container["volumeMounts"]:
        empty_dir = volumes[mount["name"]]["emptyDir"]
        size = empty_dir.get("sizeLimit")
        options = "mode=1777" + (f",size={size.removesuffix('i').lower()}" if size else "")
        tmpfs.append(f"{mount['mountPath']}:{options}")
    security, pod_security = container["securityContext"], pod["securityContext"]
    return {
        "image": container["image"],
        "command": container["args"],
        "environment": environment,
        "user": f"{pod_security['runAsUser']}:{pod_security['runAsGroup']}",
        "read_only": security["readOnlyRootFilesystem"],
        "cap_drop": security["capabilities"]["drop"],
        "security_opt": (
            ["no-new-privileges:true"] if security["allowPrivilegeEscalation"] is False else []
        ),
        "tmpfs": tmpfs,
        "ports": [f"127.0.0.1::{port['containerPort']}" for port in container["ports"]],
        "depends_on": {
            "postgres": {"condition": "service_healthy"},
            "minio": {"condition": "service_healthy"},
        },
    }


@pytest.mark.slow
def test_the_rendered_environment_boots_against_postgres_and_minio(
    helm: Helm, tmp_path: Path
) -> None:
    repository, tag = quick_start_image().rsplit(":", 1)
    postgres_password, minio_password = secrets.token_hex(24), secrets.token_hex(24)
    token = secrets.token_hex(32)
    secret_values = {
        ("earshot", "token"): token,
        ("earshot-db", "database-url"): (
            f"postgresql://earshot:{postgres_password}@postgres:5432/earshot"
        ),
        ("earshot-db", "correlation-key"): base64.b64encode(secrets.token_bytes(32)).decode(),
        ("earshot-s3", "access-key-id"): "earshot",
        ("earshot-s3", "secret-access-key"): minio_password,
    }
    values = {
        **VARIANTS["postgres-and-s3"],
        "image": {"repository": repository, "tag": tag},
        "blobs": {
            "url": "s3://earshot/recordings?endpoint=http://minio:9000&region=us-east-1&path_style=1",
            "existingSecret": "earshot-s3",
        },
    }
    pod = _deployment(_render(helm, values))["spec"]["template"]["spec"]
    [container] = pod["containers"]

    with compose_project(tmp_path / "earshot", FULL_STACK) as project:
        # The full stack's datastores; its own earshot service is not started.
        (project.directory / ".env").write_text(
            f"POSTGRES_PASSWORD={postgres_password}\nMINIO_ROOT_PASSWORD={minio_password}\n"
            "EARSHOT_TOKEN=unused\nEARSHOT_CORRELATION_KEY=unused\n",
            encoding="utf-8",
        )
        service = _compose_service(pod, container, secret_values)
        (project.directory / "chart.yaml").write_text(
            yaml.safe_dump({"services": {"earshot-chart": service}}), encoding="utf-8"
        )
        files = [FULL_STACK.name, "chart.yaml"]
        project.compose("up", "--detach", "--wait", "earshot-chart", files=files)
        shown = project.compose("port", "earshot-chart", "4319", files=files).stdout.strip()
        client = replace(project.client(token), port=int(shown.rsplit(":", 1)[1]))

        for probe in ("readinessProbe", "livenessProbe"):
            path = container[probe]["httpGet"]["path"]
            wait_for(
                f"{path} to answer 200",
                lambda path=path: client.call("GET", path).status == 200,
                timeout=60,
            )
        assert client.json("GET", "/readyz")["status"] == "ready"
        assert replace(client, credential=None).call("GET", "/v1/calls").status == 401
        assert client.call("GET", "/v1/calls").status == 200

        # /readyz does not reach the blob store: a recording proves the S3 settings.
        audio = (FIXTURE / captured_manifest()["audio"]["user"]["file"]).read_bytes()
        client.json(
            "POST",
            "/v1/recordings",
            {
                "recording_id": "boot-gate.user",
                "session_key": "boot-gate",
                "speaker": "user",
                "content_type": "audio/wav",
                "started_at_unix_nano": "1792372161387965000",
            },
        )
        stored = client.call(
            "PUT", "/v1/recordings/boot-gate.user/content", audio, {"Content-Type": "audio/wav"}
        )
        assert stored.status == 200, stored.body
        assert client.call("GET", "/v1/recordings/boot-gate.user/content").body == audio
