"""Stage only unuploaded, hash-verified distributions for a PyPI release retry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PROJECT_NAME = "earshot-observability"
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class ReleasePreparationError(ValueError):
    """Raised when local release artifacts cannot be safely reconciled with PyPI."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _published_files(version: str) -> dict[str, str]:
    url = f"https://pypi.org/pypi/{PROJECT_NAME}/{quote(version, safe='')}/json"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            document = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise ReleasePreparationError(
            f"PyPI release lookup failed with HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ReleasePreparationError(f"PyPI release lookup failed: {error}") from error
    except json.JSONDecodeError as error:
        raise ReleasePreparationError("PyPI returned invalid JSON for the release") from error

    if not isinstance(document, dict) or not isinstance(document.get("urls"), list):
        raise ReleasePreparationError("PyPI release JSON has no valid urls list")

    published: dict[str, str] = {}
    for item in document["urls"]:
        if not isinstance(item, dict):
            raise ReleasePreparationError("PyPI release JSON contains an invalid file entry")
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not filename:
            raise ReleasePreparationError("PyPI release JSON contains a file without a filename")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ReleasePreparationError(
                f"PyPI release JSON has no valid SHA-256 for {filename!r}"
            )
        if filename in published:
            raise ReleasePreparationError(f"PyPI release JSON repeats filename {filename!r}")
        published[filename] = digest.lower()
    return published


def _local_distributions(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        raise ReleasePreparationError(f"distribution directory does not exist: {directory}")
    entries = list(directory.iterdir())
    if any(not entry.is_file() for entry in entries):
        raise ReleasePreparationError("distribution directory contains a non-file entry")
    distributions = {entry.name: entry for entry in entries}
    wheels = [name for name in distributions if name.endswith(".whl")]
    sdists = [name for name in distributions if name.endswith(".tar.gz")]
    if len(distributions) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ReleasePreparationError(
            "expected exactly one wheel and one source distribution in the release artifact"
        )
    return distributions


def prepare_upload(version: str, dist_dir: Path, upload_dir: Path) -> bool:
    """Stage missing files, failing on filename or content conflicts with PyPI."""
    if upload_dir.exists():
        raise ReleasePreparationError(f"upload directory already exists: {upload_dir}")

    local = _local_distributions(dist_dir)
    published = _published_files(version)
    unexpected = sorted(published.keys() - local.keys())
    if unexpected:
        raise ReleasePreparationError(
            "PyPI already contains unexpected distributions for this version: "
            + ", ".join(unexpected)
        )

    local_hashes = {filename: _sha256(path) for filename, path in local.items()}
    mismatches = sorted(
        filename for filename, digest in published.items() if digest != local_hashes[filename]
    )
    if mismatches:
        raise ReleasePreparationError(
            "PyPI SHA-256 mismatch for existing distributions: " + ", ".join(mismatches)
        )

    missing = sorted(local.keys() - published.keys())
    upload_dir.mkdir(parents=True)
    for filename in missing:
        shutil.copyfile(local[filename], upload_dir / filename)
    return bool(missing)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--upload-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        has_uploads = prepare_upload(args.version, args.dist_dir, args.upload_dir)
    except (OSError, ReleasePreparationError) as error:
        parser.error(str(error))

    print(
        "staged missing PyPI distributions"
        if has_uploads
        else "all PyPI distributions already match"
    )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"has_uploads={str(has_uploads).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
