"""The recordings API over real HTTP, on both blob backends.

Every test runs the real app on a real uvicorn server against a real catalog
(SQLite, or Postgres under ``--db postgresql``) and a real blob store: local disk
(``[local]``) and MinIO (``[s3]``). Audio bodies are real WAV files written by the
standard ``wave`` module, real FLAC from PyAV's encoder, and the real LiveKit 1.8
stereo Ogg Opus recording.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import http.client
import io
import json
import select
import socket
import struct
import threading
import time
import uuid
import wave
from collections.abc import Iterator

import pytest

from audio_support import LIVEKIT_RECORDING, encoded
from earshot.api import ApiConfig, create_app
from earshot.clock import ManualClock
from earshot.recordings import MAX_RANGE_HEADER_LENGTH
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_recordings import RecordingManifest, RecordingStore
from otlp_support import Reply, keyed_request, post_otlp, request, serve
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
STARTED_AT = str(1_800_000_000 * SECOND)
JSON_HEADERS = {"Content-Type": "application/json"}


def wav(*, channels: int = 1, seconds: float = 1.5, rate: int = 16_000, tone: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = int(rate * seconds)
        handle.writeframes(bytes([tone, 0]) * frames * channels)
    return buffer.getvalue()


def content_digest(body: bytes) -> str:
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def post_recording(port: int, *, credential: str | None = None, **fields) -> Reply:
    body = {
        "session_key": "call-1",
        "speaker": "user",
        "content_type": "audio/wav",
        "started_at_unix_nano": STARTED_AT,
        **fields,
    }
    headers = dict(JSON_HEADERS)
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return request(port, "POST", "/v1/recordings", json.dumps(body).encode(), headers=headers)


def put_content(
    port: int,
    recording_id: str,
    body: bytes,
    *,
    content_type: str = "audio/wav",
    headers: dict[str, str] | None = None,
) -> Reply:
    sent = {"Content-Type": content_type, **(headers or {})}
    return request(port, "PUT", f"/v1/recordings/{recording_id}/content", body, headers=sent)


@pytest.fixture
def server(tmp_path, blob_backend) -> Iterator[tuple[int, object]]:
    store = make_store(tmp_path)
    app = create_app(store=store, blob_store=blob_backend.store)
    with serve(app) as port:
        yield port, store


def _journal(tmp_path) -> list[tuple]:
    # Upload attempts' keys; the server's analysis also journals its outputs briefly.
    return catalog_rows(
        tmp_path, "SELECT blob_key FROM pending_blob_deletions WHERE blob_key LIKE 'recordings/%'"
    )


def _wait_for_empty_journal(tmp_path) -> None:
    deadline = time.monotonic() + 10
    while _journal(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)


# --- the lifecycle ----------------------------------------------------------------------------


def test_a_mono_wav_is_created_uploaded_and_read_back(tmp_path, blob_backend, server) -> None:
    port, _ = server
    body = wav(seconds=1.5)

    created = post_recording(port, recording_id="call-1.user", label="caller")
    metadata = request(port, "GET", "/v1/recordings/call-1.user")
    early = request(port, "GET", "/v1/recordings/call-1.user/content")
    uploaded = put_content(
        port, "call-1.user", body, headers={"Content-Digest": content_digest(body)}
    )
    whole = request(port, "GET", "/v1/recordings/call-1.user/content")

    assert created.status == 201, created.body
    assert {key: created.json()[key] for key in ("recording_id", "state", "upload_path")} == {
        "recording_id": "call-1.user",
        "state": "pending",
        "upload_path": "/v1/recordings/call-1.user/content",
    }
    call_id = created.json()["call_id"]
    assert catalog_rows(tmp_path, "SELECT key_source FROM calls WHERE call_id = ?", (call_id,)) == [
        ("recording",)
    ]
    assert metadata.status == 200 and metadata.json()["state"] == "pending"
    assert (early.status, early.problem_code()) == (409, "EARSHOT_RECORDING_NOT_STORED")
    assert uploaded.status == 200, uploaded.body
    stored = uploaded.json()
    assert {
        key: stored[key]
        for key in ("state", "size", "sha256", "duration_ms", "label", "started_at_unix_nano")
    } == {
        "state": "stored",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "duration_ms": 1500,
        "label": "caller",
        "started_at_unix_nano": STARTED_AT,
    }
    assert whole.status == 200 and whole.body == body
    assert whole.headers["content-type"] == "audio/wav"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-length"] == str(len(body))
    assert catalog_rows(
        tmp_path, "SELECT recording_count, dirty FROM calls WHERE call_id = ?", (call_id,)
    ) == [(1, 1)]
    (key,) = blob_backend.objects("recordings/")
    prefix, _, attempt = key.rpartition("/")
    assert prefix == "recordings/default" and uuid.UUID(attempt)
    assert blob_backend.leftovers() == [] and _journal(tmp_path) == []


def test_ranges_are_answered_from_the_blob_store_with_206_and_416(blob_backend, server) -> None:
    port, _ = server
    body = wav(seconds=0.5)
    post_recording(port, recording_id="r1")
    put_content(port, "r1", body)

    def ranged(value: str) -> Reply:
        return request(port, "GET", "/v1/recordings/r1/content", headers={"Range": value})

    middle, suffix, open_ended, several, past = (
        ranged("bytes=10-19"),
        ranged("bytes=-4"),
        ranged(f"bytes={len(body) - 3}-"),
        ranged("bytes=0-1,5-6"),
        ranged(f"bytes={len(body)}-"),
    )
    # Positions past any object: never a 500.
    huge_end, huge_start = ranged("bytes=0-" + "9" * 200), ranged("bytes=" + "9" * 200 + "-")
    too_long = ranged("x" * (MAX_RANGE_HEADER_LENGTH + 1))

    assert (middle.status, middle.body) == (206, body[10:20])
    assert middle.headers["content-range"] == f"bytes 10-19/{len(body)}"
    assert middle.headers["content-length"] == "10"
    assert (suffix.status, suffix.body) == (206, body[-4:])
    assert (open_ended.status, open_ended.body) == (206, body[-3:])
    assert (several.status, several.body) == (200, body)
    assert past.status == 416
    assert past.headers["content-range"] == f"bytes */{len(body)}"
    assert past.problem_code() == "EARSHOT_RANGE_NOT_SATISFIABLE"
    assert (huge_end.status, huge_end.body) == (206, body)
    assert (huge_start.status, huge_start.problem_code()) == (416, "EARSHOT_RANGE_NOT_SATISFIABLE")
    assert too_long.status == 416
    assert too_long.problem_code() == "EARSHOT_RANGE_NOT_SATISFIABLE"
    assert too_long.headers["content-range"] == f"bytes */{len(body)}"


def test_create_is_idempotent_by_recording_id_and_a_different_manifest_conflicts(
    tmp_path, server
) -> None:
    port, _ = server

    first = post_recording(port, recording_id="call-1.user")
    again = post_recording(port, recording_id="call-1.user")
    other_key = post_recording(port, recording_id="call-1.user", session_key="call-2")
    other_speaker = post_recording(port, recording_id="call-1.user", speaker="agent")
    minted = [post_recording(port), post_recording(port)]

    assert (first.status, again.status) == (201, 200)
    assert again.json() == first.json()
    for conflict in (other_key, other_speaker):
        assert (conflict.status, conflict.problem_code()) == (409, "EARSHOT_RECORDING_CONFLICT")
    assert [reply.status for reply in minted] == [201, 201]
    minted_ids = [reply.json()["recording_id"] for reply in minted]
    assert minted_ids[0] != minted_ids[1]
    assert all(uuid.UUID(value) for value in minted_ids)
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(3,)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(1,)]


@pytest.mark.parametrize(
    "fields",
    [
        {"recording_id": ".."},
        {"recording_id": "a/b"},
        {"recording_id": "x" * 129},
        {"speaker": "both"},
        {"content_type": "audio/x-wav"},
        {"content_type": "audio/wav; rate=16000"},
        {"started_at_unix_nano": 1_800_000_000},
        {"started_at_unix_nano": "01"},
        {"started_at_unix_nano": "18446744073709551616"},
        # The call pipeline is int64: [1, 2^63 - 1] only.
        {"started_at_unix_nano": "0"},
        {"started_at_unix_nano": "9223372036854775808"},
        {"started_at_unix_nano": "18446744073709551615"},
        {"label": "x" * 129},
        {"label": "a\x00b"},
        {"session_key": "   "},
        {"session_key": "x" * 257},
        {"session_key": "call\x001"},
        {"channels": [0]},
    ],
    ids=lambda fields: next(iter(fields)),
)
def test_an_invalid_manifest_is_422_and_creates_nothing(tmp_path, server, fields) -> None:
    port, _ = server

    reply = post_recording(port, **fields)

    assert reply.status == 422, reply.body
    assert reply.problem_code() in {"EARSHOT_INVALID_REQUEST", "EARSHOT_INVALID_SESSION_KEY"}
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(0,)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(0,)]


@pytest.mark.parametrize("started_at", ["1", str(2**63 - 1)], ids=["one", "int64-max"])
def test_a_recording_at_either_bound_of_its_start_closes_into_a_revision(
    tmp_path, blob_backend, started_at
) -> None:
    """What the API accepts, the closer can materialize."""

    store = make_store(tmp_path)
    app = create_app(
        store=store,
        blob_store=blob_backend.store,
        call_timings=CallTimings(long_quiet_seconds=0.5, upload_hold_seconds=2.0),
    )
    with serve(app) as port:
        created = post_recording(port, recording_id="r1", started_at_unix_nano=started_at)
        uploaded = put_content(port, "r1", wav(seconds=0.5))
        detail: dict = {}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            detail = request(port, "GET", f"/v1/calls/{created.json()['call_id']}").json()
            if detail["latest_revision"] is not None or detail["materialize_error"] is not None:
                break
            time.sleep(0.1)

    assert (created.status, uploaded.status) == (201, 200)
    assert detail["materialize_error"] is None
    assert detail["latest_revision"]["revision"] == 1


def test_a_call_takes_sixteen_recordings_and_refuses_the_seventeenth(tmp_path) -> None:
    """A revision holds a call's recordings with one
    media clock domain each, and the recorder keeps at most 16; a 17th recording
    made every build of its call fail. The limit is per call, and a repeated create
    of a recording the call has is still answered."""

    with serve(create_app(store=make_store(tmp_path))) as port:
        sixteen = [post_recording(port, recording_id=f"r{index}") for index in range(1, 17)]
        seventeenth = post_recording(port, recording_id="r17")
        repeated = post_recording(port, recording_id="r16")
        other_call = post_recording(port, recording_id="other", session_key="call-2")

    assert [reply.status for reply in sixteen] == [201] * 16
    assert (seventeenth.status, seventeenth.problem_code()) == (409, "EARSHOT_RECORDING_LIMIT")
    assert "16" in seventeenth.json()["error"]["message"]
    assert (repeated.status, other_call.status) == (200, 201)
    assert catalog_rows(
        tmp_path, "SELECT count(*) FROM recordings GROUP BY call_id ORDER BY 1"
    ) == [(1,), (16,)]


def test_a_metadata_only_project_gets_422_for_a_recording(tmp_path, server) -> None:
    port, store = server
    store.set_capture_preset("default", "metadata_only")

    reply = post_recording(port, recording_id="r1")

    assert (reply.status, reply.problem_code()) == (422, "EARSHOT_PRESET_FORBIDS_MEDIA")
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(0,)]


# --- stereo recordings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "body_of", "duration_ms"),
    [
        ("audio/ogg", lambda: LIVEKIT_RECORDING.read_bytes(), 34_010),
        (
            "audio/flac",
            lambda: encoded("flac", "flac", rate=16_000, channels=2, seconds=1.5),
            1500,
        ),
        ("audio/wav", lambda: wav(channels=2, seconds=1.5), 1500),
    ],
    ids=["livekit-ogg-opus", "flac", "wav"],
)
def test_a_stereo_recording_with_a_channel_map_is_created_uploaded_and_read_back(
    tmp_path, blob_backend, server, content_type, body_of, duration_ms
) -> None:
    port, _ = server
    body = body_of()
    manifest = {
        "recording_id": "call-1.stereo",
        "speaker": "mixed",
        "content_type": content_type,
        "channel_speakers": ["user", "agent"],
    }

    created = post_recording(port, **manifest)
    again = post_recording(port, **manifest)
    swapped = post_recording(port, **{**manifest, "channel_speakers": ["agent", "user"]})
    uploaded = put_content(port, "call-1.stereo", body, content_type=content_type)
    whole = request(port, "GET", "/v1/recordings/call-1.stereo/content")
    mono = post_recording(port, recording_id="call-1.user")

    assert created.status == 201, created.body
    assert (created.json()["speaker"], created.json()["channel_speakers"]) == (
        "mixed",
        ["user", "agent"],
    )
    assert (again.status, again.json()) == (200, created.json())
    assert (swapped.status, swapped.problem_code()) == (409, "EARSHOT_RECORDING_CONFLICT")
    assert uploaded.status == 200, uploaded.body
    assert {key: uploaded.json()[key] for key in ("state", "duration_ms", "channel_speakers")} == {
        "state": "stored",
        "duration_ms": duration_ms,
        "channel_speakers": ["user", "agent"],
    }
    assert (whole.status, whole.body, whole.headers["content-type"]) == (200, body, content_type)
    assert mono.json()["channel_speakers"] is None
    assert catalog_rows(
        tmp_path, "SELECT recording_id, channel_speakers_json FROM recordings ORDER BY 1"
    ) == [("call-1.stereo", '["user","agent"]'), ("call-1.user", None)]


@pytest.mark.parametrize(
    ("manifest", "content_type", "body_of"),
    [
        ({"speaker": "agent"}, "audio/ogg", lambda: LIVEKIT_RECORDING.read_bytes()),
        ({"speaker": "mixed"}, "audio/wav", lambda: wav(channels=2, seconds=0.5)),
        (
            {"speaker": "user"},
            "audio/flac",
            lambda: encoded("flac", "flac", rate=16_000, channels=2, seconds=0.5),
        ),
        (
            {"speaker": "mixed", "channel_speakers": ["user", "agent"]},
            "audio/wav",
            lambda: wav(channels=1, seconds=0.5),
        ),
        (
            {"speaker": "mixed", "channel_speakers": ["agent"]},
            "audio/ogg",
            lambda: LIVEKIT_RECORDING.read_bytes(),
        ),
    ],
    ids=[
        "mono-ogg-gets-stereo",
        "mixed-gets-stereo",
        "mono-flac-gets-stereo",
        "map-2-gets-1",
        "map-1-gets-2",
    ],
)
def test_an_upload_whose_header_has_another_channel_count_is_refused(
    tmp_path, blob_backend, server, manifest, content_type, body_of
) -> None:
    port, _ = server
    post_recording(port, recording_id="r1", content_type=content_type, **manifest)

    reply = put_content(port, "r1", body_of(), content_type=content_type)
    state = request(port, "GET", "/v1/recordings/r1").json()["state"]

    assert (reply.status, reply.problem_code()) == (422, "EARSHOT_RECORDING_CHANNEL_MISMATCH")
    assert state == "pending"
    assert blob_backend.objects() == [] and blob_backend.leftovers() == []
    assert _journal(tmp_path) == []


@pytest.mark.parametrize(
    ("content_type", "body", "code"),
    [
        ("audio/flac", b"ID3\x04" + bytes(4096), "EARSHOT_RECORDING_INVALID_FLAC"),
        ("audio/ogg", b"RIFF" + bytes(4096), "EARSHOT_RECORDING_INVALID_OGG"),
        # Ogg Vorbis: Earshot reads the channel count of Ogg Opus only.
        (
            "audio/ogg",
            b"OggS\x00\x02" + bytes(20) + b"\x01\x1e\x01vorbis" + bytes(23),
            "EARSHOT_RECORDING_INVALID_OGG",
        ),
    ],
    ids=["not-flac", "not-ogg", "ogg-vorbis"],
)
def test_a_flac_or_ogg_body_whose_header_cannot_be_read_is_refused(
    tmp_path, blob_backend, server, content_type, body, code
) -> None:
    port, _ = server
    post_recording(port, recording_id="r1", content_type=content_type)

    reply = put_content(port, "r1", body, content_type=content_type)

    assert (reply.status, reply.problem_code()) == (422, code)
    assert blob_backend.objects() == [] and _journal(tmp_path) == []


def _wav_declaring_no_audio() -> bytes:
    body = bytearray(wav(seconds=0.5))
    struct.pack_into("<I", body, 40, 0)  # its data size: libsndfile reads no frames
    return bytes(body)


def _flac_declaring_no_samples() -> bytes:
    """A real FLAC whose STREAMINFO total was never written back (0, "unknown")."""

    body = bytearray(encoded("flac", "flac", rate=16_000, channels=1, seconds=0.5))
    (packed,) = struct.unpack_from(">Q", body, 18)
    struct.pack_into(">Q", body, 18, packed & ~0xF_FFFF_FFFF)
    return bytes(body)


@pytest.mark.parametrize(
    ("content_type", "body_of", "code"),
    [
        ("audio/wav", _wav_declaring_no_audio, "EARSHOT_RECORDING_INVALID_WAV"),
        ("audio/flac", _flac_declaring_no_samples, "EARSHOT_RECORDING_INVALID_FLAC"),
    ],
    ids=["wav-data-size-0", "flac-total-samples-0"],
)
def test_an_upload_whose_header_declares_no_audio_is_refused(
    tmp_path, blob_backend, server, content_type, body_of, code
) -> None:
    """The analysis could never read it, so it is refused before it is stored."""

    port, _ = server
    post_recording(port, recording_id="r1", content_type=content_type)

    reply = put_content(port, "r1", body_of(), content_type=content_type)

    assert (reply.status, reply.problem_code()) == (422, code)
    assert request(port, "GET", "/v1/recordings/r1").json()["state"] == "pending"
    assert blob_backend.objects() == [] and _journal(tmp_path) == []


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"speaker": "mixed", "channel_speakers": []}, "EARSHOT_INVALID_REQUEST"),
        ({"speaker": "mixed", "channel_speakers": ["mixed"]}, "EARSHOT_INVALID_REQUEST"),
        (
            {"speaker": "mixed", "channel_speakers": ["user", "agent", "user"]},
            "EARSHOT_INVALID_REQUEST",
        ),
        ({"speaker": "mixed", "channel_speakers": "user"}, "EARSHOT_INVALID_REQUEST"),
        (
            {"speaker": "mixed", "channel_speakers": ["user", "user"]},
            "EARSHOT_INVALID_CHANNEL_SPEAKERS",
        ),
        ({"speaker": "user", "channel_speakers": ["user"]}, "EARSHOT_INVALID_CHANNEL_SPEAKERS"),
        (
            {
                "speaker": "mixed",
                "content_type": "audio/mpeg",
                "channel_speakers": ["user", "agent"],
            },
            "EARSHOT_INVALID_CHANNEL_SPEAKERS",
        ),
    ],
    ids=["empty", "mixed-channel", "three", "not-a-list", "twice", "not-mixed", "mp3"],
)
def test_an_invalid_channel_map_is_422_and_creates_nothing(tmp_path, server, fields, code) -> None:
    port, _ = server

    reply = post_recording(port, recording_id="r1", **fields)

    assert (reply.status, reply.problem_code()) == (422, code), reply.body
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recordings") == [(0,)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM calls") == [(0,)]


# --- uploads ------------------------------------------------------------------------------------


def test_an_upload_to_a_stored_recording_is_hashed_and_never_written(
    tmp_path, blob_backend, server
) -> None:
    port, _ = server
    body = wav()
    post_recording(port, recording_id="r1")
    put_content(port, "r1", body)
    objects = blob_backend.objects()

    same = put_content(port, "r1", body)
    different = put_content(port, "r1", wav(tone=2))

    assert same.status == 200 and same.json()["sha256"] == hashlib.sha256(body).hexdigest()
    assert (different.status, different.problem_code()) == (409, "EARSHOT_RECORDING_CONFLICT")
    assert blob_backend.objects() == objects
    assert blob_backend.leftovers() == [] and _journal(tmp_path) == []
    assert request(port, "GET", "/v1/recordings/r1/content").body == body


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("stereo", 422, "EARSHOT_RECORDING_CHANNEL_MISMATCH"),
        ("not a wav", 422, "EARSHOT_RECORDING_INVALID_WAV"),
        ("wrong type", 415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE"),
        ("no type", 415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE"),
        ("gzip", 415, "EARSHOT_UNSUPPORTED_CONTENT_ENCODING"),
        ("over the limit", 413, "EARSHOT_BODY_TOO_LARGE"),
        ("digest mismatch", 400, "EARSHOT_CONTENT_DIGEST_MISMATCH"),
        ("malformed digest", 400, "EARSHOT_INVALID_CONTENT_DIGEST"),
        ("empty", 400, "EARSHOT_EMPTY_BODY"),
    ],
)
def test_a_refused_upload_stores_nothing_and_leaves_the_recording_pending(
    tmp_path, blob_backend, case, status, code
) -> None:
    store = make_store(tmp_path)
    config = ApiConfig(recording_max_bytes=64 * 1024)
    body, headers, content_type = wav(seconds=1.0), {}, "audio/wav"
    if case == "stereo":
        body = wav(channels=2, seconds=0.5)
    elif case == "not a wav":
        body = b"OggS" + bytes(4096)
    elif case == "wrong type":
        content_type = "audio/ogg"
    elif case == "no type":
        content_type = ""
    elif case == "gzip":
        headers["Content-Encoding"] = "gzip"
    elif case == "over the limit":
        body = wav(seconds=3.0)
    elif case == "digest mismatch":
        headers["Content-Digest"] = content_digest(body + b"!")
    elif case == "malformed digest":
        headers["Content-Digest"] = "sha-256=not-a-digest"
    elif case == "empty":
        body = b""

    with serve(create_app(store=store, blob_store=blob_backend.store, config=config)) as port:
        post_recording(port, recording_id="r1")
        reply = put_content(port, "r1", body, content_type=content_type, headers=headers)
        state = request(port, "GET", "/v1/recordings/r1").json()["state"]

    assert (reply.status, reply.problem_code()) == (status, code)
    assert state == "pending"
    assert blob_backend.objects() == [] and blob_backend.leftovers() == []
    assert _journal(tmp_path) == []


def test_a_body_over_the_limit_without_a_length_is_cut_off_at_the_limit(
    tmp_path, blob_backend
) -> None:
    store = make_store(tmp_path)
    config = ApiConfig(recording_max_bytes=64 * 1024)
    body = wav(seconds=3.0)

    with serve(create_app(store=store, blob_store=blob_backend.store, config=config)) as port:
        post_recording(port, recording_id="r1")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(
                "PUT",
                "/v1/recordings/r1/content",
                body=iter([body[:40_000], body[40_000:]]),
                headers={"Content-Type": "audio/wav"},
                encode_chunked=True,
            )
            response = connection.getresponse()
            reply = (response.status, json.loads(response.read())["error"]["code"])
        finally:
            connection.close()

    assert reply == (413, "EARSHOT_BODY_TOO_LARGE")
    assert blob_backend.objects() == [] and blob_backend.leftovers() == []
    assert _journal(tmp_path) == []


def test_an_upload_to_a_failed_or_unknown_recording_is_refused(tmp_path, blob_backend) -> None:
    clock = ManualClock(wall=1_800_000_000 * SECOND, monotonic=0)
    store = make_store(tmp_path, clock=clock)
    sweeper = RecordingStore(store, calls=CallStore(store), blobs=blob_backend.store)

    with serve(create_app(store=store, blob_store=blob_backend.store)) as port:
        post_recording(port, recording_id="r1")
        clock.advance(601 * SECOND)
        swept = sweeper.fail_stale_uploads()
        failed = put_content(port, "r1", wav())
        again = post_recording(port, recording_id="r1")
        unknown = put_content(port, "r2", wav())
        unknown_content = request(port, "GET", "/v1/recordings/r2/content")
        bad_id = request(port, "GET", "/v1/recordings/..%2Fx")

    assert swept == 1
    assert (failed.status, failed.problem_code()) == (409, "EARSHOT_RECORDING_CONFLICT")
    assert (again.status, again.problem_code()) == (409, "EARSHOT_RECORDING_CONFLICT")
    assert (unknown.status, unknown.problem_code()) == (404, "EARSHOT_RECORDING_NOT_FOUND")
    assert unknown_content.status == 404
    assert bad_id.status == 404
    assert blob_backend.objects() == []


def test_a_put_cancelled_while_it_discards_its_attempt_still_ends_the_attempt(
    tmp_path, blob_backend
) -> None:
    """A cancellation (a server stopping, a proxy
    aborting) that lands while the handler discards its attempt skipped ``end_upload``,
    so the attempt stayed "in flight" for the process's life: its recording was never
    swept, and its call never closed. The real upload endpoint, awaited directly so
    the cancel reaches it (whether a middleware layer happens to shield it is not
    the endpoint's guarantee); its discard waits for the store's write lock, which
    the test holds."""

    from starlette.requests import Request

    clock = ManualClock(wall=1_800_000_000 * SECOND, monotonic=0)
    store = make_store(tmp_path, clock=clock)
    app = create_app(store=store, blob_store=blob_backend.store)
    recordings = app.state.recordings
    recordings.create("default", RecordingManifest("r1", "call-1", "user", "audio/wav", STARTED_AT))
    [upload] = [
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/v1/recordings/{recording_id}/content"
        and "PUT" in route.methods
    ]
    body = wav()
    lock_held, release, disconnected = threading.Event(), threading.Event(), threading.Event()

    def hold_the_write_lock() -> None:
        with store._mutation("test_holds_the_write_lock"):
            lock_held.set()
            release.wait(30)

    holder = threading.Thread(target=hold_the_write_lock)
    received = []

    async def receive() -> dict:
        received.append(None)
        if len(received) == 1:  # the attempt has begun: half the body arrives
            return {"type": "http.request", "body": body[: len(body) // 2], "more_body": True}
        holder.start()  # then the client goes away while the store is busy
        await asyncio.to_thread(lock_held.wait, 10)
        disconnected.set()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/v1/recordings/r1/content",
        "headers": [
            (b"content-type", b"audio/wav"),
            (b"content-length", str(len(body)).encode()),
        ],
        "state": {"project_id": "default"},
    }

    async def scenario() -> None:
        handler = asyncio.create_task(upload("r1", Request(scope, receive)))
        await asyncio.to_thread(disconnected.wait, 10)
        await asyncio.sleep(0.3)  # the handler now waits in discard_upload for the lock
        handler.cancel()
        release.set()  # the abandoned discard then finishes on its own
        await asyncio.gather(handler, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        if holder.is_alive():
            holder.join(30)
    _wait_for_empty_journal(tmp_path)
    clock.advance(601 * SECOND)  # past the pending deadline

    assert received and disconnected.is_set()
    assert _journal(tmp_path) == []
    assert recordings.fail_stale_uploads() == 1  # no attempt is left "in flight"


def test_a_put_cancelled_while_it_begins_its_attempt_still_ends_the_attempt(
    tmp_path, blob_backend
) -> None:
    """A cancellation that lands while the handler
    awaits ``begin_upload`` in the threadpool cannot stop the thread, which goes on
    to register the attempt; the handler never reached the ``finally`` that ends it,
    so the attempt held its call, and kept its recording from the sweep, for the
    process's life. The real upload endpoint, awaited directly (as in the test above); its
    ``begin_upload`` waits for the store's write lock, which the test holds."""

    from starlette.requests import Request

    clock = ManualClock(wall=1_800_000_000 * SECOND, monotonic=0)
    store = make_store(tmp_path, clock=clock)
    app = create_app(store=store, blob_store=blob_backend.store)
    recordings = app.state.recordings
    recordings.create("default", RecordingManifest("r1", "call-1", "user", "audio/wav", STARTED_AT))
    [upload] = [
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/v1/recordings/{recording_id}/content"
        and "PUT" in route.methods
    ]
    body = wav()
    lock_held, release = threading.Event(), threading.Event()

    def hold_the_write_lock() -> None:
        with store._mutation("test_holds_the_write_lock"):
            lock_held.set()
            release.wait(30)

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/v1/recordings/r1/content",
        "headers": [
            (b"content-type", b"audio/wav"),
            (b"content-length", str(len(body)).encode()),
        ],
        "state": {"project_id": "default"},
    }
    holder = threading.Thread(target=hold_the_write_lock)

    async def scenario() -> None:
        holder.start()
        await asyncio.to_thread(lock_held.wait, 10)
        handler = asyncio.create_task(upload("r1", Request(scope, receive)))
        await asyncio.sleep(0.3)  # the handler now awaits begin_upload, which waits
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        release.set()  # the abandoned begin_upload then registers its attempt
        deadline = time.monotonic() + 10
        while not _journal(tmp_path) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)  # begin_upload journals the attempt as it commits
        await asyncio.sleep(0.3)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        holder.join(30)
    clock.advance(601 * SECOND)  # past the pending deadline

    assert len(_journal(tmp_path)) == 1  # the attempt did begin
    assert recordings.uploading_calls() == frozenset()
    assert recordings.fail_stale_uploads() == 1  # no attempt is left "in flight"


