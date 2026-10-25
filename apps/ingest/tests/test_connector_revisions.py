"""Two deliveries of one call, under a controlled clock: which revisions they make.

Retell sends ``call_ended`` then ``call_analyzed``; ElevenLabs may send a call's
transcript twice (plain and OTel). The spans the events share are byte-identical,
so the second delivery adds only what is new to it; ``span_conflicts`` stays 0 in
every order. The closer is the real one; the fetch job is not built yet, so a
finished fetch is the recordings store's own upload, as the job will do it.
"""

from __future__ import annotations

import pytest

import test_elevenlabs_connector as elevenlabs
import test_retell_connector as retell
from connector_support import SECOND, ConnectorWorld, connector_world
from earshot.blobstore import put_stream
from earshot.storage_closer import Closer

pytestmark = pytest.mark.integration

END_GRACE = 10 * SECOND
LATE_DEBOUNCE = 30 * SECOND


def _closer(world: ConnectorWorld) -> Closer:
    return Closer(world.store, calls=world.calls, recordings=world.recordings)


def _close_due(world: ConnectorWorld) -> None:
    closer = _closer(world)
    while closer.close_next() is not None:
        pass


def _revisions(world: ConnectorWorld) -> list[tuple]:
    return world.rows(
        "SELECT revision, close_reason, finality, span_count, recording_count "
        "FROM call_revisions ORDER BY revision"
    )


def _finish_fetch(world: ConnectorWorld) -> None:
    [(recording_id,)] = world.rows("SELECT recording_id FROM recordings")
    attempt = world.recordings.begin_upload("support", recording_id)
    try:
        sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [b"RIFF" * 256])
        world.recordings.finish_upload(
            "support", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
    finally:
        world.recordings.end_upload("support", attempt.blob_key)


def _spans(world: ConnectorWorld) -> int:
    return world.rows("SELECT count(*) FROM spans")[0][0]


@pytest.fixture
def world(tmp_path) -> ConnectorWorld:
    return connector_world(tmp_path, "retell")


def test_call_ended_closes_r1_with_its_audio_and_call_analyzed_is_r2(world) -> None:
    world.deliver(retell._call("call_ended"))
    world.clock.advance(END_GRACE + SECOND)
    _close_due(world)
    assert _revisions(world) == []  # held: the recording is still being fetched

    _finish_fetch(world)
    world.clock.advance(END_GRACE + SECOND)
    _close_due(world)
    ended_spans = _spans(world)

    world.clock.advance(60 * SECOND)
    world.deliver(retell._call("call_analyzed"))
    world.clock.advance(LATE_DEBOUNCE + SECOND)
    _close_due(world)

    assert _revisions(world) == [
        (1, "end_signal", "final", ended_spans, 1),
        (2, "late_data", "final", _spans(world), 1),
    ]
    assert _spans(world) - ended_spans == 8  # analysis, cost, 3 latency series, 3 turns
    assert world.counters()["span_conflicts"] == 0


def test_call_analyzed_before_the_claim_is_folded_into_r1(world) -> None:
    world.deliver(retell._call("call_ended"))
    world.clock.advance(2 * SECOND)
    world.deliver(retell._call("call_analyzed"))
    _finish_fetch(world)
    world.clock.advance(END_GRACE + SECOND)
    _close_due(world)

    assert _revisions(world) == [(1, "end_signal", "final", _spans(world), 1)]
    assert world.counters()["span_conflicts"] == 0


def test_call_analyzed_first_leaves_call_ended_nothing_new(world) -> None:
    world.deliver(retell._call("call_analyzed"))
    _finish_fetch(world)
    world.clock.advance(END_GRACE + SECOND)
    _close_due(world)
    stored = _spans(world)

    world.clock.advance(5 * SECOND)
    world.deliver(retell._call("call_ended"))
    world.clock.advance(LATE_DEBOUNCE + END_GRACE)
    _close_due(world)

    assert _spans(world) == stored
    assert _revisions(world) == [(1, "end_signal", "final", stored, 1)]
    assert world.counters()["span_conflicts"] == 0


@pytest.mark.parametrize("second_before_the_claim", [True, False])
def test_both_elevenlabs_transcription_events_make_one_revision(
    tmp_path, second_before_the_claim
) -> None:
    world = connector_world(tmp_path, "elevenlabs")
    otel = elevenlabs._event("post_call_transcription_otel", dict(elevenlabs.OTEL))

    world.deliver(elevenlabs._transcription())
    if second_before_the_claim:
        world.deliver(otel)
    world.clock.advance(END_GRACE + SECOND)
    _close_due(world)
    if not second_before_the_claim:
        world.deliver(otel)
        world.clock.advance(LATE_DEBOUNCE + END_GRACE)
        _close_due(world)

    assert _revisions(world) == [(1, "end_signal", "final", _spans(world), 0)]
    assert world.counters()["span_conflicts"] == 0
