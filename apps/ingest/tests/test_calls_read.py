"""Reading calls over HTTP (``GET /v1/calls``, ``GET /v1/calls/{call_id}``).

Every test runs the real app on a real uvicorn server over a real catalog (both
dialects), fed by ``CallStore.record_spans`` (the receiver's storage call) with
synthetic Pipecat-shaped OTLP requests, and closed by the real closer. The store's injected
clock (§7 seam 1) is set after the wall clock, so the app's own closer and
reapers never act on these calls behind the test's back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from earshot.api import create_app
from earshot.blobstore import LocalBlobStore, put_stream
from earshot.call_capture import spans_from_request
from earshot.call_materializer import transcript_lines
from earshot.clock import ManualClock
from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingManifest, RecordingStore
from otlp_support import make_request, make_span, request, serve, string_attribute
from store_support import catalog, make_store
from synthetic_otlp import SYNTHETIC_LINES, SYNTHETIC_SESSION_KEY, synthetic_requests

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TIMINGS = CallTimings()
END_GRACE = TIMINGS.end_grace_nano
LONG_QUIET = TIMINGS.long_quiet_nano
LATE_DEBOUNCE = TIMINGS.late_debounce_nano
LEASE = 60 * SECOND


@dataclass
class World:
    clock: ManualClock
    store: IncidentStore
    calls: CallStore
    recordings: RecordingStore
    closer: Closer


def _world(tmp_path: Path) -> World:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=LocalBlobStore(tmp_path / "blobs"))
    return World(clock, store, calls, recordings, Closer(store, calls=calls, recordings=recordings))


def _replay(world: World, requests=None, *, project_id: str = "default") -> str:
    """Record seven synthetic export batches 5 seconds apart and return the call ID."""

    call_ids: set[str] = set()
    for item in synthetic_requests() if requests is None else requests:
        call_ids.update(world.calls.record_spans(project_id, spans_from_request(item)).call_ids)
        world.clock.advance(5 * SECOND)
    [call_id] = call_ids
    return call_id


def _keyed(world: World, *keys_and_traces: tuple[str, int], span: int = 1) -> None:
    """One request: one span per ``(session_key, trace)``, each naming its key."""

    world.calls.record_spans(
        "default",
        spans_from_request(
            make_request(
                [
                    make_span(trace, span, attributes=[string_attribute("earshot.session.id", key)])
                    for key, trace in keys_and_traces
                ]
            )
        ),
    )


def _store_recording(world: World, speaker: str = "user") -> None:
    recording_id = f"conv-1.{speaker}"
    world.recordings.create(
        "default",
        RecordingManifest(
            recording_id=recording_id,
            session_key=SYNTHETIC_SESSION_KEY,
            speaker=speaker,
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )
    attempt = world.recordings.begin_upload("default", recording_id)
    try:
        sha256, size = put_stream(world.recordings.blobs, attempt.blob_key, [b"RIFF" * 16])
        world.recordings.finish_upload(
            "default", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=None
        )
    finally:
        world.recordings.end_upload("default", attempt.blob_key)


def _get(port: int, path: str, *, credential: str | None = None):
    headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    return request(port, "GET", path, headers=headers)


def _json(port: int, path: str):
    reply = _get(port, path)
    assert reply.status == 200, (reply.status, reply.body[:300])
    return reply.json()


# --- the list --------------------------------------------------------------------------------


def test_the_call_list_is_newest_first_and_stable_while_calls_arrive_and_receive_spans(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    for index in range(3):
        _keyed(world, (f"call-{index}", index + 1))
        world.clock.advance(SECOND)
    _keyed(world, ("call-3", 4), ("call-4", 5))  # one batch: equal first evidence
    world.clock.advance(SECOND)
    ids = {
        f"call-{index}": world.calls.call_id("default", f"call-{index}", 0) for index in range(5)
    }
    tie = sorted([ids["call-3"], ids["call-4"]], reverse=True)
    expected = [*tie, ids["call-2"], ids["call-1"], ids["call-0"]]

    with serve(create_app(store=world.store)) as port:
        first = _json(port, "/v1/calls?limit=2")
        # While the client pages: a new call arrives, and a call on a later page
        # receives spans (its last evidence moves; its place in the list does not).
        _keyed(world, ("call-new", 50))
        _keyed(world, ("call-1", 2), span=2)
        second = _json(port, f"/v1/calls?limit=2&cursor={first['next_cursor']}")
        third = _json(port, f"/v1/calls?limit=2&cursor={second['next_cursor']}")
        fresh = _json(port, "/v1/calls?limit=1")

    pages = [first, second, third]
    assert [item["call_id"] for page in pages for item in page["items"]] == expected
    assert [len(page["items"]) for page in pages] == [2, 2, 1]
    assert third["next_cursor"] is None
    assert [item["call_id"] for item in fresh["items"]] == [
        world.calls.call_id("default", "call-new", 0)
    ]


def test_a_listed_call_carries_its_key_counts_evidence_times_and_latest_revision(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)  # 7 requests from START, 5 s apart; the clock ends at +35 s
    _store_recording(world)
    stored_at = world.clock.wall
    world.clock.advance(END_GRACE)
    assert world.closer.close_next().revision == 1
    _keyed(world, ("still-open", 9))
    open_id = world.calls.call_id("default", "still-open", 0)

    with serve(create_app(store=world.store)) as port:
        items = {item["call_id"]: item for item in _json(port, "/v1/calls")["items"]}
        closed_only = _json(port, "/v1/calls?state=closed")["items"]
        open_only = _json(port, "/v1/calls?state=open")["items"]
        bad_state = _get(port, "/v1/calls?state=ended")
        bad_cursor = _get(port, "/v1/calls?cursor=not-a-cursor")
        bad_limit = _get(port, "/v1/calls?limit=101")

    # The server's projection job projects a closed call in the background: its
    # projection fields are test_call_view_api's and test_call_projection_job's.
    listed_call = {key: value for key, value in items[call_id].items() if "projection" not in key}
    assert listed_call == {
        "call_id": call_id,
        "key_source": "conversation.id",
        "state": "closed",
        "session_key": SYNTHETIC_SESSION_KEY,
        "first_evidence_unix_nano": str(START),
        "last_evidence_unix_nano": str(stored_at),
        "span_count": 17,
        "recording_count": 1,
        "latest_revision": {
            "revision": 1,
            "bundle_id": f"{call_id}.r1",
            "close_reason": "end_signal",
            "finality": "final",
        },
        "materialize_error": None,
    }
    assert items[open_id]["state"] == "open"
    assert items[open_id]["latest_revision"] is None
    assert [item["call_id"] for item in closed_only] == [call_id]
    assert [item["call_id"] for item in open_only] == [open_id]
    assert bad_state.status == 422
    assert (bad_cursor.status, bad_cursor.problem_code()) == (400, "EARSHOT_INVALID_CURSOR")
    assert bad_limit.status == 422


# --- the detail ------------------------------------------------------------------------------


def _late_llm_span(world: World, span: int, text: str) -> None:
    """One more ``llm`` span of the call's trace (unkeyed: it joins through its trace)."""

    trace = int.from_bytes(spans_from_request(synthetic_requests()[0])[0].span.trace_id, "big")
    world.calls.record_spans(
        "default",
        spans_from_request(
            make_request(
                [
                    make_span(
                        trace,
                        span,
                        name="llm",
                        start=world.clock.wall,
                        attributes=[string_attribute("output", text)],
                    )
                ]
            )
        ),
    )