class _HeldBody:
    """A request body that stops halfway until ``release`` is set."""

    def __init__(self, body: bytes, release: threading.Event, halfway: threading.Event) -> None:
        self.body, self.release, self.halfway = body, release, halfway

    def __iter__(self) -> Iterator[bytes]:
        middle = len(self.body) // 2
        yield self.body[:middle]
        self.halfway.set()
        assert self.release.wait(30)
        yield self.body[middle:]


def test_a_slow_put_that_crosses_the_pending_deadline_still_stores(tmp_path, blob_backend) -> None:
    """The server's own upload sweep (a 3 s hold here, a real
    clock and maintenance loop) never fails a recording whose PUT is still streaming.
    The PUT starts late, so it crosses the pending deadline while it lasts less
    than the hold cap, which ends a PUT."""

    store = make_store(tmp_path)
    app = create_app(
        store=store,
        blob_store=blob_backend.store,
        call_timings=CallTimings(upload_hold_seconds=3.0),
    )
    body = wav(seconds=2.0)
    release, halfway = threading.Event(), threading.Event()
    results: dict[str, tuple[int, dict]] = {}

    with serve(app) as port:
        created = post_recording(port, recording_id="r1")
        time.sleep(2.0)
        upload = threading.Thread(
            target=_put_held, args=(port, _HeldBody(body, release, halfway), results, "slow")
        )
        upload.start()
        assert halfway.wait(30)
        time.sleep(2.0)  # past the 3 s deadline, across at least one sweep tick
        release.set()
        upload.join(60)
        content = request(port, "GET", "/v1/recordings/r1/content")

    assert created.status == 201
    status, reply = results["slow"]
    assert (status, reply.get("state")) == (200, "stored"), reply
    assert (content.status, content.body) == (200, body)


