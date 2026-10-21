"""A recording's transcript over real HTTP: ``POST /v1/recordings/{id}/transcript`` (FR-5).

Every test runs the real app on a real uvicorn server over a real catalog (SQLite,
or Postgres under ``--db postgresql``) and a local blob store. Transcripts are read
back through the public call and span routes, so the spans are checked where a
client sees them: on the recording's call.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from audio_support import wav
from earshot.api import create_app
from earshot.blobstore import LocalBlobStore
from earshot.clock import ManualClock
from earshot.connectors import evidence
from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingStore
from earshot.transcripts import MAX_TRANSCRIPT_BYTES, parse_transcript, span_id, trace_id
from otlp_support import Reply, request, serve
from store_support import make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
MILLISECOND = 1_000_000
START = 1_800_000_000 * SECOND
STARTED_AT = START - 60 * SECOND
VTT = b"""WEBVTT

00:01.250 --> 00:03.000
<v user>I'd like to book a table.

00:03.500 --> 00:05.000
<v agent>For how many people?
"""
TEXT = b"user: I'd like to book a table.\nagent: For how many people?\n"


@dataclass
class World:
    port: int
    store: IncidentStore
    clock: ManualClock
    blobs: LocalBlobStore


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    blobs = LocalBlobStore(tmp_path / "blobs")
    with serve(create_app(store=store, blob_store=blobs)) as port:
        yield World(port, store, clock, blobs)


def create_recording(
    world: World, recording_id: str, *, speaker: str = "mixed", credential: str | None = None
) -> dict:
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    body = {
        "recording_id": recording_id,
        "session_key": f"uploaded-{recording_id}",
        "speaker": speaker,
        "content_type": "audio/wav",
        "started_at_unix_nano": str(STARTED_AT),
    }
    reply = request(
        world.port, "POST", "/v1/recordings", json.dumps(body).encode(), headers=headers
    )
    assert reply.status == 201, (reply.status, reply.body[:300])
    return reply.json()


def post_transcript(
    world: World,
    recording_id: str,
    body: bytes,
    *,
    content_type: str = "text/vtt",
    credential: str | None = None,
) -> Reply:
    headers = {"Content-Type": content_type}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return request(
        world.port, "POST", f"/v1/recordings/{recording_id}/transcript", body, headers=headers
    )


def call(world: World, call_id: str) -> dict:
    reply = request(world.port, "GET", f"/v1/calls/{call_id}")
    assert reply.status == 200, (reply.status, reply.body[:300])
    return reply.json()


def line_span(world: World, call_id: str, recording_id: str, index: int) -> Reply:
    trace = trace_id(call_id, recording_id).hex()
    span = span_id(evidence.TRANSCRIPT_LINE, index).hex()
    return request(world.port, "GET", f"/v1/calls/{call_id}/spans/{trace}/{span}")


def attributes(reply: Reply) -> dict[str, object]:
    return {item["key"]: item["value"] for item in reply.json()["attributes"]}


# --- what a transcript becomes ---------------------------------------------------------------


def test_a_webvtt_transcript_becomes_measured_lines_on_the_recordings_call(world) -> None:
    recording = create_recording(world, "rec-1")

    reply = post_transcript(world, "rec-1", VTT)

    assert reply.status == 201, reply.body[:300]
    assert reply.json() == {
        "recording_id": "rec-1",
        "call_id": recording["call_id"],
        "format": "vtt",
        "timing": "measured",
        "line_count": 2,
        "sha256": parse_transcript(VTT, "vtt", default_speaker=None).digest(),
    }
    assert call(world, recording["call_id"])["span_count"] == 3
    # The span route serves only spans of the call's own traces: the line is the call's.
    second = line_span(world, recording["call_id"], "rec-1", 1)
    assert second.status == 200, second.body[:300]
    assert second.json()["scope_name"] == "earshot.upload"
    assert second.json()["name"] == evidence.TRANSCRIPT_LINE
    # Cue times are offsets from the recording's started_at.
    assert (int(second.json()["start_unix_nano"]), int(second.json()["end_unix_nano"])) == (
        STARTED_AT + 3_500 * MILLISECOND,
        STARTED_AT + 5_000 * MILLISECOND,
    )
    assert attributes(second) == {
        evidence.TRANSCRIPT_SPEAKER: "agent",
        evidence.TRANSCRIPT_TEXT: "For how many people?",
        evidence.TRANSCRIPT_TIMING: "measured",
        evidence.TRANSCRIPT_INDEX: 1,
    }


def test_a_plain_text_transcript_is_order_only_at_the_recordings_start(world) -> None:
    recording = create_recording(world, "rec-1")

    reply = post_transcript(world, "rec-1", TEXT, content_type="text/plain; charset=utf-8")

    assert reply.status == 201, reply.body[:300]
    assert (reply.json()["format"], reply.json()["timing"]) == ("text", "order_only")
    first = line_span(world, recording["call_id"], "rec-1", 0)
    assert (int(first.json()["start_unix_nano"]), int(first.json()["end_unix_nano"])) == (
        STARTED_AT,
        STARTED_AT,
    )
    assert attributes(first)[evidence.TRANSCRIPT_TIMING] == "order_only"
    assert attributes(first)[evidence.TRANSCRIPT_TEXT] == "I'd like to book a table."


def test_a_one_speaker_recordings_unlabelled_lines_are_its_speakers(world) -> None:
    user = create_recording(world, "rec-user", speaker="user")
    create_recording(world, "rec-mixed")
    unlabelled = b"Hello there.\nI'd like a table.\n"

    taken = post_transcript(world, "rec-user", unlabelled, content_type="text/plain")
    refused = post_transcript(world, "rec-mixed", unlabelled, content_type="text/plain")

    assert taken.status == 201, taken.body[:300]
    assert (
        attributes(line_span(world, user["call_id"], "rec-user", 1))[evidence.TRANSCRIPT_SPEAKER]
        == "user"
    )
    assert refused.status == 422
    assert refused.problem_code() == "EARSHOT_TRANSCRIPT_INVALID"
    assert refused.json()["error"]["message"].startswith("line 1: start the line with")


# --- the same transcript, another transcript (re-uploads) ------------------------------------


def test_the_same_transcript_again_is_200_and_writes_nothing(world) -> None:
    recording = create_recording(world, "rec-1")
    first = post_transcript(world, "rec-1", VTT)
    before = call(world, recording["call_id"])
    world.clock.advance(SECOND)

    # The same lines, saved again with other line endings: the same transcript.
    again = post_transcript(world, "rec-1", VTT.replace(b"\n", b"\r\n"))

    assert (first.status, again.status) == (201, 200)
    assert again.json() == first.json()
    after = call(world, recording["call_id"])
    assert after["span_count"] == before["span_count"] == 3
    assert after["last_evidence_unix_nano"] == before["last_evidence_unix_nano"]


@pytest.mark.parametrize(
    "other",
    [
        VTT.replace(b"For how many people?", b"For how many guests?"),
        VTT + b"\n00:06.000 --> 00:07.000\n<v user>Four.\n",
        TEXT,
    ],
    ids=["changed", "longer", "plain-text"],
)
def test_a_different_transcript_is_409_and_never_replaces_the_first(world, other) -> None:
    recording = create_recording(world, "rec-1")
    assert post_transcript(world, "rec-1", VTT).status == 201
    content_type = "text/plain" if other is TEXT else "text/vtt"

    reply = post_transcript(world, "rec-1", other, content_type=content_type)

    assert reply.status == 409
    assert reply.problem_code() == "EARSHOT_TRANSCRIPT_EXISTS"
    assert call(world, recording["call_id"])["span_count"] == 3
    kept = attributes(line_span(world, recording["call_id"], "rec-1", 1))
    assert kept[evidence.TRANSCRIPT_TEXT] == "For how many people?"
    assert line_span(world, recording["call_id"], "rec-1", 2).status == 404


def test_two_different_transcripts_at_once_store_exactly_one(world) -> None:
    recording = create_recording(world, "rec-1")
    bodies = [VTT, VTT.replace(b"table", b"booth")]
    replies: list[Reply] = []
    threads = [
        threading.Thread(
            target=lambda body=body: replies.append(post_transcript(world, "rec-1", body))
        )
        for body in bodies
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(reply.status for reply in replies) == [201, 409]
    assert call(world, recording["call_id"])["span_count"] == 3


# --- presets, projects and the call's lifecycle ----------------------------------------------


def test_a_metadata_only_project_is_422_and_stores_nothing(world) -> None:
    recording = create_recording(world, "rec-1")
    world.store.set_capture_preset("default", "metadata_only")

    reply = post_transcript(world, "rec-1", VTT)

    assert reply.status == 422
    assert reply.problem_code() == "EARSHOT_PRESET_FORBIDS_TRANSCRIPT"
    assert call(world, recording["call_id"])["span_count"] == 0


def test_another_projects_recording_is_not_found(world) -> None:
    create_recording(world, "rec-1")
    world.store.create_project("other", display_name="Other")
    other = world.store.issue_api_key("other", label="other").credential

    reply = post_transcript(world, "rec-1", VTT, credential=other)

    assert reply.status == 404
    assert reply.problem_code() == "EARSHOT_RECORDING_NOT_FOUND"


def revisions_once_closed(world: World, call_id: str, count: int) -> list[dict]:
    """The call's revisions once it has ``count``: closed by this test's closer, or by
    the server's own (both run on the store's manual clock)."""

    calls = CallStore(world.store)
    recordings = RecordingStore(world.store, calls=calls, blobs=world.blobs)
    closer = Closer(world.store, calls=calls, recordings=recordings)
    deadline = time.monotonic() + 30
    while len(revisions := call(world, call_id)["revisions"]) < count:
        assert time.monotonic() < deadline, revisions
        closer.close_next()
        time.sleep(0.05)
    return revisions


def test_a_transcript_of_a_closed_call_is_late_evidence_for_its_next_revision(world) -> None:
    recording = create_recording(world, "rec-1")
    uploaded = request(
        world.port,
        "PUT",
        recording["upload_path"],
        wav(seconds=0.5),
        headers={"Content-Type": "audio/wav"},
    )
    assert uploaded.status == 200, uploaded.body[:300]
    world.clock.advance(CallTimings().long_quiet_nano + SECOND)
    first = revisions_once_closed(world, recording["call_id"], 1)
    assert [(item["span_count"], item["recording_count"]) for item in first] == [(0, 1)]

    assert post_transcript(world, "rec-1", VTT).status == 201
    world.clock.advance(CallTimings().long_quiet_nano)

    revisions = revisions_once_closed(world, recording["call_id"], 2)
    assert [item["span_count"] for item in revisions] == [0, 3]


# --- refusals ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recording_id", "body", "content_type", "status", "code"),
    [
        ("nope", VTT, "text/vtt", 404, "EARSHOT_RECORDING_NOT_FOUND"),
        ("..bad", VTT, "text/vtt", 404, "EARSHOT_RECORDING_NOT_FOUND"),
        ("rec-1", VTT, "application/json", 415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE"),
        ("rec-1", VTT, "text/vtt; charset=latin-1", 415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE"),
        ("rec-1", b"", "text/vtt", 400, "EARSHOT_EMPTY_BODY"),
        (
            "rec-1",
            b"WEBVTT\n\n" + b"x" * MAX_TRANSCRIPT_BYTES,
            "text/vtt",
            413,
            "EARSHOT_BODY_TOO_LARGE",
        ),
        ("rec-1", b"user: caf\xe9\n", "text/plain", 422, "EARSHOT_TRANSCRIPT_INVALID"),
        (
            "rec-1",
            b"WEBVTT\n\n00:02.000 --> 00:01.000\n<v user>hi\n",
            "text/vtt",
            422,
            "EARSHOT_TRANSCRIPT_INVALID",
        ),
        (
            "rec-1",
            b"WEBVTT\n\n9999999999:00:00.000 --> 9999999999:00:01.000\n<v user>hi\n",
            "text/vtt",
            422,
            "EARSHOT_TRANSCRIPT_INVALID",
        ),
    ],
    ids=[
        "unknown-recording",
        "invalid-id",
        "json",
        "latin-1",
        "empty",
        "over-the-cap",
        "not-utf-8",
        "cue-ends-before-it-starts",
        "past-the-latest-time",
    ],
)
def test_a_refused_transcript_stores_nothing(
    world, recording_id, body, content_type, status, code
) -> None:
    recording = create_recording(world, "rec-1")

    reply = post_transcript(world, recording_id, body, content_type=content_type)

    assert (reply.status, reply.problem_code()) == (status, code), reply.body[:300]
    assert call(world, recording["call_id"])["span_count"] == 0
    # Errors name the line and the rule, never the transcript's text.
    assert b"caf" not in reply.body
