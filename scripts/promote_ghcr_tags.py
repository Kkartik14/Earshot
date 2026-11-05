#!/usr/bin/env python3
"""Promote GHCR aliases from an already-published, verified image digest."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence

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


def _missing_tag(result: subprocess.CompletedProcess[str], reference: str) -> bool:
    lines = (result.stderr + result.stdout).strip().splitlines()
    return lines == [f"ERROR: {reference}: not found"]


def _manifest_digest(reference: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{.Manifest.Digest}}",
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
    digest = result.stdout.strip()
    if not _DIGEST.fullmatch(digest):
        raise RuntimeError(f"registry returned an invalid digest for {reference}")
    return digest


def _pull_labels(reference: str, expected_digest: str) -> dict[str, str]:
    _run(["docker", "pull", "--platform", "linux/amd64", reference])
    repo_digests = json.loads(
        _run(
            [
                "docker",
                "image",
                "inspect",
                reference,
                "--format",
                "{{json .RepoDigests}}",
            ]
        )
    )
    if not isinstance(repo_digests, list) or not any(
        isinstance(value, str) and value.rsplit("@", 1)[-1] == expected_digest
        for value in repo_digests
    ):
        raise RuntimeError(f"{reference} changed while its digest was being checked")
    labels = json.loads(
        _run(
            [
                "docker",
                "image",
                "inspect",
                reference,
                "--format",
                "{{json .Config.Labels}}",
            ]
        )
    )
    if not isinstance(labels, dict):
        raise RuntimeError(f"{reference} has no readable OCI labels")
    return labels


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
    if _manifest_digest(reference) != digest:
        raise RuntimeError(f"promoting {reference} did not preserve {digest}")


def _promote_sha_tag(image: str, reference: str, digest: str) -> None:
    existing = _manifest_digest(reference)
    if existing == digest:
        print(f"Verified immutable tag {reference} at {digest}")
        return
    if existing is not None:
        raise RuntimeError(
            f"immutable tag {reference} already points to {existing}; expected {digest}"
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
    current = _manifest_digest(reference)
    if current == digest:
        print(f"Verified alias {reference} at {digest}")
        return
    if current is None:
        _create_tag(image, reference, digest)
        print(f"Created alias {reference} at {digest}")
        return

    labels = _pull_labels(reference, current)
    existing_version = labels.get("org.opencontainers.image.version")
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
        print(f"Keeping newer alias {reference} at {existing_version} ({current})")
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
    if _manifest_digest(version_reference) != digest:
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
    except (KeyError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"GHCR tag promotion failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