SHORT_TIMINGS = CallTimings(
    end_grace_seconds=1.0,
    long_quiet_seconds=1.0,
    late_debounce_seconds=1.0,
    upload_hold_seconds=1.0,
)


def _revisions(tmp_path) -> list[tuple]:
    return catalog_rows(
        tmp_path, "SELECT revision, close_reason, recording_count FROM call_revisions"
    )


def _wait_for_revision(tmp_path, seconds: float = 20.0) -> list[tuple]:
    deadline = time.monotonic() + seconds
    while not _revisions(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.1)
    return _revisions(tmp_path)


def test_the_server_closes_a_call_only_after_its_slow_put_and_revision_one_has_it(
    tmp_path, blob_backend
) -> None:
    """End to end: the real maintenance
    loop and closer (1 s timings, a 4 s hold), a call with spans and a PUT that starts
    at 2.5 s and stalls 3 s mid-body, past the call's due time and its pending
    deadline but inside the hold cap that ends a PUT. The call stays open, and
    revision 1 holds the audio (before: revision 1 without it, then a late one)."""

    store = make_store(tmp_path)
    app = create_app(
        store=store,
        blob_store=blob_backend.store,
        call_timings=dataclasses.replace(SHORT_TIMINGS, upload_hold_seconds=4.0),
    )
    body = wav(seconds=2.0)
    release, halfway = threading.Event(), threading.Event()
    results: dict[str, tuple[int, dict]] = {}

    with serve(app) as port:
        assert post_otlp(port, keyed_request("call-1", 7, 3)).status == 200
        assert post_recording(port, recording_id="r1").status == 201
        time.sleep(2.5)
        upload = threading.Thread(
            target=_put_held, args=(port, _HeldBody(body, release, halfway), results, "slow")
        )
        upload.start()
        assert halfway.wait(30)
        time.sleep(3.0)  # several closer and sweep ticks past due and deadline
        while_streaming = (
            _revisions(tmp_path),
            catalog_rows(tmp_path, "SELECT state FROM calls"),
            catalog_rows(tmp_path, "SELECT state FROM recordings"),
        )
        release.set()
        upload.join(60)
        revisions = _wait_for_revision(tmp_path)
        time.sleep(2.0)  # past the late debounce: no second revision follows

    assert while_streaming == ([], [("open",)], [("pending",)])
    assert results["slow"][0] == 200
    assert revisions == _revisions(tmp_path) == [(1, "quiet_period", 1)]


