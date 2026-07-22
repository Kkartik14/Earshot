"""Verify that a published source archive is self-contained for server installs."""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path


def check_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    required_suffixes = {
        "/pyproject.toml",
        "/packages/sdk-python/src/earshot/__init__.py",
        "/packages/sdk-python/src/earshot/cli.py",
        "/packages/sdk-python/src/earshot/generated/earshot/v1alpha1/incident_pb2.py",
        "/packages/sdk-python/src/earshot/web/index.html",
    }
    missing = {
        suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in names)
    }
    if missing:
        raise SystemExit(f"{path}: required source files are missing: {sorted(missing)!r}")
    assets = "/packages/sdk-python/src/earshot/web/assets/"
    if not any(assets in name for name in names):
        raise SystemExit(f"{path}: bundled viewer assets are missing")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise SystemExit("usage: check_sdist.py PATH_TO_SDIST")
    path = Path(arguments[0])
    if not path.is_file() or not path.name.endswith(".tar.gz"):
        raise SystemExit(f"{path}: source distribution does not exist")
    check_sdist(path)
    print(f"source distribution contains bundled viewer: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
