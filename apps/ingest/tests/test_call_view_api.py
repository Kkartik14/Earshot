"""The call list's projection fields and filters, the call view's projection, and
the lazy span route.

Every test runs the real app on a real uvicorn server over a real catalog (both
dialects). Calls come from ``CallStore.record_spans`` (the receiver's storage
call); their projections are rows ``projection_support.insert_projection``
writes, as the projection job does, so the reads run their production SQL. The
store's injected clock is set after the wall clock, so the app's own closer and
reapers never act on these calls behind the test's back.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest

from captured_otlp import CAPTURED_SESSION_KEY, captured_requests
from earshot.api import CallTurnResponse, _live_turns_etag, _LiveTurnsBodyCache, create_app
from earshot.call_capture import spans_from_request
from earshot.call_projection import (
    CALL_PROJECTION_VERSION,
    ProjectionInputs,
    ProjectionStamps,
    projection_state,
)
from earshot.clock import ManualClock
from earshot.storage import ProjectInactiveError
from earshot.storage_call_projections import CallProjector
from earshot.storage_call_purge import CallPurger
from earshot.storage_call_reads import CallReads
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingManifest, RecordingStore
from host_profile_support import HOST_SESSION_KEY, HostStage, HostTurn, host_request
from otlp_support import make_request, make_span, request, serve, string_attribute
from projection_support import SUMMARY_COLUMNS, example_detail, insert_projection
from store_support import catalog, catalog_rows, make_store
from test_hosted_jwt import _hosted_config, _token, issuer  # noqa: F401

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
# Each list filter, with a value the projected call in ``_projected_world`` matches.
MATCHING_FILTERS = {
    "agent": "booking-agent",
    "outcome": "completed",
    "tag": "vip",
    "min_reported_latency_ms": 1352,
    "max_reported_latency_ms": 1352,
    "min_audible_gap_ms": 1479,
    "max_audible_gap_ms": 1479,
    "min_cost_nano_usd": 1,
    "q": "window",
}


def _store(tmp_path: Path):
    clock = ManualClock(wall=START, monotonic=START)
    return make_store(tmp_path, clock=clock), clock


def _keyed_call(store, clock: ManualClock, key: str, trace: int, *, spans: int = 1) -> str:
    calls = CallStore(store)
    attributes = [string_attribute("earshot.session.id", key)]
    calls.record_spans(
        "default",
        spans_from_request(
            make_request(
                [
                    make_span(trace, span, start=clock.wall, attributes=attributes)
                    for span in range(1, spans + 1)
                ]
            )
        ),
    )
    clock.advance(SECOND)
    return calls.call_id("default", key, 0)


def _close_all(store, clock: ManualClock) -> None:
    """Close every open call (quiet period): a projection belongs to a revision."""

    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=None)
    closer = Closer(store, calls=calls, recordings=recordings)
    clock.advance(CallTimings().long_quiet_nano)
    while closer.close_next() is not None:
        pass


def _get(port: int, path: str, **params):
    query = urlencode(params, doseq=True)
    return request(port, "GET", f"{path}?{query}" if query else path)


def _ids(reply) -> list[str]:
    assert reply.status == 200, (reply.status, reply.body[:300])
    return [item["call_id"] for item in reply.json()["items"]]


# --- the list ----------------------------------------------------------------------------------


def test_a_call_without_a_projection_lists_as_none_and_no_projection_filter_matches_it(
    tmp_path,
) -> None:
    store, clock = _store(tmp_path)
    call_id = _keyed_call(store, clock, "bare", 1)

    with serve(create_app(store=store)) as port:
        [item] = _get(port, "/v1/calls").json()["items"]
        detail = _get(port, f"/v1/calls/{call_id}").json()
        matched = {
            name: _ids(_get(port, "/v1/calls", **{name: value}))
            for name, value in MATCHING_FILTERS.items()
        }
        capped = _get(port, "/v1/calls", max_cost_nano_usd=10**12).json()

    assert (item["projection_state"], item["projection"]) == ("none", None)
    assert (detail["projection_state"], detail["projection"]) == ("none", None)
    # Unknown is NULL, and a filter never matches NULL.
    assert matched == {name: [] for name in MATCHING_FILTERS}
    assert (capped["items"], capped["excluded_incomplete"]) == ([], 0)


def _projected_world(tmp_path: Path):
    """Four calls: one projected (tagged vip and refund, a transcript line about a
    window seat), one projected with its cost complete and cheap, one projected
    with nothing known, and one still open, so never projected. The projections are
    fixture rows with current stamps: the served app's projection job leaves them."""

    store, clock = _store(tmp_path)
    names = ("projected", "complete", "unknown")
    ids = {name: _keyed_call(store, clock, name, trace) for trace, name in enumerate(names, 1)}
    _close_all(store, clock)
    ids["bare"] = _keyed_call(store, clock, "bare", 4)
    insert_projection(
        tmp_path,
        ids["projected"],
        tags=("vip", "refund"),
        transcript=[("user", "Could we sit near the WINDOW?"), ("agent", "Sure, 100% done_now.")],
    )
    insert_projection(
        tmp_path,
        ids["complete"],
        columns={
            "agent": "support-agent",
            "outcome": "error",
            "reported_latency_p50_ms": 900,
            "audible_gap_p50_ms": 1000,
            "cost_lower_bound_nano_usd": 500,
            "cost_estimated_nano_usd": 200,
            "cost_unknown_items": 0,
        },
        tags=("vip",),
    )
    insert_projection(
        tmp_path,
        ids["unknown"],
        columns={
            name: None
            for name, value in SUMMARY_COLUMNS.items()
            if value is not None and name not in ("turn_count", "measurable_turns")
        }
        | {"cost_unknown_items": 0, "analysis_state": "none"},
    )
    return store, ids