def test_an_upload_that_sends_nothing_for_its_idle_timeout_is_ended_and_releases_its_call(
    tmp_path, blob_backend
) -> None:
    """The progress bound: a PUT that stops sending (a client
    whose connection died without a FIN) is answered 408 after the idle timeout (60 s
    in the server, 1 s here, under a 3 s hold cap), and its attempt ends: the call
    closes, without it."""

    store = make_store(tmp_path)
    app = create_app(
        store=store,
        blob_store=blob_backend.store,
        call_timings=dataclasses.replace(SHORT_TIMINGS, upload_hold_seconds=3.0),
        upload_idle_timeout_seconds=1.0,
    )
    body = wav(seconds=2.0)

    with serve(app) as port:
        assert post_otlp(port, keyed_request("call-1", 7, 3)).status == 200
        assert post_recording(port, recording_id="r1").status == 201
        connection = socket.create_connection(("127.0.0.1", port), timeout=15)
        try:
            started = time.monotonic()
            connection.sendall(
                b"PUT /v1/recordings/r1/content HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: audio/wav\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body[: len(body) // 2]
            )
            reply = connection.recv(65536)  # then nothing more is sent
            answered_after = time.monotonic() - started
            while chunk := connection.recv(65536):  # the server closes the connection
                reply += chunk
        finally:
            connection.close()
        revisions = _wait_for_revision(tmp_path)
        deadline = time.monotonic() + 10  # the sweep's own tick fails it
        while catalog_rows(tmp_path, "SELECT state FROM recordings") != [("failed",)]:
            if time.monotonic() > deadline:
                break
            time.sleep(0.1)
        recording = request(port, "GET", "/v1/recordings/r1").json()

    assert reply.startswith(b"HTTP/1.1 408 "), reply[:200]
    assert b'"EARSHOT_UPLOAD_IDLE_TIMEOUT"' in reply
    assert 0.9 < answered_after < 5.0
    assert revisions == [(1, "quiet_period", 0)]
    assert recording["state"] == "failed"
    _wait_for_empty_journal(tmp_path)
    assert _journal(tmp_path) == [] and blob_backend.leftovers() == []


