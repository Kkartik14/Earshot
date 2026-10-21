"""The call list's projection fields and filters, the call view's projection, and
the lazy span route (FR-5, FR-6; M7, M8, M14, M18, M23, M24, M26).

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

from captured_otlp import captured_requests
from earshot.api import create_app
from earshot.call_capture import spans_from_request
from earshot.call_projection import (
    CALL_PROJECTION_VERSION,
    ProjectionInputs,
    ProjectionStamps,
    projection_state,
)
from earshot.clock import ManualClock
from earshot.storage_call_reads import CallReads
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingStore
from otlp_support import make_request, make_span, request, serve, string_attribute
from projection_support import SUMMARY_COLUMNS, example_detail, insert_projection
from store_support import catalog, make_store

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
    # Unknown is NULL, and a filter never matches NULL (M8).
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
        # max_cost: complete calls only, on lower bound + estimate (M7).
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
    # ``q`` follows the project's current preset (M26).
    assert (forbidden.status, forbidden.problem_code()) == (
        422,
        "EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH",
    )
    assert unsearched.status == 200
    # A list that searched content is never stored by a cache (M18).
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
        source_seq=4, price_version=2, builtin_price_version=1, projection_version=0
    )

    assert projection_state(stamps, inputs) == "stale"
    assert projection_state(
        replace(stamps, projection_version=CALL_PROJECTION_VERSION), inputs
    ) == ("current")
    assert projection_state(None, inputs) == "none"


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
