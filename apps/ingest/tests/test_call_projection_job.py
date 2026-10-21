"""The call projection job on a real catalog (FR-3, FR-4, FR-10; M4, M5, M7, M25, M26).

A call is built the way a server builds it: the real captures recorded through
``CallStore.record_spans``, its recordings stored in a real blob store, the call
closed by the real ``Closer``, each recording analysed by the real analysis job in
a child process, then projected by ``CallProjector``. Every test runs on the
dialect under test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("soundfile")

from captured_livekit_otlp import (
    LIVEKIT_SESSION_KEY,
    livekit_manifest,
    livekit_requests,
)
from captured_otlp import CAPTURED_SESSION_KEY, captured_manifest, captured_requests
from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.call_projection import CALL_PROJECTION_VERSION
from earshot.clock import ManualClock
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.storage_analysis import AnalysisRunner, AnalysisStore
from earshot.storage_call_projections import CallProjector, projection_job
from earshot.storage_call_purge import CallPurger
from earshot.storage_call_reads import CallFilters, CallReads
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_prices import NewPrice, PriceStore
from earshot.storage_recordings import RecordingManifest, RecordingStore
from store_support import catalog, catalog_rows, make_store, stored_needles

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
LIVEKIT_AUDIO = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"
SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


class World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = ManualClock(wall=START, monotonic=START)
        self.store = make_store(path, clock=self.clock)
        self.calls = CallStore(self.store)
        self.blobs = LocalBlobStore(path / "blobs")
        self.recordings = RecordingStore(self.store, calls=self.calls, blobs=self.blobs)
        self.closer = Closer(self.store, calls=self.calls, recordings=self.recordings)
        self.runner = AnalysisRunner(
            AnalysisStore(self.store), blobs=self.blobs, spool_root=path / "analysis-tmp"
        )
        self.projector = CallProjector(self.store)
        self.job = projection_job(self.projector)
        self.reads = CallReads(self.store)

    def replay(self, requests, *, project_id: str = "default") -> str:
        call_ids: set[str] = set()
        for item in requests:
            call_ids.update(self.calls.record_spans(project_id, spans_from_request(item)).call_ids)
            self.clock.advance(5 * SECOND)
        [call_id] = call_ids
        return call_id

    def store_recording(
        self, recording_id: str, body: bytes, manifest: dict, *, duration_ms: int
    ) -> None:
        self.recordings.create("default", RecordingManifest(recording_id=recording_id, **manifest))
        attempt = self.recordings.begin_upload("default", recording_id)
        try:
            sha256, size = put_stream(self.blobs, attempt.blob_key, [body])
            self.recordings.finish_upload(
                "default",
                recording_id,
                attempt.blob_key,
                sha256=sha256,
                size=size,
                duration_ms=duration_ms,
            )
        finally:
            self.recordings.end_upload("default", attempt.blob_key)

    def close(self) -> None:
        self.clock.advance(CallTimings().long_quiet_nano)
        while self.closer.close_next() is not None:
            pass

    def analyse(self) -> None:
        while self.runner.run_next() is not None:
            pass

    def project(self) -> int:
        """Run the server's projection job until it has nothing left; the calls it
        wrote a projection for."""

        before = self.written()
        while asyncio.run(self.job(self.clock.unix_nano())) is not None:
            pass
        return len(self.written() - before)

    def written(self) -> set[tuple]:
        return set(
            catalog_rows(
                self.path, "SELECT call_id, input_digest, source_seq FROM call_projections"
            )
        )

    def projection(self, call_id: str) -> dict:
        detail = self.reads.get_call("default", call_id)
        assert detail is not None
        return {
            "state": detail.summary.projection_state,
            "summary": detail.summary.projection,
            "detail": detail.projection_detail,
        }


def _pipecat_world(path: Path) -> tuple[World, str]:
    world = World(path)
    call_id = world.replay(captured_requests())
    started = captured_manifest()["audio"]["started_at_unix_nano"]
    for speaker in ("user", "agent"):
        world.store_recording(
            f"conv.{speaker}",
            (PIPECAT_AUDIO / f"{speaker}.wav").read_bytes(),
            {
                "session_key": CAPTURED_SESSION_KEY,
                "speaker": speaker,
                "content_type": "audio/wav",
                "started_at_unix_nano": started,
            },
            duration_ms=32_486,
        )
    world.close()
    return world, call_id


def _gaps(world: World, call_id: str) -> list[tuple]:
    turns = world.projection(call_id)["detail"]["turns"]
    return [
        (
            turn["audible_gap"]["status"],
            turn["audible_gap"]["value_ms"],
            turn["audible_gap"]["bound_ms"],
        )
        for turn in turns
    ]


# --- the job end to end ------------------------------------------------------------------------


def test_a_closed_call_is_projected_once_its_analyses_are_stored(tmp_path) -> None:
    world, call_id = _pipecat_world(tmp_path)

    assert world.projection(call_id)["state"] == "none"
    assert world.project() == 1  # projected before its analyses: not measurable yet
    early = world.projection(call_id)
    world.analyse()  # each stored result bumps the call's projection_seq
    stale = world.projection(call_id)["state"]
    assert world.project() == 1
    projected = world.projection(call_id)

    assert early["state"] == "current"
    assert {turn["audible_gap"]["reason"] for turn in early["detail"]["turns"]} == {
        "analysis_not_stored"
    }
    assert early["summary"].analysis_state == "pending"
    assert stale == "stale"
    assert projected["state"] == "current"
    assert _gaps(world, call_id) == [
        ("not_measurable", None, 3080),
        ("measured", 1510, None),
        ("measured", 1490, None),
    ]
    summary = projected["summary"]
    assert (summary.analysis_state, summary.measurable_turns, summary.framework) == (
        "stored",
        2,
        "pipecat",
    )
    assert summary.cost_unknown_items == 2 and summary.cost_lower_bound_nano_usd > 0
    [media] = projected["detail"]["media"]
    assert (media["source"], media["peaks_path"]) == (
        "rendition",
        "/v1/recordings/conv.user/rendition/peaks",
    )
    # The built-ins are seeded once, by the job.
    assert catalog_rows(tmp_path, "SELECT count(*) FROM prices") == [(len(BUILTIN_PRICES),)]
    assert catalog_rows(tmp_path, "SELECT version FROM builtin_price_version") == [(1,)]
    assert world.project() == 0


def test_a_livekit_call_with_its_stereo_ogg_is_projected_with_measured_turns(tmp_path) -> None:
    world = World(tmp_path)
    call_id = world.replay(livekit_requests())
    audio = livekit_manifest()["audio"]
    world.store_recording(
        "lk.stereo",
        LIVEKIT_AUDIO.read_bytes(),
        {
            "session_key": LIVEKIT_SESSION_KEY,
            "speaker": "mixed",
            "channel_speakers": ("user", "agent"),
            "content_type": "audio/ogg",
            "started_at_unix_nano": audio["started_at_unix_nano"],
        },
        duration_ms=34_004,
    )
    world.close()
    world.analyse()
    world.project()

    assert _gaps(world, call_id) == [
        ("measured", 1290, None),
        ("measured", 1660, None),
        ("measured", 1250, None),
    ]
    projection = world.projection(call_id)
    assert projection["summary"].framework == "livekit"
    [media] = projection["detail"]["media"]
    assert (media["source"], media["content_path"], media["channels"]) == (
        "recording",
        "/v1/recordings/lk.stereo/content",
        [{"index": 0, "speaker": "user"}, {"index": 1, "speaker": "agent"}],
    )


# --- staleness and the compare-and-set write (M4) ----------------------------------------------


def test_a_new_revision_makes_a_projection_stale_until_it_is_recomputed(tmp_path) -> None:
    world = World(tmp_path)
    call_id = world.replay(captured_requests())
    world.close()
    world.project()
    [(seq,)] = catalog_rows(tmp_path, "SELECT projection_seq FROM calls")
    # Late data: one more span of the call's trace, then a second revision.
    late = captured_requests()[-1]
    late.resource_spans[0].scope_spans[0].spans[0].span_id = b"\x01" * 8
    world.calls.record_spans("default", spans_from_request(late))
    world.close()

    assert catalog_rows(tmp_path, "SELECT projection_seq, latest_revision FROM calls") == [
        (seq + 1, 2)
    ]
    assert world.projection(call_id)["state"] == "stale"
    world.project()
    assert world.projection(call_id)["state"] == "current"
    assert world.projection(call_id)["summary"].revision == 2


def test_a_projection_computed_while_its_call_changed_is_discarded(tmp_path) -> None:
    world = World(tmp_path)
    call_id = world.replay(captured_requests())
    world.close()
    snapshot = world.projector.snapshot("default", call_id)
    computed = world.projector.compute(snapshot)
    # An analysis result (or a revision) lands while the projection is computed.
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE calls SET projection_seq = projection_seq + 1")

    assert world.projector.write(snapshot, computed) is False
    assert world.projection(call_id)["state"] == "none"
    world.project()
    assert world.projection(call_id)["state"] == "current"


def test_a_call_that_cannot_be_projected_is_skipped_and_the_others_are_not(
    tmp_path, caplog
) -> None:
    world = World(tmp_path)
    broken = world.replay(captured_requests())
    attributes = [("earshot.session.id", "fine")]
    from otlp_support import make_request, make_span, string_attribute

    world.calls.record_spans(
        "default",
        spans_from_request(
            make_request([make_span(9, 1, attributes=[string_attribute(*a) for a in attributes])])
        ),
    )
    fine = world.calls.call_id("default", "fine", 0)
    world.close()
    with catalog(tmp_path) as connection:  # the broken call's stored spans are unreadable
        connection.execute(
            "UPDATE spans SET payload = ? WHERE trace_id IN "
            "(SELECT trace_id FROM call_traces WHERE call_id = ?)",
            (b"\xff", broken),
        )

    world.project()  # returns: the job does not spin on the call it cannot read

    assert world.projection(fine)["state"] == "current"
    assert world.projection(broken)["state"] == "none"
    assert "EARSHOT_PROJECTION_FAILED" in caplog.text
    assert world.projector.run_pending().more is False


# --- prices (M5, M7) ---------------------------------------------------------------------------


def test_a_project_price_reprices_its_calls_from_their_usage_without_reading_spans(
    tmp_path,
) -> None:
    world, call_id = _pipecat_world(tmp_path)
    world.analyse()
    world.project()
    before = world.projection(call_id)["summary"].cost_lower_bound_nano_usd
    # Unreadable spans: only a reprice from the stored usage can succeed now.
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE spans SET payload = ?", (b"\xff",))
    PriceStore(world.store).add(
        "default",
        NewPrice(
            provider="groq",
            model="openai/gpt-oss-20b",
            unit="output_token",
            price_nano_usd=1_000_300,
            per_quantity=1,
            effective_from_unix_nano=0,
            source_url=None,
            note=None,
        ),
    )

    assert world.projection(call_id)["state"] == "stale"
    assert world.project() == 1
    after = world.projection(call_id)
    assert after["state"] == "current"
    assert after["summary"].cost_lower_bound_nano_usd == before + 319 * 1_000_000
    [item] = [item for item in after["detail"]["cost_items"] if item["unit"] == "output_token"]
    assert (item["price_nano_usd"], item["cost_nano_usd"]) == (1_000_300, 319 * 1_000_300)


def test_seeding_the_built_in_prices_is_idempotent(tmp_path) -> None:
    store = make_store(tmp_path)
    prices = PriceStore(store)

    assert prices.seed_builtins(BUILTIN_PRICES) == len(BUILTIN_PRICES)
    assert prices.seed_builtins(BUILTIN_PRICES) == 0
    assert catalog_rows(
        tmp_path, "SELECT count(*), count(DISTINCT note) > 0 FROM prices WHERE project_id IS NULL"
    ) == [(len(BUILTIN_PRICES), True)]
    assert catalog_rows(tmp_path, "SELECT version FROM builtin_price_version") == [(1,)]
    assert not catalog_rows(
        tmp_path,
        "SELECT id FROM prices WHERE note LIKE '%promotional%' AND note NOT LIKE '%not used%'",
    )


# --- transcripts and search (AC3.7, M23, M25, M26) ---------------------------------------------


def test_the_projected_transcript_and_tags_are_searchable(tmp_path) -> None:
    world = World(tmp_path)
    call_id = world.replay(captured_requests())
    world.close()
    world.project()

    rows = catalog_rows(
        tmp_path, "SELECT line_no, speaker, timing FROM call_transcripts ORDER BY line_no"
    )
    found = world.reads.list_calls("default", filters=CallFilters(q="WINDOW"))
    missing = world.reads.list_calls("default", filters=CallFilters(q="nowhere"))

    assert [speaker for _, speaker, _ in rows] == [
        "user",
        "agent",
        "user",
        "user",
        "agent",
        "user",
        "user",
        "agent",
    ]
    assert {timing for _, _, timing in rows} == {"measured"}
    assert [item.call_id for item in found.items] == [call_id]
    assert missing.items == ()


def test_a_metadata_only_call_writes_no_transcript(tmp_path) -> None:
    world = World(tmp_path)
    world.store.set_capture_preset("default", "metadata_only")
    call_id = world.replay(captured_requests())
    world.close()
    world.project()

    assert world.projection(call_id)["state"] == "current"
    assert world.projection(call_id)["detail"]["transcript"] == []
    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_transcripts") == [(0,)]


# --- purge (FR-10, AC10.1, AC10.2) -------------------------------------------------------------


def test_purging_a_projected_call_leaves_no_transcript_text_anywhere(tmp_path) -> None:
    world = World(tmp_path)
    call_id = world.replay(captured_requests())
    world.close()
    world.project()
    needle = b"somewhere quiet near the window"
    assert stored_needles(tmp_path, [needle]) == {needle}

    CallPurger(world.store, calls=world.calls, recordings=world.recordings).purge_call(
        "default", call_id
    )
    world.store.close()

    for table in ("call_projections", "call_transcripts", "call_tags"):
        assert catalog_rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)], table
    assert stored_needles(tmp_path, [needle]) == set()


def test_deleting_a_project_deletes_its_prices_and_never_the_built_ins(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("gone", display_name="Gone")
    prices = PriceStore(store)
    prices.seed_builtins(BUILTIN_PRICES[:2])
    own = NewPrice("groq", "m", "input_token", 1, 1, 0, None, None)
    prices.add("gone", own)
    prices.add("default", own)

    while not store.delete_project_data("gone", batch_size=10):
        pass

    assert catalog_rows(
        tmp_path, "SELECT project_id, count(*) FROM prices GROUP BY project_id ORDER BY project_id"
    ) in ([("default", 1), (None, 2)], [(None, 2), ("default", 1)])


def test_a_projection_row_records_the_versions_it_was_computed_from(tmp_path) -> None:
    world = World(tmp_path)
    world.replay(captured_requests())
    world.close()
    world.project()

    [row] = catalog_rows(
        tmp_path,
        "SELECT p.source_seq = c.projection_seq, p.projection_version, p.builtin_price_version, "
        "length(p.input_digest), p.usage_json IS NOT NULL FROM call_projections p "
        "JOIN calls c ON c.call_id = p.call_id",
    )
    assert row == (True, CALL_PROJECTION_VERSION, 1, 64, True)
    [(usage,)] = catalog_rows(tmp_path, "SELECT usage_json FROM call_projections")
    assert {entry["kind"] for entry in json.loads(usage)} == {
        "reported",
        "estimate",
        "not_reported",
    }
