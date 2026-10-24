"""FR-12's provider-free replay e2e: the README quick start, on a real server.

``earshot serve`` runs with its default configuration (SQLite and local disk in
its data directory, anonymous on loopback). Deterministic, synthetic Pipecat-
shaped requests are sent in seven OTLP/HTTP batches. Two-second silent WAVs go
through the recordings API: both recordings are created before the spans and
uploaded afterward. The resulting call must pass the same done-when checks as
the real-provider e2e (``call_done_when``), and its transcript must match the
synthetic caller and agent lines.
"""

from __future__ import annotations

import pytest

from call_done_when import Earshot, SentCall, assert_done_when
from server_process import ServerProcess
from synthetic_otlp import (
    SYNTHETIC_CALL_START,
    SYNTHETIC_LINES,
    SYNTHETIC_SESSION_KEY,
    synthetic_recording_bytes,
    synthetic_requests,
)

pytestmark = [pytest.mark.e2e, pytest.mark.sqlite_only, pytest.mark.slow]

TRANSCRIPT = SYNTHETIC_LINES


def test_the_quick_start_turns_synthetic_requests_into_a_closed_call_with_its_audio(
    tmp_path,
) -> None:
    requests = synthetic_requests()
    sent = SentCall(
        session_key=SYNTHETIC_SESSION_KEY,
        exported_span_count=sum(
            len(scope_spans.spans)
            for request in requests
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
        ),
        started_at_unix_nano=SYNTHETIC_CALL_START,
        tracks={speaker: synthetic_recording_bytes(speaker) for speaker in ("user", "agent")},
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
        # Replay each synthetic export batch in arrival order.
        for request in requests:
            reply = earshot.call(
                "POST",
                "/v1/traces",
                request.SerializeToString(),
                {"Content-Type": "application/x-protobuf"},
            )
            assert (reply.status, reply.body) == (200, b"")
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
