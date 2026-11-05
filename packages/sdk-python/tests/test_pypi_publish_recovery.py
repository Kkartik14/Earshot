from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "prepare_pypi_upload.py"
SPEC = importlib.util.spec_from_file_location("prepare_pypi_upload", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prepare_pypi_upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_pypi_upload)


def write_artifacts(directory: Path) -> dict[str, bytes]:
    directory.mkdir()
    artifacts = {
        "earshot_observability-0.4.0-py3-none-any.whl": b"wheel bytes",
        "earshot_observability-0.4.0.tar.gz": b"sdist bytes",
    }
    for filename, content in artifacts.items():
        (directory / filename).write_bytes(content)
    return artifacts


def response_for(files: dict[str, bytes]) -> bytes:
    return json.dumps(
        {
            "urls": [
                {
                    "filename": filename,
                    "digests": {"sha256": hashlib.sha256(content).hexdigest()},
                }
                for filename, content in files.items()
            ]
        }
    ).encode()


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content


def test_first_upload_stages_both_artifacts_when_release_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = write_artifacts(tmp_path / "dist")
    upload_dir = tmp_path / "upload"

    def missing_release(url: str, *, timeout: float) -> FakeResponse:
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(prepare_pypi_upload, "urlopen", missing_release)

    has_uploads = prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)

    assert has_uploads is True
    assert {path.name: path.read_bytes() for path in upload_dir.iterdir()} == artifacts


def test_partial_upload_stages_only_the_missing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = write_artifacts(tmp_path / "dist")
    wheel_name = next(name for name in artifacts if name.endswith(".whl"))
    upload_dir = tmp_path / "upload"
    monkeypatch.setattr(
        prepare_pypi_upload,
        "urlopen",
        lambda url, *, timeout: FakeResponse(response_for({wheel_name: artifacts[wheel_name]})),
    )

    has_uploads = prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)

    sdist_name = next(name for name in artifacts if name.endswith(".tar.gz"))
    assert has_uploads is True
    assert [path.name for path in upload_dir.iterdir()] == [sdist_name]
    assert (upload_dir / sdist_name).read_bytes() == artifacts[sdist_name]


def test_same_filename_with_different_published_hash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = write_artifacts(tmp_path / "dist")
    wheel_name = next(name for name in artifacts if name.endswith(".whl"))
    upload_dir = tmp_path / "upload"
    monkeypatch.setattr(
        prepare_pypi_upload,
        "urlopen",
        lambda url, *, timeout: FakeResponse(
            response_for({wheel_name: b"different published wheel"})
        ),
    )

    with pytest.raises(prepare_pypi_upload.ReleasePreparationError, match="SHA-256 mismatch"):
        prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)

    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_unexpected_published_artifact_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_artifacts(tmp_path / "dist")
    upload_dir = tmp_path / "upload"
    monkeypatch.setattr(
        prepare_pypi_upload,
        "urlopen",
        lambda url, *, timeout: FakeResponse(
            response_for({"earshot_observability-0.4.0-py2.py3-none-any.whl": b"old"})
        ),
    )

    with pytest.raises(prepare_pypi_upload.ReleasePreparationError, match="unexpected"):
        prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)


def test_pypi_lookup_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_artifacts(tmp_path / "dist")
    upload_dir = tmp_path / "upload"

    def unavailable(url: str, *, timeout: float) -> FakeResponse:
        raise HTTPError(url, 503, "Unavailable", {}, None)

    monkeypatch.setattr(prepare_pypi_upload, "urlopen", unavailable)

    with pytest.raises(prepare_pypi_upload.ReleasePreparationError, match="HTTP 503"):
        prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)

    assert not upload_dir.exists()


def test_complete_matching_release_needs_no_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = write_artifacts(tmp_path / "dist")
    upload_dir = tmp_path / "upload"
    monkeypatch.setattr(
        prepare_pypi_upload,
        "urlopen",
        lambda url, *, timeout: FakeResponse(response_for(artifacts)),
    )

    has_uploads = prepare_pypi_upload.prepare_upload("0.4.0", tmp_path / "dist", upload_dir)

    assert has_uploads is False
    assert list(upload_dir.iterdir()) == []