def test_an_upload_that_trickles_past_its_hold_cap_is_ended_and_releases_its_call(
    tmp_path, blob_backend
) -> None:
    """The idle timeout bounds silence,
    not progress, so a PUT sending a byte just inside it held its call for as long
    as it trickled. A PUT is ended with 408 once it has run for the upload hold cap
    (600 s in the server, 2 s here), and its attempt ends: the call closes without it."""

    store = make_store(tmp_path)
    app = create_app(
        store=store,
        blob_store=blob_backend.store,
        call_timings=dataclasses.replace(SHORT_TIMINGS, upload_hold_seconds=2.0),
        upload_idle_timeout_seconds=1.0,
    )
    body = wav(seconds=2.0)

    with serve(app) as port:
        assert post_otlp(port, keyed_request("call-1", 7, 3)).status == 200
        assert post_recording(port, recording_id="r1").status == 201
        connection = socket.create_connection(("127.0.0.1", port), timeout=15)
        try:
            started = time.monotonic()
            connection.sendall(
                b"PUT /v1/recordings/r1/content HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: audio/wav\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body[:1000]
            )
            reply, sent = b"", 1000
            while not reply and time.monotonic() - started < 8:
                if select.select([connection], [], [], 0.3)[0]:
                    reply = connection.recv(65536)
                else:  # one byte, well inside the idle timeout
                    connection.sendall(body[sent : sent + 1])
                    sent += 1
            answered_after = time.monotonic() - started
            with contextlib.suppress(ConnectionError):
                while reply and (chunk := connection.recv(65536)):
                    reply += chunk
        finally:
            connection.close()
        assert reply.startswith(b"HTTP/1.1 408 "), (reply[:200], answered_after)
        assert b'"EARSHOT_UPLOAD_DEADLINE_EXCEEDED"' in reply
        assert 1.8 < answered_after < 6.0
        revisions = _wait_for_revision(tmp_path)
        deadline = time.monotonic() + 10  # the sweep's own tick fails it
        while catalog_rows(tmp_path, "SELECT state FROM recordings") != [("failed",)]:
            if time.monotonic() > deadline:
                break
            time.sleep(0.1)
        recording = request(port, "GET", "/v1/recordings/r1").json()

    assert revisions == [(1, "quiet_period", 0)]
    assert recording["state"] == "failed"
    _wait_for_empty_journal(tmp_path)
    assert _journal(tmp_path) == [] and blob_backend.leftovers() == []


