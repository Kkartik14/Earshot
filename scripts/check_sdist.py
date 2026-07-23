"""Verify that a published source archive is self-contained for server installs."""

from __future__ import annotations

import gzip
import pathlib
import sys
import tarfile
import zlib
from typing import BinaryIO

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
MAX_TRAILING_ZERO_BYTES = 64 * 1024
TAR_BLOCK_BYTES = 512
TAR_DIRECTORY_TYPE = b"5"
TAR_LOCAL_PAX_TYPE = b"x"
TAR_REGULAR_TYPES = frozenset({b"0"})
ALLOWED_LOCAL_PAX_KEYS = frozenset({"path"})
HATCH_BUILD_MTIME = b"13615410400\0"
TAR_FILE_MODE = b"0000644\0"
TAR_PAX_MODE = b"0000000\0"
TAR_ZERO_ID = b"0000000\0"
TAR_ZERO_NUMBER = b"\0" * 8
GZIP_HEADER_BYTES = 10
GZIP_MTIME = b"\x00\x11\x36\x5e"
MAX_GZIP_OUTPUT_BYTES = (
    MAX_HEADER_BYTES
    + MAX_UNPACKED_BYTES
    + MAX_FILE_COUNT * (TAR_BLOCK_BYTES - 1)
    + MAX_TRAILING_ZERO_BYTES
)


def _archive_path(name: str) -> tuple[str, str]:
    if "\\" in name or "\x00" in name:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"unsafe source archive path: {name!r}")
    return path.parts[0], "/".join(path.parts[1:])


