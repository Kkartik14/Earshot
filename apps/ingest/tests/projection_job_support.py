"""Shared end-to-end call projection world for ingest integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage_analysis import AnalysisRunner, AnalysisStore
from earshot.storage_call_projections import CallProjector, projection_job
from earshot.storage_call_purge import CallPurger
from earshot.storage_call_reads import CallReads
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingManifest, RecordingStore
from store_support import catalog_rows, make_store

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


class World:
    """Exercise the receiver, close, projection and reads against a real catalog."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.clock = ManualClock(wall=START, monotonic=START)
        self.store = make_store(path, clock=self.clock)
        self.calls = CallStore(self.store)
        self.blobs = LocalBlobStore(path / "blobs")
        self.recordings = RecordingStore(self.store, calls=self.calls, blobs=self.blobs)
        self.closer = Closer(self.store, calls=self.calls, recordings=self.recordings)
        self.purger = CallPurger(self.store, calls=self.calls, recordings=self.recordings)
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
        """Run the server projection job until it has nothing left."""

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


def gaps(world: World, call_id: str) -> list[tuple]:
    """The audible-gap fields from every projected turn."""

    turns = world.projection(call_id)["detail"]["turns"]
    return [
        (
            turn["audible_gap"]["status"],
            turn["audible_gap"]["value_ms"],
            turn["audible_gap"]["bound_ms"],
        )
        for turn in turns
    ]