def test_a_listed_projection_carries_its_summary_and_tags(tmp_path) -> None:
    store, ids = _projected_world(tmp_path)

    with serve(create_app(store=store)) as port:
        items = {item["call_id"]: item for item in _get(port, "/v1/calls").json()["items"]}

    assert items[ids["projected"]]["projection_state"] == "current"
    assert items[ids["projected"]]["projection"] == {
        "revision": 1,
        "agent": "booking-agent",
        "framework": "pipecat",
        "outcome": "completed",
        "tags": ["refund", "vip"],
        "duration_ms": 32486,
        "turn_count": 3,
        "measurable_turns": 2,
        "reported_latency_p50_ms": 1352,
        "reported_latency_max_ms": 1800,
        "audible_gap_p50_ms": 1479,
        "audible_gap_max_ms": 1510,
        "cost": {
            "lower_bound_nano_usd": 3_672_825,
            "estimated_nano_usd": 4_169_037,
            "unknown_items": 2,
            "complete": False,
        },
        "analysis_state": "stored",
    }
    assert items[ids["complete"]]["projection"]["cost"]["complete"] is True
    unknown = items[ids["unknown"]]["projection"]
    assert unknown["cost"] == {
        "lower_bound_nano_usd": None,
        "estimated_nano_usd": None,
        "unknown_items": 0,
        "complete": False,
    }
    assert (unknown["agent"], unknown["audible_gap_p50_ms"], unknown["tags"]) == (None, None, [])
    assert items[ids["bare"]]["projection"] is None


def test_list_filters_match_projection_columns_and_never_null(tmp_path) -> None:
    store, ids = _projected_world(tmp_path)
    projected, complete = ids["projected"], ids["complete"]

    with serve(create_app(store=store)) as port:

        def listed(**params) -> list[str]:
            return _ids(_get(port, "/v1/calls", **params))

        each = {name: sorted(listed(**{name: value})) for name, value in MATCHING_FILTERS.items()}
        both_tags = listed(tag=["vip", "refund"])
        vip = listed(tag="vip")
        latency_band = listed(min_reported_latency_ms=800, max_reported_latency_ms=1000)
        gap_floor = listed(min_audible_gap_ms=1001)
        every_filter = listed(**MATCHING_FILTERS, state="closed")
        # max_cost: complete calls only, on lower bound + estimate.
        within = _get(port, "/v1/calls", max_cost_nano_usd=700).json()
        below = _get(port, "/v1/calls", max_cost_nano_usd=699).json()
        generous = _get(port, "/v1/calls", max_cost_nano_usd=10**12).json()
        unfiltered = _get(port, "/v1/calls").json()

    # The complete call is also within each one-sided bound it meets, and tagged vip.
    also_complete = {"tag", "max_reported_latency_ms", "max_audible_gap_ms", "min_cost_nano_usd"}
    assert each == {
        name: sorted([projected, complete]) if name in also_complete else [projected]
        for name in MATCHING_FILTERS
    }
    assert both_tags == [projected]
    assert sorted(vip) == sorted([projected, complete])
    assert latency_band == [complete]
    assert gap_floor == [projected]
    assert every_filter == [projected]
    assert ([item["call_id"] for item in within["items"]], within["excluded_incomplete"]) == (
        [complete],
        0,
    )
    assert (below["items"], below["excluded_incomplete"]) == ([], 0)
    # The projected call's known cost is within the cap, but two items are unknown.
    assert ([item["call_id"] for item in generous["items"]], generous["excluded_incomplete"]) == (
        [complete],
        1,
    )
    assert unfiltered["excluded_incomplete"] is None


