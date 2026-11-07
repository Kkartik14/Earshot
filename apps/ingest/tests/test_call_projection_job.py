"""The call projection job on a real catalog.

A call is built the way a server builds it: the real captures recorded through
``CallStore.record_spans``, its recordings stored in a real blob store, the call
closed by the real ``Closer``, each recording analysed by the real analysis job in
a child process, then projected by ``CallProjector``. Every test runs on the
dialect under test.

One test makes the database refuse a call's projection with a real trigger on
``call_projections``: a fault the catalog itself raises, not a fake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("soundfile")

from sqlalchemy import BigInteger, CheckConstraint

from captured_livekit_otlp import (
    LIVEKIT_SESSION_KEY,
    livekit_manifest,
    livekit_requests,
)
from captured_otlp import CAPTURED_SESSION_KEY, captured_manifest, captured_requests
from earshot import storage_schema
from earshot.api import _live_turns_etag, create_app
from earshot.call_capture import spans_from_request
from earshot.call_projection import CALL_PROJECTION_VERSION, compute_projection
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.storage_call_projections import (
    COLUMN_RANGES,
    PROJECTION_RETRY_SECONDS,
    PROJECTION_WRITE_REFUSED,
    TRANSCRIPT_TIME_RANGE,
)
from earshot.storage_call_purge import CallPurger
from earshot.storage_call_reads import CallFilters
from earshot.storage_calls import CallTimings
from earshot.storage_dashboard import DashboardQuery, DashboardReads
from earshot.storage_prices import NewPrice, PriceStore
from otlp_support import make_request, make_span, serve, string_attribute
from otlp_support import request as http_request
from projection_job_support import SECOND, World, gaps
from store_support import catalog, catalog_rows, current_dialect, make_store, stored_needles

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
LIVEKIT_AUDIO = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"


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
    assert gaps(world, call_id) == [
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


def test_live_turns_after_close_match_the_durable_projection_with_recording_analysis(
    tmp_path,
) -> None:
    """The live endpoint must use the durable Revision's stored recording analyses."""

    world, call_id = _pipecat_world(tmp_path)
    world.analyse()
    world.project()
    rows_before = world.written()

    with serve(create_app(store=world.store)) as port:
        live = http_request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        detail = http_request(port, "GET", f"/v1/calls/{call_id}")
        unchanged = http_request(
            port,
            "GET",
            f"/v1/calls/{call_id}/live-turns",
            headers={"If-None-Match": live.headers["etag"]},
        )

    assert live.status == detail.status == 200
    assert live.json()["state"] == "closed"
    assert live.json()["revision"] == detail.json()["latest_revision"]["revision"] == 1
    assert live.json()["turns"] == detail.json()["projection"]["turns"]
    gap = live.json()["turns"][1]["audible_gap"]
    assert (gap["status"], gap["value_ms"], gap["uncertainty_ms"]) == (
        "measured",
        1510,
        75,
    )
    assert (unchanged.status, unchanged.body, unchanged.headers["etag"]) == (
        304,
        b"",
        live.headers["etag"],
    )
    assert unchanged.headers["cache-control"] == "no-store"
    assert world.written() == rows_before


def test_open_live_turns_etag_changes_when_recording_analysis_changes_turns(tmp_path) -> None:
    world = World(tmp_path)
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

    def read_live_turns() -> tuple[str, str, list[dict]]:
        with world.reads.live_turns_snapshot("default", call_id) as snapshot:
            assert snapshot is not None
            turns = compute_projection(snapshot.load_source(), ()).detail["turns"]
            return snapshot.state, _live_turns_etag(snapshot), turns

    before_state, before_etag, before_turns = read_live_turns()
    world.analyse()
    after_state, after_etag, after_turns = read_live_turns()

    assert before_state == after_state == "open"
    assert before_turns != after_turns
    assert before_etag != after_etag


@pytest.mark.parametrize("fixture", ["pipecat", "livekit"])
def test_live_turns_before_close_match_each_captured_fixture_revision(
    tmp_path, fixture: str
) -> None:
    world = World(tmp_path)
    requests = captured_requests() if fixture == "pipecat" else livekit_requests()
    call_id = world.replay(requests)

    with serve(create_app(store=world.store)) as port:
        before_close = http_request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        world.close()
        world.project()
        after_close = http_request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        detail = http_request(port, "GET", f"/v1/calls/{call_id}")

    assert before_close.status == after_close.status == detail.status == 200
    assert before_close.json()["state"] == "open"
    assert before_close.json()["revision"] is None
    assert after_close.json()["state"] == "closed"
    assert after_close.json()["revision"] == detail.json()["latest_revision"]["revision"] == 1
    assert before_close.json()["turns"] == after_close.json()["turns"]
    assert after_close.json()["turns"] == detail.json()["projection"]["turns"]


