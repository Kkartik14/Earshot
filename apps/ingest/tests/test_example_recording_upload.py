"""The Pipecat example's stereo call recording, against a real Earshot server.

``examples/pipecat_otlp/call_recording.py`` is what ``run_call.py --upload`` uses:
it builds one stereo WAV from ``AudioBufferProcessor``'s two tracks (channel 0
user, channel 1 agent), creates its recording with ``earshot.client`` at the
call's first audio and uploads it after the run. Here it talks to the real app on
a real uvicorn server, with a real project API key, a real catalog and both blob
backends.
"""

from __future__ import annotations

import array
import hashlib
import io
import sys
import wave
from pathlib import Path

import pytest

from earshot.api import create_app
from earshot.client import EarshotClient
from otlp_support import serve
from store_support import catalog_rows, make_store

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "pipecat_otlp"
sys.path.insert(0, str(EXAMPLE))

from call_recording import create_call_recording, stereo_wav  # noqa: E402

pytestmark = pytest.mark.integration

STARTED_AT = 1_800_000_000_123_456_789
RATE = 16_000


def _pcm(seconds: float, value: int) -> bytes:
    return array.array("h", [value] * int(RATE * seconds)).tobytes()


def _channels(body: bytes) -> tuple[int, int, list[int], list[int]]:
    with wave.open(io.BytesIO(body), "rb") as handle:
        channels, rate = handle.getnchannels(), handle.getframerate()
        samples = array.array("h", handle.readframes(handle.getnframes()))
    return channels, rate, list(samples[0::2]), list(samples[1::2])


def test_the_two_tracks_become_one_stereo_wav_with_the_user_on_channel_0() -> None:
    user, agent = _pcm(1.0, 7), _pcm(0.5, -9)  # the agent's track ends first

    channels, rate, left, right = _channels(stereo_wav(user, agent, RATE))

    assert (channels, rate, len(left), len(right)) == (2, RATE, RATE, RATE)
    assert left == [7] * RATE
    assert right == [-9] * (RATE // 2) + [0] * (RATE // 2)  # padded with silence


def test_the_call_recording_is_created_at_first_audio_and_uploaded_after_the_call(
    tmp_path, blob_backend
) -> None:
    store = make_store(tmp_path)
    key = store.issue_api_key("default", label="example").credential
    path = tmp_path / "call.wav"
    path.write_bytes(stereo_wav(_pcm(1.25, 7), _pcm(0.75, 9), RATE))

    with (
        serve(create_app(store=store, blob_store=blob_backend.store)) as port,
        EarshotClient(f"http://127.0.0.1:{port}", api_key=key) as client,
    ):
        created = create_call_recording(client, "conv-1", STARTED_AT)
        retried = create_call_recording(client, "conv-1", STARTED_AT)
        uploaded = client.upload_recording(created, path)
        call = client.get_call(client.resolve_call("conv-1")["call_id"])

    assert retried == created
    assert {
        name: created[name]
        for name in ("recording_id", "state", "speaker", "channel_speakers", "content_type")
    } == {
        "recording_id": "conv-1.call",
        "state": "pending",
        "speaker": "mixed",
        "channel_speakers": ["user", "agent"],
        "content_type": "audio/wav",
    }
    assert (uploaded["state"], uploaded["duration_ms"], uploaded["sha256"]) == (
        "stored",
        1250,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    assert uploaded["started_at_unix_nano"] == str(STARTED_AT)
    assert [item["recording_id"] for item in call["recordings"]] == ["conv-1.call"]
    assert catalog_rows(tmp_path, "SELECT recording_count FROM calls") == [(1,)]
    assert len(blob_backend.objects("recordings/")) == 1
