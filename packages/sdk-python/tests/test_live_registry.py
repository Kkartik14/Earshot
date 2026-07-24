"""The live registry: what it publishes, what it bounds, what it refuses."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from earshot.checkpoint import CheckpointConfig, CheckpointWriter
from earshot.checkpoint.framing import CHECKSUM_SIZE, HEADER_SIZE, encode_frame
from earshot.checkpoint.records import JournalRecordEntry, encode_entry
from earshot.checkpoint.writer import DEFAULT_MAX_FRAME_BYTES
from earshot.live import (
    EVENT_END,
    EVENT_FINALIZE,
    EVENT_OPEN,
    EVENT_OPERATION_OPEN,
    EVENT_OVERFLOW,
    EVENT_RECORD,
    EVENT_REPLAY_TRUNCATED,
    EVENT_RESET,
    EVENT_WITHHELD,
    LIVE_TAIL_DESTINATION,
    UNKNOWN_UNTIL_CLOSE,
    CheckpointDivergedError,
    CheckpointFinalizedError,
    CheckpointFramesInvalidError,
    CheckpointSequenceError,
    LiveCapacityError,
    LiveConfig,
    LiveSessionRegistry,
    SessionNotLiveError,
    SessionNotSealableError,
    TailCapacityError,
    render_sse,
)
from earshot.privacy import CaptureClass, CaptureGovernance, CapturePolicy, ExportConfig
from earshot.recorder import IncidentRecorder, RecorderConfig

pytestmark = pytest.mark.unit

# Transcript content an export policy forbids leaving the process.
SENTINEL = "earshot-restricted-transcript-sentinel"


def _writer(directory: Path, **kwargs) -> CheckpointWriter:
    return CheckpointWriter(CheckpointConfig(checkpoint_dir=directory, **kwargs))


def _journal_path(directory: Path) -> Path:
    return next(directory.glob("*.eck"))


def _record(recorder: IncidentRecorder, count: int = 3) -> None:
    recorder.add_participant("caller", role="caller")
    for index in range(count):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")


def _names(events) -> list[str]:
    return [event.name for event in events]


def _payload(event) -> dict:
    return json.loads(event.payload)


# ------------------------------------------------------------ local journals


def test_a_journal_in_the_directory_becomes_a_live_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder)

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    sessions = registry.sessions(project_id="default")
    assert [item.session_id for item in sessions] == ["s-1"]
    assert sessions[0].bundle_id == "b-1"
    assert sessions[0].state == "live"
    assert sessions[0].close_observed is False
    writer.release()


def test_a_subscriber_replays_the_journal_and_then_follows_it(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    replayed = subscription.drain()
    assert _names(replayed)[0] == EVENT_OPEN
    assert EVENT_RECORD in _names(replayed)

    recorder.record_event("earshot.turn.start", turn_id="turn-late")
    registry.refresh()
    followed = subscription.drain()
    assert _names(followed) == [EVENT_RECORD]
    assert _payload(followed[0])["kind"] == "event"
    writer.release()


def test_the_open_event_enumerates_what_cannot_be_known_yet(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    opened = _payload(registry.subscribe("s-1", project_id="default").drain()[0])

    assert opened["in_progress"] is True
    assert opened["unknown_until_close"] == list(UNKNOWN_UNTIL_CLOSE)
    # The things a live view must never claim.
    for unknown in ("session_status", "session_ended_at", "derived_analysis", "diagnoses"):
        assert unknown in opened["unknown_until_close"]
    writer.release()


def test_an_unfinished_operation_is_its_own_event_with_no_end(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    with recorder.operation("llm", turn_id="turn-0"):
        registry = LiveSessionRegistry(journal_dir=tmp_path)
        registry.refresh()
        events = registry.subscribe("s-1", project_id="default").drain()

    opened = [event for event in events if event.name == EVENT_OPERATION_OPEN]
    assert len(opened) == 1
    value = _payload(opened[0])
    assert value["status"] == "unknown"
    assert value["ended_at"] is None
    assert value["duration_nano"] is None
    assert value["end_observed"] is False
    # It is never published as a completed operation record.
    assert not any(
        event.name == EVENT_RECORD and _payload(event)["kind"] == "operation" for event in events
    )
    writer.release()


def test_close_is_published_as_finalize_without_the_artifact(tmp_path: Path) -> None:
    writer = _writer(tmp_path, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=1)
    recorder.close()

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    events = registry.subscribe("s-1", project_id="default").drain()
    finalize = next(event for event in events if event.name == EVENT_FINALIZE)

    assert _payload(finalize)["artifact_available"] is False
    assert registry.sessions(project_id="default")[0].state == "finalized"


def test_the_stream_never_carries_analysis_or_turn_metrics(tmp_path: Path) -> None:
    writer = _writer(tmp_path, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)
    with recorder.operation("llm", turn_id="turn-0"):
        pass
    recorder.close()

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    events = registry.subscribe("s-1", project_id="default").drain()
    opened, facts = events[0], events[1:]
    rendered = "".join(render_sse(event) for event in facts)

    for forbidden in ("analysis", "diagnos", "p50", "p95", "percentile", "first_token"):
        assert forbidden not in rendered.lower()
    # The only place those words may appear is the header's own list of what
    # this stream structurally cannot answer.
    assert "derived_analysis" in _payload(opened)["unknown_until_close"]
    assert "diagnoses" in _payload(opened)["unknown_until_close"]


def test_a_removed_journal_ends_the_stream(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    subscription.drain()

    writer.release()
    _journal_path(tmp_path).unlink()
    registry.refresh()

    assert subscription.finished
    ended = subscription.terminal()
    assert _names(ended) == [EVENT_END]
    assert _payload(ended[0])["reason"] == "journal_removed"
    assert _payload(ended[0])["close_observed"] is False


def test_a_new_journal_for_the_same_session_resets_subscribers(tmp_path: Path) -> None:
    first = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=first)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    subscription.drain()
    first.release()
    _journal_path(tmp_path).unlink()

    second = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=second)
    registry.refresh()

    delivered = _names(subscription.drain())
    assert EVENT_RESET in delivered or _names(subscription.terminal()) == [EVENT_END]
    second.release()


# ----------------------------------------------------------- resume + bounds


def test_last_event_id_resumes_without_gaps_or_duplicates(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=4)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    first = registry.subscribe("s-1", project_id="default")
    delivered = first.drain()
    first.close()
    seen = [event.sequence for event in delivered if event.sequence]
    resume_id = delivered[-1].event_id

    recorder.record_event("earshot.turn.start", turn_id="turn-late")
    registry.refresh()
    second = registry.subscribe("s-1", project_id="default", last_event_id=resume_id)
    resumed = second.drain()

    assert _names(resumed)[0] != EVENT_OPEN  # already delivered; never repeated
    later = [event.sequence for event in resumed if event.sequence]
    assert seen + later == list(range(1, len(seen) + len(later) + 1))
    writer.release()


def test_a_foreign_last_event_id_resets_before_anything_else(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    events = registry.subscribe(
        "s-1", project_id="default", last_event_id="a-different-journal:9"
    ).drain()

    assert _names(events)[:2] == [EVENT_RESET, EVENT_OPEN]
    assert events[0].event_id is None  # a control event never advances the cursor
    writer.release()


def test_from_live_says_that_earlier_facts_are_not_shown(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=3)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    events = registry.subscribe("s-1", project_id="default", from_spec="live").drain()

    assert _names(events) == [EVENT_OPEN, EVENT_REPLAY_TRUNCATED]
    truncated = _payload(events[1])
    assert truncated["reason"] == "requested_live_only"
    assert truncated["withheld_records"] > 0
    writer.release()


def test_a_rolled_replay_window_is_declared_not_hidden(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=8)
    registry = LiveSessionRegistry(
        journal_dir=tmp_path,
        config=LiveConfig(max_records_per_session=3),
    )
    registry.refresh()

    events = registry.subscribe("s-1", project_id="default").drain()

    truncated = next(event for event in events if event.name == EVENT_REPLAY_TRUNCATED)
    assert _payload(truncated)["reason"] == "replay_window_exceeded"
    assert _payload(truncated)["withheld_records"] > 0
    writer.release()


def test_a_subscriber_that_falls_behind_is_closed_not_silently_trimmed(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(
        journal_dir=tmp_path,
        config=LiveConfig(max_queue_records=2),
    )
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    subscription.drain()

    _record(recorder, count=10)
    registry.refresh()

    delivered = subscription.drain()
    assert len(delivered) == 2
    assert subscription.finished
    overflow = subscription.terminal()
    assert _names(overflow) == [EVENT_OVERFLOW]
    resume = _payload(overflow[0])
    assert resume["resume_with"].endswith(f":{delivered[-1].sequence}")
    writer.release()


def test_tail_capacity_is_bounded_per_server_and_per_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(
        journal_dir=tmp_path,
        config=LiveConfig(max_connections=2, max_subscribers_per_session=1),
    )
    registry.refresh()

    registry.subscribe("s-1", project_id="default")
    with pytest.raises(TailCapacityError):
        registry.subscribe("s-1", project_id="default")
    writer.release()


def test_another_project_cannot_see_a_local_journal_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    assert registry.sessions(project_id="other") == ()
    with pytest.raises(SessionNotLiveError):
        registry.subscribe("s-1", project_id="other")
    writer.release()


def test_an_expired_session_is_dropped_and_says_so(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    now = [1_000.0]
    registry = LiveSessionRegistry(
        journal_dir=tmp_path,
        config=LiveConfig(session_ttl_seconds=5.0),
        clock=lambda: now[0],
    )
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    subscription.drain()

    now[0] += 10.0
    registry.expire()

    assert registry.sessions(project_id="default") == ()
    assert _payload(subscription.terminal()[0])["reason"] == "session_expired"
    writer.release()


# ------------------------------------------------------ uploaded checkpoints


def _frames(path: Path) -> bytes:
    return path.read_bytes()


def _split_frames(payload: bytes) -> list[bytes]:
    """Cut a journal into its individual frames, header lengths only."""

    pieces: list[bytes] = []
    offset = 0
    while offset < len(payload):
        length = int.from_bytes(payload[offset + 5 : offset + 9], "big")
        end = offset + 9 + length + 4
        pieces.append(payload[offset:end])
        offset = end
    return pieces


def test_uploaded_frames_become_the_same_live_session(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "journals")
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)
    payload = _frames(_journal_path(tmp_path / "journals"))

    registry = LiveSessionRegistry()
    accepted = registry.accept_frames("s-1", payload, project_id="p")

    assert accepted.accepted_through > 1
    assert accepted.sealable is True
    events = registry.subscribe("s-1", project_id="p").drain()
    assert _names(events)[0] == EVENT_OPEN
    writer.release()


def test_an_uploaded_batch_continues_the_sequence_or_is_refused(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "journals")
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=1)
    path = _journal_path(tmp_path / "journals")
    head = _frames(path)
    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", head, project_id="p")

    recorder.record_event("earshot.turn.start", turn_id="turn-9")
    recorder.record_event("earshot.turn.start", turn_id="turn-10")
    grown = _frames(path)
    added = _split_frames(grown[len(head) :])
    assert len(added) == 2
    # Skipping the frame that came first is a gap, not a batch.
    with pytest.raises(CheckpointSequenceError):
        registry.accept_frames("s-1", added[1], project_id="p")
    accepted = registry.accept_frames("s-1", b"".join(added), project_id="p")
    assert accepted.accepted_records == 2
    writer.release()


def test_a_torn_upload_is_refused_whole(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "journals")
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)
    payload = _frames(_journal_path(tmp_path / "journals"))

    registry = LiveSessionRegistry()
    with pytest.raises(CheckpointFramesInvalidError):
        registry.accept_frames("s-1", payload[:-3], project_id="p")
    assert registry.sessions(project_id="p") == ()
    writer.release()


def test_an_encrypted_journal_cannot_be_uploaded(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    key = base64.b64encode(bytes(32)).decode("ascii")
    writer = _writer(tmp_path / "journals", checkpoint_key=key)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    payload = _frames(_journal_path(tmp_path / "journals"))

    registry = LiveSessionRegistry()
    with pytest.raises(CheckpointFramesInvalidError):
        registry.accept_frames("s-1", payload, project_id="p")
    writer.release()


def test_a_header_for_another_session_is_refused(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "journals")
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    payload = _frames(_journal_path(tmp_path / "journals"))

    registry = LiveSessionRegistry()
    with pytest.raises(CheckpointFramesInvalidError):
        registry.accept_frames("s-2", payload, project_id="p")
    writer.release()


def test_live_session_quota_is_enforced_per_project(tmp_path: Path) -> None:
    registry = LiveSessionRegistry(config=LiveConfig(max_sessions_per_project=1))
    for index in (1, 2):
        writer = _writer(tmp_path / f"journals-{index}")
        IncidentRecorder(session_id=f"s-{index}", bundle_id=f"b-{index}", checkpoint=writer)
        payload = _frames(_journal_path(tmp_path / f"journals-{index}"))
        if index == 1:
            registry.accept_frames("s-1", payload, project_id="p")
        else:
            with pytest.raises(LiveCapacityError):
                registry.accept_frames("s-2", payload, project_id="p")
        writer.release()


def _uploadable(
    directory: Path,
    session_id: str,
    bundle_id: str,
    *,
    count: int = 2,
    close: bool = False,
    **kwargs,
) -> tuple[CheckpointWriter, bytes]:
    """One journal on disk, and the exact bytes an uploader would forward."""

    writer = _writer(directory, keep_finalized=close, **kwargs)
    recorder = IncidentRecorder(session_id=session_id, bundle_id=bundle_id, checkpoint=writer)
    _record(recorder, count=count)
    if close:
        recorder.close()
    return writer, _frames(_journal_path(directory))


def _entry_frame(sequence: int, entry) -> bytes:
    return encode_frame(sequence, encode_entry(entry), max_body_bytes=DEFAULT_MAX_FRAME_BYTES)


# ------------------------------------------------- per-tenant session identity


def test_two_projects_hold_the_same_session_id_independently(tmp_path: Path) -> None:
    """A session id is a tenant's name for its own session, not a global one.

    Keyed by session id alone, the first tenant to use an id owns it: a second
    tenant's upload is refused as "not live" forever, which is both a denial of
    service and a cross-tenant coupling of identity.
    """

    alpha_writer, alpha = _uploadable(tmp_path / "alpha", "s-1", "b-alpha")
    beta_writer, beta = _uploadable(tmp_path / "beta", "s-1", "b-beta")

    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", alpha, project_id="alpha")
    accepted = registry.accept_frames("s-1", beta, project_id="beta")

    assert accepted.accepted_through > 1
    assert registry.summary("s-1", project_id="alpha").bundle_id == "b-alpha"
    assert registry.summary("s-1", project_id="beta").bundle_id == "b-beta"
    assert [item.bundle_id for item in registry.sessions(project_id="alpha")] == ["b-alpha"]
    assert [item.bundle_id for item in registry.sessions(project_id="beta")] == ["b-beta"]
    # Each tenant tails its own journal, never the other's.
    opened = _payload(registry.subscribe("s-1", project_id="beta").drain()[0])
    assert opened["bundle_id"] == "b-beta"
    alpha_writer.release()
    beta_writer.release()


def test_a_foreign_continuation_batch_answers_exactly_like_an_unknown_session(
    tmp_path: Path,
) -> None:
    """Refusing a squatter must not tell it whose id it just guessed."""

    alpha_writer, alpha = _uploadable(tmp_path / "alpha", "s-1", "b-alpha", count=1)
    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", alpha, project_id="alpha")

    continuation = _entry_frame(2, JournalRecordEntry(kind="event", value={"name": "probe"}))
    with pytest.raises(CheckpointSequenceError) as squatted:
        registry.accept_frames("s-1", continuation, project_id="beta")
    with pytest.raises(CheckpointSequenceError) as unknown:
        registry.accept_frames("s-never-used", continuation, project_id="beta")

    assert squatted.value.expected_sequence == unknown.value.expected_sequence == 1
    assert str(squatted.value) == str(unknown.value)
    # And the tenant that does hold the id is untouched by the probe.
    assert registry.summary("s-1", project_id="alpha").last_sequence == 3
    alpha_writer.release()


def test_one_projects_sessions_do_not_consume_another_projects_quota(tmp_path: Path) -> None:
    alpha_writer, alpha = _uploadable(tmp_path / "alpha", "s-1", "b-alpha", count=1)
    beta_writer, beta = _uploadable(tmp_path / "beta", "s-1", "b-beta", count=1)
    registry = LiveSessionRegistry(config=LiveConfig(max_sessions_per_project=1))

    registry.accept_frames("s-1", alpha, project_id="alpha")
    registry.accept_frames("s-1", beta, project_id="beta")

    assert len(registry.sessions(project_id="alpha")) == 1
    assert len(registry.sessions(project_id="beta")) == 1
    alpha_writer.release()
    beta_writer.release()


def test_tail_connections_are_bounded_per_project_not_only_per_server(
    tmp_path: Path,
) -> None:
    """One tenant must not be able to spend the whole connection budget."""

    alpha_writer, alpha = _uploadable(tmp_path / "alpha", "s-1", "b-alpha", count=1)
    beta_writer, beta = _uploadable(tmp_path / "beta", "s-1", "b-beta", count=1)
    registry = LiveSessionRegistry(
        config=LiveConfig(max_connections=4, max_connections_per_project=1)
    )
    registry.accept_frames("s-1", alpha, project_id="alpha")
    registry.accept_frames("s-1", beta, project_id="beta")

    held = registry.subscribe("s-1", project_id="alpha")
    with pytest.raises(TailCapacityError):
        registry.subscribe("s-1", project_id="alpha")
    # The other tenant still has its own budget.
    registry.subscribe("s-1", project_id="beta")
    held.close()
    registry.subscribe("s-1", project_id="alpha")
    alpha_writer.release()
    beta_writer.release()


# ----------------------------------------------- a finalized journal is closed


def test_a_finalized_journal_refuses_a_later_frame(tmp_path: Path) -> None:
    """``finalize`` is the end of the journal, so nothing may follow it."""

    writer, payload = _uploadable(tmp_path / "journals", "s-1", "b-1", count=1, close=True)
    registry = LiveSessionRegistry()
    accepted = registry.accept_frames("s-1", payload, project_id="p")
    subscription = registry.subscribe("s-1", project_id="p")
    subscription.drain()

    later = _entry_frame(
        accepted.accepted_through + 1,
        JournalRecordEntry(kind="event", value={"name": "after-the-end"}),
    )
    with pytest.raises(CheckpointFinalizedError):
        registry.accept_frames("s-1", later, project_id="p")

    assert subscription.drain() == []
    assert registry.summary("s-1", project_id="p").last_sequence == accepted.accepted_through
    # A transport retry of the journal it already holds is still idempotent.
    again = registry.accept_frames("s-1", payload, project_id="p")
    assert again.accepted_records == 0
    writer.release()


def test_a_frame_appended_to_a_finalized_journal_is_never_published(
    tmp_path: Path,
) -> None:
    """The same rule on the followed-directory path, where there is no client.

    Nothing legitimate appends to a journal after ``finalize``, so a file that
    grew past it is damaged or hostile. Either way the closed session does not
    start speaking again.
    """

    writer = _writer(tmp_path, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=1)
    recorder.close()
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    subscription = registry.subscribe("s-1", project_id="default")
    delivered = subscription.drain()
    assert _names(delivered)[-1] == EVENT_FINALIZE

    path = _journal_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(
            _entry_frame(
                len(_split_frames(path.read_bytes())) + 1,
                JournalRecordEntry(kind="event", value={"name": "after-the-end"}),
            )
        )
    registry.refresh()

    assert subscription.drain() == []
    assert registry.summary("s-1", project_id="default").last_sequence == len(delivered)


def test_a_batch_that_appends_after_its_own_finalize_is_refused_whole(
    tmp_path: Path,
) -> None:
    writer, payload = _uploadable(tmp_path / "journals", "s-1", "b-1", count=1, close=True)
    frames = _split_frames(payload)
    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", b"".join(frames[:-1]), project_id="p")
    before = registry.summary("s-1", project_id="p").last_sequence

    tail = frames[-1] + _entry_frame(
        len(frames) + 1,
        JournalRecordEntry(kind="event", value={"name": "after-the-end"}),
    )
    with pytest.raises(CheckpointFinalizedError):
        registry.accept_frames("s-1", tail, project_id="p")

    # Refused whole: even the finalize frame that led the batch is not recorded.
    assert registry.summary("s-1", project_id="p").last_sequence == before
    assert registry.summary("s-1", project_id="p").close_observed is False
    writer.release()


# ------------------------------------------------------- retries never rewrite


def test_a_retry_that_rewrites_an_accepted_frame_is_refused(tmp_path: Path) -> None:
    """A retry may repeat history idempotently; it may never edit it."""

    writer, payload = _uploadable(tmp_path / "journals", "s-1", "b-1", count=1)
    registry = LiveSessionRegistry()
    accepted = registry.accept_frames("s-1", payload, project_id="p")
    subscription = registry.subscribe("s-1", project_id="p")
    subscription.drain()

    # The identical bytes again: accepted, and nothing is republished.
    repeated = registry.accept_frames("s-1", payload, project_id="p")
    assert repeated.accepted_records == 0
    assert subscription.drain() == []

    divergent = _entry_frame(
        accepted.accepted_through,
        JournalRecordEntry(kind="event", value={"name": "rewritten"}),
    )
    with pytest.raises(CheckpointDivergedError):
        registry.accept_frames("s-1", divergent, project_id="p")
    assert subscription.drain() == []
    writer.release()


def test_a_divergent_frame_refuses_the_frames_that_follow_it(tmp_path: Path) -> None:
    writer, payload = _uploadable(tmp_path / "journals", "s-1", "b-1", count=1)
    registry = LiveSessionRegistry()
    accepted = registry.accept_frames("s-1", payload, project_id="p")

    rewritten = _entry_frame(
        accepted.accepted_through,
        JournalRecordEntry(kind="event", value={"name": "rewritten"}),
    )
    appended = _entry_frame(
        accepted.accepted_through + 1,
        JournalRecordEntry(kind="event", value={"name": "appended"}),
    )
    with pytest.raises(CheckpointDivergedError):
        registry.accept_frames("s-1", rewritten + appended, project_id="p")

    assert registry.summary("s-1", project_id="p").last_sequence == accepted.accepted_through
    writer.release()


def test_a_session_that_outgrew_its_frame_window_stops_being_sealable(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path / "journals")
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=6)
    payload = _frames(_journal_path(tmp_path / "journals"))

    registry = LiveSessionRegistry(config=LiveConfig(max_seal_bytes=len(payload) // 2))
    accepted = registry.accept_frames("s-1", payload, project_id="p")

    assert accepted.sealable is False
    with pytest.raises(SessionNotSealableError):
        registry.seal_source("s-1", project_id="p")
    writer.release()


def test_the_seal_source_of_a_local_journal_is_the_journal_itself(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()

    kind, source = registry.seal_source("s-1", project_id="default")

    assert kind == "journal"
    assert Path(str(source)).is_file()
    writer.release()


# ---------------------------------------------------- restricted egress


def _restricted(directory: Path, export: ExportConfig, **kwargs):
    """A journalling recorder whose transcript class carries ``export``."""

    writer = _writer(directory, **kwargs)
    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
        governance={CaptureClass.TRANSCRIPT: CaptureGovernance(export=export)},
    )
    recorder = IncidentRecorder(
        session_id="s-1",
        bundle_id="b-1",
        checkpoint=writer,
        config=RecorderConfig(capture_policy=policy),
    )
    return writer, recorder


def test_uploaded_frames_obey_the_same_destination_policy(tmp_path: Path) -> None:
    """The remote path is the same egress, so it gets the same refusal."""

    journals = tmp_path / "journals"
    writer, recorder = _restricted(journals, ExportConfig(allowed=False))
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="t-0")

    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", _frames(_journal_path(journals)), project_id="p")
    events = registry.subscribe("s-1", project_id="p").drain()

    assert SENTINEL not in json.dumps([_payload(event) for event in events])
    assert _names(events) == [EVENT_OPEN, EVENT_WITHHELD]
    writer.release()


def test_the_record_before_a_restricted_class_is_retained_still_streams(
    tmp_path: Path,
) -> None:
    """The gate closes on the frame that first carries restricted content.

    A capture class is journaled as retained in the same frame as the mutation
    that retained it, so metadata admitted earlier is not restricted evidence
    and is not withheld — the keying is what was captured, exactly as a finished
    bundle's is, not what the policy merely enabled.
    """

    writer, recorder = _restricted(tmp_path, ExportConfig(allowed=False))
    recorder.add_participant("caller", role="caller")
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="t-0")
    recorder.record_event("earshot.turn.start", turn_id="t-1")

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    events = registry.subscribe("s-1", project_id="default").drain()

    assert _names(events) == [EVENT_OPEN, EVENT_RECORD, EVENT_WITHHELD, EVENT_WITHHELD]
    assert _payload(events[1])["kind"] == "participant"
    # Once restricted content has been captured the whole artifact is restricted,
    # so what follows is withheld too rather than resuming after one bad record.
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    writer.release()


def test_a_capture_class_this_build_cannot_name_withholds_everything(
    tmp_path: Path,
) -> None:
    """A check that could not run is not a check that passed.

    A journal written by a later build can retain a capture class this one has
    no name — and therefore no governance — for. Its export policy is unreadable
    rather than absent, so everything after it is withheld.
    """

    journals = tmp_path / "journals"
    writer, recorder = _restricted(
        journals, ExportConfig(allowed=True, destinations=(LIVE_TAIL_DESTINATION,))
    )
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="t-0")
    frames = _split_frames(_frames(_journal_path(journals)))

    registry = LiveSessionRegistry()
    registry.accept_frames("s-1", b"".join(frames), project_id="p")
    unknown = encode_frame(
        len(frames) + 1,
        encode_entry(JournalRecordEntry(kind="omission", retained_classes=("class_from_2027",))),
        max_body_bytes=DEFAULT_MAX_FRAME_BYTES,
    )
    replayed = encode_frame(
        len(frames) + 2,
        frames[-1][HEADER_SIZE:-CHECKSUM_SIZE],
        max_body_bytes=DEFAULT_MAX_FRAME_BYTES,
    )
    registry.accept_frames("s-1", unknown + replayed, project_id="p")
    events = registry.subscribe("s-1", project_id="p").drain()

    assert _names(events) == [EVENT_OPEN, EVENT_RECORD, EVENT_WITHHELD, EVENT_WITHHELD]
    assert _payload(events[-1])["denied_capture_classes"] == [
        {"capture_class": None, "reason": "export_policy_unreadable"}
    ]
    # The permitted record that arrived before it is still on the wire in full.
    assert _payload(events[1])["value"]["attributes"] == {"transcript": SENTINEL}
    writer.release()


def test_a_restricted_session_still_reports_its_close(tmp_path: Path) -> None:
    """Withholding content must not withhold the shape of the session.

    ``finalize`` carries status, counters and the recorder's own truncation
    bookkeeping — never captured content — so it keeps flowing. A stream that
    suppressed it would leave a subscriber unable to tell a governed session
    from one that simply stopped.
    """

    writer, recorder = _restricted(tmp_path, ExportConfig(allowed=False), keep_finalized=True)
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="t-0")
    recorder.close()

    registry = LiveSessionRegistry(journal_dir=tmp_path)
    registry.refresh()
    events = registry.subscribe("s-1", project_id="default").drain()

    assert _names(events) == [EVENT_OPEN, EVENT_WITHHELD, EVENT_FINALIZE]
    assert SENTINEL not in json.dumps([_payload(event) for event in events])
    writer.release()
