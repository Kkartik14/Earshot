from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
REQUIRED_NAMES = [
    "pyproject.toml",
    "packages/sdk-python/src/earshot/__init__.py",
    "packages/sdk-python/src/earshot/cli.py",
    "packages/sdk-python/src/earshot/generated/earshot/v1alpha1/incident_pb2.py",
    "packages/sdk-python/src/earshot/web/index.html",
    "packages/sdk-python/src/earshot/web/assets/index.js",
]


def _write_archive(
    path: Path,
    names: list[str],
    *,
    payload_size: int = 13,
    links: list[tuple[str, str]] | None = None,
) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = payload_size
            archive.addfile(member, io.BytesIO(b"x" * payload_size))
        for name, target in links or []:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)


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


def test_sdist_checker_rejects_excessive_file_count(tmp_path: Path) -> None:
    root = "earshot_observability-0.1.0"
    archive = tmp_path / f"{root}.tar.gz"
    _write_archive(
        archive,
        [
            *(f"{root}/{name}" for name in REQUIRED_NAMES),
            *(f"{root}/packages/sdk-python/src/earshot/extra-{index}.py" for index in range(513)),
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