def test_q_is_an_escaped_ascii_case_insensitive_substring_of_the_current_presets_transcript(
    tmp_path,
) -> None:
    store, ids = _projected_world(tmp_path)
    projected = ids["projected"]

    with serve(create_app(store=store)) as port:
        searched = _get(port, "/v1/calls", q="window")
        found = {q: _ids(_get(port, "/v1/calls", q=q)) for q in ("window", "Window?", "100%")}
        literal = {q: _ids(_get(port, "/v1/calls", q=q)) for q in ("00%", "e_n", "1_0", "%%%")}
        short = _get(port, "/v1/calls", q="wi")
        long = _get(port, "/v1/calls", q="w" * 257)
        nul = _get(port, "/v1/calls", q="win\x00")
        store.set_capture_preset("default", "metadata_only")
        forbidden = _get(port, "/v1/calls", q="window")
        unsearched = _get(port, "/v1/calls")

    assert found == {q: [projected] for q in ("window", "Window?", "100%")}
    # ``%`` and ``_`` match only themselves.
    assert literal == {"00%": [projected], "e_n": [projected], "1_0": [], "%%%": []}
    for reply in (short, long, nul):
        assert (reply.status, reply.problem_code()) == (422, "EARSHOT_INVALID_REQUEST")
    # ``q`` follows the project's current preset.
    assert (forbidden.status, forbidden.problem_code()) == (
        422,
        "EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH",
    )
    assert unsearched.status == 200
    # A list that searched content is never stored by a cache.
    for reply in (searched, forbidden):
        assert reply.headers["cache-control"] == "no-store"


def test_list_filter_values_are_validated(tmp_path) -> None:
    store, _ = _store(tmp_path)

    with serve(create_app(store=store)) as port:
        replies = {
            params: _get(port, "/v1/calls", **dict([params]))
            for params in (
                ("min_cost_nano_usd", -1),
                ("max_audible_gap_ms", "1.5"),
                ("max_reported_latency_ms", 2**63),
                ("agent", ""),
                ("outcome", "x" * 257),
                ("tag", "a\x00b"),
                ("tag", ("t",) * 11),
            )
        }

    assert {params: reply.status for params, reply in replies.items()} == {
        params: 422 for params in replies
    }


# --- the detail --------------------------------------------------------------------------------


def test_call_detail_keeps_month_1_fields_and_adds_the_projection_and_its_freshness(
    tmp_path,
) -> None:
    store, ids = _projected_world(tmp_path)
    call_id = ids["projected"]
    path = f"/v1/calls/{call_id}"

    with serve(create_app(store=store)) as port:
        listed = {item["call_id"]: item for item in _get(port, "/v1/calls").json()["items"]}
        detail = _get(port, path).json()
    # Freshness, read with no server running (its job would make the call current).
    reads = CallReads(store)
    states = [reads.get_call("default", call_id).summary.projection_state]
    stale_by = [
        ("UPDATE calls SET projection_seq = projection_seq + 1", ()),
        ("UPDATE projects SET price_version = price_version + 1", ()),
        ("UPDATE builtin_price_version SET version = version + 1", ()),
    ]
    for statement, parameters in stale_by:
        _make_current(tmp_path)
        with catalog(tmp_path) as connection:
            connection.execute(statement, parameters)
        states.append(reads.get_call("default", call_id).summary.projection_state)

    summary = {key: value for key, value in listed[call_id].items() if key != "projection"}
    assert {key: value for key, value in detail.items() if key in summary} == summary
    assert {"revisions", "recordings", "transcript", "transcript_coverage"} <= set(detail)
    assert detail["projection"] == {**listed[call_id]["projection"], **example_detail()}
    assert states == ["current", "stale", "stale", "stale"]


def test_a_projection_from_an_older_projection_version_is_stale() -> None:
    inputs = ProjectionInputs(projection_seq=4, price_version=2, builtin_price_version=1)
    stamps = ProjectionStamps(
        source_seq=4,
        price_version=2,
        builtin_price_version=1,
        projection_version=CALL_PROJECTION_VERSION - 1,
    )

    assert projection_state(stamps, inputs) == "stale"
    assert projection_state(
        replace(stamps, projection_version=CALL_PROJECTION_VERSION), inputs
    ) == ("current")
    assert projection_state(None, inputs) == "none"


