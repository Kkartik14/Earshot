"""The provider fixture scrubber checks the env file passed on its CLI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRUBBER = ROOT / "scripts" / "scrub_provider_capture.py"


def load_scrubber_module():
    spec = importlib.util.spec_from_file_location("earshot_test_scrub_provider_capture", SCRUBBER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_env_file_is_scanned_by_the_leak_guard(monkeypatch, tmp_path) -> None:
    module = load_scrubber_module()
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "ELEVENLABS_API_KEY=synthetic-secret-value-123456\n"
        "CARTESIA_API_KEY=synthetic-cartesia-value-123456\n"
        "CARTESIA_VOICE_ID=synthetic-voice-value\n"
    )
    output = tmp_path / "fixture"
    output.mkdir()
    (output / "payload.json").write_text("synthetic-secret-value-123456")
    monkeypatch.setattr(module, "ENV_FILES", [])
    monkeypatch.setattr(module, "build_elevenlabs", lambda: module._guard(output, set()))
    monkeypatch.setattr(
        sys, "argv", ["scrub_provider_capture.py", "elevenlabs", "--env", str(env_file)]
    )
    monkeypatch.setenv("CARTESIA_API_KEY", "present-in-process")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "present-in-process")

    with pytest.raises(SystemExit, match="refusing to write"):
        module.main()

    assert not output.exists()
