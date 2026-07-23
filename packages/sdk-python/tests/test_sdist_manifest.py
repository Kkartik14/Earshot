from __future__ import annotations

import gzip
import io
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
) -> None:
    with tarfile.open(
        path,
        mode="w:gz",
        compresslevel=compresslevel,
        format=tarfile.PAX_FORMAT,
        pax_headers=pax_headers,
    ) as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = payload_size
            archive.addfile(member, io.BytesIO(b"x" * payload_size))
        for name, payload in (payloads or {}).items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        for name in directories or []:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, target in links or []:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)


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
    with gzip.open(archive, mode="wb") as stream:
        stream.write(release_tar)
        stream.write(hidden_tar.getvalue())

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_sdist.py"), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "non-zero data after source archive end marker" in result.stderr


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
        [f"{root}/{name}" for name in REQUIRED_NAMES],
        directories=[
            (
                f"{root}/packages/sdk-python/src/earshot/generated/"
                f"{'long-directory-name-' * 100}{index}"
            )
            for index in range(400)
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
