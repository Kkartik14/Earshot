"""Verify that a published source archive is self-contained for server installs."""

from __future__ import annotations

import pathlib
import sys
import tarfile

FORBIDDEN_PREFIXES = (
    ".earshot/",
    ".git/",
    ".venv/",
    "dist/",
    "docs/private/",
    "local/",
    "node_modules/",
)
ALLOWED_ROOT_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
ALLOWED_PACKAGE_PREFIX = "packages/sdk-python/src/earshot/"
MAX_FILE_COUNT = 512
MAX_UNPACKED_BYTES = 32 * 1024 * 1024


def _archive_relative_path(name: str) -> str:
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    return "/".join(path.parts[1:])


def check_sdist(path: pathlib.Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
    file_count = sum(member.isfile() for member in members)
    if file_count > MAX_FILE_COUNT:
        raise SystemExit(
            f"{path}: source archive contains too many files: {file_count} exceeds {MAX_FILE_COUNT}"
        )
    unpacked_bytes = sum(member.size for member in members if member.isfile())
    if unpacked_bytes > MAX_UNPACKED_BYTES:
        raise SystemExit(
            f"{path}: source archive unpacked size is too large: "
            f"{unpacked_bytes} bytes exceeds {MAX_UNPACKED_BYTES}"
        )
    for member in members:
        relative_path = _archive_relative_path(member.name)
        if any(
            relative_path == prefix.removesuffix("/") or relative_path.startswith(prefix)
            for prefix in FORBIDDEN_PREFIXES
        ):
            raise SystemExit(f"{path}: forbidden archive path: {relative_path!r}")
        if not (
            relative_path in ALLOWED_ROOT_FILES
            or relative_path == ALLOWED_PACKAGE_PREFIX.removesuffix("/")
            or relative_path.startswith(ALLOWED_PACKAGE_PREFIX)
        ):
            raise SystemExit(f"{path}: unexpected archive path: {relative_path!r}")
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
    path = pathlib.Path(arguments[0])
    if not path.is_file() or not path.name.endswith(".tar.gz"):
        raise SystemExit(f"{path}: source distribution does not exist")
    check_sdist(path)
    print(f"source distribution contains bundled viewer: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