def test_an_older_projection_keeps_its_reported_cost_quantities(tmp_path) -> None:
    store, ids = _projected_world(tmp_path)
    call_id = ids["projected"]
    with catalog(tmp_path) as connection:
        connection.execute(
            "UPDATE call_projections SET projection_version = ? "
            "WHERE project_id = ? AND call_id = ?",
            (CALL_PROJECTION_VERSION - 1, "default", call_id),
        )

    detail = CallReads(store).get_call("default", call_id)

    assert detail is not None
    assert detail.summary.projection_state == "stale"
    stt = next(
        item for item in detail.projection_detail["cost_items"] if item["operation"] == "stt"
    )
    assert stt["quantity"] == 32.486


def _make_current(path: Path) -> None:
    """Stamp every projection with the versions it would be computed from now."""

    with catalog(path) as connection:
        connection.execute(
            "UPDATE call_projections SET "
            "source_seq = (SELECT c.projection_seq FROM calls c "
            "WHERE c.project_id = call_projections.project_id "
            "AND c.call_id = call_projections.call_id), "
            "price_version = (SELECT p.price_version FROM projects p "
            "WHERE p.project_id = call_projections.project_id), "
            "builtin_price_version = (SELECT version FROM builtin_price_version), "
            "projection_version = ?",
            (CALL_PROJECTION_VERSION,),
        )


def test_call_detail_is_no_store_with_an_etag_and_answers_304_to_a_matching_if_none_match(
    tmp_path,
) -> None:
    store, clock = _store(tmp_path)
    call_id = _keyed_call(store, clock, "etag", 1)
    path = f"/v1/calls/{call_id}"

    with serve(create_app(store=store)) as port:
        first = request(port, "GET", path)
        etag = first.headers["etag"]
        matching = {
            header: request(port, "GET", path, headers={"If-None-Match": header})
            for header in (etag, f"W/{etag}", f'"other", {etag}', "*")
        }
        other = request(port, "GET", path, headers={"If-None-Match": '"sha256:00"'})
        _keyed_call(store, clock, "etag", 1, spans=2)  # one more span: a new body
        changed = request(port, "GET", path, headers={"If-None-Match": etag})

    assert (first.status, first.headers["cache-control"]) == (200, "no-store")
    for reply in matching.values():
        assert (reply.status, reply.body, reply.headers["etag"]) == (304, b"", etag)
        assert reply.headers["cache-control"] == "no-store"
    assert (other.status, other.json()) == (200, first.json())
    assert changed.status == 200 and changed.headers["etag"] != etag
    assert changed.json()["span_count"] == 2


# --- the span route ----------------------------------------------------------------------------


def _captured_spans():
    return [span for item in captured_requests() for span in spans_from_request(item)]


def _host_turns(count: int = 3, *, attributes: dict | None = None) -> list[HostTurn]:
    turns = []
    for index in range(1, count + 1):
        start = START + index * 10 * SECOND
        turns.append(
            HostTurn(
                f"turn-{index}",
                start,
                start + 6 * SECOND,
                user_speech=((start, start + SECOND),),
                agent_speech=(start + 3 * SECOND, start + 6 * SECOND),
                stages=(HostStage("llm", start + SECOND, start + 2 * SECOND),),
                reported_latency_ms=900.0 + index,
                attributes=attributes or {},
            )
        )
    return turns


def _project_revision(store, project_id: str, call_id: str) -> None:
    assert CallProjector(store).project_call(project_id, call_id)


def test_call_turn_response_accepts_a_projection_without_the_new_parent_ref() -> None:
    turn = {**example_detail()["turns"][0]}
    turn.pop("turn_span")

    assert CallTurnResponse.model_validate(turn).turn_span is None


def test_live_turns_rebase_when_the_root_span_arrives_last(tmp_path) -> None:
    store, _ = _store(tmp_path)
    calls = CallStore(store)
    turn_start = START + 5 * SECOND
    request_data = host_request(
        [HostTurn("turn-1", turn_start, turn_start + 3 * SECOND)], ended=False
    )
    spans = spans_from_request(request_data)
    root = next(span for span in spans if span.span.name == "call")
    turn = next(span for span in spans if span.span.name == "turn")
    root.span.start_time_unix_nano = turn_start - 2 * SECOND
    calls.record_spans("default", [turn])
    call_id = calls.call_id("default", HOST_SESSION_KEY, 0)

    with serve(create_app(store=store)) as port:
        path = f"/v1/calls/{call_id}/live-turns"
        before_root = request(port, "GET", path)
        calls.record_spans("default", [root])
        after_root = request(
            port, "GET", path, headers={"If-None-Match": before_root.headers["etag"]}
        )

    assert before_root.status == after_root.status == 200
    assert before_root.json()["axis_origin_unix_nano"] == str(turn_start)
    assert before_root.json()["axis_origin"] == "first_evidence"
    assert before_root.json()["turns"][0]["start_ms"] == 0
    assert after_root.json()["axis_origin_unix_nano"] == str(turn_start - 2 * SECOND)
    assert after_root.json()["axis_origin"] == "first_evidence"
    assert after_root.json()["turns"][0]["start_ms"] == 2_000
    assert after_root.headers["etag"] != before_root.headers["etag"]