def _put_held(port: int, body: _HeldBody, results: dict, name: str) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(
            "PUT",
            "/v1/recordings/r1/content",
            body=iter(body),
            headers={"Content-Type": "audio/wav", "Content-Length": str(len(body.body))},
        )
        response = connection.getresponse()
        results[name] = (response.status, json.loads(response.read()))
    finally:
        connection.close()


@pytest.mark.parametrize("same_bytes", [False, True], ids=["different", "same"])
def test_two_concurrent_uploads_store_one_object_and_the_loser_cleans_up(
    tmp_path, blob_backend, server, same_bytes
) -> None:
    port, _ = server
    bodies = {"a": wav(seconds=2.0, tone=3), "b": wav(seconds=2.0, tone=3 if same_bytes else 4)}
    post_recording(port, recording_id="r1")
    release = threading.Event()
    halfway = {name: threading.Event() for name in bodies}
    results: dict[str, tuple[int, dict]] = {}
    threads = [
        threading.Thread(
            target=_put_held,
            args=(port, _HeldBody(bodies[name], release, halfway[name]), results, name),
        )
        for name in bodies
    ]
    for thread in threads:
        thread.start()
    # Both attempts are journaled (begun, streaming) before either may finish.
    deadline = time.monotonic() + 30
    while len(_journal(tmp_path)) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    began = len(_journal(tmp_path))
    release.set()
    for thread in threads:
        thread.join(60)

    assert began == 2 and all(event.is_set() for event in halfway.values())
    statuses = sorted(status for status, _ in results.values())
    assert statuses == ([200, 200] if same_bytes else [200, 409])
    winner = next(name for name, (status, body) in results.items() if status == 200)
    stored = request(port, "GET", "/v1/recordings/r1").json()
    assert stored["sha256"] == hashlib.sha256(bodies[winner]).hexdigest()
    assert request(port, "GET", "/v1/recordings/r1/content").body == bodies[winner]
    _wait_for_empty_journal(tmp_path)
    assert _journal(tmp_path) == []
    assert len(blob_backend.objects("recordings/")) == 1 and blob_backend.leftovers() == []


