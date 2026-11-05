#!/usr/bin/env python3
"""Promote GHCR aliases from an already-published, verified image digest."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence

from ghcr_image import inspect_image

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_SHA_TAG = re.compile(r"sha-[0-9a-f]{7,40}\Z")
_MINOR_TAG = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def _run(args: Sequence[str]) -> str:
    result = subprocess.run(args, capture_output=True, check=False, text=True)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{args[0]} command failed: {message}")
    return result.stdout.strip()


def _stable_version(value: str) -> tuple[int, int, int]:
    match = _STABLE_VERSION.fullmatch(value)
    if match is None:
        raise RuntimeError(f"expected a stable SemVer label, got {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _create_tag(image: str, reference: str, digest: str) -> None:
    _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--prefer-index=false",
            "--tag",
            reference,
            f"{image}@{digest}",
        ]
    )
    info = inspect_image(reference)
    if info is None or info.digest != digest:
        raise RuntimeError(f"promoting {reference} did not preserve {digest}")


def _promote_sha_tag(image: str, reference: str, digest: str) -> None:
    existing = inspect_image(reference)
    if existing is not None and existing.digest == digest:
        print(f"Verified immutable tag {reference} at {digest}")
        return
    if existing is not None:
        raise RuntimeError(
            f"immutable tag {reference} already points to {existing.digest}; expected {digest}"
        )
    _create_tag(image, reference, digest)
    print(f"Created immutable tag {reference} at {digest}")


def _promote_moving_alias(
    image: str,
    version: str,
    reference: str,
    tag: str,
    digest: str,
) -> None:
    current = inspect_image(reference)
    if current is not None and current.digest == digest:
        print(f"Verified alias {reference} at {digest}")
        return
    if current is None:
        _create_tag(image, reference, digest)
        print(f"Created alias {reference} at {digest}")
        return

    existing_version = current.labels.get("org.opencontainers.image.version")
    if not isinstance(existing_version, str):
        raise RuntimeError(f"{reference} has no usable OCI version label")
    existing_order = _stable_version(existing_version)
    release_order = _stable_version(version)
    minor_match = _MINOR_TAG.fullmatch(tag)
    if minor_match is not None and existing_order[:2] != tuple(
        int(part) for part in minor_match.groups()
    ):
        raise RuntimeError(
            f"{reference} points outside its {tag} release line ({existing_version})"
        )
    if existing_order > release_order:
        print(f"Keeping newer alias {reference} at {existing_version} ({current.digest})")
        return
    if existing_order == release_order:
        raise RuntimeError(f"{reference} labels version {version} but points to a different digest")

    _create_tag(image, reference, digest)
    print(f"Advanced alias {reference} from {existing_version} to {version}")


def promote_tags(image: str, version: str, digest: str, tag_lines: Sequence[str]) -> None:
    if not image:
        raise RuntimeError("the release image is empty")
    if not _DIGEST.fullmatch(digest):
        raise RuntimeError("the release digest is not a SHA-256 OCI digest")
    version_reference = f"{image}:{version}"
    if version_reference not in tag_lines:
        raise RuntimeError(f"metadata output omitted immutable version tag {version_reference}")
    info = inspect_image(version_reference)
    if info is None or info.digest != digest:
        raise RuntimeError(
            f"immutable version tag {version_reference} does not resolve to {digest}"
        )

    for reference in tag_lines:
        if not reference:
            continue
        prefix = f"{image}:"
        if not reference.startswith(prefix):
            raise RuntimeError(f"metadata emitted a tag for an unexpected image: {reference}")
        tag = reference[len(prefix) :]
        if reference == version_reference:
            continue
        if _SHA_TAG.fullmatch(tag):
            _promote_sha_tag(image, reference, digest)
        elif tag == "latest" or _MINOR_TAG.fullmatch(tag):
            _promote_moving_alias(image, version, reference, tag, digest)
        else:
            raise RuntimeError(f"metadata emitted an unsupported release alias: {reference}")


def main() -> int:
    import os

    try:
        promote_tags(
            os.environ["RELEASE_IMAGE"],
            os.environ["RELEASE_VERSION"],
            os.environ["RELEASE_DIGEST"],
            sys.stdin.read().splitlines(),
        )
    except (KeyError, RuntimeError, ValueError) as error:
        print(f"GHCR tag promotion failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
