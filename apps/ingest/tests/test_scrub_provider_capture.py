"""The provider fixture scrubber checks the env file passed on its CLI."""

from __future__ import annotations

import importlib.util
import shutil
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
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr(module, "FIXTURES", fixtures)
    monkeypatch.setattr(module, "ENV_FILES", [])

    def build(out: Path | None = None) -> None:
        target = out or (fixtures / "elevenlabs")
        target.mkdir(parents=True)
        (target / "payload.json").write_text("synthetic-secret-value-123456")
        module._guard(target, set())

    monkeypatch.setattr(module, "build_elevenlabs", build)
    monkeypatch.setattr(
        sys, "argv", ["scrub_provider_capture.py", "elevenlabs", "--env", str(env_file)]
    )
    monkeypatch.setenv("CARTESIA_API_KEY", "present-in-process")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "present-in-process")

    with pytest.raises(SystemExit, match="refusing to write"):
        module.main()

    assert not (fixtures / "elevenlabs").exists()


def test_guard_scans_distinct_values_for_duplicate_env_keys(tmp_path, monkeypatch) -> None:
    module = load_scrubber_module()
    default_env = tmp_path / "default.env"
    selected_env = tmp_path / "selected.env"
    default_env.write_text("CARTESIA_API_KEY=default-env-key-12345678\n")
    selected_env.write_text("CARTESIA_API_KEY=selected-env-key-87654321\n")
    output = tmp_path / "fixture"
    output.mkdir()
    (output / "payload.json").write_text("selected-env-key-87654321")
    monkeypatch.setattr(module, "ENV_FILES", [default_env, selected_env])
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="refusing to write"):
        module._guard(output, set())

    assert not output.exists()


def test_guard_scans_process_environment_credentials(tmp_path, monkeypatch) -> None:
    module = load_scrubber_module()
    env_file = tmp_path / "provider.env"
    env_file.write_text("CARTESIA_API_KEY=file-env-key-12345678\n")
    output = tmp_path / "fixture"
    output.mkdir()
    secret = "process-env-key-123456789"
    (output / "payload.json").write_text(secret)
    monkeypatch.setattr(module, "ENV_FILES", [env_file])
    monkeypatch.setenv("CARTESIA_API_KEY", secret)

    with pytest.raises(SystemExit, match="refusing to write"):
        module._guard(output, set())

    assert not output.exists()


@pytest.mark.parametrize(
    ("entry", "secret"),
    [
        (
            "CARTESIA_API_KEY=selected-env-key-87654321 # local note",
            "selected-env-key-87654321",
        ),
        (
            'CARTESIA_API_KEY="selected-env-key-87654321" # local note',
            "selected-env-key-87654321",
        ),
        (
            'CARTESIA_API_KEY="selected-env-key-87654321 # part of value"',
            "selected-env-key-87654321 # part of value",
        ),
        ("CARTESIA_API_KEY=selected-env-key-87654321#suffix", "selected-env-key-87654321#suffix"),
    ],
)
def test_guard_and_loader_share_dotenv_value_parsing(
    tmp_path, monkeypatch, entry: str, secret: str
) -> None:
    module = load_scrubber_module()
    env_file = tmp_path / "provider.env"
    env_file.write_text(entry + "\n")
    output = tmp_path / "fixture"
    output.mkdir()
    (output / "payload.json").write_text(secret)
    monkeypatch.setattr(module, "ENV_FILES", [env_file])
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="refusing to write"):
        module._guard(output, set())

    assert not output.exists()
    assert module.load_keys(env_file, ["CARTESIA_API_KEY"]) == {"CARTESIA_API_KEY": secret}