def test_live_turns_metadata_only_match_revision_without_text(tmp_path) -> None:
    store, clock = _store(tmp_path)
    store.set_capture_preset("default", "metadata_only")
    sentinel = "SENTINEL-TRANSCRIPT-7f3a"
    calls = CallStore(store)
    calls.record_spans(
        "default",
        spans_from_request(
            host_request(
                _host_turns(
                    attributes={
                        "earshot.transcript.text": sentinel,
                        "gen_ai.prompt": sentinel,
                    }
                )
            )
        ),
    )
    call_id = calls.call_id("default", HOST_SESSION_KEY, 0)

    with serve(create_app(store=store)) as port:
        live = request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        _close_all(store, clock)
        _project_revision(store, "default", call_id)
        closed = request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        detail = request(port, "GET", f"/v1/calls/{call_id}")

    assert sentinel.encode() not in live.body
    assert sentinel.encode() not in closed.body
    assert live.json()["turns"] == closed.json()["turns"]
    assert closed.json()["turns"] == detail.json()["projection"]["turns"]


def test_live_turns_etag_includes_call_state_and_revision(tmp_path) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    calls.record_spans("default", spans_from_request(host_request(_host_turns(1))))
    call_id = calls.call_id("default", HOST_SESSION_KEY, 0)
    clock.advance(SECOND)

    with CallReads(store).live_turns_snapshot("default", call_id) as snapshot:
        assert snapshot is not None
        baseline = _live_turns_etag(snapshot)
        state_changed = _live_turns_etag(replace(snapshot, state="closed"))
        revision_changed = _live_turns_etag(replace(snapshot, revision=1))

    assert state_changed != baseline
    assert revision_changed != baseline


def test_live_turns_body_cache_evicts_by_lru_and_byte_limit() -> None:
    count_bounded = _LiveTurnsBodyCache(max_entries=2, max_bytes=100)
    first = ("project", "call-1", "etag-1")
    second = ("project", "call-2", "etag-2")
    third = ("project", "call-3", "etag-3")
    count_bounded.put(first, b"first")
    count_bounded.put(second, b"second")
    assert count_bounded.get(first) == b"first"
    count_bounded.put(third, b"third")
    assert count_bounded.get(second) is None
    assert count_bounded.get(first) == b"first"
    assert count_bounded.get(third) == b"third"

    byte_bounded = _LiveTurnsBodyCache(max_entries=4, max_bytes=5)
    byte_bounded.put(first, b"12")
    byte_bounded.put(second, b"34")
    assert byte_bounded.get(first) == b"12"
    byte_bounded.put(third, b"abc")
    assert byte_bounded.get(second) is None
    assert byte_bounded.get(first) == b"12"
    assert byte_bounded.get(third) == b"abc"
    byte_bounded.put(("project", "call-4", "etag-4"), b"123456")
    assert byte_bounded.get(("project", "call-4", "etag-4")) is None


def test_another_projects_token_gets_the_detail_404_for_live_turns(tmp_path, issuer) -> None:  # noqa: F811
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="A")
    store.create_project("project-b", display_name="B")
    calls = CallStore(store)
    calls.record_spans("project-a", spans_from_request(host_request(_host_turns(2), ended=False)))
    call_id = calls.call_id("project-a", HOST_SESSION_KEY, 0)

    def headers(project_id: str) -> dict[str, str]:
        token = _token(issuer, project_id=project_id, scope="earshot:calls:read")
        return {"Authorization": f"Bearer {token}"}

    with serve(create_app(store=store, config=_hosted_config(issuer))) as port:
        own = request(port, "GET", f"/v1/calls/{call_id}/live-turns", headers=headers("project-a"))
        other_live = request(
            port, "GET", f"/v1/calls/{call_id}/live-turns", headers=headers("project-b")
        )
        other_detail = request(port, "GET", f"/v1/calls/{call_id}", headers=headers("project-b"))

    assert own.status == 200 and len(own.json()["turns"]) == 2
    assert (other_live.status, other_live.body) == (other_detail.status, other_detail.body)
    assert other_live.status == 404


