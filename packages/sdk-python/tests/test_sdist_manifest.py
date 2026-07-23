from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
PACKAGE_NAMES = (ROOT / "scripts" / "sdist_package_manifest.txt").read_text().splitlines()
REQUIRED_NAMES = [
    "pyproject.toml",
    *PACKAGE_NAMES,
    "packages/sdk-python/src/earshot/web/index.html",
    "packages/sdk-python/src/earshot/web/assets/index.js",
]
HATCH_BUILD_MTIME = 1_580_601_600


def _write_canonical_gzip(
    path: Path,
    payload: bytes,
    *,
    compresslevel: int = 9,
) -> None:
    with (
        path.open("wb") as raw_stream,
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_stream,
            compresslevel=compresslevel,
            mtime=HATCH_BUILD_MTIME,
        ) as stream,
    ):
        stream.write(payload)


def _replace_first_tar_header_field(
    path: Path,
    field: slice,
    replacement: bytes,
) -> None:
    with gzip.open(path, mode="rb") as stream:
        tar_payload = bytearray(stream.read())
    assert field.start is not None
    assert field.stop is not None
    assert len(replacement) == field.stop - field.start
    header = bytearray(tar_payload[:512])
    header[field] = replacement
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):06o}\0 ".encode("ascii")
    tar_payload[:512] = header
    _write_canonical_gzip(path, bytes(tar_payload))


def _use_noncanonical_first_tar_checksum(path: Path) -> None:
    with gzip.open(path, mode="rb") as stream:
        tar_payload = bytearray(stream.read())
    header = bytearray(tar_payload[:512])
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):07o}\0".encode("ascii")
    tar_payload[:512] = header
    _write_canonical_gzip(path, bytes(tar_payload))


def _replace_gzip_header_field(path: Path, field: slice, replacement: bytes) -> None:
    payload = bytearray(path.read_bytes())
    assert field.start is not None
    assert field.stop is not None
    assert len(replacement) == field.stop - field.start
    payload[field] = replacement
    path.write_bytes(payload)


def _inject_gzip_optional_header(path: Path, flag: int, payload: bytes) -> None:
    compressed = bytearray(path.read_bytes())
    compressed[3] = flag
    compressed[10:10] = payload
    path.write_bytes(compressed)


