"""The recording fetch job: a connector's provider recording, fetched into the blob store.

A signed delivery goes through the real ingestion kernel, which holds the
recording (``pending``) and queues its fetch. ``FetchRunner`` then claims the
fetch, fetches it from a real local HTTPS server (a test CA trusted through the
fetcher's SSL context; ``MappedResolver`` maps the provider hosts to it as
public addresses) and streams it into the blob store through the recordings
store's own upload attempt. The closer is the real one, under a controlled clock.
Everything runs on the dialect under test (``--db``), and the blob paths on local
disk and MinIO.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
import time

import pytest

import test_elevenlabs_connector as elevenlabs
import test_retell_connector as retell
import test_vapi_connector as vapi
from connector_support import SECOND, START, ConnectorWorld, connector_world
from earshot.blobstore import LocalBlobStore
from earshot.connectors import MappingSecretResolver
from earshot.recording_fetcher import (
    FETCH_GONE,
    FETCH_HOST_NOT_ALLOWED,
    FETCH_NOT_AUDIO,
    FETCH_UPSTREAM_UNAVAILABLE,
    RecordingFetcher,
)
from earshot.storage_call_purge import CallPurger
from earshot.storage_closer import Closer
from earshot.storage_fetches import (
    FETCH_ID_INVALID,
    FETCH_KEY_MISSING,
    FETCH_PRESET_FORBIDS_MEDIA,
    FETCH_URL_EXPIRED,
    PAYLOAD_HOSTS,
    FetchRunner,
    FetchStore,
    fetch_job,
    payload_allowlists,
)
from earshot.storage_recordings import RecordingManifest, RecordingStore
from fetch_support import AudioServer, Reply, make_certificates, resolver_for, wav_bytes
from store_support import current_dialect, stored_needles

pytestmark = pytest.mark.integration

AUDIO = wav_bytes(0.5)
RETELL_HOST = "retellai.s3.us-west-2.amazonaws.com"
RETELL_PATH = f"/{retell.CALL_ID}/recording.wav"
VAPI_KEY = "vapi-private-key-CANARY-5f2e"
ELEVENLABS_KEY = "sk_elevenlabs_CANARY_91c0"
END_GRACE = 10 * SECOND
LATE_DEBOUNCE = 30 * SECOND
MINUTE = 60 * SECOND


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    return make_certificates(tmp_path_factory.mktemp("fetch-ca"))


@pytest.fixture
def server(certificates):
    with AudioServer(certificates) as running:
        yield running


class FetchWorld:
    """A connector world whose fetches go to the local HTTPS server."""

    def __init__(
        self,
        tmp_path,
        provider: str,
        server: AudioServer,
        certificates,
        *,
        blobs=None,
        config=None,
        preset: str = "full",
        keys: dict[str, str] | None = None,
    ) -> None:
        self.world: ConnectorWorld = connector_world(
            tmp_path, provider, config=config, preset=preset
        )
        self.blobs = blobs if blobs is not None else LocalBlobStore(tmp_path / "blobs")
        self.world.recordings = RecordingStore(
            self.world.store, calls=self.world.calls, blobs=self.blobs
        )
        self.server = server
        self._certificates = certificates
        self._keys = keys or {}
        self.runner = self.new_runner()

    def new_runner(self) -> FetchRunner:
        """A runner on this world's store, as a (re)started server builds one."""

        return FetchRunner(
            FetchStore(self.world.store),
            recordings=self.world.recordings,
            fetcher=RecordingFetcher(
                resolver=resolver_for(self.server),
                ssl_context=self._certificates.client_context(),
            ),
            secrets=MappingSecretResolver(self._keys),
        )

    def __getattr__(self, name):
        return getattr(self.world, name)

    def fetch(self):
        return self.runner.run_next()

    def fetch_rows(self) -> list[tuple]:
        return self.world.rows(
            "SELECT state, attempts, error_code, url, url_expires_unix_nano "
            "FROM recording_fetches ORDER BY track"
        )

    def recording_rows(self) -> list[tuple]:
        return self.world.rows(
            "SELECT state, size, duration_ms, blob_key IS NOT NULL FROM recordings ORDER BY label"
        )

    def close_due(self) -> None:
        closer = Closer(self.world.store, calls=self.world.calls, recordings=self.world.recordings)
        while closer.close_next() is not None:
            pass

    def revisions(self) -> list[tuple]:
        return self.world.rows(
            "SELECT revision, close_reason, finality, recording_count FROM call_revisions "
            "ORDER BY revision"
        )


