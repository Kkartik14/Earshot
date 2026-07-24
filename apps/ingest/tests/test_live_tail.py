"""The live tail over real HTTP: its contract, its auth, its resume, its bounds.

The streaming tests run a real ``uvicorn`` listener on loopback and read the
response incrementally, because both ASGI test transports buffer a response body
to completion and would therefore never observe a stream at all. What is under
test is the endpoint — that it inherits the whole HTTP middleware stack, that
``Last-Event-ID`` resumes losslessly, that a slow subscriber is closed rather
than silently trimmed — not the buffer behind it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from earshot.api import CHECKPOINT_MEDIA_TYPE, ApiConfig, create_app
from earshot.checkpoint import CheckpointConfig, CheckpointWriter, assemble_incident
from earshot.checkpoint.framing import encode_frame
from earshot.checkpoint.limits import (
    DEFAULT_MAX_FRAME_BYTES,
    MAX_CHECKPOINT_BATCH_BYTES,
    MAX_CHECKPOINT_FRAME_BYTES,
)
from earshot.checkpoint.records import JournalRecordEntry, encode_entry
from earshot.codec import PROTOBUF_MEDIA_TYPE, encode_incident_protobuf
from earshot.live import LiveConfig, LiveSessionRegistry
from earshot.privacy import CaptureClass, CaptureGovernance, CapturePolicy, ExportConfig
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.storage import IncidentStore

pytestmark = pytest.mark.integration

# Long enough that the polling thread never fires on its own, so a test that is
# not about latency stays deterministic and drives ``refresh()`` itself.
QUIET_POLL_MS = 3_600_000

# Transcript content an export policy forbids leaving the process. Asserted
# absent from the whole stream, not merely from the record that carried it.
SENTINEL = "earshot-restricted-transcript-sentinel"


def _writer(directory: Path, **kwargs) -> CheckpointWriter:
    return CheckpointWriter(CheckpointConfig(checkpoint_dir=directory, **kwargs))


def _restricted_recorder(writer: CheckpointWriter, export: ExportConfig) -> IncidentRecorder:
    """A journalling recorder whose transcript class carries ``export``."""

    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
        governance={CaptureClass.TRANSCRIPT: CaptureGovernance(export=export)},
    )
    return IncidentRecorder(
        session_id="s-1",
        bundle_id="b-1",
        checkpoint=writer,
        config=RecorderConfig(capture_policy=policy),
    )


def _journal(directory: Path) -> Path:
    return next(directory.glob("*.eck"))


def _frame(sequence: int, name: str) -> bytes:
    """One journal frame an upload could carry at ``sequence``."""

    return encode_frame(
        sequence,
        encode_entry(JournalRecordEntry(kind="event", value={"name": name})),
        max_body_bytes=DEFAULT_MAX_FRAME_BYTES,
    )


def _record(recorder: IncidentRecorder, count: int = 3) -> None:
    recorder.add_participant("caller", role="caller")
    for index in range(count):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")


@dataclasses.dataclass
class _Harness:
    store: IncidentStore
    registry: LiveSessionRegistry
    journals: Path
    app: object


def _build(
    tmp_path: Path,
    *,
    config: ApiConfig | None = None,
    live: LiveConfig | None = None,
    poll_interval_ms: int = QUIET_POLL_MS,
) -> _Harness:
    journals = tmp_path / "journals"
    store = IncidentStore(tmp_path / "data")
    settings = dataclasses.replace(live or LiveConfig(), poll_interval_ms=poll_interval_ms)
    registry = LiveSessionRegistry(journal_dir=journals, config=settings)
    app = create_app(store=store, config=config, live_registry=registry, analyzer=None)
    return _Harness(store=store, registry=registry, journals=journals, app=app)


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


@contextlib.contextmanager
def _serve(app) -> Iterator[str]:
    """Run the app on a loopback listener so responses really stream."""

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise AssertionError("live tail server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _open(url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=timeout)


def _events(response, wanted: int, *, timeout: float = 5.0) -> list[dict[str, str]]:
    """Read whole SSE frames until ``wanted`` arrive, the stream ends, or time runs out."""

    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    while len(events) < wanted and time.monotonic() < deadline:
        try:
            raw = response.readline()
        except (TimeoutError, OSError):
            break
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith(":"):
            continue  # an SSE comment; browsers never dispatch one
        if line == "":
            if current:
                events.append(current)
                current = {}
        else:
            key, _, value = line.partition(":")
            current[key.strip()] = value.lstrip()
    return events


def _data(event: dict[str, str]) -> dict:
    return json.loads(event["data"])


# ------------------------------------------------------------- SSE contract


def test_the_tail_opens_with_the_journal_header_and_stable_event_ids(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        events = _events(response, 2)
        response.close()

    assert events[0]["event"] == "open"
    journal_id = _data(events[0])["journal_id"]
    assert events[0]["id"] == f"{journal_id}:1"
    assert events[1]["event"] == "record"
    assert events[1]["id"] == f"{journal_id}:2"
    writer.release()


def test_an_in_flight_operation_reaches_the_wire_as_unfinished(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    with recorder.operation("llm", turn_id="turn-0"):
        harness.registry.refresh()
        with _serve(harness.app) as base:
            response = _open(f"{base}/v1/live/sessions/s-1/tail")
            events = _events(response, 2)
            response.close()

    assert events[1]["event"] == "operation_open"
    value = _data(events[1])
    assert value["status"] == "unknown"
    assert value["ended_at"] is None
    assert value["duration_nano"] is None
    assert value["end_observed"] is False
    writer.release()


def test_the_tail_body_never_carries_analysis_or_a_turn_metric(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=2)
    with recorder.operation("llm", turn_id="turn-0"):
        pass
    recorder.close()
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail?from=live")
        events = _events(response, 3, timeout=1.5)
        response.close()

    # The header's own list of unanswerable questions is the only allowed
    # mention; the facts that follow it carry no verdicts at all.
    facts = json.dumps([event for event in events if event.get("event") != "open"]).lower()
    for forbidden in ("analysis", "diagnos", "p50", "p95", "percentile", "latency_ms"):
        assert forbidden not in facts


def test_an_idle_tail_sends_a_heartbeat_instead_of_going_quiet(tmp_path: Path) -> None:
    harness = _build(tmp_path, live=LiveConfig(heartbeat_seconds=0.05))
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        events = _events(response, 2)
        response.close()

    assert events[0]["event"] == "open"
    assert events[1]["event"] == "heartbeat"
    # A named event, not a comment: a browser never dispatches a comment, so a
    # comment keepalive would leave a client unable to tell a quiet session from
    # a dead connection. It carries no id, so it cannot advance the resume cursor.
    assert "id" not in events[1]
    assert _data(events[1]) == {"as_of_sequence": 1, "close_observed": False}
    writer.release()


# ------------------------------------------------------- restricted export


def test_a_class_forbidden_from_this_destination_never_reaches_a_subscriber(
    tmp_path: Path,
) -> None:
    """The tail is an egress path, so it reapplies the destination policy.

    An operator who wrote ``ExportConfig(allowed=False)`` for a class forbade
    that content leaving the process. An authenticated subscriber is still
    outside the process, so the tail owes the same refusal an exporter owes.
    """

    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = _restricted_recorder(writer, ExportConfig(allowed=False, policy_id="no-egress"))
    recorder.add_participant("caller", role="caller")
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="turn-0")
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        events = _events(response, 3)
        response.close()

    assert SENTINEL not in json.dumps(events)
    # The record still exists on the wire at its own sequence: a live view that
    # silently skipped it would read as a session that never said anything.
    journal_id = _data(events[0])["journal_id"]
    assert events[2]["event"] == "withheld"
    assert events[2]["id"] == f"{journal_id}:3"
    withheld = _data(events[2])
    assert withheld["entry"] == "record"
    assert withheld["kind"] == "event"
    assert withheld["destination"] == "live_tail"
    assert withheld["denied_capture_classes"] == [
        {"capture_class": "transcript", "reason": "export_denied_by_policy"}
    ]
    # And the open event said so before any fact arrived.
    assert _data(events[0])["export_policy"] == {
        "destination": "live_tail",
        "denied_capture_classes": ["transcript"],
        "policy_readable": True,
        "note": (
            "this stream is an export; a record is withheld once this session has "
            "retained a capture class whose policy forbids this destination"
        ),
    }
    writer.release()


def test_a_class_that_permits_this_destination_still_streams(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = _restricted_recorder(
        writer, ExportConfig(allowed=True, destinations=("live_tail",), policy_id="tail-only")
    )
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="turn-0")
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        events = _events(response, 2)
        response.close()

    assert events[1]["event"] == "record"
    assert _data(events[1])["value"]["attributes"] == {"transcript": SENTINEL}
    assert _data(events[0])["export_policy"]["denied_capture_classes"] == []
    writer.release()


def test_a_destination_allowlist_that_omits_the_tail_withholds_the_record(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = _restricted_recorder(
        writer, ExportConfig(allowed=True, destinations=("otlp",), policy_id="otlp-only")
    )
    recorder.record_event("stt.final", attributes={"transcript": SENTINEL}, turn_id="turn-0")
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        events = _events(response, 2)
        response.close()

    assert SENTINEL not in json.dumps(events)
    assert events[1]["event"] == "withheld"
    assert _data(events[1])["denied_capture_classes"] == [
        {"capture_class": "transcript", "reason": "export_destination_not_permitted"}
    ]
    writer.release()


# ------------------------------------------------------------- latency gate


def test_an_admitted_fact_reaches_a_subscriber_in_under_two_seconds(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path, poll_interval_ms=200)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()
    harness.registry.start()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail?from=live", timeout=5.0)
        assert _events(response, 1)[0]["event"] == "open"
        started = time.monotonic()
        recorder.record_event("earshot.turn.start", turn_id="turn-live")
        events = _events(response, 1, timeout=2.0)
        elapsed = time.monotonic() - started
        response.close()

    assert events[0]["event"] == "record"
    assert _data(events[0])["kind"] == "event"
    assert elapsed < 2.0
    harness.registry.close()
    writer.release()


# ---------------------------------------------------------- resume + bounds


def test_reconnecting_with_last_event_id_loses_and_repeats_nothing(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    _record(recorder, count=3)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        first_stream = _open(f"{base}/v1/live/sessions/s-1/tail")
        first = _events(first_stream, 3)
        first_stream.close()

        recorder.record_event("earshot.turn.start", turn_id="turn-late")
        harness.registry.refresh()
        second_stream = _open(
            f"{base}/v1/live/sessions/s-1/tail",
            headers={"Last-Event-ID": first[-1]["id"]},
        )
        second = _events(second_stream, 2)
        second_stream.close()

    identifiers = [event["id"] for event in [*first, *second] if "id" in event]
    journal_id = _data(first[0])["journal_id"]
    assert identifiers == [f"{journal_id}:{index}" for index in range(1, len(identifiers) + 1)]
    assert all(event["event"] != "open" for event in second)
    writer.release()


def test_a_foreign_last_event_id_resets_before_replaying(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(
            f"{base}/v1/live/sessions/s-1/tail",
            headers={"Last-Event-ID": "not-this-journal:4"},
        )
        events = _events(response, 2)
        response.close()

    assert [event["event"] for event in events] == ["reset", "open"]
    assert "id" not in events[0]
    writer.release()


def test_a_subscriber_that_falls_behind_is_closed_with_overflow(tmp_path: Path) -> None:
    harness = _build(tmp_path, live=LiveConfig(max_queue_records=2))
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        assert _events(response, 1)[0]["event"] == "open"
        _record(recorder, count=20)
        harness.registry.refresh()
        events = _events(response, 4, timeout=3.0)
        # The server closed the stream; nothing follows the overflow event.
        trailing = _events(response, 1, timeout=1.0)
        response.close()

    assert events[-1]["event"] == "overflow"
    assert trailing == []
    overflow = _data(events[-1])
    assert overflow["reason"] == "subscriber_fell_behind"
    assert overflow["resume_with"].endswith(":" + events[-2]["id"].split(":")[1])
    writer.release()


def test_resuming_after_an_overflow_recovers_every_record(tmp_path: Path) -> None:
    harness = _build(tmp_path, live=LiveConfig(max_queue_records=2))
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        response = _open(f"{base}/v1/live/sessions/s-1/tail")
        first = _events(response, 1)
        _record(recorder, count=6)
        harness.registry.refresh()
        first += _events(response, 4, timeout=3.0)
        response.close()
        assert first[-1]["event"] == "overflow"

        resume = [event["id"] for event in first if "id" in event][-1]
        second_stream = _open(
            f"{base}/v1/live/sessions/s-1/tail",
            headers={"Last-Event-ID": resume},
        )
        second = _events(second_stream, 6, timeout=3.0)
        second_stream.close()

    identifiers = [event["id"] for event in [*first, *second] if "id" in event]
    journal_id = _data(first[0])["journal_id"]
    assert identifiers == [f"{journal_id}:{index}" for index in range(1, 9)]
    writer.release()


def test_tail_capacity_is_refused_rather_than_queued(tmp_path: Path) -> None:
    harness = _build(tmp_path, live=LiveConfig(max_connections=1))
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with _serve(harness.app) as base:
        held = _open(f"{base}/v1/live/sessions/s-1/tail")
        assert _events(held, 1)[0]["event"] == "open"
        with pytest.raises(urllib.error.HTTPError) as refused:
            _open(f"{base}/v1/live/sessions/s-1/tail")
        held.close()

    assert refused.value.code == 429
    assert json.loads(refused.value.read())["error"]["code"] == "EARSHOT_TAIL_CAPACITY"
    writer.release()


def test_an_unknown_session_is_not_live(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    with TestClient(harness.app) as client:
        response = client.get("/v1/live/sessions/nope/tail")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EARSHOT_SESSION_NOT_LIVE"


# ---------------------------------------------------------------- auth path


def test_the_tail_requires_the_same_credential_every_other_route_does(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path, config=ApiConfig(token="secret"))
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with TestClient(harness.app) as client:
        anonymous = client.get("/v1/live/sessions/s-1/tail")
        wrong = client.get(
            "/v1/live/sessions/s-1/tail",
            headers={"Authorization": "Bearer nope"},
        )
        listing = client.get("/v1/live/sessions")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "EARSHOT_UNAUTHORIZED"
    assert wrong.status_code == 401
    assert listing.status_code == 401
    writer.release()


def test_a_non_loopback_host_header_is_refused_on_the_tail(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with TestClient(harness.app) as client:
        response = client.get(
            "/v1/live/sessions/s-1/tail",
            headers={"Host": "evil.example"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EARSHOT_UNTRUSTED_HOST"
    writer.release()


def test_a_cookie_tail_from_a_foreign_origin_is_refused(tmp_path: Path) -> None:
    harness = _build(tmp_path, config=ApiConfig(token="secret"))
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with TestClient(harness.app) as client:
        client.post("/v1/auth/session", headers={"Authorization": "Bearer secret"})
        foreign = client.get(
            "/v1/live/sessions/s-1/tail",
            headers={"Origin": "http://evil.example"},
        )
        same = client.get("/v1/live/sessions", headers={"Origin": "http://testserver"})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "EARSHOT_ORIGIN_NOT_ALLOWED"
    assert same.status_code == 200
    harness.store.close()
    writer.release()


def test_the_live_listing_is_scoped_to_the_authenticated_project(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    issued = harness.store.issue_api_key("default", label="live")
    harness.store.create_project("other", display_name="Other")
    other = harness.store.issue_api_key("other", label="live")
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with TestClient(harness.app) as client:
        mine = client.get(
            "/v1/live/sessions",
            headers={"Authorization": f"Bearer {issued.credential}"},
        ).json()
        theirs = client.get(
            "/v1/live/sessions",
            headers={"Authorization": f"Bearer {other.credential}"},
        )
        foreign_tail = client.get(
            "/v1/live/sessions/s-1/tail",
            headers={"Authorization": f"Bearer {other.credential}"},
        )
    assert [item["session_id"] for item in mine["items"]] == ["s-1"]
    assert theirs.json()["items"] == []
    assert foreign_tail.status_code == 404
    harness.store.close()
    writer.release()


def test_the_live_listing_states_what_it_cannot_answer(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    IncidentRecorder(session_id="s-1", bundle_id="b-1", checkpoint=writer)
    harness.registry.refresh()

    with TestClient(harness.app) as client:
        body = client.get("/v1/live/sessions").json()

    assert body["following_journal_directory"] is True
    joined = " ".join(body["limitations"]).lower()
    assert "not an incident" in joined
    assert "no analysis" in joined
    assert body["items"][0]["close_observed"] is False
    writer.release()


# ------------------------------------------------------- remote checkpoints


def test_uploaded_checkpoints_feed_the_same_tail(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=2)
    payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        accepted = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        listed = client.get("/v1/live/sessions").json()
    assert accepted.status_code == 202
    assert accepted.json()["accepted_through"] > 1
    assert accepted.json()["sealable"] is True
    assert [item["source"] for item in listed["items"]] == ["checkpoint"]
    writer.release()


def test_a_checkpoint_batch_with_a_gap_is_a_conflict(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=1)
    head = _journal(harness.journals).read_bytes()
    recorder.record_event("earshot.turn.start", turn_id="turn-9")
    recorder.record_event("earshot.turn.start", turn_id="turn-10")
    added = _journal(harness.journals).read_bytes()[len(head) :]
    second_frame_start = 9 + int.from_bytes(added[5:9], "big") + 4

    with TestClient(harness.app) as client:
        client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=head,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        gapped = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=added[second_frame_start:],
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        torn = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=added[:-3],
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        wrong_type = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=added,
            headers={"Content-Type": "application/json"},
        )
    assert gapped.status_code == 409
    assert gapped.json()["error"]["code"] == "EARSHOT_CHECKPOINT_SEQUENCE_GAP"
    assert torn.status_code == 400
    assert torn.json()["error"]["code"] == "EARSHOT_CHECKPOINT_FRAMES_INVALID"
    assert wrong_type.status_code == 415
    writer.release()


def test_two_projects_upload_the_same_session_id_without_colliding(tmp_path: Path) -> None:
    """A session id belongs to a tenant, so one tenant cannot squat another's.

    Keyed by session id alone, whichever project uploaded first owned the id and
    every later upload from the other project was refused as "not live" — a
    cross-tenant denial of service dressed as a 404.
    """

    harness = _build(tmp_path)
    mine = harness.store.issue_api_key("default", label="live")
    harness.store.create_project("other", display_name="Other")
    theirs = harness.store.issue_api_key("other", label="live")
    first = _writer(tmp_path / "first")
    _record(IncidentRecorder(session_id="s-1", bundle_id="b-mine", checkpoint=first), count=1)
    second = _writer(tmp_path / "second")
    _record(IncidentRecorder(session_id="s-1", bundle_id="b-theirs", checkpoint=second), count=1)

    with TestClient(harness.app) as client:
        accepted = [
            client.post(
                "/v1/live/sessions/s-1/checkpoints",
                content=_journal(directory).read_bytes(),
                headers={
                    "Content-Type": CHECKPOINT_MEDIA_TYPE,
                    "Authorization": f"Bearer {credential.credential}",
                },
            )
            for directory, credential in (
                (tmp_path / "first", mine),
                (tmp_path / "second", theirs),
            )
        ]
        listings = [
            client.get(
                "/v1/live/sessions",
                headers={"Authorization": f"Bearer {credential.credential}"},
            ).json()
            for credential in (mine, theirs)
        ]

    assert [response.status_code for response in accepted] == [202, 202]
    assert accepted[0].json()["journal_id"] != accepted[1].json()["journal_id"]
    assert [item["bundle_id"] for item in listings[0]["items"]] == ["b-mine"]
    assert [item["bundle_id"] for item in listings[1]["items"]] == ["b-theirs"]
    harness.store.close()
    first.release()
    second.release()


def test_a_frame_after_finalize_is_a_conflict(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=1)
    recorder.close()
    payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        accepted = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        refused = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=_frame(accepted.json()["accepted_through"] + 1, "after-the-end"),
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EARSHOT_CHECKPOINT_JOURNAL_FINALIZED"
    writer.release()


def test_a_retry_that_rewrites_an_accepted_frame_is_a_conflict(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=1)
    payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        accepted = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        repeated = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        refused = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=_frame(accepted.json()["accepted_through"], "rewritten"),
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
    assert repeated.status_code == 202
    assert repeated.json()["accepted_records"] == 0
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EARSHOT_CHECKPOINT_DIVERGED"
    writer.release()


def test_a_frame_larger_than_the_upload_contract_is_refused_and_declared(
    tmp_path: Path,
) -> None:
    """The wire bound is one number, and the listing says what it costs."""

    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=1)
    payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        accepted = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        refused = client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=_frame(
                accepted.json()["accepted_through"] + 1,
                "x" * MAX_CHECKPOINT_BATCH_BYTES,
            ),
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        limitations = client.get("/v1/live/sessions").json()["limitations"]

    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == "EARSHOT_BODY_TOO_LARGE"
    assert any(str(MAX_CHECKPOINT_FRAME_BYTES) in note for note in limitations)
    writer.release()


def test_a_live_buffer_is_never_listed_as_an_incident(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=2)
    payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        incidents = client.get("/v1/incidents").json()
        live = client.get("/v1/live/sessions").json()
    assert incidents["items"] == []
    assert len(live["items"]) == 1
    harness.store.close()
    writer.release()


def test_the_final_artifact_supersedes_the_live_buffer(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=2)
    recorder.close()
    payload = _journal(harness.journals).read_bytes()
    bundle = assemble_incident(_journal(harness.journals)).bundle

    with TestClient(harness.app) as client:
        client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        assert len(client.get("/v1/live/sessions").json()["items"]) == 1
        ingested = client.post(
            "/v1/incidents",
            content=encode_incident_protobuf(bundle),
            headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
        )
        remaining = client.get("/v1/live/sessions").json()
    assert ingested.status_code == 201
    assert remaining["items"] == []
    harness.store.close()


def test_sealing_an_unclosed_session_yields_a_declared_provisional_artifact(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=2)
    with recorder.operation("llm", turn_id="turn-0"):
        payload = _journal(harness.journals).read_bytes()

    with TestClient(harness.app) as client:
        client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        sealed = client.post("/v1/live/sessions/s-2/seal")
        listed = client.get("/v1/incidents").json()
    body = sealed.json()
    assert sealed.status_code == 201
    assert body["close_observed"] is False
    assert body["finality"] == "provisional"
    assert body["completeness"] == "incomplete"
    # A distinct bundle id, so the producer's own final artifact can still land.
    assert body["bundle_id"].startswith("b-2.s")
    assert body["unfinished_operations"] == 1
    assert [item["finality"] for item in listed["items"]] == ["provisional"]
    harness.store.close()
    writer.release()


def test_sealing_is_refused_when_the_frame_window_was_exceeded(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    writer = _writer(harness.journals)
    recorder = IncidentRecorder(session_id="s-2", bundle_id="b-2", checkpoint=writer)
    _record(recorder, count=6)
    payload = _journal(harness.journals).read_bytes()
    harness.registry.config = dataclasses.replace(
        harness.registry.config, max_seal_bytes=len(payload) // 2
    )

    with TestClient(harness.app) as client:
        client.post(
            "/v1/live/sessions/s-2/checkpoints",
            content=payload,
            headers={"Content-Type": CHECKPOINT_MEDIA_TYPE},
        )
        refused = client.post("/v1/live/sessions/s-2/seal")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EARSHOT_SESSION_NOT_SEALABLE"
    writer.release()
