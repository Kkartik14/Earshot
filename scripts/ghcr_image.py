"""Inspect a release image's digest and config metadata without pulling its layers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INSPECT_FORMAT = "{{.Manifest.Digest}}|{{json .Image}}"


@dataclass(frozen=True, slots=True)
class ImageInfo:
    digest: str
    labels: dict[str, Any]


def _missing_tag(result: subprocess.CompletedProcess[str], reference: str) -> bool:
    lines = (result.stderr + result.stdout).strip().splitlines()
    return lines == [f"ERROR: {reference}: not found"]


def inspect_image(reference: str) -> ImageInfo | None:
    """Return the root digest and AMD64 image labels; ``None`` means tag absent."""

    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            _INSPECT_FORMAT,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        if _missing_tag(result, reference):
            return None
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"could not inspect {reference}: {message}")

    fields = result.stdout.strip().split("|", 1)
    if len(fields) != 2:
        raise RuntimeError(f"registry returned incomplete image metadata for {reference}")
    digest, raw_image = fields
    if not _DIGEST.fullmatch(digest):
        raise RuntimeError(f"registry returned an invalid digest for {reference}")
    try:
        image = json.loads(raw_image)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"registry returned malformed image metadata for {reference}") from error

    if (
        isinstance(image, dict)
        and image.get("os") == "linux"
        and image.get("architecture") == "amd64"
    ):
        selected = image
    elif (
        isinstance(image, dict)
        and set(image) == {"linux/amd64"}
        and isinstance(image.get("linux/amd64"), dict)
    ):
        selected = image["linux/amd64"]
        if selected.get("os") != "linux" or selected.get("architecture") != "amd64":
            raise RuntimeError(f"{reference} has invalid linux/amd64 image metadata")
    else:
        raise RuntimeError(f"{reference} has no unambiguous linux/amd64 image config")

    config = selected.get("config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise RuntimeError(f"{reference} has unreadable OCI config labels")
    return ImageInfo(digest=digest, labels=labels)


def main() -> int:
    import os

    try:
        reference = f"{os.environ['RELEASE_IMAGE']}:{os.environ['RELEASE_VERSION']}"
        info = inspect_image(reference)
        if info is None:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("exists=false\n")
                output.write("digest=\n")
            print(f"{reference} does not exist; a first publication will build and push it.")
            return 0
        actual_version = info.labels.get("org.opencontainers.image.version")
        actual_revision = info.labels.get("org.opencontainers.image.revision")
        expected_version = os.environ["RELEASE_VERSION"]
        expected_revision = os.environ["RELEASE_SHA"]
        if actual_version != expected_version or actual_revision != expected_revision:
            raise RuntimeError(
                f"{reference} already exists at {info.digest} with version={actual_version!r} "
                f"revision={actual_revision!r}; expected version={expected_version!r} "
                f"revision={expected_revision!r}. Refusing to retarget it."
            )
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("exists=true\n")
            output.write(f"digest={info.digest}\n")
        print(f"Reusing {reference} at {info.digest}; skipping image build and tag updates.")
        return 0
    except (KeyError, RuntimeError) as error:
        print(f"GHCR release image check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
