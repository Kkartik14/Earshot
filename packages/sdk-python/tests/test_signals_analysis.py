"""The recording analysis itself: speech runs, their
uncertainty, the noise floor, exact-zero runs and audiowaveform peaks.

Every test runs the real analysis (the vendored Silero ONNX model, libsndfile) on
the real fixture audio: the Pipecat 1.5 capture's two mono WAVs and the LiveKit 1.8
capture's stereo Ogg Opus. Expected values come from sources independent of this
code: an energy-VAD scan of the fixture, an exact-zero scan, and the standard
library's ``wave``.
"""

from __future__ import annotations

import array
import struct
import wave
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
soundfile = pytest.importorskip("soundfile")

from earshot.signals import analyze  # noqa: E402
from earshot.signals.audio import DecodeError, Resampler  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
LIVEKIT_RECORDING = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"

# An energy-VAD scan: a 10 ms RMS energy above -50 dBFS on the fixture, in seconds.
ENERGY_RUNS = {
    "user": [(1.29, 3.88), (11.77, 13.01), (13.52, 15.24), (22.5, 23.94), (24.28, 25.43)],
    "agent": [
        (6.96, 7.36), (8.03, 9.89), (16.75, 19.67), (20.12, 21.79), (26.92, 27.51),
        (28.13, 28.71),
    ],
}  # fmt: skip
# The exact-zero scan: runs of 200 ms or more, in seconds.
ZERO_RUNS = {
    "user": [(0.000, 1.000), (3.879, 11.505), (15.267, 22.267), (25.471, 32.486)],
    "agent": [(0.000, 6.306), (9.881, 16.566), (21.791, 26.726), (28.720, 32.486)],
}


def _seconds(runs: list[dict], start: str = "start_ms", end: str = "end_ms") -> list[tuple]:
    return [(run[start] / 1000, run[end] / 1000) for run in runs]


@pytest.fixture(scope="module")
def pipecat(tmp_path_factory) -> dict[str, tuple[dict, bytes]]:
    """Each fixture track analysed once, with its peaks file."""

    analysed = {}
    for speaker in ("user", "agent"):
        peaks = tmp_path_factory.mktemp(speaker) / "peaks.dat"
        result = analyze(PIPECAT_AUDIO / f"{speaker}.wav", speakers=[speaker], peaks_path=peaks)
        analysed[speaker] = (result, peaks.read_bytes())
    return analysed


@pytest.mark.parametrize("speaker", ["user", "agent"])
def test_refined_speech_runs_match_an_energy_vad_of_the_fixture(pipecat, speaker) -> None:
    [channel] = pipecat[speaker][0]["channels"]

    runs = _seconds(channel["runs"])

    assert channel["speaker"] == speaker
    assert len(runs) == len(ENERGY_RUNS[speaker])
    for (start, end), (energy_start, energy_end) in zip(runs, ENERGY_RUNS[speaker], strict=True):
        assert abs(start - energy_start) <= 0.02 and abs(end - energy_end) <= 0.05


@pytest.mark.parametrize("speaker", ["user", "agent"])
def test_each_boundary_carries_its_refinement_distance_as_sigma(pipecat, speaker) -> None:
    [channel] = pipecat[speaker][0]["channels"]

    for run in channel["runs"]:
        # Silero boundaries sit on its 32 ms frames; refined ones on 10 ms frames.
        assert run["vad_start_ms"] % 32 == 0 and run["vad_end_ms"] % 32 == 0
        assert run["start_ms"] % 10 == 0 and run["end_ms"] % 10 == 0
        for edge in ("start", "end"):
            moved = abs(run[f"vad_{edge}_ms"] - run[f"{edge}_ms"])
            assert run[f"{edge}_sigma_ms"] == max(10, moved)
            assert moved <= 100


@pytest.mark.parametrize("speaker", ["user", "agent"])
def test_exact_zero_runs_and_the_noise_floor_exclude_recorder_padding(pipecat, speaker) -> None:
    [channel] = pipecat[speaker][0]["channels"]

    long_runs = [run for run in channel["zero_runs"] if run["end_ms"] - run["start_ms"] >= 200]

    # The exact-zero scan truncated to whole milliseconds; the result rounds to the nearest.
    assert len(long_runs) == len(ZERO_RUNS[speaker])
    for (start, end), (scan_start, scan_end) in zip(
        _seconds(long_runs), ZERO_RUNS[speaker], strict=True
    ):
        assert abs(start - scan_start) < 0.0011 and abs(end - scan_end) < 0.0011
    assert all(run["end_ms"] - run["start_ms"] >= 50 for run in channel["zero_runs"])
    # Cartesia's floor is about -66 dBFS; padding would make it -inf.
    assert -70 <= channel["noise_floor_dbfs"] <= -60
    assert channel["threshold_dbfs"] == -50


def test_the_same_input_gives_identical_results(pipecat, tmp_path) -> None:
    again = analyze(PIPECAT_AUDIO / "user.wav", speakers=["user"], peaks_path=tmp_path / "p.dat")

    assert again == pipecat["user"][0]
    assert (tmp_path / "p.dat").read_bytes() == pipecat["user"][1]


