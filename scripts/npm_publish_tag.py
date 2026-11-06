"""Select an npm publish dist-tag without moving ``latest`` backwards."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any

from check_release import SEMVER


class NpmDistTagError(ValueError):
    """Raised when the npm release would move a dist-tag backwards."""


def _version_order(value: str, *, label: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None or match.group(5) is not None:
        raise NpmDistTagError(f"npm {label} is not a valid SemVer version: {value!r}")
    return tuple(int(component) for component in match.groups()[:3])  # type: ignore[return-value]


def current_npm_latest(package: str) -> str | None:
    """Read the public ``latest`` dist-tag, treating only a missing package as empty."""

    result = subprocess.run(
        ["npm", "view", package, "dist-tags", "--json"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        if re.search(r"\bE404\b", message):
            return None
        raise NpmDistTagError(f"could not read npm dist-tags: {message}")
    try:
        tags: Any = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise NpmDistTagError("npm returned malformed dist-tags JSON") from error
    if not isinstance(tags, dict):
        raise NpmDistTagError("npm returned invalid dist-tags")
    latest = tags.get("latest")
    if latest is None:
        return None
    if not isinstance(latest, str):
        raise NpmDistTagError("npm latest dist-tag is not a string")
    return latest


def publish_dist_tag(version: str, *, prerelease: bool, current_latest: str | None) -> str:
    """Return ``next`` for prereleases and reject stable versions older than ``latest``."""

    release_match = SEMVER.fullmatch(version)
    if release_match is None or release_match.group(5) is not None:
        raise NpmDistTagError(f"release version is not valid SemVer: {version!r}")
    has_prerelease = release_match.group(4) is not None
    if has_prerelease != prerelease:
        raise NpmDistTagError("release version and prerelease flag disagree")
    if prerelease:
        return "next"
    release_order = _version_order(version, label="release version")
    if current_latest is not None:
        latest_order = _version_order(current_latest, label="latest dist-tag")
        if latest_order > release_order:
            raise NpmDistTagError(
                f"refusing to move npm latest backwards from {current_latest} to {version}"
            )
    return "latest"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--prerelease", choices=("true", "false"), required=True)
    args = parser.parse_args()
    is_prerelease = args.prerelease == "true"
    try:
        latest = None if is_prerelease else current_npm_latest(args.package)
        print(publish_dist_tag(args.version, prerelease=is_prerelease, current_latest=latest))
    except NpmDistTagError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
