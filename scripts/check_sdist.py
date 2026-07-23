"""Verify that a published source archive is self-contained for server installs."""

from __future__ import annotations

import pathlib
import sys
import tarfile

FORBIDDEN_DIRECTORY_SEGMENTS = frozenset(
    {
        ".earshot",
        ".git",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "coverage",
        "dev",
        "dist",
        "htmlcov",
        "local",
        "node_modules",
        "private",
        "test",
        "tests",
    }
)
ALLOWED_ROOT_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
ALLOWED_PACKAGE_PREFIX = "packages/sdk-python/src/earshot/"
PACKAGE_MANIFEST_PATH = pathlib.Path(__file__).with_name("sdist_package_manifest.txt")
_package_manifest_lines = PACKAGE_MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
if _package_manifest_lines != sorted(set(_package_manifest_lines)) or any(
    not name.startswith(ALLOWED_PACKAGE_PREFIX)
    or name.startswith(f"{ALLOWED_PACKAGE_PREFIX}web/")
    or not (name.endswith((".py", ".pyi")) or name == f"{ALLOWED_PACKAGE_PREFIX}py.typed")
    for name in _package_manifest_lines
):
    raise RuntimeError(f"invalid source package manifest: {PACKAGE_MANIFEST_PATH}")
ALLOWED_PACKAGE_FILES = frozenset(_package_manifest_lines)
VIEWER_DIRECTORY = "packages/sdk-python/src/earshot/web"
VIEWER_ASSETS_DIRECTORY = f"{VIEWER_DIRECTORY}/assets"
VIEWER_ASSET_SUFFIXES = frozenset({".css", ".js"})
ALLOWED_PACKAGE_ANCESTORS = {
    "packages",
    "packages/sdk-python",
    "packages/sdk-python/src",
    ALLOWED_PACKAGE_PREFIX.removesuffix("/"),
}
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_FILE_COUNT = 512
MAX_HEADER_BYTES = 1 * 1024 * 1024
MAX_MEMBER_COUNT = 1_024
MAX_UNPACKED_BYTES = 32 * 1024 * 1024


def _archive_path(name: str) -> tuple[str, str]:
    if "\\" in name or "\x00" in name:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    return path.parts[0], "/".join(path.parts[1:])


def _path_is_allowed(relative_path: str, *, directory: bool) -> bool:
    if relative_path == VIEWER_DIRECTORY or relative_path.startswith(f"{VIEWER_DIRECTORY}/"):
        if directory:
            return relative_path in {VIEWER_DIRECTORY, VIEWER_ASSETS_DIRECTORY}
        if relative_path == f"{VIEWER_DIRECTORY}/index.html":
            return True
        path = pathlib.PurePosixPath(relative_path)
        assets = pathlib.PurePosixPath(VIEWER_ASSETS_DIRECTORY)
        try:
            asset_path = path.relative_to(assets)
        except ValueError:
            return False
        return (
            len(asset_path.parts) == 1
            and not asset_path.name.startswith(".")
            and asset_path.suffix in VIEWER_ASSET_SUFFIXES
        )
    if directory:
        return (
            not relative_path
            or relative_path in ALLOWED_PACKAGE_ANCESTORS
            or relative_path.startswith(ALLOWED_PACKAGE_PREFIX)
        )
    if relative_path in ALLOWED_ROOT_FILES:
        return True
    return relative_path in ALLOWED_PACKAGE_FILES


def _path_is_forbidden(name: str) -> bool:
    parts = pathlib.PurePosixPath(name).parts
    return any(
        part in FORBIDDEN_DIRECTORY_SEGMENTS
        or part == ".envrc"
        or part == ".env"
        or part.startswith(".env.")
        for part in parts
    )


def check_sdist(path: pathlib.Path) -> None:
    compressed_bytes = path.stat().st_size
    if compressed_bytes > MAX_COMPRESSED_BYTES:
        raise SystemExit(
            f"{path}: source archive compressed size is too large: "
            f"{compressed_bytes} bytes exceeds {MAX_COMPRESSED_BYTES}"
        )
    archive_root: str | None = None
    relative_files: set[str] = set()
    seen_paths: set[str] = set()
    member_count = 0
    file_count = 0
    header_bytes = 0
    unpacked_bytes = 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            member_count += 1
            if member_count > MAX_MEMBER_COUNT:
                raise SystemExit(
                    f"{path}: source archive contains too many archive members: "
                    f"{member_count} exceeds {MAX_MEMBER_COUNT}"
                )
            header_bytes += member.offset_data - member.offset
            if header_bytes > MAX_HEADER_BYTES:
                raise SystemExit(
                    f"{path}: source archive header metadata is too large: "
                    f"{header_bytes} bytes exceeds {MAX_HEADER_BYTES}"
                )
            if member.isfile():
                file_count += 1
                if file_count > MAX_FILE_COUNT:
                    raise SystemExit(
                        f"{path}: source archive contains too many files: "
                        f"{file_count} exceeds {MAX_FILE_COUNT}"
                    )
                unpacked_bytes += member.size
                if unpacked_bytes > MAX_UNPACKED_BYTES:
                    raise SystemExit(
                        f"{path}: source archive unpacked size is too large: "
                        f"{unpacked_bytes} bytes exceeds {MAX_UNPACKED_BYTES}"
                    )
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
            if _path_is_forbidden(member.name):
                raise SystemExit(f"{path}: forbidden archive path: {relative_path!r}")
            if not _path_is_allowed(relative_path, directory=member.isdir()):
                raise SystemExit(f"{path}: unexpected archive path: {relative_path!r}")
            if member.isfile():
                relative_files.add(relative_path)
    required_files = ALLOWED_PACKAGE_FILES | {
        "pyproject.toml",
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