def test_peaks_are_audiowaveform_dat_v2_8_bit_at_256_samples_per_pixel(pipecat) -> None:
    result, peaks = pipecat["user"]
    with wave.open(str(PIPECAT_AUDIO / "user.wav"), "rb") as source:
        samples = array.array("h", source.readframes(source.getnframes()))
    blocks = [samples[start : start + 256] for start in range(0, len(samples), 256)]

    version, flags, rate, per_pixel, length, channels = struct.unpack_from("<iIiiIi", peaks)
    body = struct.unpack_from(f"<{2 * length}b", peaks, 24)

    assert (version, flags, rate, per_pixel, length, channels) == (2, 1, 16000, 256, len(blocks), 1)
    assert len(peaks) == 24 + 2 * length
    assert body == tuple(value >> 8 for block in blocks for value in (min(block), max(block)))
    assert result["peaks"] == {
        "format": "audiowaveform-dat",
        "version": 2,
        "bits": 8,
        "sample_rate": 16000,
        "samples_per_pixel": 256,
        "channels": 1,
        "length": len(blocks),
    }


def test_a_stereo_ogg_opus_recording_is_analysed_per_channel(tmp_path) -> None:
    peaks = tmp_path / "peaks.dat"

    result = analyze(LIVEKIT_RECORDING, speakers=["user", "agent"], peaks_path=peaks)

    user, agent = result["channels"]
    assert (user["speaker"], agent["speaker"]) == ("user", "agent")
    assert result["source"]["sample_rate"] == 48000 and result["source"]["channels"] == 2
    assert abs(result["source"]["duration_ms"] - 34004) <= 1
    # The caller speaks three lines; the agent answers each (the real capture).
    assert len(user["runs"]) >= 3 and len(agent["runs"]) >= 3
    assert user["runs"][0]["start_ms"] < agent["runs"][0]["start_ms"]
    header = struct.unpack_from("<iIiiIi", peaks.read_bytes())
    assert header[2:] == (48000, 256, -(-1632181 // 256), 2)


def test_resampling_keeps_speech_band_level_and_rejects_aliases() -> None:
    import numpy

    rate = 48000
    time = numpy.arange(rate) / rate
    tone = (numpy.sin(2 * numpy.pi * 1000 * time) * 16000).astype(numpy.int16)
    alias = (numpy.sin(2 * numpy.pi * 12000 * time) * 16000).astype(numpy.int16)

    def resampled(signal):
        resampler = Resampler(rate, 16000)
        parts = [resampler.feed(signal[i : i + 7000]) for i in range(0, len(signal), 7000)]
        return numpy.concatenate([*parts, resampler.flush()])

    kept, rejected = resampled(tone), resampled(alias)

    assert len(kept) == len(rejected) == 16000
    middle = slice(1000, 15000)
    expected_rms = 16000 / 32768 / numpy.sqrt(2)
    assert numpy.sqrt(numpy.mean(kept[middle] ** 2)) == pytest.approx(expected_rms, rel=0.01)
    # 12 kHz lies above the new Nyquist (8 kHz): it must not fold back to 4 kHz.
    assert numpy.sqrt(numpy.mean(rejected[middle] ** 2)) < expected_rms / 100


def test_a_body_that_is_not_audio_is_a_decode_error(tmp_path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"RIFF\x00\x00\x00\x00WAVEnot really a wave file" * 10)

    with pytest.raises(DecodeError) as raised:
        analyze(broken, speakers=["user"], peaks_path=tmp_path / "p.dat")

    assert raised.value.code == "EARSHOT_ANALYSIS_DECODE_FAILED"


def test_8_khz_telephony_runs_silero_natively_on_256_sample_frames(pipecat, tmp_path) -> None:
    import numpy

    samples, rate = soundfile.read(PIPECAT_AUDIO / "user.wav", dtype="int16")
    resampler = Resampler(rate, 8000)
    narrow = numpy.concatenate([resampler.feed(samples), resampler.flush()])
    telephony = tmp_path / "user-8k.wav"
    soundfile.write(telephony, numpy.round(narrow * 32767).astype(numpy.int16), 8000)

    result = analyze(telephony, speakers=["user"], peaks_path=tmp_path / "p.dat")

    assert result["source"]["analysis_rate"] == 8000
    [channel] = result["channels"]
    wide = pipecat["user"][0]["channels"][0]["runs"]
    # Silero's 8 kHz model places edges less exactly than its 16 kHz one (up to about
    # 160 ms apart on this audio); the runs are the same speech.
    assert len(channel["runs"]) == len(wide)
    for run, reference in zip(channel["runs"], wide, strict=True):
        assert abs(run["start_ms"] - reference["start_ms"]) <= 200
        assert abs(run["end_ms"] - reference["end_ms"]) <= 200
        assert run["vad_start_ms"] % 32 == 0 and run["end_sigma_ms"] >= 10


def test_loading_the_model_turns_off_onnxruntime_telemetry_before_importing_it() -> None:
    """Some onnxruntime builds (1.31 on Linux) start a usage-telemetry client when
    imported. ``ORT_DISABLE_TELEMETRY`` stops it, and is read only at import."""

    import subprocess
    import sys

    probe = (
        "import os, sys\n"
        "os.environ.pop('ORT_DISABLE_TELEMETRY', None)\n"
        "from earshot.signals.vad import SileroModel\n"
        "assert 'onnxruntime' not in sys.modules\n"
        "SileroModel()\n"
        "print(os.environ.get('ORT_DISABLE_TELEMETRY'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )

    assert result.stdout.strip() == "1", result.stderr
