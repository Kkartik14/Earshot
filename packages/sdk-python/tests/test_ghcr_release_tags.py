from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
IMAGE = "ghcr.io/example/earshot"
OLD_DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "c" * 64


def _load_script(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ghcr_image = _load_script("ghcr_image", SCRIPTS / "ghcr_image.py")
promote_ghcr_tags = _load_script("promote_ghcr_tags", SCRIPTS / "promote_ghcr_tags.py")


def _info(version: str, revision: str, digest: str):
    return ghcr_image.ImageInfo(
        digest=digest,
        labels={
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": revision,
        },
    )


class RegistryState:
    """Record registry observations while exercising the real promotion algorithm."""

    def __init__(self, tags: dict[str, Any], by_digest: dict[str, Any]) -> None:
        self.tags = tags
        self.by_digest = by_digest
        self.created: list[tuple[str, str]] = []

    def inspect(self, reference: str):
        return self.tags.get(reference)

    def create(self, args: list[str]) -> str:
        target = args[args.index("--tag") + 1]
        source_image, digest = args[-1].rsplit("@", 1)
        if source_image != IMAGE or digest not in self.by_digest:
            raise AssertionError("promotion must copy the verified immutable image digest")
        self.tags[target] = self.by_digest[digest]
        self.created.append((target, digest))
        return ""


def _patch_registry(monkeypatch: pytest.MonkeyPatch, registry: RegistryState) -> None:
    monkeypatch.setattr(promote_ghcr_tags, "inspect_image", registry.inspect)
    monkeypatch.setattr(promote_ghcr_tags, "_run", registry.create)


def test_inspect_image_uses_buildx_metadata_and_selects_linux_amd64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = f"{IMAGE}:0.4.0"
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"Labels": {"org.opencontainers.image.version": "0.4.0"}},
    }
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        calls.append(args)
        assert kwargs == {"capture_output": True, "check": False, "text": True}
        return CompletedProcess(args, 0, f"{OLD_DIGEST}|{json.dumps(config)}\n", "")

    monkeypatch.setattr(ghcr_image.subprocess, "run", run)

    info = ghcr_image.inspect_image(reference)

    assert info == ghcr_image.ImageInfo(
        digest=OLD_DIGEST,
        labels={"org.opencontainers.image.version": "0.4.0"},
    )
    assert calls == [
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            ghcr_image._INSPECT_FORMAT,
        ]
    ]


def test_inspect_image_treats_only_the_registry_missing_tag_response_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = f"{IMAGE}:0.4.0"

    def missing(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        return CompletedProcess(args, 1, "", f"ERROR: {reference}: not found\n")

    monkeypatch.setattr(ghcr_image.subprocess, "run", missing)
    assert ghcr_image.inspect_image(reference) is None

    def denied(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        return CompletedProcess(args, 1, "", "unauthorized: access denied")

    monkeypatch.setattr(ghcr_image.subprocess, "run", denied)
    with pytest.raises(RuntimeError, match="could not inspect"):
        ghcr_image.inspect_image(reference)


def test_release_retry_reuses_immutable_digest_and_never_rolls_aliases_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _info("0.4.0", "a" * 40, OLD_DIGEST)
    new = _info("0.4.1", "b" * 40, NEW_DIGEST)
    old_version = f"{IMAGE}:0.4.0"
    new_version = f"{IMAGE}:0.4.1"
    sha_old = f"{IMAGE}:sha-{'a' * 12}"
    sha_new = f"{IMAGE}:sha-{'b' * 12}"
    minor = f"{IMAGE}:0.4"
    latest = f"{IMAGE}:latest"
    registry = RegistryState(
        tags={old_version: old},
        by_digest={OLD_DIGEST: old, NEW_DIGEST: new},
    )
    _patch_registry(monkeypatch, registry)

    promote_ghcr_tags.promote_tags(
        IMAGE, "0.4.0", OLD_DIGEST, [old_version, sha_old, minor, latest]
    )
    assert registry.created == [(sha_old, OLD_DIGEST), (minor, OLD_DIGEST), (latest, OLD_DIGEST)]

    before_retry = list(registry.created)
    promote_ghcr_tags.promote_tags(
        IMAGE, "0.4.0", OLD_DIGEST, [old_version, sha_old, minor, latest]
    )
    assert registry.created == before_retry

    registry.tags[new_version] = new
    promote_ghcr_tags.promote_tags(
        IMAGE, "0.4.1", NEW_DIGEST, [new_version, sha_new, minor, latest]
    )
    assert registry.tags[minor].digest == NEW_DIGEST
    assert registry.tags[latest].digest == NEW_DIGEST

    before_old_retry = list(registry.created)
    promote_ghcr_tags.promote_tags(
        IMAGE, "0.4.0", OLD_DIGEST, [old_version, sha_old, minor, latest]
    )

    assert registry.created == before_old_retry
    assert registry.tags[sha_old].digest == OLD_DIGEST
    assert registry.tags[minor].digest == NEW_DIGEST
    assert registry.tags[latest].digest == NEW_DIGEST


def test_same_version_alias_with_a_different_digest_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _info("0.4.0", "a" * 40, OLD_DIGEST)
    conflict = _info("0.4.0", "c" * 40, OTHER_DIGEST)
    version = f"{IMAGE}:0.4.0"
    latest = f"{IMAGE}:latest"
    registry = RegistryState(
        tags={version: release, latest: conflict},
        by_digest={OLD_DIGEST: release},
    )
    _patch_registry(monkeypatch, registry)

    with pytest.raises(RuntimeError, match="different digest"):
        promote_ghcr_tags.promote_tags(IMAGE, "0.4.0", OLD_DIGEST, [version, latest])

    assert registry.created == []
    assert registry.tags[latest].digest == OTHER_DIGEST


def test_immutable_sha_tag_with_another_digest_is_never_retargeted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _info("0.4.0", "a" * 40, OLD_DIGEST)
    other = _info("0.3.0", "c" * 40, OTHER_DIGEST)
    version = f"{IMAGE}:0.4.0"
    sha = f"{IMAGE}:sha-{'a' * 12}"
    registry = RegistryState(
        tags={version: release, sha: other},
        by_digest={OLD_DIGEST: release},
    )
    _patch_registry(monkeypatch, registry)

    with pytest.raises(RuntimeError, match="immutable tag"):
        promote_ghcr_tags.promote_tags(IMAGE, "0.4.0", OLD_DIGEST, [version, sha])

    assert registry.created == []
    assert registry.tags[sha].digest == OTHER_DIGEST


def test_created_alias_must_resolve_to_the_requested_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _info("0.4.0", "a" * 40, OLD_DIGEST)
    wrong = _info("0.4.0", "c" * 40, OTHER_DIGEST)
    version = f"{IMAGE}:0.4.0"
    alias = f"{IMAGE}:latest"
    registry = RegistryState(
        tags={version: release},
        by_digest={OLD_DIGEST: release},
    )

    def create_wrong_digest(args: list[str]) -> str:
        target = args[args.index("--tag") + 1]
        registry.tags[target] = wrong
        registry.created.append((target, OTHER_DIGEST))
        return ""

    monkeypatch.setattr(promote_ghcr_tags, "inspect_image", registry.inspect)
    monkeypatch.setattr(promote_ghcr_tags, "_run", create_wrong_digest)

    with pytest.raises(RuntimeError, match="did not preserve"):
        promote_ghcr_tags.promote_tags(IMAGE, "0.4.0", OLD_DIGEST, [version, alias])

    assert registry.created == [(alias, OTHER_DIGEST)]
