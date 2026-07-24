"""The uploader forwards durable frames and fails open, never onto the voice path."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from earshot.api import ApiConfig
from earshot.checkpoint import CheckpointConfig, CheckpointUploader, CheckpointWriter
from earshot.checkpoint.framing import FRAME_OVERHEAD, encode_frame
from earshot.checkpoint.limits import (
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_FRAME_BYTES,
    MAX_CHECKPOINT_BATCH_BYTES,
    MAX_CHECKPOINT_FRAME_BODY_BYTES,
    MAX_CHECKPOINT_FRAME_BYTES,
)
from earshot.checkpoint.records import JournalRecordEntry, encode_entry
from earshot.checkpoint.uploader import CHECKPOINT_MEDIA_TYPE, DIAGNOSTIC_FRAME_UNDELIVERABLE
from earshot.live import LiveConfig, LiveSessionRegistry
from earshot.recorder import IncidentRecorder

pytestmark = pytest.mark.unit


def _writer(directory: Path, **kwargs) -> CheckpointWriter:
    return CheckpointWriter(CheckpointConfig(checkpoint_dir=directory, **kwargs))


def _journal(directory: Path) -> Path:
    return next(directory.glob("*.eck"))


class _Sink:
    """Stand-in for the backend: records batches and can be told to refuse."""

    def __init__(self) -> None:
        self.batches: list[bytes] = []
        self.registry = LiveSessionRegistry()
        self.fail = False

    def install(self, uploader: CheckpointUploader, session_id: str) -> None:
        def post(batch: bytes) -> None:
            if self.fail:
                raise OSError("backend refused")
            self.batches.append(batch)
            self.registry.accept_frames(session_id, batch, project_id="p")

        uploader._post = post  # type: ignore[method-assign]


def test_uploaded_batches_reassemble_into_the_same_live_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    recorder.add_participant("caller", role="caller")
    uploader = CheckpointUploader("http://127.0.0.1:9/", _journal(tmp_path), "s-1")
    sink = _Sink()
    sink.install(uploader, "s-1")

    assert uploader.flush() is True
    recorder.record_event("earshot.turn.start", turn_id="turn-1")
    assert uploader.flush() is True

    summary = sink.registry.summary("s-1", project_id="p")
    assert summary.bundle_id == "b-1"
    assert summary.last_sequence == 3
    assert uploader.status().batches == 2
    writer.release()


def test_a_partially_written_frame_waits_for_the_next_pass(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    recorder.record_event("earshot.turn.start", turn_id="turn-0")
    path = _journal(tmp_path)
    complete = path.read_bytes()
    path.write_bytes(complete[:-3])

    uploader = CheckpointUploader("http://127.0.0.1:9/", path, "s-1")
    sink = _Sink()
    sink.install(uploader, "s-1")
    assert uploader.flush() is True
    partial = sum(len(batch) for batch in sink.batches)

    path.write_bytes(complete)
    assert uploader.flush() is True
    assert partial + len(sink.batches[-1]) == len(complete)
    writer.release()


def test_a_backend_failure_degrades_to_local_journal_only(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    uploader = CheckpointUploader("http://127.0.0.1:9/", _journal(tmp_path), "s-1")
    sink = _Sink()
    sink.install(uploader, "s-1")
    sink.fail = True

    assert uploader.flush() is False
    status = uploader.status()
    assert status.state == "degraded"
    assert status.last_failure == "OSError"
    # A degraded uploader stays degraded rather than retrying into a hot loop.
    sink.fail = False
    assert uploader.flush() is False
    writer.release()


def test_the_batch_is_bounded_by_bytes_and_cut_at_a_frame(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    for index in range(8):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")
    uploader = CheckpointUploader(
        "http://127.0.0.1:9/", _journal(tmp_path), "s-1", max_batch_bytes=64
    )
    sink = _Sink()
    sink.install(uploader, "s-1")

    while uploader.flush():
        pass

    assert len(sink.batches) > 1
    assert sum(len(batch) for batch in sink.batches) == _journal(tmp_path).stat().st_size
    assert sink.registry.summary("s-1", project_id="p").last_sequence == 9
    writer.release()


# ---------------------------------------------------------- the size ladder


def _oversized_frame(sequence: int) -> bytes:
    """One whole frame the journal permits and the ingest API never will."""

    body = encode_entry(
        JournalRecordEntry(
            kind="event",
            value={"name": "big", "pad": "x" * MAX_CHECKPOINT_BATCH_BYTES},
        )
    )
    return encode_frame(sequence, body, max_body_bytes=DEFAULT_MAX_FRAME_BYTES)


def test_the_checkpoint_size_ladder_is_decided_in_one_place() -> None:
    """Every bound on the upload path derives from ``checkpoint.limits``.

    They were independently chosen and mutually incompatible: a 32 MiB journal
    frame, a 512 KiB batching bound that could still emit one whole frame above
    it, and a 1 MiB ingest body. One legitimately large frame could therefore
    never be delivered at all.
    """

    assert MAX_CHECKPOINT_FRAME_BYTES == MAX_CHECKPOINT_BATCH_BYTES
    assert MAX_CHECKPOINT_FRAME_BODY_BYTES + FRAME_OVERHEAD == MAX_CHECKPOINT_FRAME_BYTES
    # A batching bound is a cadence, never a licence to exceed the wire bound.
    assert DEFAULT_MAX_BATCH_BYTES <= MAX_CHECKPOINT_BATCH_BYTES
    # The local journal keeps the larger frame; durability is not capped by a transport.
    assert MAX_CHECKPOINT_FRAME_BYTES < DEFAULT_MAX_FRAME_BYTES
    # And the two ends of the wire read the same numbers.
    assert ApiConfig().max_checkpoint_body_bytes == MAX_CHECKPOINT_BATCH_BYTES
    assert LiveConfig().max_frame_bytes == MAX_CHECKPOINT_FRAME_BODY_BYTES
    with pytest.raises(ValueError, match="max_checkpoint_body_bytes"):
        ApiConfig(max_checkpoint_body_bytes=MAX_CHECKPOINT_FRAME_BYTES - 1)


def test_a_batch_never_exceeds_what_the_ingest_api_accepts(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    recorder.record_event("earshot.turn.start", turn_id="turn-0")
    path = _journal(tmp_path)
    with path.open("ab") as handle:
        handle.write(_oversized_frame(3))

    posted: list[int] = []
    uploader = CheckpointUploader("http://127.0.0.1:9/", path, "s-1")
    uploader._post = lambda batch: posted.append(len(batch))  # type: ignore[method-assign]
    while uploader.flush():
        pass

    assert posted
    assert max(posted) <= MAX_CHECKPOINT_BATCH_BYTES
    writer.release()


def test_a_frame_too_large_to_deliver_is_declared_and_terminal(tmp_path: Path) -> None:
    """An undeliverable frame ends remote coverage loudly, never silently.

    It cannot be skipped — the server refuses a batch that skips a sequence, and
    a client must never decide where the server's evidence stops — so the
    uploader states which frame it is, emits its own diagnostic, and stops. The
    local journal still holds every record.
    """

    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    recorder.record_event("earshot.turn.start", turn_id="turn-0")
    path = _journal(tmp_path)
    oversized = _oversized_frame(3)
    with path.open("ab") as handle:
        handle.write(oversized)

    diagnostics: list[str] = []
    posted: list[bytes] = []
    uploader = CheckpointUploader(
        "http://127.0.0.1:9/",
        path,
        "s-1",
        diagnostic=lambda item: diagnostics.append(item.code),
    )
    uploader._post = posted.append  # type: ignore[method-assign]

    assert uploader.flush() is True  # everything before it is still delivered
    assert uploader.flush() is False
    status = uploader.status()
    assert status.state == "undeliverable_frame"
    assert status.undeliverable_sequence == 3
    assert status.undeliverable_bytes == len(oversized)
    assert status.last_failure == "CheckpointFrameUndeliverable"
    assert diagnostics == [DIAGNOSTIC_FRAME_UNDELIVERABLE]

    # Terminal and bounded: no retry, no further post, no growth.
    assert uploader.flush() is False
    assert len(posted) == 1
    assert diagnostics == [DIAGNOSTIC_FRAME_UNDELIVERABLE]
    assert uploader.status().batches == 1
    writer.release()


def test_a_deliverable_frame_above_the_batching_bound_is_still_sent(tmp_path: Path) -> None:
    """The batching bound cuts a batch; it never strands a deliverable frame."""

    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    recorder.record_event("earshot.turn.start", turn_id="turn-0")
    path = _journal(tmp_path)
    body = encode_entry(
        JournalRecordEntry(kind="event", value={"name": "big", "pad": "x" * (200 * 1024)})
    )
    frame = encode_frame(3, body, max_body_bytes=DEFAULT_MAX_FRAME_BYTES)
    with path.open("ab") as handle:
        handle.write(frame)

    posted: list[bytes] = []
    uploader = CheckpointUploader("http://127.0.0.1:9/", path, "s-1", max_batch_bytes=64 * 1024)
    uploader._post = posted.append  # type: ignore[method-assign]
    while uploader.flush():
        pass

    assert uploader.status().state == "ready"
    assert b"".join(posted) == path.read_bytes()
    assert max(len(batch) for batch in posted) <= MAX_CHECKPOINT_BATCH_BYTES
    writer.release()


def test_a_batch_budget_above_the_wire_bound_is_refused(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    with pytest.raises(ValueError, match="max_batch_bytes"):
        CheckpointUploader(
            "http://127.0.0.1:9/",
            _journal(tmp_path),
            "s-1",
            max_batch_bytes=MAX_CHECKPOINT_BATCH_BYTES + 1,
        )
    writer.release()


def test_the_uploader_never_runs_on_the_calling_thread(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    uploader = CheckpointUploader(
        "http://127.0.0.1:9/", _journal(tmp_path), "s-1", batch_interval_ms=10
    )
    threads: list[str] = []

    def post(batch: bytes) -> None:
        threads.append(threading.current_thread().name)

    uploader._post = post  # type: ignore[method-assign]
    uploader.start()
    deadline = time.monotonic() + 2
    while not threads and time.monotonic() < deadline:
        time.sleep(0.01)
    uploader.close(drain=False)

    assert threads
    assert all(name == "earshot-checkpoint-upload" for name in threads)
    assert threading.current_thread().name not in threads
    writer.release()


def test_a_plaintext_remote_endpoint_is_refused(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    with pytest.raises(ValueError, match="HTTPS"):
        CheckpointUploader("http://collector.example", _journal(tmp_path), "s-1")
    with pytest.raises(ValueError, match="userinfo"):
        CheckpointUploader("https://a:b@collector.example", _journal(tmp_path), "s-1")
    writer.release()


def test_the_upload_endpoint_is_derived_from_the_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s/1", bundle_id="b-1", checkpoint=writer)
    uploader = CheckpointUploader("http://127.0.0.1:4319/v1/live", _journal(tmp_path), "s/1")
    assert uploader.endpoint == "http://127.0.0.1:4319/v1/live/sessions/s%2F1/checkpoints"
    assert CHECKPOINT_MEDIA_TYPE == "application/vnd.earshot.checkpoint+frames"
    writer.release()