def _retell(event: str = "call_ended") -> dict:
    payload = retell._call(event)
    payload["call"]["recording_url"] = f"https://{RETELL_HOST}{RETELL_PATH}"
    return payload


def _audio(**changes) -> Reply:
    return Reply(body=[AUDIO], **changes)


def _statuses(*statuses: int):
    """A route that answers each status in turn, then the audio."""

    remaining = list(statuses)

    def reply() -> Reply:
        if remaining:
            return Reply(remaining.pop(0), (("Content-Type", "text/plain"),), [b"busy"])
        return _audio()

    return reply


# --- fetching ------------------------------------------------------------------------------


def test_a_payload_recording_is_fetched_into_the_blob_store(
    tmp_path, server, certificates, blob_backend
):
    world = FetchWorld(tmp_path, "retell", server, certificates, blobs=blob_backend.store)
    server.route(RETELL_HOST, RETELL_PATH, _audio())
    world.deliver(_retell())

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("stored", None)
    assert world.recording_rows() == [("stored", len(AUDIO), 500, True)]
    assert world.fetch_rows() == [("stored", 0, None, None, None)]
    assert len(blob_backend.objects("recordings/")) == 1
    assert world.rows("SELECT state FROM audio_analysis") == [("pending",)]
    [seen] = server.seen()
    assert (seen.host, seen.path) == (RETELL_HOST, RETELL_PATH)
    assert world.fetch() is None


def test_vapi_recordings_come_from_its_api_with_the_key_on_its_own_origin_only(
    tmp_path, server, certificates
):
    world = FetchWorld(
        tmp_path,
        "vapi",
        server,
        certificates,
        config={"api_key_ref": "env:VAPI_PRIVATE_KEY"},
        keys={"env:VAPI_PRIVATE_KEY": VAPI_KEY},
    )
    for track in ("customer", "assistant"):
        server.route(
            "api.vapi.ai",
            f"/call/{vapi.CALL_ID}/{track}-recording",
            Reply(302, (("Location", f"https://other.test/{track}?sig=1"),), [b""]),
        )
        server.route("other.test", f"/{track}", _audio())
    world.deliver(vapi._report())

    results = [world.fetch().result, world.fetch().result]

    assert results == ["stored", "stored"]
    assert world.fetch_rows() == [("stored", 0, None, None, None)] * 2
    api = server.seen("api.vapi.ai")
    assert [seen.headers["authorization"] for seen in api] == [f"Bearer {VAPI_KEY}"] * 2
    assert all(
        set(seen.headers) == {"host", "accept", "user-agent"} for seen in server.seen("other.test")
    )


def test_elevenlabs_audio_comes_from_the_conversations_api_with_its_key(
    tmp_path, server, certificates
):
    world = FetchWorld(
        tmp_path,
        "elevenlabs",
        server,
        certificates,
        config={"api_key_ref": "env:ELEVENLABS_API_KEY"},
        keys={"env:ELEVENLABS_API_KEY": ELEVENLABS_KEY},
    )
    path = f"/v1/convai/conversations/{elevenlabs.CONVERSATION_ID}/audio"
    mp3 = b"ID3" + bytes(997)
    server.route(
        "api.elevenlabs.io", path, Reply(headers=(("Content-Type", "audio/mpeg"),), body=[mp3])
    )
    world.deliver(elevenlabs._transcription())

    assert world.fetch().result == "stored"
    assert world.recording_rows() == [("stored", len(mp3), None, True)]
    [seen] = server.seen()
    assert (seen.host, seen.path, seen.headers["xi-api-key"]) == (
        "api.elevenlabs.io",
        path,
        ELEVENLABS_KEY,
    )


def test_a_provider_id_that_is_not_a_plain_id_is_never_put_in_a_url(tmp_path, server, certificates):
    world = FetchWorld(
        tmp_path,
        "elevenlabs",
        server,
        certificates,
        config={"api_key_ref": "env:ELEVENLABS_API_KEY"},
        keys={"env:ELEVENLABS_API_KEY": ELEVENLABS_KEY},
    )
    world.deliver(elevenlabs._transcription(conversation_id="conv..%2Fadmin?x=1"))

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("failed", FETCH_ID_INVALID)
    assert world.recording_rows() == [("failed", None, None, False)]
    assert server.seen() == []