def test_open_live_turns_keep_pending_claim_recordings_in_the_projection(tmp_path) -> None:
    world = World(tmp_path)
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
    world.analyse()
    path = f"/v1/calls/{call_id}/live-turns"

    with serve(create_app(store=world.store)) as port:
        before_claim = http_request(port, "GET", path)
        world.clock.advance(CallTimings().long_quiet_nano)
        claim = world.closer.claim()
        assert claim is not None and claim.revision == 1
        during_claim = http_request(port, "GET", path)

    assert before_claim.status == during_claim.status == 200
    assert before_claim.json()["state"] == during_claim.json()["state"] == "open"
    assert before_claim.json()["turns"] == during_claim.json()["turns"]
    assert any(turn["audible_gap"]["status"] == "measured" for turn in before_claim.json()["turns"])
    assert any(turn["audible_gap"]["status"] == "measured" for turn in during_claim.json()["turns"])


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

    assert gaps(world, call_id) == [
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


# --- staleness and the compare-and-set write ---------------------------------------------------


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


def test_stale_v7_projection_treats_nonpositive_latency_as_unknown_on_public_reads(
    tmp_path,
) -> None:
    world = World(tmp_path)
    call_id = world.replay(captured_requests())
    world.close()
    world.project()
    [(detail_json, started_unix_nano)] = catalog_rows(
        tmp_path,
        "SELECT detail_json, started_unix_nano FROM call_projections "
        "WHERE project_id = ? AND call_id = ?",
        ("default", call_id),
    )
    detail = json.loads(detail_json)
    turns = detail["turns"]
    assert len(turns) == 3
    turns[0]["reported_latency_ms"] = 0
    turns[1]["reported_latency_ms"] = 1000
    turns[2]["reported_latency_ms"] = 1100
    fourth = dict(turns[2], index=max(turn["index"] for turn in turns) + 1)
    fourth["reported_latency_ms"] = 1200
    turns.append(fourth)
    with catalog(tmp_path) as connection:
        connection.execute(
            "UPDATE call_projections SET projection_version = 7, turn_count = 4, "
            "reported_latency_p50_ms = 1000, reported_latency_max_ms = 1200, detail_json = ? "
            "WHERE project_id = ? AND call_id = ?",
            (json.dumps(detail), "default", call_id),
        )

    call = world.reads.get_call("default", call_id)
    assert call is not None
    assert call.summary.projection_state == "stale"
    assert call.summary.projection is not None
    assert (
        call.summary.projection.reported_latency_p50_ms,
        call.summary.projection.reported_latency_max_ms,
    ) == (None, None)
    assert [turn["reported_latency_ms"] for turn in call.projection_detail["turns"]] == [
        None,
        1000,
        1100,
        1200,
    ]

    page = world.reads.list_calls("default")
    [listed] = page.items
    assert listed.projection_state == "stale"
    assert listed.projection is not None
    assert (
        listed.projection.reported_latency_p50_ms,
        listed.projection.reported_latency_max_ms,
    ) == (None, None)
    assert not world.reads.list_calls(
        "default", filters=CallFilters(min_reported_latency_ms=1000)
    ).items
    assert not world.reads.list_calls(
        "default", filters=CallFilters(max_reported_latency_ms=1000)
    ).items

    dashboard = DashboardReads(world.store).dashboard(
        "default", DashboardQuery(started_unix_nano, started_unix_nano + 1, bucket="day")
    )
    assert dashboard["overall"]["reported_latency"] == {
        "p50_ms": 1100,
        "p95_ms": 1200,
        "turns": 3,
        "of_turns": 4,
        "calls": 1,
        "of_calls": 1,
    }

    world.project()
    recomputed = world.reads.get_call("default", call_id)
    assert recomputed is not None
    assert recomputed.summary.projection_state == "current"
    assert recomputed.summary.projection is not None
    assert recomputed.summary.projection.reported_latency_p50_ms > 0
    assert recomputed.summary.projection.reported_latency_max_ms > 0
    assert all(
        latency is None or latency > 0
        for latency in (
            turn["reported_latency_ms"] for turn in recomputed.projection_detail["turns"]
        )
    )


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


# --- a call's failure is its own (one call never stalls the others) -----------------------------

LATER_PROJECT = "zz-later"


def _later_call(world: World) -> str:
    """A healthy call in a project that sorts after ``default``, so the job reaches it
    only after the call under test."""

    world.store.create_project(LATER_PROJECT, display_name="Later")
    span = make_span(9, 1, attributes=[string_attribute("earshot.session.id", "fine")])
    world.calls.record_spans(LATER_PROJECT, spans_from_request(make_request([span])))
    return world.calls.call_id(LATER_PROJECT, "fine", 0)


def _state(world: World, project_id: str, call_id: str) -> str:
    return world.reads.get_call(project_id, call_id).summary.projection_state


def _spans_named(requests, name: str):
    for request in requests:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                yield from (span for span in scope_spans.spans if span.name == name)


@pytest.mark.parametrize("seconds", [1e20, 1e30])
def test_an_out_of_range_reported_latency_is_unknown_and_never_stalls_the_job(
    tmp_path, seconds: float
) -> None:
    world = World(tmp_path)
    requests = captured_requests()
    for span in _spans_named(requests, "turn"):
        for item in span.attributes:
            if item.key == "turn.user_bot_latency_seconds":
                item.value.double_value = seconds
    poisoned = world.replay(requests)
    later = _later_call(world)
    world.close()

    world.project()

    assert _state(world, LATER_PROJECT, later) == "current"
    assert _state(world, "default", poisoned) == "current"
    summary = world.projection(poisoned)["summary"]
    assert (summary.reported_latency_p50_ms, summary.reported_latency_max_ms) == (None, None)
    assert world.projector.failure("default", poisoned) is None


def test_text_holding_u0000_is_never_written_and_never_stalls_the_job(tmp_path) -> None:
    world = World(tmp_path)
    requests = captured_requests()
    next(_spans_named(requests, "turn")).attributes.add(
        key="earshot.agent.name"
    ).value.string_value = "bad\x00agent"
    for span in _spans_named(requests, "stt"):
        for item in span.attributes:
            if item.key == "transcript":
                item.value.string_value += "\x00"
    poisoned = world.replay(requests)
    later = _later_call(world)
    world.close()

    world.project()

    assert _state(world, LATER_PROJECT, later) == "current"
    assert _state(world, "default", poisoned) == "current"
    assert world.projection(poisoned)["summary"].agent == "earshot-pipecat-otlp-example"
    texts = [text for (text,) in catalog_rows(tmp_path, "SELECT text FROM call_transcripts")]
    assert texts and not any("\x00" in text for text in texts)


def _refuse_projections_of(path: Path, call_id: str) -> None:
    with catalog(path) as connection:
        if current_dialect() == "sqlite":
            connection.execute(
                "CREATE TRIGGER refuse_projection BEFORE INSERT ON call_projections "
                f"WHEN NEW.call_id = '{call_id}' BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            return
        connection.execute(
            "CREATE FUNCTION refuse_projection() RETURNS trigger AS $$ BEGIN "
            f"IF NEW.call_id = '{call_id}' THEN RAISE EXCEPTION 'refused'; END IF; "
            "RETURN NEW; END $$ LANGUAGE plpgsql"
        )
        connection.execute(
            "CREATE TRIGGER refuse_projection BEFORE INSERT ON call_projections "
            "FOR EACH ROW EXECUTE FUNCTION refuse_projection()"
        )


def _accept_projections(path: Path) -> None:
    with catalog(path) as connection:
        if current_dialect() == "sqlite":
            connection.execute("DROP TRIGGER refuse_projection")
            return
        connection.execute("DROP TRIGGER refuse_projection ON call_projections")
        connection.execute("DROP FUNCTION refuse_projection()")


def test_a_refused_write_sets_its_call_aside_with_backoff_and_the_job_moves_on(
    tmp_path, caplog
) -> None:
    world = World(tmp_path)
    refused = world.replay(captured_requests())
    later = _later_call(world)
    world.close()
    _refuse_projections_of(tmp_path, refused)

    world.project()  # returns: the refusal is the call's, not the job's

    assert _state(world, LATER_PROJECT, later) == "current"
    assert _state(world, "default", refused) == "none"
    first = world.projector.failure("default", refused)
    assert (first.code, first.attempts) == (PROJECTION_WRITE_REFUSED, 1)
    assert f"EARSHOT_PROJECTION_FAILED: call {refused}" in caplog.text
    assert PROJECTION_WRITE_REFUSED in caplog.text
    world.project()  # set aside until its retry is due
    assert world.projector.failure("default", refused).attempts == 1

    world.clock.advance(PROJECTION_RETRY_SECONDS[0] * SECOND)
    world.project()
    second = world.projector.failure("default", refused)
    assert second.attempts == 2
    assert second.retry_unix_nano == world.clock.unix_nano() + PROJECTION_RETRY_SECONDS[1] * SECOND

    _accept_projections(tmp_path)
    world.clock.advance(PROJECTION_RETRY_SECONDS[1] * SECOND)
    world.project()
    assert _state(world, "default", refused) == "current"
    assert world.projector.failure("default", refused) is None


def test_the_bounds_checked_before_a_write_are_the_catalog_columns() -> None:
    metadata = storage_schema.build_v19_baseline_metadata()
    storage_schema.add_calls_tables(metadata)
    for table, column in storage_schema.projection_columns():
        metadata.tables[table].append_column(column)
    storage_schema.add_projection_tables(metadata)

    def bounds(table: str, name: str) -> tuple[int, int]:
        column = metadata.tables[table].c[name]
        checks = {
            str(item.sqltext)
            for item in (*column.constraints, *metadata.tables[table].constraints)
            if isinstance(item, CheckConstraint)
        }
        bits = 64 if isinstance(column.type, BigInteger) else 32
        low = 0 if f"{name} >= 0" in checks else -(2 ** (bits - 1))
        return low, 2 ** (bits - 1) - 1

    stamps = {
        "revision",
        "source_seq",
        "price_version",
        "builtin_price_version",
        "projection_version",
        "updated_unix_nano",
    }
    integers = {
        column.name
        for column in metadata.tables["call_projections"].columns
        if column.type.python_type is int and column.name not in stamps
    }
    assert set(COLUMN_RANGES) == integers
    assert {name: bounds("call_projections", name) for name in integers} == COLUMN_RANGES
    assert bounds("call_transcripts", "start_ms") == TRANSCRIPT_TIME_RANGE
    assert bounds("call_transcripts", "end_ms") == TRANSCRIPT_TIME_RANGE


# --- a projection is its revision's ----------------------------------------------------------


def test_a_projection_follows_its_revisions_close_not_a_later_end_signal(tmp_path) -> None:
    world = World(tmp_path)
    requests = livekit_requests()
    call_id = world.replay(requests[:5])
    world.close()  # revision 1 closes on the quiet period: provisional
    for request in requests[5:]:  # the end signal arrives after it
        world.calls.record_spans("default", spans_from_request(request))
    with catalog(tmp_path) as connection:  # an analysis result lands, say
        connection.execute("UPDATE calls SET projection_seq = projection_seq + 1")

    world.project()

    assert catalog_rows(
        tmp_path, "SELECT latest_revision, end_signal_unix_nano IS NOT NULL FROM calls"
    ) in ([(1, True)], [(1, 1)])
    summary = world.projection(call_id)["summary"]
    assert (summary.revision, summary.outcome) == (1, "interrupted")
    world.close()
    world.project()
    summary = world.projection(call_id)["summary"]
    assert (summary.revision, summary.outcome) == (2, "completed")


def _shown(world: World, call_id: str) -> tuple:
    summary = world.projection(call_id)["summary"]
    return summary.revision, summary.turn_count, summary.outcome


def test_late_evidence_changes_a_projection_only_once_a_revision_holds_it(tmp_path) -> None:
    world = World(tmp_path)
    requests = livekit_requests()
    call_id = world.replay(requests[:5])
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
        duration_ms=34_010,
    )
    world.close()
    world.analyse()
    world.project()
    assert _shown(world, call_id) == (1, 2, "interrupted")

    for request in requests[5:]:  # late evidence: the call is dirty, still at revision 1
        world.calls.record_spans("default", spans_from_request(request))
    with catalog(tmp_path) as connection:  # an algorithm upgrade requeues its analysis
        connection.execute("UPDATE audio_analysis SET algorithm_version = 0")
    world.runner.analyses.requeue(extra_available=True)
    world.analyse()
    world.project()
    assert _shown(world, call_id) == (1, 2, "interrupted")
    assert world.projection(call_id)["state"] == "current"

    world.close()
    world.project()
    assert _shown(world, call_id) == (2, 3, "completed")


# --- prices ------------------------------------------------------------------------------------


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


# --- transcripts and search --------------------------------------------------------------------


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


# --- purge -------------------------------------------------------------------------------------


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
