from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_release import (
    ReleaseIdentityError,
    project_version,
    release_identity,
)


def write_project(path: Path, version: str) -> Path:
    project = path / "pyproject.toml"
    project.write_text(f'[project]\nname = "example"\nversion = "{version}"\n')
    return project


def test_release_identity_binds_tag_version_and_lowercase_image(tmp_path: Path) -> None:
    project = write_project(tmp_path, "0.1.0")

    assert release_identity(project, tag="v0.1.0", repository="Kkartik14/Earshot") == {
        "image": "ghcr.io/kkartik14/earshot",
        "prerelease": "false",
        "version": "0.1.0",
    }


def test_release_identity_rejects_a_tag_for_different_artifacts(tmp_path: Path) -> None:
    project = write_project(tmp_path, "0.1.0")

    with pytest.raises(ReleaseIdentityError, match=r"expected v0\.1\.0"):
        release_identity(project, tag="v0.2.0", repository="Kkartik14/Earshot")


@pytest.mark.parametrize("version", ["latest", "1.2", "01.2.3", "1.2.3_rc1"])
def test_project_version_requires_semver(tmp_path: Path, version: str) -> None:
    project = write_project(tmp_path, version)

    with pytest.raises(ReleaseIdentityError, match="semantic version"):
        project_version(project)
