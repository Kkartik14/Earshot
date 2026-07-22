"""Validate the identities that bind one release across registries."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ReleaseIdentityError(ValueError):
    """Raised when package, tag, or registry identities disagree."""


def project_version(project_file: Path) -> str:
    document = tomllib.loads(project_file.read_text())
    version = document.get("project", {}).get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseIdentityError("project.version must be an explicit semantic version")
    return version


def release_identity(project_file: Path, *, tag: str, repository: str) -> dict[str, str]:
    version = project_version(project_file)
    if tag and tag != f"v{version}":
        raise ReleaseIdentityError(
            f"release tag {tag!r} does not match project version {version!r}; expected v{version}"
        )
    if repository.count("/") != 1 or any(not part for part in repository.split("/")):
        raise ReleaseIdentityError("repository must have the form owner/name")
    return {
        "image": f"ghcr.io/{repository.lower()}",
        "prerelease": str("-" in version).lower(),
        "version": version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        identity = release_identity(
            args.project_file,
            tag=args.tag,
            repository=args.repository,
        )
    except (OSError, tomllib.TOMLDecodeError, ReleaseIdentityError) as error:
        parser.error(str(error))

    for key, value in identity.items():
        print(f"{key}={value}")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in identity.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
