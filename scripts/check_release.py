"""Validate the identities that bind one release across registries."""

from __future__ import annotations

import argparse
import json
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


NODE_PACKAGE_NAME = "earshot-observability"


def project_version(project_file: Path) -> str:
    document = tomllib.loads(project_file.read_text())
    version = document.get("project", {}).get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseIdentityError("project.version must be an explicit semantic version")
    return version


def node_package_identity(node_package_file: Path) -> tuple[str, str]:
    document = json.loads(node_package_file.read_text())
    if not isinstance(document, dict):
        raise ReleaseIdentityError("Node package manifest must contain a JSON object")
    name = document.get("name")
    version = document.get("version")
    if name != NODE_PACKAGE_NAME:
        raise ReleaseIdentityError(f"Node package name must be {NODE_PACKAGE_NAME!r}, got {name!r}")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseIdentityError("Node package version must be an explicit semantic version")
    return name, version


def release_identity(
    project_file: Path,
    *,
    node_package_file: Path,
    tag: str,
    repository: str,
) -> dict[str, str]:
    version = project_version(project_file)
    npm_package, npm_version = node_package_identity(node_package_file)
    if npm_version != version:
        raise ReleaseIdentityError(
            f"Node package version {npm_version!r} does not match project version {version!r}"
        )
    if tag and tag != f"v{version}":
        raise ReleaseIdentityError(
            f"release tag {tag!r} does not match project version {version!r}; expected v{version}"
        )
    if repository.count("/") != 1 or any(not part for part in repository.split("/")):
        raise ReleaseIdentityError("repository must have the form owner/name")
    return {
        "image": f"ghcr.io/{repository.lower()}",
        "npm_package": npm_package,
        "prerelease": str("-" in version).lower(),
        "version": version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--node-package-file", type=Path, default=Path("packages/node/package.json")
    )
    parser.add_argument("--tag", default="")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        identity = release_identity(
            args.project_file,
            node_package_file=args.node_package_file,
            tag=args.tag,
            repository=args.repository,
        )
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, ReleaseIdentityError) as error:
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