def _hostile_pythonpath(tmp_path: Path) -> dict[str, str]:
    fake_package = tmp_path / "hostile-pythonpath" / "earshot"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text(
        "raise RuntimeError('release tool imported an unrelated checkout')\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(fake_package.parent)
    # This child deliberately imports a copied checkout outside the coverage
    # source tree. Inheriting pytest-cov's subprocess hooks would write a
    # statement-only shard beside the parent's branch data, making the final
    # combine step fail before it can report coverage.
    for variable in tuple(environment):
        if variable.startswith("COV_CORE_") or variable == "COVERAGE_PROCESS_START":
            environment.pop(variable)
    return environment


def test_fault_fixture_generator_imports_sdk_from_its_own_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    script = checkout / "scripts" / "generate_fault_fixtures.py"
    package = checkout / "packages" / "sdk-python" / "src" / "earshot"
    script.parent.mkdir(parents=True)
    package.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / script.name, script)
    shutil.copytree(ROOT / "packages" / "sdk-python" / "src" / "earshot", package)

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=checkout,
        env=_hostile_pythonpath(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (checkout / "fixtures" / "faults" / "slow_endpointing.incident.json").is_file()


def test_capture_scrubber_imports_sdk_from_its_own_checkout(tmp_path: Path) -> None:
    destination = tmp_path / "scrubbed.incident.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "scrub_captured_fixture.py"),
            str(ROOT / "fixtures" / "captured" / "deepgram.incident.json"),
            str(destination),
            "--surface",
            "hostile-path-test",
        ],
        cwd=ROOT,
        env=_hostile_pythonpath(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert destination.is_file()


def test_sdist_package_manifest_matches_runtime_tree() -> None:
    package_root = ROOT / "packages" / "sdk-python" / "src" / "earshot"
    runtime_names = sorted(
        path.relative_to(ROOT).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
        and "web" not in path.relative_to(package_root).parts
        and (path.suffix in {".py", ".pyi"} or path.name == "py.typed")
    )

    assert runtime_names == PACKAGE_NAMES


def _write_archive(
    path: Path,
    names: list[str],
    *,
    payload_size: int = 13,
    payloads: dict[str, bytes] | None = None,
    compresslevel: int = 9,
    directories: list[str] | None = None,
    links: list[tuple[str, str]] | None = None,
    pax_headers: dict[str, str] | None = None,
    local_pax_headers: dict[str, dict[str, str]] | None = None,
) -> None:
    tar_payload = io.BytesIO()
    with tarfile.open(
        fileobj=tar_payload,
        mode="w",
        format=tarfile.PAX_FORMAT,
        pax_headers=pax_headers,
    ) as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = payload_size
            member.mtime = HATCH_BUILD_MTIME
            member.pax_headers = (local_pax_headers or {}).get(name, {})
            archive.addfile(member, io.BytesIO(b"x" * payload_size))
        for name, payload in (payloads or {}).items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mtime = HATCH_BUILD_MTIME
            archive.addfile(member, io.BytesIO(payload))
        for name in directories or []:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mtime = HATCH_BUILD_MTIME
            archive.addfile(member)
        for name, target in links or []:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            member.mtime = HATCH_BUILD_MTIME
            archive.addfile(member)
    _write_canonical_gzip(path, tar_payload.getvalue(), compresslevel=compresslevel)


def test_sdist_checker_accepts_minimal_release_layout(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "source distribution contains bundled viewer" in result.stdout


def test_sdist_checker_rejects_forbidden_local_payload(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/local/sdist-sentinel/SHOULD_NOT_SHIP.md",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "forbidden archive path" in result.stderr


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/sdk-python/src/earshot/adapters/node_modules/leak.js",
        "packages/sdk-python/src/earshot/local/debug.json",
        "packages/sdk-python/src/earshot/private/config.py",
        "packages/sdk-python/src/earshot/dev/debug.py",
        "packages/sdk-python/src/earshot/tests/test_release_secret.py",
        "packages/sdk-python/src/earshot/.git/config",
        "packages/sdk-python/src/earshot/.venv/pyvenv.cfg",
        "packages/sdk-python/src/earshot/__pycache__/api.pyc",
        "packages/sdk-python/src/earshot/.pytest_cache/state",
        "packages/sdk-python/src/earshot/dist/debug.js",
        "packages/sdk-python/src/earshot/.env",
        "packages/sdk-python/src/earshot/.env.production",
        "packages/sdk-python/src/earshot/.envrc",
    ],
)
def test_sdist_checker_rejects_forbidden_segments_anywhere(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/{relative_path}",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "forbidden archive path" in result.stderr


def test_sdist_checker_rejects_path_outside_manifest_allowlist(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/notes/private-release-notes.txt",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unexpected archive path" in result.stderr


def test_sdist_checker_rejects_cross_platform_path_separators(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/..\\..\\private.py",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe source archive path" in result.stderr


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/sdk-python/src/earshot/web/src/main.tsx",
        "packages/sdk-python/src/earshot/web/vite.config.ts",
        "packages/sdk-python/src/earshot/web/README.md",
        "packages/sdk-python/src/earshot/web/assets/index.js.map",
        "packages/sdk-python/src/earshot/web/assets/source.ts",
        "packages/sdk-python/src/earshot/web/assets/nested/chunk.js",
    ],
)
def test_sdist_checker_allows_only_compiled_viewer_outputs(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/{relative_path}",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unexpected archive path" in result.stderr


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/sdk-python/src/earshot/.coverage",
        "packages/sdk-python/src/earshot/.DS_Store",
        "packages/sdk-python/src/earshot/api.py.bak",
        "packages/sdk-python/src/earshot/api.py~",
        "packages/sdk-python/src/earshot/credentials.json",
        "packages/sdk-python/src/earshot/debug.log",
        "packages/sdk-python/src/earshot/notes.md",
    ],
)
def test_sdist_checker_rejects_non_runtime_package_debris(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/{relative_path}",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unexpected archive path" in result.stderr


def test_sdist_checker_rejects_unlisted_python_module(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/scratch/credentials.py",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unexpected archive path" in result.stderr


def test_sdist_checker_rejects_multiple_archive_roots(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            "unrelated-distribution/README.md",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "multiple archive roots" in result.stderr


def test_sdist_checker_rejects_links_inside_allowed_package_tree(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        links=[
            (
                f"{root}/packages/sdk-python/src/earshot/leaked.py",
                "/etc/passwd",
            )
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported archive member type" in result.stderr


def test_sdist_checker_rejects_duplicate_archive_paths(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/pyproject.toml",
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "duplicate archive path" in result.stderr


def test_sdist_checker_rejects_nonzero_member_padding(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    with gzip.open(archive, mode="rb") as stream:
        tar_payload = bytearray(stream.read())
    tar_payload[512 + 13] = 1
    _write_canonical_gzip(archive, bytes(tar_payload))

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "non-zero source archive padding" in result.stderr


def test_sdist_checker_rejects_nonzero_payload_after_end_marker(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    with gzip.open(archive, mode="rb") as stream:
        release_tar = stream.read()
    hidden_tar = io.BytesIO()
    with tarfile.open(fileobj=hidden_tar, mode="w") as trailing_archive:
        member = tarfile.TarInfo(f"{root}/hidden/secret.txt")
        member.size = 6
        trailing_archive.addfile(member, io.BytesIO(b"secret"))
    _write_canonical_gzip(archive, release_tar + hidden_tar.getvalue())

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "non-zero data after source archive end marker" in result.stderr


def test_sdist_checker_rejects_non_block_aligned_end_padding(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    with gzip.open(archive, mode="rb") as stream:
        release_tar = stream.read()
    _write_canonical_gzip(archive, release_tar + b"\0")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "end padding is not block-aligned" in result.stderr


def test_sdist_checker_rejects_excessive_file_count(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            *(
                f"{root}/packages/sdk-python/src/earshot/web/assets/extra-{index}.js"
                for index in range(513)
            ),
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "too many files" in result.stderr


def test_sdist_checker_rejects_excessive_total_member_count(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        directories=[
            f"{root}/packages/sdk-python/src/earshot/generated/member-{index}"
            for index in range(1_025)
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "too many archive members" in result.stderr


def test_sdist_checker_rejects_excessive_header_metadata(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            *(
                (
                    f"{root}/packages/sdk-python/src/earshot/web/assets/"
                    f"{'long-asset-name-' * 100}{index}.js"
                )
                for index in range(400)
            ),
        ],
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "header metadata is too large" in result.stderr


def test_sdist_checker_rejects_global_pax_metadata_before_payload_parse(
    tmp_path: Path,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        pax_headers={"comment": "x" * (2 * 1024 * 1024)},
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert archive.stat().st_size < 8 * 1024 * 1024
    assert result.returncode != 0
    assert "global archive metadata" in result.stderr


def test_sdist_checker_rejects_unknown_local_pax_metadata(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    pyproject = f"{root}/pyproject.toml"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        local_pax_headers={pyproject: {"comment": "unexpected release metadata"}},
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported local archive metadata key" in result.stderr


def test_sdist_checker_rejects_raw_path_hidden_by_local_pax_path(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    hidden_path = f"{root}/local/PRIVATE_RELEASE_PATH.txt"
    allowed_path = f"{root}/packages/sdk-python/src/earshot/web/assets/index.css"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            hidden_path,
        ],
        local_pax_headers={hidden_path: {"path": allowed_path}},
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "raw archive path conflicts with local path metadata" in result.stderr


@pytest.mark.parametrize(
    ("field", "replacement", "label"),
    [
        (slice(100, 108), b"0000755\0", "mode"),
        (slice(108, 116), b"0000001\0", "uid"),
        (slice(116, 124), b"0000001\0", "gid"),
        (slice(124, 136), b"         15\0", "size"),
        (slice(136, 148), b"00000000000\0", "mtime"),
        (slice(156, 157), b"\0", "type"),
        (slice(157, 257), b"private-target" + b"\0" * 86, "linkname"),
        (slice(257, 263), b"ustar ", "magic"),
        (slice(263, 265), b"01", "version"),
        (slice(265, 297), b"private-user" + b"\0" * 20, "uname"),
        (slice(297, 329), b"private-group" + b"\0" * 19, "gname"),
        (slice(329, 337), b"0000001\0", "device major"),
        (slice(337, 345), b"0000001\0", "device minor"),
        (slice(345, 500), b"hidden-prefix" + b"\0" * 142, "path prefix"),
        (slice(500, 512), b"hidden-bytes", "reserved"),
    ],
)
def test_sdist_checker_rejects_noncanonical_standard_header_metadata(
    tmp_path: Path,
    field: slice,
    replacement: bytes,
    label: str,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    _replace_first_tar_header_field(archive, field, replacement)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert f"non-canonical {label} archive metadata" in result.stderr


def test_sdist_checker_rejects_noncanonical_checksum_encoding(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    _use_noncanonical_first_tar_checksum(archive)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "non-canonical checksum archive metadata" in result.stderr


@pytest.mark.parametrize(
    ("flag", "metadata"),
    [
        (0x01, b""),
        (0x02, b"\0\0"),
        (0x04, b"\x04\0leak"),
        (0x08, b"private-source.tar\0"),
        (0x10, b"private build comment\0"),
        (0x20, b""),
    ],
    ids=("text", "header-crc", "extra", "filename", "comment", "reserved"),
)
def test_sdist_checker_rejects_optional_or_reserved_gzip_header_flags(
    tmp_path: Path,
    flag: int,
    metadata: bytes,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    _inject_gzip_optional_header(archive, flag, metadata)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported gzip header flags" in result.stderr


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (slice(0, 2), b"\x1f\x9d", "invalid gzip signature"),
        (slice(2, 3), b"\0", "unsupported gzip compression method"),
        (slice(4, 8), b"\0\0\0\0", "non-canonical gzip mtime metadata"),
        (slice(8, 9), b"\0", "non-canonical gzip compression metadata"),
        (slice(9, 10), b"\x03", "non-canonical gzip platform metadata"),
    ],
)
def test_sdist_checker_rejects_noncanonical_fixed_gzip_header_metadata(
    tmp_path: Path,
    field: slice,
    replacement: bytes,
    message: str,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    _replace_gzip_header_field(archive, field, replacement)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr


def test_sdist_checker_rejects_concatenated_gzip_member_with_hidden_metadata(
    tmp_path: Path,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            f"{root}/packages/sdk-python/src/earshot/web/assets/index.css",
        ],
    )
    trailing_member = tmp_path / "trailing.gz"
    _write_canonical_gzip(trailing_member, b"\0" * 512)
    _inject_gzip_optional_header(trailing_member, 0x08, b"private-source.tar\0")
    archive.write_bytes(archive.read_bytes() + trailing_member.read_bytes())

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "multiple gzip members or trailing compressed data" in result.stderr


def test_sdist_checker_rejects_excessive_compressed_size_before_parsing(
    tmp_path: Path,
) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        payloads={
            f"{root}/packages/sdk-python/src/earshot/large_but_valid.py": b"x" * (9 * 1024 * 1024)
        },
        compresslevel=0,
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert archive.stat().st_size > 8 * 1024 * 1024
    assert result.returncode != 0
    assert "compressed size is too large" in result.stderr


def test_sdist_checker_rejects_excessive_unpacked_size(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        payload_size=6 * 1024 * 1024,
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unpacked size" in result.stderr
