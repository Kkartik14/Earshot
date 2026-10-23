"""FR-12's provider-free replay e2e: the README quick start, on a real server.

``earshot serve`` runs with its default configuration (SQLite and local disk in
its data directory, anonymous on loopback). The real Pipecat 1.5 capture
(``fixtures/captured/pipecat-otlp-1.5``) is replayed as the exporter sent it:
its requests in arrival order, as OTLP/HTTP POSTs with the exporter's content
type. Its two real tracks go through the recordings API in the example's run
order: both are created at the call's first audio (so the call is held), and
uploaded after the spans. Then the call must pass the same done-when checks as
the real-provider e2e (``call_done_when``), and its transcript must be the
fixture's known caller script and the agent's replies.
"""

from __future__ import annotations

import pytest

from call_done_when import Earshot, SentCall, assert_done_when
from captured_otlp import FIXTURE, captured_manifest
from server_process import ServerProcess

pytestmark = [pytest.mark.e2e, pytest.mark.sqlite_only, pytest.mark.slow]

# Deepgram's transcript of the fixture's caller script, and the agent's replies
# (``llm.output``, exactly as the model wrote them), in the order they were spoken.
TRANSCRIPT = [
    ("user", "Hi. I would like to book a table for two people tomorrow evening."),
    ("agent", "Sure, what time would you like to come in tomorrow evening?"),
    ("user", "Seven thirty works for us."),
    ("user", "Could we sit somewhere quiet near the window?"),
    (
        "agent",
        "Sure, we\u2019ve booked a quiet window seat for you at 7:30. "
        "We look forward to welcoming you tomorrow.",
    ),
    ("user", "Perfect. That is everything I needed."),
    ("user", "Thank you very much. Goodbye."),
    ("agent", "You're welcome! See you tomorrow."),
]


def test_the_quick_start_turns_the_replayed_capture_into_a_closed_call_with_its_audio(
    tmp_path,
) -> None:
    manifest = captured_manifest()
    audio = manifest["audio"]
    sent = SentCall(
        session_key=manifest["provenance"]["conversation_id"],
        exported_span_count=manifest["provenance"]["exported_span_count_tee"],
        started_at_unix_nano=int(audio["started_at_unix_nano"]),
        tracks={
            speaker: (FIXTURE / audio[speaker]["file"]).read_bytes()
            for speaker in ("user", "agent")
        },
    )
    data_dir = tmp_path / "data"
    server = ServerProcess.start(data_dir, tmp_path / "serve.log", entry_point="serve")
    try:
        earshot = Earshot(
            port=server.port,
            project_id="default",
            catalog=data_dir,
            read_blob=lambda key: (data_dir / "blobs" / key).read_bytes(),
        )
        recording_ids = {speaker: f"{sent.session_key}.{speaker}" for speaker in sent.tracks}
        # At the call's first audio: create both recordings (README step 3).
        for speaker, recording_id in recording_ids.items():
            created = earshot.json(
                "POST",
                "/v1/recordings",
                {
                    "recording_id": recording_id,
                    "session_key": sent.session_key,
                    "speaker": speaker,
                    "content_type": "audio/wav",
                    "started_at_unix_nano": str(sent.started_at_unix_nano),
                },
            )
            assert created["state"] == "pending"
        # The exporter's requests, in arrival order, as it sent them.
        for entry in manifest["requests"]:
            reply = earshot.call(
                "POST",
                "/v1/traces",
                (FIXTURE / entry["file"]).read_bytes(),
                {"Content-Type": entry["wire"]["content_type"]},
            )
            assert (reply.status, reply.body) == (200, b""), entry["file"]
        # After the call: upload both tracks.
        for speaker, recording_id in recording_ids.items():
            reply = earshot.call(
                "PUT",
                f"/v1/recordings/{recording_id}/content",
                sent.tracks[speaker],
                {"Content-Type": "audio/wav"},
            )
            assert reply.status == 200, reply.body

        detail = assert_done_when(earshot, sent).detail
    finally:
        server.stop()

    assert [(line["speaker"], line["text"]) for line in detail["transcript"]] == TRANSCRIPT
    assert detail["transcript_coverage"] == "available"