# --- project isolation ------------------------------------------------------------------------


def test_another_projects_recording_is_404_and_ids_are_per_project(tmp_path, blob_backend) -> None:
    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")
    default_key = store.issue_api_key("default", label="a").credential
    support_key = store.issue_api_key("support", label="b").credential
    body = wav()

    with serve(create_app(store=store, blob_store=blob_backend.store)) as port:
        mine = post_recording(port, credential=default_key, recording_id="r1")
        support = {"Authorization": f"Bearer {support_key}"}
        foreign = [
            request(port, "GET", "/v1/recordings/r1", headers=support),
            request(port, "GET", "/v1/recordings/r1/content", headers=support),
            put_content(port, "r1", body, headers=support),
        ]
        theirs = post_recording(port, credential=support_key, recording_id="r1", speaker="agent")

    assert mine.status == 201 and theirs.status == 201
    assert [(reply.status, reply.problem_code()) for reply in foreign] == [
        (404, "EARSHOT_RECORDING_NOT_FOUND")
    ] * 3
    assert sorted(
        catalog_rows(tmp_path, "SELECT project_id, recording_id, speaker FROM recordings")
    ) == [("default", "r1", "user"), ("support", "r1", "agent")]
    assert mine.json()["call_id"] != theirs.json()["call_id"]
    assert blob_backend.objects() == []
