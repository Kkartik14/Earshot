"""The Pipecat example's call recording: one stereo WAV, uploaded with ``earshot.client``.

``run_call.py --upload`` uses it in two steps, so the call stays held open in
Earshot until its audio arrives:

1. at the call's first audio, ``create_call_recording`` creates one pending
   ``mixed`` recording whose channels are ``["user", "agent"]``, keyed by the same
   session key the spans carry;
2. after the run and the tracer's ``force_flush``, ``client.upload_recording``
   streams ``stereo_wav``'s file to it.

One stereo file keeps both speakers on one media clock, so a player plays one
element and both channels stay in step. Its recording ID comes from the
conversation ID, so a retried create or upload is idempotent.
"""

from __future__ import annotations

import array
import io
import wave
from typing import Any

from earshot.client import EarshotClient, recording_id_for

CHANNEL_SPEAKERS = ("user", "agent")
CONTENT_TYPE = "audio/wav"
RECORDING_NAME = "call"


def stereo_wav(user_pcm: bytes, agent_pcm: bytes, sample_rate: int) -> bytes:
    """A PCM16 stereo WAV of two mono PCM16 tracks: the user on channel 0, the agent on
    channel 1, the shorter one padded with silence (``AudioBufferProcessor``'s tracks
    both start at the input's first audio frame)."""

    # Samples are moved as 2-byte units, never read, so the byte order is kept.
    size = max(len(user_pcm), len(agent_pcm))
    frames = array.array("h", bytes(2 * size))
    frames[0::2] = array.array("h", user_pcm.ljust(size, b"\0"))
    frames[1::2] = array.array("h", agent_pcm.ljust(size, b"\0"))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(len(CHANNEL_SPEAKERS))
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames.tobytes())
    return buffer.getvalue()


def create_call_recording(
    client: EarshotClient, conversation_id: str, started_at_unix_nano: int
) -> dict[str, Any]:
    """Create (or find again) the call's pending stereo recording."""

    return client.create_recording(
        session_key=conversation_id,
        speaker="mixed",
        channel_speakers=CHANNEL_SPEAKERS,
        content_type=CONTENT_TYPE,
        started_at_unix_nano=started_at_unix_nano,
        recording_id=recording_id_for(conversation_id, RECORDING_NAME),
    )
