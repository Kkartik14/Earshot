from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("npm_publish_tag", SCRIPTS / "npm_publish_tag.py")
assert spec is not None and spec.loader is not None
npm_publish_tag = importlib.util.module_from_spec(spec)
sys.modules["npm_publish_tag"] = npm_publish_tag
spec.loader.exec_module(npm_publish_tag)


def test_stable_release_keeps_latest_when_it_is_newer_or_equal() -> None:
    assert (
        npm_publish_tag.publish_dist_tag("0.4.0", prerelease=False, current_latest="0.3.9")
        == "latest"
    )
    assert (
        npm_publish_tag.publish_dist_tag("0.4.0", prerelease=False, current_latest="0.4.0")
        == "latest"
    )


def test_stable_release_cannot_move_latest_backwards() -> None:
    with pytest.raises(npm_publish_tag.NpmDistTagError, match="move npm latest backwards"):
        npm_publish_tag.publish_dist_tag("0.4.0", prerelease=False, current_latest="0.5.0")


def test_prerelease_uses_next_without_considering_latest() -> None:
    assert (
        npm_publish_tag.publish_dist_tag("0.6.0-rc.1", prerelease=True, current_latest="0.5.0")
        == "next"
    )


def test_current_npm_latest_ignores_only_a_missing_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        assert args == ["npm", "view", "earshot-observability", "dist-tags", "--json"]
        assert kwargs == {"capture_output": True, "check": False, "text": True}
        return CompletedProcess(args, 1, "", "npm error code E404\n")

    monkeypatch.setattr(npm_publish_tag.subprocess, "run", missing)
    assert npm_publish_tag.current_npm_latest("earshot-observability") is None


def test_current_npm_latest_fails_closed_on_registry_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        return CompletedProcess(args, 1, "", "npm error code ECONNRESET\n")

    monkeypatch.setattr(npm_publish_tag.subprocess, "run", unavailable)
    with pytest.raises(npm_publish_tag.NpmDistTagError, match="could not read npm dist-tags"):
        npm_publish_tag.current_npm_latest("earshot-observability")


def test_current_npm_latest_reads_the_registry_dist_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def found(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        return CompletedProcess(args, 0, '{"latest":"0.4.0","next":"0.5.0-rc.1"}', "")

    monkeypatch.setattr(npm_publish_tag.subprocess, "run", found)
    assert npm_publish_tag.current_npm_latest("earshot-observability") == "0.4.0"
