"""The real-call fixture cache follows its synthesis settings and contains valid audio."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
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


def timeline_checksum(lines: object) -> str:
    encoded = json.dumps(lines, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_generator_imports_without_posix_fcntl(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", None)

    load_caller_audio_module()


def test_cartesia_request_matches_the_supported_pipecat_voice_shape(monkeypatch) -> None:
    module = load_caller_audio_module()
    monkeypatch.setenv("CARTESIA_API_KEY", "test-key")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "test-voice")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b"\x00\x00"

    def urlopen(request, *, timeout):
        assert timeout == 60
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

    assert module._synthesize("Hello") == b"\x00\x00"
    assert requests == [
        {
            "model_id": module.DEFAULT_CARTESIA_MODEL,
            "transcript": "Hello",
            "voice": {"mode": "id", "id": "test-voice"},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": module.SAMPLE_RATE,
            },
            "language": "en",
        }
    ]


def test_elevenlabs_capture_uses_the_shared_digest_cache(monkeypatch) -> None:
    capture_path = ROOT / "scripts" / "capture_elevenlabs.py"
    spec = importlib.util.spec_from_file_location("earshot_capture_elevenlabs", capture_path)
    assert spec is not None and spec.loader is not None
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)

    pcm = b"\x01\x00" * 6400
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(pcm)
    cached = buffer.getvalue()
    metadata = {"lines": [{"start_s": 0.0, "duration_s": 0.2}]}
    loads = []
    monkeypatch.setattr(
        capture,
        "load_caller_wav_and_metadata",
        lambda: loads.append(True) or (cached, metadata),
    )

    assert capture._caller_lines() == [pcm[: int((0.2 + 0.15) * 16_000) * 2]]
    assert loads == [True]


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
        "wrong_timeline_checksum",
        "missing_timeline_checksum",
        "invalid_wav",
        "nonfinite_timeline",
        "boolean_timeline",
        "negative_timeline",
        "out_of_range_timeline",
        "misordered_timeline",
        "overlapping_timeline",
        "shifted_in_range_timeline",
        "wrong_speech_duration",
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
    elif corruption == "wrong_timeline_checksum":
        metadata["timeline_sha256"] = "stale-timeline"
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "missing_timeline_checksum":
        metadata.pop("timeline_sha256")
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "invalid_wav":
        path.write_bytes(b"not a WAV file")
    elif corruption == "nonfinite_timeline":
        metadata["lines"][0]["start_s"] = float("nan")
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "boolean_timeline":
        metadata["lines"][0]["duration_s"] = True
        metadata["timeline_sha256"] = timeline_checksum(metadata["lines"])
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "negative_timeline":
        metadata["lines"][0]["start_s"] = -1.0
        metadata["timeline_sha256"] = timeline_checksum(metadata["lines"])
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "out_of_range_timeline":
        metadata["lines"][0]["start_s"] = metadata["duration_s"] + 1.0
        metadata["timeline_sha256"] = timeline_checksum(metadata["lines"])
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "misordered_timeline":
        metadata["lines"][0]["start_s"] = metadata["lines"][1]["start_s"] + 0.1
        metadata["timeline_sha256"] = timeline_checksum(metadata["lines"])
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "overlapping_timeline":
        metadata["lines"][1]["start_s"] = (
            metadata["lines"][0]["start_s"] + metadata["lines"][0]["duration_s"] / 2
        )
        metadata["timeline_sha256"] = timeline_checksum(metadata["lines"])
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "shifted_in_range_timeline":
        metadata["lines"][0]["start_s"] = 0.2
        metadata_path.write_text(module.json.dumps(metadata))
    elif corruption == "wrong_speech_duration":
        metadata["lines"][0]["duration_s"] /= 2
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
    assert sorted(item.name for item in tmp_path.iterdir()) == ["caller.wav"]
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
    assert metadata["timeline_sha256"] == timeline_checksum(metadata["lines"])
    assert module._valid_cached_wav(
        path, module.caller_wav_metadata_path(path), module._cache_digest()
    )


def test_load_rejects_a_cache_pair_changed_after_validation(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    monkeypatch.setattr(module, "_synthesize", lambda _text: b"\x01\x00" * 4)
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"
    ensure = module._ensure_caller_wav

    def replace_audio_after_validation(wav_path, metadata_path, digest, force):
        result = ensure(wav_path, metadata_path, digest, force)
        audio = bytearray(wav_path.read_bytes())
        audio[-1] ^= 1
        wav_path.write_bytes(audio)
        return result

    monkeypatch.setattr(module, "_ensure_caller_wav", replace_audio_after_validation)

    with pytest.raises(RuntimeError, match="cache changed while it was being read"):
        module.load_caller_wav_and_metadata(path)


def test_load_rejects_timeline_changed_after_validation(monkeypatch, tmp_path) -> None:
    module = load_caller_audio_module()
    monkeypatch.setattr(module, "_synthesize", lambda _text: b"\x01\x00" * 4)
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-a")
    path = tmp_path / "caller.wav"
    ensure = module._ensure_caller_wav

    def replace_timeline_after_validation(wav_path, metadata_path, digest, force):
        result = ensure(wav_path, metadata_path, digest, force)
        metadata = module.json.loads(metadata_path.read_text())
        metadata["lines"][0]["start_s"] = 0.2
        metadata_path.write_text(module.json.dumps(metadata))
        return result

    monkeypatch.setattr(module, "_ensure_caller_wav", replace_timeline_after_validation)

    with pytest.raises(RuntimeError, match="cache changed while it was being read"):
        module.load_caller_wav_and_metadata(path)
