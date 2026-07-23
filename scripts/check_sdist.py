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
ALLOWED_PACKAGE_ANCESTORS = {
    "packages",
    "packages/sdk-python",
    "packages/sdk-python/src",
    ALLOWED_PACKAGE_PREFIX.removesuffix("/"),
}
MAX_FILE_COUNT = 512
MAX_UNPACKED_BYTES = 32 * 1024 * 1024


def _archive_path(name: str) -> tuple[str, str]:
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    return path.parts[0], "/".join(path.parts[1:])


def _path_is_allowed(relative_path: str, *, directory: bool) -> bool:
    if directory:
        return (
            not relative_path
            or relative_path in ALLOWED_PACKAGE_ANCESTORS
            or relative_path.startswith(ALLOWED_PACKAGE_PREFIX)
        )
    return relative_path in ALLOWED_ROOT_FILES or relative_path.startswith(ALLOWED_PACKAGE_PREFIX)


def check_sdist(path: pathlib.Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
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
    archive_root: str | None = None
    relative_files: set[str] = set()
    seen_paths: set[str] = set()
    for member in members:
        member_root, relative_path = _archive_path(member.name)
        if archive_root is None:
            archive_root = member_root
        elif member_root != archive_root:
            raise SystemExit(
                f"{path}: multiple archive roots: {archive_root!r} and {member_root!r}"
            )
        normalized_path = f"{member_root}/{relative_path}" if relative_path else member_root
        if normalized_path in seen_paths:
            raise SystemExit(f"{path}: duplicate archive path: {normalized_path!r}")
        seen_paths.add(normalized_path)
        if not member.isfile() and not member.isdir():
            raise SystemExit(f"{path}: unsupported archive member type at {relative_path!r}")
        if any(
            relative_path == prefix.removesuffix("/") or relative_path.startswith(prefix)
            for prefix in FORBIDDEN_PREFIXES
        ):
            raise SystemExit(f"{path}: forbidden archive path: {relative_path!r}")
        if not _path_is_allowed(relative_path, directory=member.isdir()):
            raise SystemExit(f"{path}: unexpected archive path: {relative_path!r}")
        if member.isfile():
            relative_files.add(relative_path)
    required_files = {
        "pyproject.toml",
        "packages/sdk-python/src/earshot/__init__.py",
        "packages/sdk-python/src/earshot/cli.py",
        "packages/sdk-python/src/earshot/generated/earshot/v1alpha1/incident_pb2.py",
        "packages/sdk-python/src/earshot/web/index.html",
    }
    missing = required_files - relative_files
    if missing:
        raise SystemExit(f"{path}: required source files are missing: {sorted(missing)!r}")
    if not any(
        name.startswith("packages/sdk-python/src/earshot/web/assets/") for name in relative_files
    ):
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