def _validate_gzip_header(path: pathlib.Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(GZIP_HEADER_BYTES)
    if len(header) != GZIP_HEADER_BYTES:
        raise SystemExit(f"{path}: truncated gzip header")
    if header[:2] != b"\x1f\x8b":
        raise SystemExit(f"{path}: invalid gzip signature")
    if header[2] != 8:
        raise SystemExit(f"{path}: unsupported gzip compression method")
    if header[3] != 0:
        raise SystemExit(f"{path}: unsupported gzip header flags")
    if header[4:8] != GZIP_MTIME:
        raise SystemExit(f"{path}: non-canonical gzip mtime metadata")
    if header[8] != 2:
        raise SystemExit(f"{path}: non-canonical gzip compression metadata")
    if header[9] != 255:
        raise SystemExit(f"{path}: non-canonical gzip platform metadata")


def _validate_single_gzip_member(path: pathlib.Path) -> None:
    decompressor = zlib.decompressobj(wbits=31)
    output_bytes = 0
    try:
        with path.open("rb") as stream:
            while compressed := stream.read(64 * 1024):
                pending = compressed
                while pending:
                    output = decompressor.decompress(pending, 64 * 1024)
                    output_bytes += len(output)
                    if output_bytes > MAX_GZIP_OUTPUT_BYTES:
                        raise SystemExit(
                            f"{path}: source archive gzip output or unpacked size is too large"
                        )
                    pending = decompressor.unconsumed_tail
                    if decompressor.eof:
                        if decompressor.unused_data or pending or stream.read(1):
                            raise SystemExit(
                                f"{path}: multiple gzip members or trailing compressed data"
                            )
                        return
    except (OSError, zlib.error) as error:
        raise SystemExit(f"{path}: unreadable compressed source archive") from error
    raise SystemExit(f"{path}: truncated gzip member")


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


def _tar_size(header: bytes, path: pathlib.Path) -> int:
    raw = header[124:136]
    if len(raw) != 12 or raw[-1:] != b"\0" or any(byte not in b"01234567" for byte in raw[:-1]):
        raise SystemExit(f"{path}: non-canonical size archive metadata")
    return int(raw[:-1], 8)


def _require_header_field(
    header: bytes,
    field: slice,
    expected: bytes,
    label: str,
    path: pathlib.Path,
) -> None:
    if header[field] != expected:
        raise SystemExit(f"{path}: non-canonical {label} archive metadata")


def _validate_tar_header_metadata(
    header: bytes,
    member_type: bytes,
    path: pathlib.Path,
) -> int:
    mode = TAR_PAX_MODE if member_type == TAR_LOCAL_PAX_TYPE else TAR_FILE_MODE
    mtime = b"00000000000\0" if member_type == TAR_LOCAL_PAX_TYPE else HATCH_BUILD_MTIME
    for field, expected, label in (
        (slice(100, 108), mode, "mode"),
        (slice(108, 116), TAR_ZERO_ID, "uid"),
        (slice(116, 124), TAR_ZERO_ID, "gid"),
        (slice(136, 148), mtime, "mtime"),
        (slice(156, 157), member_type, "type"),
        (slice(157, 257), b"\0" * 100, "linkname"),
        (slice(257, 263), b"ustar\0", "magic"),
        (slice(263, 265), b"00", "version"),
        (slice(265, 297), b"\0" * 32, "uname"),
        (slice(297, 329), b"\0" * 32, "gname"),
        (slice(329, 337), TAR_ZERO_NUMBER, "device major"),
        (slice(337, 345), TAR_ZERO_NUMBER, "device minor"),
        (slice(345, 500), b"\0" * 155, "path prefix"),
        (slice(500, 512), b"\0" * 12, "reserved"),
    ):
        _require_header_field(header, field, expected, label, path)

    checksum = header[148:156]
    checksum_header = bytearray(header)
    checksum_header[148:156] = b"        "
    expected_checksum = f"{sum(checksum_header):06o}\0 ".encode("ascii")
    if checksum != expected_checksum:
        raise SystemExit(f"{path}: non-canonical checksum archive metadata")
    return _tar_size(header, path)


def _discard(stream: BinaryIO, count: int, path: pathlib.Path) -> None:
    remaining = count
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit(f"{path}: truncated source archive")
        remaining -= len(chunk)


def _discard_zero_padding(stream: BinaryIO, count: int, path: pathlib.Path) -> None:
    remaining = count
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit(f"{path}: truncated source archive")
        if any(chunk):
            raise SystemExit(f"{path}: non-zero source archive padding")
        remaining -= len(chunk)


def _read_exact(stream: BinaryIO, count: int, path: pathlib.Path) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit(f"{path}: truncated source archive")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _tar_text_field(field: bytes, label: str, path: pathlib.Path) -> bytes:
    value, separator, padding = field.partition(b"\0")
    if separator and any(padding):
        raise SystemExit(f"{path}: non-canonical {label} archive metadata")
    return value


def _raw_tar_path(header: bytes, path: pathlib.Path) -> tuple[bytes, str]:
    raw_name = _tar_text_field(header[:100], "path", path)
    raw_prefix = _tar_text_field(header[345:500], "path prefix", path)
    if not raw_name:
        raise SystemExit(f"{path}: empty raw source archive path")
    raw_path = (raw_prefix + b"/" if raw_prefix else b"") + raw_name
    try:
        decoded = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit(f"{path}: raw source archive path is not UTF-8") from error
    return raw_name, decoded


def _validate_allowed_archive_path(
    name: str,
    *,
    directory: bool,
    path: pathlib.Path,
) -> None:
    _, relative_path = _archive_path(name)
    if _path_is_forbidden(name):
        raise SystemExit(f"{path}: forbidden archive path: {relative_path!r}")
    if not _path_is_allowed(relative_path, directory=directory):
        raise SystemExit(f"{path}: unexpected archive path: {relative_path!r}")


def _validate_local_pax(payload: bytes, path: pathlib.Path) -> str:
    """Allow only unambiguous UTF-8 path records needed by the built sdist."""

    if not payload:
        raise SystemExit(f"{path}: empty local archive metadata")
    offset = 0
    seen: set[str] = set()
    pax_path: str | None = None
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        raw_length = payload[offset:separator] if separator >= 0 else b""
        if not raw_length or any(byte not in b"0123456789" for byte in raw_length):
            raise SystemExit(f"{path}: malformed local archive metadata")
        record_length = int(raw_length)
        record_end = offset + record_length
        if record_end > len(payload) or record_length <= separator - offset + 2:
            raise SystemExit(f"{path}: malformed local archive metadata")
        record = payload[separator + 1 : record_end]
        if not record.endswith(b"\n"):
            raise SystemExit(f"{path}: malformed local archive metadata")
        try:
            assignment = record[:-1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise SystemExit(f"{path}: malformed local archive metadata") from error
        key, delimiter, value = assignment.partition("=")
        if not delimiter or not key or not value:
            raise SystemExit(f"{path}: malformed local archive metadata")
        if key not in ALLOWED_LOCAL_PAX_KEYS:
            raise SystemExit(f"{path}: unsupported local archive metadata key: {key!r}")
        if key in seen:
            raise SystemExit(f"{path}: duplicate local archive metadata key: {key!r}")
        seen.add(key)
        if key == "path":
            pax_path = value
        offset = record_end
    if pax_path is None:  # pragma: no cover - the allowed-key check makes this unreachable
        raise SystemExit(f"{path}: local archive metadata has no path")
    return pax_path


def _consume_tar_end(stream: BinaryIO, path: pathlib.Path) -> None:
    """Require the canonical two-block marker and bounded zero-only padding."""

    second_marker = stream.read(TAR_BLOCK_BYTES)
    if len(second_marker) != TAR_BLOCK_BYTES:
        raise SystemExit(f"{path}: source archive has no complete end marker")
    if any(second_marker):
        raise SystemExit(f"{path}: non-zero data after source archive end marker")

    trailing_zero_bytes = 2 * TAR_BLOCK_BYTES
    while chunk := stream.read(64 * 1024):
        if any(chunk):
            raise SystemExit(f"{path}: non-zero data after source archive end marker")
        trailing_zero_bytes += len(chunk)
        if trailing_zero_bytes > MAX_TRAILING_ZERO_BYTES:
            raise SystemExit(
                f"{path}: source archive end padding is too large: "
                f"{trailing_zero_bytes} bytes exceeds {MAX_TRAILING_ZERO_BYTES}"
            )
    if trailing_zero_bytes % TAR_BLOCK_BYTES:
        raise SystemExit(f"{path}: source archive end padding is not block-aligned")


def _prescan_tar_headers(path: pathlib.Path) -> None:
    """Bound metadata before tarfile can materialize hidden extension records."""

    member_count = 0
    metadata_bytes = 0
    unpacked_bytes = 0
    pending_pax_path: str | None = None
    try:
        with gzip.open(path, mode="rb") as stream:
            while True:
                header = stream.read(TAR_BLOCK_BYTES)
                if not header:
                    raise SystemExit(f"{path}: source archive has no end marker")
                if len(header) != TAR_BLOCK_BYTES:
                    raise SystemExit(f"{path}: truncated source archive header")
                if header == b"\0" * TAR_BLOCK_BYTES:
                    if pending_pax_path is not None:
                        raise SystemExit(f"{path}: local archive metadata has no payload member")
                    _consume_tar_end(stream, path)
                    return

                member_count += 1
                if member_count > MAX_MEMBER_COUNT:
                    raise SystemExit(
                        f"{path}: source archive contains too many archive members: "
                        f"{member_count} exceeds {MAX_MEMBER_COUNT}"
                    )
                metadata_bytes += TAR_BLOCK_BYTES
                if metadata_bytes > MAX_HEADER_BYTES:
                    raise SystemExit(
                        f"{path}: source archive header metadata is too large: "
                        f"{metadata_bytes} bytes exceeds {MAX_HEADER_BYTES}"
                    )

                member_type = header[156:157]
                raw_name, raw_path = _raw_tar_path(header, path)
                if member_type == b"g":
                    # tarfile consumes a global PAX payload before yielding the
                    # first member, so reject it from the bounded raw stream.
                    raise SystemExit(f"{path}: unsupported global archive metadata")
                if member_type not in TAR_REGULAR_TYPES | {
                    TAR_DIRECTORY_TYPE,
                    TAR_LOCAL_PAX_TYPE,
                }:
                    if member_type == b"\0":
                        raise SystemExit(f"{path}: non-canonical type archive metadata")
                    raise SystemExit(f"{path}: unsupported archive member type: {member_type!r}")

                size = _validate_tar_header_metadata(header, member_type, path)
                padded_size = ((size + TAR_BLOCK_BYTES - 1) // TAR_BLOCK_BYTES) * TAR_BLOCK_BYTES
                if member_type == TAR_LOCAL_PAX_TYPE:
                    if pending_pax_path is not None:
                        raise SystemExit(f"{path}: consecutive local archive metadata")
                    if raw_path != "././@PaxHeader":
                        raise SystemExit(f"{path}: non-canonical local archive metadata path")
                    metadata_bytes += padded_size
                    if metadata_bytes > MAX_HEADER_BYTES:
                        raise SystemExit(
                            f"{path}: source archive header metadata is too large: "
                            f"{metadata_bytes} bytes exceeds {MAX_HEADER_BYTES}"
                        )
                    payload = _read_exact(stream, size, path)
                    pending_pax_path = _validate_local_pax(payload, path)
                    _validate_allowed_archive_path(
                        pending_pax_path,
                        directory=False,
                        path=path,
                    )
                    _discard_zero_padding(stream, padded_size - size, path)
                elif member_type in TAR_REGULAR_TYPES:
                    if pending_pax_path is not None:
                        if raw_name != pending_pax_path.encode("utf-8")[:100]:
                            raise SystemExit(
                                f"{path}: raw archive path conflicts with local path metadata"
                            )
                        pending_pax_path = None
                    else:
                        _validate_allowed_archive_path(raw_path, directory=False, path=path)
                    unpacked_bytes += size
                    if unpacked_bytes > MAX_UNPACKED_BYTES:
                        raise SystemExit(
                            f"{path}: source archive unpacked size is too large: "
                            f"{unpacked_bytes} bytes exceeds {MAX_UNPACKED_BYTES}"
                        )
                elif size:
                    raise SystemExit(f"{path}: source archive directory carries payload bytes")
                else:
                    if pending_pax_path is not None:
                        raise SystemExit(
                            f"{path}: local path metadata does not describe a regular file"
                        )
                    _validate_allowed_archive_path(raw_path, directory=True, path=path)
                if member_type in TAR_REGULAR_TYPES:
                    _discard(stream, size, path)
                    _discard_zero_padding(stream, padded_size - size, path)
    except (EOFError, OSError) as error:
        raise SystemExit(f"{path}: unreadable compressed source archive") from error


def check_sdist(path: pathlib.Path) -> None:
    compressed_bytes = path.stat().st_size
    if compressed_bytes > MAX_COMPRESSED_BYTES:
        raise SystemExit(
            f"{path}: source archive compressed size is too large: "
            f"{compressed_bytes} bytes exceeds {MAX_COMPRESSED_BYTES}"
        )
    _validate_gzip_header(path)
    _validate_single_gzip_member(path)
    _prescan_tar_headers(path)
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