def test_live_turns_snapshot_rejects_a_project_being_deleted(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_project("project-a", display_name="A")
    calls = CallStore(store)
    calls.record_spans("project-a", spans_from_request(host_request(_host_turns(1), ended=False)))
    call_id = calls.call_id("project-a", HOST_SESSION_KEY, 0)

    assert store.begin_project_deletion("project-a") == "deleting"
    with (
        pytest.raises(ProjectInactiveError),
        CallReads(store).live_turns_snapshot("project-a", call_id),
    ):
        pass


def test_live_turns_wait_for_parent_and_invalidate_on_every_new_span(tmp_path) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    spans = _captured_spans()
    root = next(span for span in spans if span.span.name == "conversation")
    turn = next(span for span in spans if span.span.name == "turn")
    calls.record_spans("default", [root])
    call_id = calls.call_id("default", CAPTURED_SESSION_KEY, 0)
    clock.advance(SECOND)

    with serve(create_app(store=store)) as port:
        path = f"/v1/calls/{call_id}/live-turns"
        no_parent = request(port, "GET", path)
        assert no_parent.status == 200, (no_parent.status, no_parent.body[:300])
        calls.record_spans("default", [turn])
        after_parent = request(
            port, "GET", path, headers={"If-None-Match": no_parent.headers["etag"]}
        )

        # A new descendant that the projected turn summary omits must still invalidate
        # the validator, so a host can observe every accepted child export.
        unprojected_child = make_span(
            int.from_bytes(turn.span.trace_id, "big"),
            900,
            name="unprojected_child",
            parent=int.from_bytes(turn.span.span_id, "big"),
            start=turn.span.start_time_unix_nano,
        )
        calls.record_spans(
            "default",
            spans_from_request(make_request([unprojected_child], scope=turn.scope.name)),
        )
        after_child = request(
            port,
            "GET",
            path,
            headers={"If-None-Match": after_parent.headers["etag"]},
        )
        unchanged = request(
            port,
            "GET",
            path,
            headers={"If-None-Match": after_child.headers["etag"]},
        )

    assert no_parent.status == after_parent.status == after_child.status == 200
    assert no_parent.json()["turns"] == []
    parent_body = after_parent.json()
    assert (parent_body["state"], parent_body["revision"], len(parent_body["turns"])) == (
        "open",
        None,
        1,
    )
    assert parent_body["turns"][0]["turn_span"] == {
        "trace_id": turn.span.trace_id.hex(),
        "span_id": turn.span.span_id.hex(),
    }
    assert after_child.json() == parent_body
    assert after_parent.headers["etag"] != no_parent.headers["etag"]
    assert after_child.headers["etag"] != after_parent.headers["etag"]
    assert (unchanged.status, unchanged.body, unchanged.headers["etag"]) == (
        304,
        b"",
        after_child.headers["etag"],
    )
    assert unchanged.headers["cache-control"] == "no-store"
    assert set(parent_body) == {
        "call_id",
        "state",
        "revision",
        "axis_origin_unix_nano",
        "axis_origin",
        "turns",
    }
    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_projections") == [(0,)]


def test_live_turns_keep_spans_visible_while_the_first_revision_is_materializing(
    tmp_path,
) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    spans = _captured_spans()
    root = next(span for span in spans if span.span.name == "conversation")
    turn = next(span for span in spans if span.span.name == "turn")
    calls.record_spans("default", [root, turn])
    call_id = calls.call_id("default", CAPTURED_SESSION_KEY, 0)
    clock.advance(SECOND)
    path = f"/v1/calls/{call_id}/live-turns"

    with serve(create_app(store=store)) as port:
        before_claim = request(port, "GET", path)

    assert before_claim.status == 200
    before_body = before_claim.json()
    assert before_body["state"] == "open" and len(before_body["turns"]) == 1

    recordings = RecordingStore(store, calls=calls, blobs=None)
    closer = Closer(store, calls=calls, recordings=recordings)
    clock.advance(CallTimings().long_quiet_nano)
    claim = closer.claim()
    assert claim is not None and claim.revision == 1

    # A child accepted after the snapshot remains unassigned while the first Revision
    # is materializing; the live view still includes it in its source version.
    child = make_span(
        int.from_bytes(turn.span.trace_id, "big"),
        900,
        name="unprojected_child",
        parent=int.from_bytes(turn.span.span_id, "big"),
        start=turn.span.start_time_unix_nano,
    )
    calls.record_spans(
        "default",
        spans_from_request(make_request([child], scope=turn.scope.name)),
    )

    with serve(create_app(store=store)) as port:
        during_claim = request(port, "GET", path)
        unchanged = request(
            port,
            "GET",
            path,
            headers={"If-None-Match": during_claim.headers["etag"]},
        )

    assert during_claim.status == 200
    assert during_claim.json() == before_body
    assert during_claim.headers["etag"] != before_claim.headers["etag"]
    assert (unchanged.status, unchanged.body, unchanged.headers["etag"]) == (
        304,
        b"",
        during_claim.headers["etag"],
    )


def test_live_turns_keep_spans_visible_after_the_first_revision_materialization_fails(
    tmp_path,
) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    spans = _captured_spans()
    root = next(span for span in spans if span.span.name == "conversation")
    turn = next(span for span in spans if span.span.name == "turn")
    calls.record_spans("default", [root, turn])
    call_id = calls.call_id("default", CAPTURED_SESSION_KEY, 0)
    clock.advance(SECOND)
    path = f"/v1/calls/{call_id}/live-turns"

    with serve(create_app(store=store)) as port:
        before = request(port, "GET", path)

    recordings = RecordingStore(store, calls=calls, blobs=None)
    closer = Closer(store, calls=calls, recordings=recordings)
    clock.advance(CallTimings().long_quiet_nano)
    claim = closer.claim()
    assert claim is not None and claim.revision == 1
    assert closer.fail(claim, "TEST_MATERIALIZE_FAILED")

    with serve(create_app(store=store)) as port:
        after_failure = request(port, "GET", path)
        unchanged = request(
            port,
            "GET",
            path,
            headers={"If-None-Match": before.headers["etag"]},
        )

    assert before.json()["state"] == "open" and len(before.json()["turns"]) == 1
    assert after_failure.status == 200
    assert after_failure.json() == before.json()
    assert after_failure.headers["etag"] == before.headers["etag"]
    assert (unchanged.status, unchanged.body, unchanged.headers["etag"]) == (
        304,
        b"",
        before.headers["etag"],
    )


def test_live_turns_missing_and_purged_calls_match_call_detail_not_found(tmp_path) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    root = next(span for span in _captured_spans() if span.span.name == "conversation")
    calls.record_spans("default", [root])
    call_id = calls.call_id("default", CAPTURED_SESSION_KEY, 0)
    clock.advance(SECOND)
    purger = CallPurger(
        store, calls=calls, recordings=RecordingStore(store, calls=calls, blobs=None)
    )

    with serve(create_app(store=store)) as port:
        missing_live = request(port, "GET", "/v1/calls/missing/live-turns")
        missing_detail = request(port, "GET", "/v1/calls/missing")
        cached_live = request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        purger.purge_call("default", call_id)
        purged_live = request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        purged_detail = request(port, "GET", f"/v1/calls/{call_id}")

    assert (missing_live.status, missing_live.body) == (missing_detail.status, missing_detail.body)
    assert cached_live.status == 200
    assert (purged_live.status, purged_live.body) == (purged_detail.status, purged_detail.body)
    assert missing_live.status == purged_live.status == 404


def test_live_turns_closed_without_revision_ignores_late_unassigned_turns(tmp_path) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=None)
    created, _ = recordings.create(
        "default",
        RecordingManifest(
            recording_id="failed-only.user",
            session_key="failed-only-recording",
            speaker="user",
            content_type="audio/wav",
            started_at_unix_nano=str(clock.wall),
        ),
    )
    clock.advance(CallTimings().upload_hold_nano + 1)
    assert recordings.fail_stale_uploads() == 1
    closer = Closer(store, calls=calls, recordings=recordings)
    outcome = closer.close_next()
    assert outcome is not None and outcome.result == "closed_without_revision"

    # A session can export a late turn after the recording-only Call closed. It is
    # unassigned evidence until the closer creates Revision 1, so it is not visible
    # through the closed Call's live-turn response.
    late_turn = make_span(
        99,
        1,
        name="turn",
        attributes=[string_attribute("earshot.session.id", "failed-only-recording")],
    )
    with serve(create_app(store=store)) as port:
        path = f"/v1/calls/{created.call_id}/live-turns"
        before_late = request(port, "GET", path)
        calls.record_spans(
            "default",
            spans_from_request(make_request([late_turn], scope="pipecat.turn")),
        )
        live = request(
            port,
            "GET",
            path,
            headers={"If-None-Match": before_late.headers["etag"]},
        )
        detail = request(port, "GET", f"/v1/calls/{created.call_id}")

    assert live.status == detail.status == 200
    assert before_late.json()["turns"] == []
    assert live.headers["etag"] != before_late.headers["etag"]
    assert live.json() == {
        "call_id": created.call_id,
        "state": "closed",
        "revision": None,
        "axis_origin_unix_nano": None,
        "axis_origin": None,
        "turns": [],
    }
    assert detail.json()["latest_revision"] is None

    clock.advance(CallTimings().long_quiet_nano)
    outcome = closer.close_next()
    assert outcome is not None and (outcome.result, outcome.revision) == ("revision", 1)
    with serve(create_app(store=store)) as port:
        after_revision = request(
            port,
            "GET",
            f"/v1/calls/{created.call_id}/live-turns",
            headers={"If-None-Match": live.headers["etag"]},
        )

    assert (after_revision.status, after_revision.json()["revision"]) == (200, 1)
    assert len(after_revision.json()["turns"]) == 1


