"""The real-call fixture cache follows its synthesis settings and contains valid audio."""

from __future__ import annotations

import hashlib
import importlib.util
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "pipecat_otlp" / "make_caller_audio.py"


def load_caller_audio_module():
    spec = importlib.util.spec_from_file_location("earshot_example_make_caller_audio", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_cache_reuses_audio_and_invalidates_model_and_voice(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    generated: list[str] = []
    monkeypatch.setattr(module, "_synthesize", lambda text: generated.append(text) or b"\x00\x00")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    monkeypatch.delenv("CARTESIA_MODEL", raising=False)
    path = tmp_path / "caller.wav"

    module.ensure_caller_wav(path)
    module.ensure_caller_wav(path)
    assert generated == list(module.CALLER_SCRIPT)

    monkeypatch.setenv("CARTESIA_MODEL", "sonic-3")
    module.ensure_caller_wav(path)
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-b")
    module.ensure_caller_wav(path)
    assert generated == list(module.CALLER_SCRIPT) * 3
    monkeypatch.setattr(module, "CARTESIA_VOICE_MODE", "name")
    module.ensure_caller_wav(path)
    assert generated == list(module.CALLER_SCRIPT) * 4

    with wave.open(str(path), "rb") as cached:
        assert cached.getnchannels() == 1
        assert cached.getsampwidth() == 2
        assert cached.getframerate() == module.SAMPLE_RATE
        assert cached.getnframes() > 0


def test_default_cache_path_is_namespaced_by_model_and_voice(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    generated: list[str] = []
    monkeypatch.setattr(module, "_synthesize", lambda text: generated.append(text) or b"\x00\x00")
    monkeypatch.setattr(module, "CALLER_WAV", tmp_path / "caller.wav")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    monkeypatch.delenv("CARTESIA_MODEL", raising=False)

    default_model = module.ensure_caller_wav()
    monkeypatch.setenv("CARTESIA_MODEL", "sonic-3")
    other_model = module.ensure_caller_wav()
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-b")
    other_voice = module.ensure_caller_wav()

    assert len({default_model, other_model, other_voice}) == 3
    assert all(path.exists() for path in (default_model, other_model, other_voice))
    assert generated == list(module.CALLER_SCRIPT) * 3


@pytest.mark.parametrize(
    "corruption",
    [
        "invalid_metadata",
        "wrong_digest",
        "wrong_audio_digest",
        "invalid_wav",
        "nonfinite_timeline",
        "boolean_timeline",
        "negative_timeline",
        "wrong_duration",
        "audio_content_mismatch",
    ],
)
def test_corrupt_or_stale_cache_is_regenerated(monkeypatch, tmp_path, corruption: str) -> None:
    module = load_caller_audio_module()
    generated: list[str] = []
    monkeypatch.setattr(module, "_synthesize", lambda text: generated.append(text) or b"\x00\x00")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"
    module.ensure_caller_wav(path)
    metadata_path = module.caller_wav_metadata_path(path)
    metadata = module.json.loads(metadata_path.read_text())
    if corruption == "invalid_metadata":
        metadata_path.write_text("not json")
    elif corruption == "wrong_digest":
        metadata["cache_sha256"] = "stale-settings"
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "wrong_audio_digest":
        metadata["wav_sha256"] = "stale-audio"
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "invalid_wav":
        path.write_bytes(b"not a WAV file")
    elif corruption == "nonfinite_timeline":
        metadata["lines"][0]["start_s"] = float("nan")
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "boolean_timeline":
        metadata["lines"][0]["duration_s"] = True
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "negative_timeline":
        metadata["lines"][0]["start_s"] = -1.0
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "wrong_duration":
        metadata["duration_s"] += 1.0
        metadata_path.write_text(module.json.dumps(metadata))
    else:
        audio = bytearray(path.read_bytes())
        audio[-1] ^= 1
        path.write_bytes(audio)
    generated.clear()

    module.ensure_caller_wav(path)

    assert generated == list(module.CALLER_SCRIPT)
    module.ensure_caller_wav(path)
    assert generated == list(module.CALLER_SCRIPT)


@pytest.mark.parametrize("invalid_pcm", [b"", b"\x00"])
def test_synthesis_rejects_empty_or_incomplete_pcm_before_writing(
    monkeypatch, tmp_path, invalid_pcm: bytes
) -> None:
    module = load_caller_audio_module()
    monkeypatch.setattr(module, "_synthesize", lambda _text: invalid_pcm)
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"

    with pytest.raises(ValueError, match="empty or incomplete 16-bit PCM"):
        module.ensure_caller_wav(path)

    assert not path.exists()
    assert not module.caller_wav_metadata_path(path).exists()


def test_failed_sidecar_replace_does_not_leave_stale_metadata(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    generated: list[str] = []
    monkeypatch.setattr(module, "_synthesize", lambda text: generated.append(text) or b"\x00\x00")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"
    module.ensure_caller_wav(path)

    monkeypatch.setenv("CARTESIA_MODEL", "sonic-3")
    replace = module.os.replace

    def fail_replace(source, target):
        if Path(target) == module.caller_wav_metadata_path(path):
            raise OSError("simulated metadata replacement failure")
        replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated metadata replacement failure"):
        module.ensure_caller_wav(path)

    assert not module.caller_wav_metadata_path(path).exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert generated == list(module.CALLER_SCRIPT) * 2


def test_load_returns_cached_audio_and_timeline_metadata(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    monkeypatch.setattr(module, "_synthesize", lambda _text: b"\x01\x00" * 4)
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"

    audio, metadata = module.load_caller_wav_and_metadata(path)

    assert audio == path.read_bytes()
    assert hashlib.sha256(audio).hexdigest() == metadata["wav_sha256"]
    assert metadata["lines"] == [
        {"text": text, "start_s": start, "duration_s": 0.00025}
        for text, start in zip(module.CALLER_SCRIPT, (1.0, 8.00025, 15.0005), strict=True)
    ]
    assert module._valid_cached_wav(
        path, module.caller_wav_metadata_path(path), module._cache_digest()
    )
