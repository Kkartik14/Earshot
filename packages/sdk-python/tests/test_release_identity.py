from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_release.py"


def write_project(
    path: Path,
    version: str,
    *,
    node_name: str = "earshot-observability",
    node_version: str | None = None,
) -> Path:
    project = path / "pyproject.toml"
    project.write_text(f'[project]\nname = "example"\nversion = "{version}"\n')
    node_package = path / "node-package.json"
    node_package.write_text(
        json.dumps(
            {
                "name": node_name,
                "version": node_version or version,
            }
        )
    )
    return project


def check_release(project: Path, *, tag: str = "v0.1.0") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-file",
            str(project),
            "--node-package-file",
            str(project.parent / "node-package.json"),
            "--tag",
            tag,
            "--repository",
            "Kkartik14/Earshot",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_identity_binds_tag_version_and_lowercase_image(tmp_path: Path) -> None:
    project = write_project(tmp_path, "0.1.0")

    result = check_release(project)

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "image=ghcr.io/kkartik14/earshot",
        "npm_package=earshot-observability",
        "prerelease=false",
        "version=0.1.0",
    ]


def test_release_identity_rejects_a_tag_for_different_artifacts(tmp_path: Path) -> None:
    project = write_project(tmp_path, "0.1.0")

    result = check_release(project, tag="v0.2.0")

    assert result.returncode == 2
    assert "expected v0.1.0" in result.stderr


def test_release_identity_rejects_unapproved_node_package_name(tmp_path: Path) -> None:
    project = write_project(tmp_path, "0.1.0", node_name="@earshot/node")

    result = check_release(project)

    assert result.returncode == 2
    assert "Node package name must be 'earshot-observability'" in result.stderr


def test_release_identity_rejects_node_version_different_from_project(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path, "0.1.0", node_version="0.2.0")

    result = check_release(project)

    assert result.returncode == 2
    assert "does not match project version" in result.stderr


@pytest.mark.parametrize("version", ["latest", "1.2", "01.2.3", "1.2.3_rc1"])
def test_project_version_requires_semver(tmp_path: Path, version: str) -> None:
    project = write_project(tmp_path, version, node_version="0.1.0")

    result = check_release(project)

    assert result.returncode == 2
    assert "semantic version" in result.stderr