def test_call_detail_lists_revisions_recordings_and_the_transcript_of_its_spans(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    call_id = _replay(world)
    _store_recording(world)
    world.clock.advance(END_GRACE)
    world.closer.close_next()
    _late_llm_span(world, 900, "One more thing, see you then.")
    world.clock.advance(LATE_DEBOUNCE)
    world.closer.close_next()
    world.store.create_project("other", display_name="Other")
    other = world.store.issue_api_key("other", label="other").credential

    with serve(create_app(store=world.store)) as port:
        listed = _json(port, "/v1/calls")["items"][0]
        detail = _json(port, f"/v1/calls/{call_id}")
        unknown = _get(port, "/v1/calls/call_0123456789abcdef0123456789abcdef")
        foreign = _get(port, f"/v1/calls/{call_id}", credential=other)

    month_1 = {key: value for key, value in listed.items() if "projection" not in key}
    assert {key: value for key, value in detail.items() if key in month_1} == month_1
    assert [
        {key: value for key, value in revision.items() if key != "created_unix_nano"}
        for revision in detail["revisions"]
    ] == [
        {
            "revision": 1,
            "bundle_id": f"{call_id}.r1",
            "close_reason": "end_signal",
            "finality": "final",
            "span_count": 17,
            "recording_count": 1,
        },
        {
            "revision": 2,
            "bundle_id": f"{call_id}.r2",
            "close_reason": "late_data",
            "finality": "final",
            "span_count": 18,
            "recording_count": 1,
        },
    ]
    assert all(revision["created_unix_nano"].isdigit() for revision in detail["revisions"])
    [recording] = detail["recordings"]
    assert (recording["recording_id"], recording["state"], recording["call_id"]) == (
        "conv-1.user",
        "stored",
        call_id,
    )
    assert detail["evidence_after_latest_revision"] is False
    assert detail["transcript_coverage"] == "available"
    lines = [(line["speaker"], line["text"]) for line in detail["transcript"]]
    assert lines == [*SYNTHETIC_LINES, ("agent", "One more thing, see you then.")]
    first = detail["transcript"][0]
    assert set(first) == {
        "speaker",
        "text",
        "start_unix_nano",
        "end_unix_nano",
        "timing",
        "trace_id",
        "span_id",
    }
    assert first["timing"] == "measured"
    assert first["start_unix_nano"].isdigit() and len(first["span_id"]) == 16
    for reply in (unknown, foreign):
        assert (reply.status, reply.problem_code()) == (404, "EARSHOT_CALL_NOT_FOUND")


def test_call_detail_keeps_source_order_for_mixed_timing_connector_lines(tmp_path) -> None:
    from earshot.connectors.base import transcript_line
    from earshot.connectors.evidence import CALL, Evidence, EvidenceSpan, otlp_spans

    world = _world(tmp_path)
    call_span = EvidenceSpan(CALL, 0, START, START + 4 * SECOND)
    lines = (
        transcript_line(
            0,
            speaker="user",
            text="measured first",
            start=START + 2 * SECOND,
            end=START + 2 * SECOND + 500_000_000,
            timing="measured",
        ),
        transcript_line(
            1,
            speaker="agent",
            text="untimed second",
            start=START,
            end=START,
            timing="order_only",
        ),
        transcript_line(
            2,
            speaker="user",
            text="measured third",
            start=START + 3 * SECOND,
            end=START + 3 * SECOND + 500_000_000,
            timing="measured",
        ),
    )
    evidence = Evidence(
        provider="vapi",
        provider_call_id="mixed_timing_transcript",
        spans=(call_span, *(line for line in lines if line is not None)),
    )
    spans = otlp_spans(
        evidence,
        session_key="vapi:mixed_timing_transcript",
        translator_version="test",
    )
    [call_id] = world.calls.record_spans("default", spans).call_ids

    with serve(create_app(store=world.store)) as port:
        detail = _json(port, f"/v1/calls/{call_id}")

    assert [(line["text"], line["timing"]) for line in detail["transcript"]] == [
        ("measured first", "measured"),
        ("untimed second", "order_only"),
        ("measured third", "measured"),
    ]
    assert detail["transcript"][1]["start_unix_nano"] is None


def test_an_open_calls_transcript_grows_as_its_spans_arrive(tmp_path) -> None:
    world = _world(tmp_path)
    requests = synthetic_requests()
    seen = []

    with serve(create_app(store=world.store)) as port:
        call_id = _replay(world, requests[:3])  # requests 1-2 parked, joined by request 3
        seen.append(_json(port, f"/v1/calls/{call_id}"))
        _replay(world, requests[3:5])
        seen.append(_json(port, f"/v1/calls/{call_id}"))
        _replay(world, requests[5:])
        seen.append(_json(port, f"/v1/calls/{call_id}"))

    assert [detail["state"] for detail in seen] == ["open"] * 3
    assert [len(detail["transcript"]) for detail in seen] == [3, 6, 8]
    assert [(line["speaker"], line["text"]) for line in seen[-1]["transcript"]] == SYNTHETIC_LINES
    # A call's transcript is its spans' lines, whatever its revisions hold.
    spans = [span for item in requests for span in spans_from_request(item)]
    assert [line["span_id"] for line in seen[-1]["transcript"]] == [
        line.span_id for line in transcript_lines(spans)
    ]


def test_a_calls_transcript_is_read_from_its_spans_lines_never_from_their_payloads(
    tmp_path,
) -> None:
    # Every llm span's payload repeats the conversation so far, so a read
    # that parses payloads costs the square of the call's length. Each span's line is
    # stored with it; with every payload unreadable the transcript is still whole.
    world = _world(tmp_path)
    call_id = _replay(world)
    with catalog(tmp_path) as connection:
        connection.execute("UPDATE spans SET payload = ?", (b"\xff",))
        connection.commit()

    with serve(create_app(store=world.store)) as port:
        detail = _json(port, f"/v1/calls/{call_id}")

    assert [(line["speaker"], line["text"]) for line in detail["transcript"]] == SYNTHETIC_LINES
    assert detail["transcript_coverage"] == "available"


@pytest.mark.sqlite_only
def test_every_read_of_a_calls_spans_starts_from_its_traces_never_the_projects_spans(
    tmp_path,
) -> None:
    # Start span reads from the call's traces to avoid project-wide scans.
    from earshot.storage_call_reads import LEGACY_TRANSCRIPT_SQL, TRANSCRIPT_SQL
    from earshot.storage_closer import (
        SNAPSHOT_KEYS_SQL,
        SNAPSHOT_RESOURCES_SQL,
        SNAPSHOT_SPANS_SQL,
    )

    world = _world(tmp_path)
    call_id = _replay(world)
    for other in range(40):  # other calls' spans, which the call's reads must not walk
        attributes = [string_attribute("earshot.session.id", f"other-{other}")]
        world.calls.record_spans(
            "default",
            spans_from_request(
                make_request(
                    [make_span(1000 + other, span, attributes=attributes) for span in range(1, 26)]
                )
            ),
        )
    statements = {
        "transcript": (TRANSCRIPT_SQL, ("default", call_id)),
        "legacy transcript": (LEGACY_TRANSCRIPT_SQL, ("default", call_id)),
        "claim span keys": (SNAPSHOT_KEYS_SQL, ("default", call_id)),
        "snapshot resources": (SNAPSHOT_RESOURCES_SQL, ("default", "default", call_id)),
        "snapshot spans": (SNAPSHOT_SPANS_SQL, ("default", call_id)),
    }

    plans = {}
    with catalog(tmp_path) as connection:
        for name, (sql, parameters) in statements.items():
            plans[name] = [
                tuple(row)[-1]
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
            ]
            rows = connection.execute(sql, parameters).fetchall()
            if name != "legacy transcript":
                assert rows, name

    def driven_from_traces(plan: list[str]) -> bool:
        steps = [step for step in plan if step.split()[:2] in (["SEARCH", "t"], ["SEARCH", "s"])]
        return (
            [step.split()[1] for step in steps] == ["t", "s"]
            and "INDEX call_traces_call_idx (project_id=? AND call_id=?)" in steps[0]
            and "(project_id=? AND trace_id=?" in steps[1]
            and not any(step.startswith(("SCAN s", "SCAN t")) for step in plan)
        )

    assert {name: plan for name, plan in plans.items() if not driven_from_traces(plan)} == {}


def test_under_metadata_only_a_call_has_no_session_key_and_no_transcript(tmp_path) -> None:
    world = _world(tmp_path)
    world.store.set_capture_preset("default", "metadata_only")
    call_id = _replay(world)

    with serve(create_app(store=world.store)) as port:
        detail = _json(port, f"/v1/calls/{call_id}")

    assert (detail["session_key"], detail["span_count"]) == (None, 17)
    assert (detail["transcript"], detail["transcript_coverage"]) == ([], "not_captured_by_preset")


def test_a_call_at_the_revision_cap_shows_the_evidence_after_its_latest_revision(
    tmp_path,
) -> None:
    world = _world(tmp_path)
    _keyed(world, ("capped-call", 77), span=1)
    call_id = world.calls.call_id("default", "capped-call", 0)
    for revision in range(1, 17):
        world.clock.advance(LONG_QUIET)
        assert world.closer.close_next().revision == revision
        _keyed(world, ("capped-call", 77), span=100 + revision)

    with serve(create_app(store=world.store)) as port:
        world.clock.advance(LONG_QUIET + LEASE)
        assert world.closer.close_next() is None  # capped: no revision 17 without an end
        detail = _json(port, f"/v1/calls/{call_id}")

    assert detail["latest_revision"]["revision"] == 16
    assert len(detail["revisions"]) == 16
    assert (detail["span_count"], detail["evidence_after_latest_revision"]) == (17, True)
    assert (detail["transcript"], detail["transcript_coverage"]) == ([], "not_observed")