@pytest.mark.parametrize("provider", ["elevenlabs", "twilio"])
def test_failed_fixture_build_preserves_previous_output(monkeypatch, tmp_path, provider) -> None:
    module = load_scrubber_module()
    fixtures = tmp_path / "fixtures"
    previous = fixtures / provider
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_text("complete previous fixture")
    monkeypatch.setattr(module, "FIXTURES", fixtures)
    monkeypatch.setattr(module, "ENV_FILES", [])
    monkeypatch.setattr(
        sys, "argv", ["scrub_provider_capture.py", provider, "--env", str(tmp_path / "e")]
    )

    def fail_after_partial_output(out: Path | None = None) -> None:
        target = out or previous
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        (target / "partial.json").write_text("partial output")
        raise RuntimeError("simulated caller cache failure")

    if provider == "elevenlabs":
        monkeypatch.setenv("CARTESIA_API_KEY", "synthetic-cartesia-key-123456")
        monkeypatch.setenv("CARTESIA_VOICE_ID", "synthetic-cartesia-voice-123456")
        monkeypatch.setattr(module, "build_elevenlabs", fail_after_partial_output)
    else:
        monkeypatch.setattr(module, "build_twilio", fail_after_partial_output)

    with pytest.raises(RuntimeError, match="simulated caller cache failure"):
        module.main()

    assert (previous / "manifest.json").read_text() == "complete previous fixture"
    assert not (previous / "partial.json").exists()
    assert list(fixtures.iterdir()) == [previous]


@pytest.mark.parametrize(
    ("exception_type", "message"),
    [(OSError, "simulated install failure"), (KeyboardInterrupt, "simulated interruption")],
)
def test_failed_fixture_install_restores_previous_output(
    monkeypatch, tmp_path, exception_type, message
) -> None:
    module = load_scrubber_module()
    fixtures = tmp_path / "fixtures"
    previous = fixtures / "elevenlabs"
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_text("complete previous fixture")
    monkeypatch.setattr(module, "FIXTURES", fixtures)
    monkeypatch.setattr(module, "ENV_FILES", [])
    monkeypatch.setattr(
        sys, "argv", ["scrub_provider_capture.py", "elevenlabs", "--env", str(tmp_path / "e")]
    )
    monkeypatch.setenv("CARTESIA_API_KEY", "synthetic-cartesia-key-123456")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "synthetic-cartesia-voice-123456")

    def build(out: Path) -> tuple[int, int]:
        out.mkdir(parents=True)
        (out / "manifest.json").write_text("complete new fixture")
        return 1, 1

    real_replace = module.os.replace

    def fail_install(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == "fixture" and Path(destination) == previous:
            raise exception_type(message)
        real_replace(source, destination)

    monkeypatch.setattr(module, "build_elevenlabs", build)
    monkeypatch.setattr(module.os, "replace", fail_install)

    with pytest.raises(exception_type, match=message):
        module.main()

    assert (previous / "manifest.json").read_text() == "complete previous fixture"
    assert list(fixtures.iterdir()) == [previous]


def test_failed_concurrent_install_preserves_previous_backup(monkeypatch, tmp_path) -> None:
    module = load_scrubber_module()
    fixtures = tmp_path / "fixtures"
    previous = fixtures / "elevenlabs"
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_text("complete previous fixture")
    monkeypatch.setattr(module, "FIXTURES", fixtures)
    monkeypatch.setattr(module, "ENV_FILES", [])
    monkeypatch.setattr(
        sys, "argv", ["scrub_provider_capture.py", "elevenlabs", "--env", str(tmp_path / "e")]
    )
    monkeypatch.setenv("CARTESIA_API_KEY", "synthetic-cartesia-key-123456")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "synthetic-cartesia-voice-123456")

    def build(out: Path) -> tuple[int, int]:
        out.mkdir(parents=True)
        (out / "manifest.json").write_text("complete new fixture")
        return 1, 1

    real_replace = module.os.replace

    def race_install(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == "fixture" and Path(destination) == previous:
            previous.mkdir()
            (previous / "partial.json").write_text("concurrent writer")
            raise OSError("simulated concurrent install")
        real_replace(source, destination)

    monkeypatch.setattr(module, "build_elevenlabs", build)
    monkeypatch.setattr(module.os, "replace", race_install)

    with pytest.raises(OSError, match="simulated concurrent install") as raised:
        module.main()

    backups = [path for path in fixtures.iterdir() if path != previous]
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").read_text() == "complete previous fixture"
    assert any(str(backups[0]) in note for note in raised.value.__notes__)