def test_the_span_route_serves_one_span_of_the_call_and_404_for_any_other(tmp_path) -> None:
    store, clock = _store(tmp_path)
    calls = CallStore(store)
    for item in captured_requests():
        calls.record_spans("default", spans_from_request(item))
        clock.advance(5 * SECOND)
    other = _keyed_call(store, clock, "other-call", 77)
    llm = next(span for span in _captured_spans() if span.span.name == "llm")
    trace_id, span_id = llm.span.trace_id.hex(), llm.span.span_id.hex()
    call_id = _call_of_trace(tmp_path, trace_id)
    other_trace = (77).to_bytes(16, "big").hex()

    with serve(create_app(store=store)) as port:
        found = request(port, "GET", f"/v1/calls/{call_id}/spans/{trace_id}/{span_id}")
        misses = [
            request(port, "GET", path)
            for path in (
                # another call's span, through this call
                f"/v1/calls/{call_id}/spans/{other_trace}/{(1).to_bytes(8, 'big').hex()}",
                # this call's span, through another call
                f"/v1/calls/{other}/spans/{trace_id}/{span_id}",
                f"/v1/calls/{call_id}/spans/{trace_id}/{'0' * 16}",
                f"/v1/calls/{call_id}/spans/{trace_id.upper()}/{span_id}",
                f"/v1/calls/{call_id}/spans/{trace_id}/{span_id[:-1]}",
                f"/v1/calls/call_unknown/spans/{trace_id}/{span_id}",
            )
        ]

    assert found.status == 200, found.body[:300]
    assert found.headers["cache-control"] == "no-store"
    body = found.json()
    assert {key: body[key] for key in ("call_id", "trace_id", "span_id", "name")} == {
        "call_id": call_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "name": "llm",
    }
    assert body["start_unix_nano"] == str(llm.span.start_time_unix_nano)
    assert body["status"] == {"code": "unset", "message": None}
    attributes = {item["key"]: item for item in body["attributes"]}
    assert attributes["output"]["semantic_key"] == "gen_ai.output.messages"
    assert attributes["output"]["capture_class"] == "model_payload"
    assert isinstance(attributes["output"]["value"], str) and attributes["output"]["value"]
    for reply in misses:
        assert (reply.status, reply.problem_code()) == (404, "EARSHOT_SPAN_NOT_FOUND")


def _call_of_trace(path: Path, trace_hex: str) -> str:
    with catalog(path) as connection:
        return connection.execute(
            "SELECT call_id FROM call_traces WHERE trace_id = ?", (bytes.fromhex(trace_hex),)
        ).fetchone()[0]


def test_under_metadata_only_the_span_route_serves_no_content(tmp_path) -> None:
    store, clock = _store(tmp_path)
    store.set_capture_preset("default", "metadata_only")
    calls = CallStore(store)
    for item in captured_requests():
        calls.record_spans("default", spans_from_request(item))
        clock.advance(5 * SECOND)
    llm = next(span for span in _captured_spans() if span.span.name == "llm")
    trace_id, span_id = llm.span.trace_id.hex(), llm.span.span_id.hex()
    call_id = _call_of_trace(tmp_path, trace_id)

    with serve(create_app(store=store)) as port:
        body = request(port, "GET", f"/v1/calls/{call_id}/spans/{trace_id}/{span_id}").json()

    classes = {item["capture_class"] for item in body["attributes"]}
    assert classes <= {"metadata"}, classes
    assert not {"input", "output"} & {item["key"] for item in body["attributes"]}
    assert body["status"]["message"] is None