def test_a_missing_key_is_retried(tmp_path, server, certificates):
    world = FetchWorld(
        tmp_path, "vapi", server, certificates, config={"api_key_ref": "env:VAPI_PRIVATE_KEY"}
    )
    world.deliver(vapi._report())

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("pending", FETCH_KEY_MISSING)
    assert server.seen() == []


def test_a_payload_url_off_the_allowlist_fails_its_recording(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route("fetch.test", "/r.wav", _audio())
    payload = retell._call("call_ended")
    payload["call"]["recording_url"] = "https://fetch.test/r.wav?sig=CANARY"
    world.deliver(payload)

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("failed", FETCH_HOST_NOT_ALLOWED)
    assert world.fetch_rows() == [("failed", 0, FETCH_HOST_NOT_ALLOWED, None, None)]
    assert world.recording_rows() == [("failed", None, None, False)]
    assert server.seen() == []


def test_a_body_that_is_not_its_declared_format_is_not_stored(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route(RETELL_HOST, RETELL_PATH, Reply(body=[b"<html>not a wav</html>" * 4]))
    world.deliver(_retell())

    assert world.fetch().error_code == FETCH_NOT_AUDIO
    assert world.recording_rows() == [("failed", None, None, False)]
    assert world.rows("SELECT count(*) FROM pending_blob_deletions") == [(0,)]
    assert list((tmp_path / "blobs").rglob("*.wav")) == []


# --- retries, the deadline and late audio --------------------------------------------------


def test_audio_that_lands_after_the_deadline_becomes_r2(tmp_path, server, certificates):
    """The hold ends at the pending deadline (600 s), so r1 closes without audio;
    the fetch keeps its 1/4/16-minute retries, and its late success is r2."""

    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route(RETELL_HOST, RETELL_PATH, _statuses(503, 503, 503))
    world.deliver(_retell())

    assert world.fetch().result == "pending"  # +0: the first attempt
    world.clock.advance(END_GRACE + SECOND)
    world.close_due()
    assert world.revisions() == []  # held: its recording is still being fetched

    world.clock.advance(MINUTE - END_GRACE - SECOND)
    assert world.fetch().result == "pending"  # +1 min
    world.clock.advance(4 * MINUTE)
    assert world.fetch().result == "pending"  # +5 min: the next one is at +21 min

    world.clock.advance(5 * MINUTE + SECOND)  # past the 600 s deadline
    assert world.recordings.fail_stale_uploads() == 0
    world.close_due()
    assert world.revisions() == [(1, "end_signal", "final", 0)]
    assert world.recording_rows() == [("pending", None, None, False)]

    world.clock.advance(11 * MINUTE)
    assert world.fetch().result == "stored"  # +21 min
    world.clock.advance(LATE_DEBOUNCE + SECOND)
    world.close_due()

    assert world.revisions() == [(1, "end_signal", "final", 0), (2, "late_data", "final", 1)]
    assert world.fetch_rows() == [("stored", 3, None, None, None)]


def test_a_fetch_that_never_succeeds_fails_its_recording_with_a_code(
    tmp_path, server, certificates
):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route(RETELL_HOST, RETELL_PATH, Reply(503, body=[b"down"]))
    world.deliver(_retell())

    results = []
    for wait in (0, 1, 4, 16):
        world.clock.advance(wait * MINUTE)
        if wait == 16:
            assert world.recordings.fail_stale_uploads() == 0
        results.append(world.fetch().result)

    assert results == ["pending", "pending", "pending", "failed"]
    assert world.fetch_rows() == [("failed", 4, FETCH_UPSTREAM_UNAVAILABLE, None, None)]
    assert world.recording_rows() == [("failed", None, None, False)]
    assert len(server.seen()) == 4


def test_the_sweep_never_fails_a_fetch_owned_recording(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    world.deliver(_retell())
    world.recordings.create(
        "support",
        RecordingManifest(
            recording_id="host-upload",
            session_key="host-call",
            speaker="mixed",
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )
    world.clock.advance(11 * MINUTE)

    assert world.recordings.fail_stale_uploads() == 1
    assert world.rows("SELECT recording_id, state FROM recordings ORDER BY state") == [
        ("host-upload", "failed"),
        (world.rows("SELECT recording_id FROM recording_fetches")[0][0], "pending"),
    ]


def test_a_payload_url_that_no_longer_serves_its_recording_expires(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route(RETELL_HOST, RETELL_PATH, Reply(404, body=[b"gone"]))
    world.deliver(_retell())

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("expired", FETCH_GONE)
    assert world.fetch_rows() == [("expired", 0, FETCH_GONE, None, None)]
    assert world.recording_rows() == [("failed", None, None, False)]


def test_a_provider_api_that_does_not_have_the_recording_yet_is_asked_again(
    tmp_path, server, certificates
):
    world = FetchWorld(
        tmp_path,
        "elevenlabs",
        server,
        certificates,
        config={"api_key_ref": "env:ELEVENLABS_API_KEY"},
        keys={"env:ELEVENLABS_API_KEY": ELEVENLABS_KEY},
    )
    world.deliver(elevenlabs._transcription())

    assert world.fetch().result == "pending"  # 404: not there yet
    assert world.fetch_rows()[0][:3] == ("pending", 1, FETCH_GONE)


def test_a_presigned_url_is_not_used_past_its_expiry(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "vapi", server, certificates)
    world.deliver(vapi._report())
    expires = world.fetch_rows()[0][4]
    world.clock.advance(expires + SECOND - world.clock.unix_nano())

    outcomes = [world.fetch(), world.fetch()]

    assert {(outcome.result, outcome.error_code) for outcome in outcomes} == {
        ("expired", FETCH_URL_EXPIRED)
    }
    assert world.fetch_rows() == [("expired", 0, FETCH_URL_EXPIRED, None, expires)] * 2
    assert server.seen() == []


def test_a_retry_is_never_scheduled_past_the_urls_expiry(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "vapi", server, certificates)
    for track in ("customer", "assistant"):
        server.route("storage.vapi.ai", f"/6f1c2b9e-{track}.wav", Reply(503, body=[b"x"]))
    world.deliver(vapi._report())
    expires = world.fetch_rows()[0][4]
    world.clock.advance(expires - 30 * SECOND - world.clock.unix_nano())

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("expired", FETCH_URL_EXPIRED)


# --- presets, purge and the URL's lifetime --------------------------------------------------


def test_metadata_only_schedules_no_fetch(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates, preset="metadata_only")
    server.route(RETELL_HOST, RETELL_PATH, _audio())

    world.deliver(_retell())

    assert world.rows("SELECT count(*) FROM recordings") == [(0,)]
    assert world.fetch_rows() == []
    assert world.fetch() is None
    assert server.seen() == []


def test_a_preset_change_before_the_upload_fails_the_fetch(tmp_path, server, certificates):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    server.route(RETELL_HOST, RETELL_PATH, _audio())
    world.deliver(_retell())
    world.store.set_capture_preset("support", "metadata_only")

    outcome = world.fetch()

    assert (outcome.result, outcome.error_code) == ("failed", FETCH_PRESET_FORBIDS_MEDIA)
    assert world.fetch_rows() == [("failed", 0, FETCH_PRESET_FORBIDS_MEDIA, None, None)]
    assert world.rows("SELECT count(*) FROM pending_blob_deletions") == [(0,)]


def test_a_call_purged_during_its_fetch_leaves_no_blob(
    tmp_path, server, certificates, blob_backend
):
    world = FetchWorld(tmp_path, "retell", server, certificates, blobs=blob_backend.store)
    hold = threading.Event()
    reply = _audio(hold=hold)
    server.route(RETELL_HOST, RETELL_PATH, reply)
    call_id = world.deliver(_retell()).call_id
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(world.fetch()))
    worker.start()
    try:
        assert reply.held.wait(10)
        deadline = time.monotonic() + 10
        while world.rows("SELECT count(*) FROM pending_blob_deletions") != [(1,)]:
            assert time.monotonic() < deadline, "the upload attempt never began"
            time.sleep(0.01)
        CallPurger(world.store, calls=world.calls, recordings=world.recordings).purge_call(
            "support", call_id
        )
    finally:
        hold.set()
        worker.join(timeout=30)

    assert [outcome.result for outcome in outcomes] == ["lost"]
    assert world.rows("SELECT count(*) FROM recordings") == [(0,)]
    assert world.fetch_rows() == []
    assert world.rows("SELECT count(*) FROM pending_blob_deletions") == [(0,)]
    assert blob_backend.objects("recordings/") == []
    assert blob_backend.leftovers() == []


def test_no_url_or_key_outlives_its_fetch(tmp_path, server, certificates, caplog):
    canary = "X-Amz-Signature=CANARY0123456789abcdef"
    world = FetchWorld(tmp_path, "vapi", server, certificates)
    server.route("storage.vapi.ai", "/6f1c2b9e-customer.wav", _audio())
    server.route("storage.vapi.ai", "/6f1c2b9e-assistant.wav", Reply(404, body=[b"gone"]))
    world.deliver(vapi._report(signature=canary))
    keyed = FetchWorld(
        tmp_path / "keyed",
        "elevenlabs",
        server,
        certificates,
        config={"api_key_ref": "env:ELEVENLABS_API_KEY"},
        keys={"env:ELEVENLABS_API_KEY": ELEVENLABS_KEY},
    )
    keyed.deliver(elevenlabs._transcription())

    with caplog.at_level(logging.DEBUG):
        results = sorted(world.fetch().result for _ in range(2))
        keyed_result = keyed.fetch().result

    assert (results, keyed_result) == (["expired", "stored"], "pending")
    assert world.rows("SELECT url FROM recording_fetches") == [(None,), (None,)]
    assert canary not in caplog.text and ELEVENLABS_KEY not in caplog.text
    assert "storage.vapi.ai" in caplog.text  # the host, and only the host
    needles = [canary.encode(), ELEVENLABS_KEY.encode()]
    for each in (world, keyed):
        each.store.close()
    if current_dialect() == "sqlite":
        for path in (tmp_path, tmp_path / "keyed"):
            # What a checkpoint leaves: old WAL frames are history, not the catalog.
            with contextlib.closing(sqlite3.connect(path / "earshot.sqlite3")) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert stored_needles(path, needles) == set()


def test_a_stopping_server_stops_its_fetch_and_the_next_start_runs_it_again(
    tmp_path, server, certificates
):
    world = FetchWorld(tmp_path, "retell", server, certificates)
    hold = threading.Event()
    first = _audio(hold=hold)
    replies = [first]
    server.route(RETELL_HOST, RETELL_PATH, lambda: replies.pop(0) if replies else _audio())
    world.deliver(_retell())

    async def serve(runner: FetchRunner, *, stop_when_held: bool) -> None:
        job = fetch_job(runner)
        await job(world.clock.unix_nano())
        if stop_when_held:
            assert await asyncio.to_thread(first.held.wait, 10)
        else:
            deadline = time.monotonic() + 10
            while world.recording_rows()[0][0] != "stored" and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        await job.aclose()

    asyncio.run(serve(world.runner, stop_when_held=True))
    hold.set()
    assert world.fetch_rows()[0][:2] == ("running", 0)
    assert world.recording_rows() == [("pending", None, None, False)]
    assert world.rows("SELECT count(*) FROM pending_blob_deletions") == [(0,)]

    asyncio.run(serve(world.new_runner(), stop_when_held=False))

    assert world.recording_rows() == [("stored", len(AUDIO), 500, True)]
    assert world.fetch_rows() == [("stored", 0, None, None, None)]


# --- configuration ----------------------------------------------------------------------------


def test_payload_hosts_are_seeded_per_provider_and_extended_by_the_operator():
    allowlists = payload_allowlists("ringg:recordings.ringg.example, retell:*.cdn.retell.test")

    assert allowlists["vapi"].entries == PAYLOAD_HOSTS["vapi"]
    assert allowlists["retell"].allows(RETELL_HOST)
    assert allowlists["retell"].allows("a.cdn.retell.test")
    assert allowlists["ringg"].allows("recordings.ringg.example")
    assert not allowlists["elevenlabs"].allows("recordings.ringg.example")
    with pytest.raises(ValueError, match="EARSHOT_FETCH_ALLOWED_HOSTS"):
        payload_allowlists("retell:*.com")
    with pytest.raises(ValueError, match="EARSHOT_FETCH_ALLOWED_HOSTS"):
        payload_allowlists("nobody:a.example.com")
