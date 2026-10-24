"""``GET /v1/dashboard`` on a real server over a real catalog (both dialects).

Calls are built the way a server builds them: spans recorded through
``CallStore.record_spans``, closed by the real ``Closer`` (which stores each
revision's incident and its ``turn_metrics`` rows), then projected. Where a test
must choose a projection's per-turn or per-call values, it takes the projection
the real job computes for the call, sets those values on it, and writes it with
``CallProjector.write``, the job's own compare-and-set write: fixture values on
the real write path, not a fake. The built-in prices are seeded first, so the
served app's projection job finds every projection current and leaves it alone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from captured_otlp import captured_requests
from earshot.api import create_app
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.storage_call_projections import CallProjector, Computed
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_prices import PriceStore
from earshot.storage_recordings import RecordingStore
from otlp_support import make_request, make_span, request, serve, string_attribute
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR
# After the wall clock, so the served app's own closer and reapers never act on
# these calls; day-aligned, so hour and day buckets start on it.
BASE = (1_800_000_000 * SECOND) // DAY * DAY


class World:
    def __init__(self, path: Path, *, wall: int = BASE) -> None:
        self.path = path
        self.clock = ManualClock(wall=wall, monotonic=wall)
        self.store = make_store(path, clock=self.clock)
        self.calls = CallStore(self.store)
        recordings = RecordingStore(self.store, calls=self.calls, blobs=None)
        self.closer = Closer(self.store, calls=self.calls, recordings=recordings)
        self.projector = CallProjector(self.store)
        PriceStore(self.store).seed_builtins(BUILTIN_PRICES)

    def call(
        self,
        key: str,
        *,
        start: int,
        agent: str | None = None,
        scope: str = "pipecat",
        project_id: str = "default",
    ) -> str:
        """A one-span call whose evidence starts at ``start`` (its receive time is
        the clock's, whatever ``start`` is)."""

        attributes = [string_attribute("earshot.session.id", key)]
        if agent is not None:
            attributes.append(string_attribute("earshot.agent.name", agent))
        trace = int.from_bytes(key.encode().ljust(16, b"\0")[:16], "big")
        span = make_span(trace, 1, start=start, attributes=attributes)
        self.calls.record_spans(
            project_id, spans_from_request(make_request([span], scope=scope, resource=()))
        )
        self.clock.advance(SECOND)
        return self.calls.call_id(project_id, key, 0)

    def close(self) -> None:
        self.clock.advance(CallTimings().long_quiet_nano)
        while self.closer.close_next() is not None:
            pass

    def project(self) -> None:
        """Project every closed call as the server's job does."""

        while self.projector.run_pending().more:
            pass

    def project_with(
        self,
        call_id: str,
        *,
        turns: list[tuple[int | None, int | None]] | None = None,
        **columns: Any,
    ) -> None:
        """Write the call's real projection with ``columns`` set and, given
        ``turns`` as ``(reported_latency_ms, audible_gap_ms)`` (``None``: not
        reported, not measured), those turns."""

        snapshot = self.projector.snapshot("default", call_id)
        assert snapshot is not None
        projection = self.projector.compute(snapshot).projection
        assert projection is not None
        detail = projection.detail
        if turns is not None:
            detail = {**detail, "turns": [_turn(index, *turn) for index, turn in enumerate(turns)]}
            columns = {
                "turn_count": len(turns),
                "measurable_turns": sum(gap is not None for _, gap in turns),
                **columns,
            }
        projection = replace(projection, columns={**projection.columns, **columns}, detail=detail)
        written = Computed(projection=projection, digest=projection.input_digest())
        assert self.projector.write(snapshot, written)


def _turn(index: int, reported_ms: int | None, gap_ms: int | None) -> dict[str, Any]:
    return {
        "index": index,
        "turn_id": None,
        "start_ms": None,
        "end_ms": None,
        "reported_latency_ms": reported_ms,
        "audible_gap": {
            "status": "not_measurable" if gap_ms is None else "measured",
            "value_ms": gap_ms,
            "uncertainty_ms": None if gap_ms is None else 30,
            "bound_ms": None,
            "user_end_ms": None,
            "agent_start_ms": None,
            "reason": "no_recording" if gap_ms is None else None,
            "basis": None if gap_ms is None else "anchors_checked",
        },
        "spans": [],
    }


def _dashboard(port: int, **params: Any):
    return request(port, "GET", f"/v1/dashboard?{urlencode(params)}")


def _range(start: int, end: int, **params: Any) -> dict[str, Any]:
    return {"from": str(start), "to": str(end), **params}


# --- latency: percentiles over turns --------------------------------------------------------------


def test_latency_percentiles_are_over_the_turns_of_each_calls_projection(tmp_path) -> None:
    world = World(tmp_path)
    short = world.call("short", start=BASE + MINUTE)
    long = world.call("long", start=BASE + 2 * MINUTE)
    silent = world.call("silent", start=BASE + 3 * MINUTE)
    world.close()
    # One fast turn, then nineteen slow ones: the median of per-call medians would
    # hide the slow tail entirely.
    world.project_with(short, turns=[(100, None)])
    world.project_with(
        long,
        turns=[(1000 * k, 500 * k if k <= 10 else None) for k in range(1, 20)],
    )
    # Unknown values are never zeros: these turns count as turns, not as values.
    world.project_with(silent, turns=[(None, None), (None, None)])

    with serve(create_app(store=world.store)) as port:
        reply = _dashboard(port, **_range(BASE, BASE + HOUR))

    assert reply.status == 200, reply.body[:300]
    assert reply.headers["cache-control"] == "no-store"
    tiles = reply.json()["overall"]
    assert tiles["calls"] == 3
    # 20 reported values: nearest rank 10 is 9000 ms and rank 19 is 18000 ms.
    assert tiles["reported_latency"] == {
        "p50_ms": 9000,
        "p95_ms": 18000,
        "turns": 20,
        "of_turns": 22,
        "calls": 2,
        "of_calls": 3,
    }
    # 10 measured gaps (500..5000 ms): ranks 5 and 10.
    assert tiles["audible_gap"] == {
        "p50_ms": 2500,
        "p95_ms": 5000,
        "turns": 10,
        "of_turns": 22,
        "calls": 1,
        "of_calls": 3,
    }


def test_llm_first_token_counts_only_the_turns_of_the_projected_revision(tmp_path) -> None:
    world = World(tmp_path)
    call_ids: set[str] = set()
    for item in captured_requests():
        call_ids.update(world.calls.record_spans("default", spans_from_request(item)).call_ids)
        world.clock.advance(5 * SECOND)
    [call_id] = call_ids
    world.close()
    # Late data: one more span of the call's trace makes a second revision, whose
    # incident has turn rows of its own beside the first revision's.
    late = captured_requests()[-1]
    late.resource_spans[0].scope_spans[0].spans[0].span_id = b"\x01" * 8
    world.calls.record_spans("default", spans_from_request(late))
    world.close()
    world.project()
    revisions = catalog_rows(
        tmp_path,
        "SELECT r.revision, m.first_token_ms FROM turn_metrics m JOIN call_revisions r "
        "ON r.project_id = m.project_id AND r.bundle_id = m.bundle_id "
        "WHERE r.call_id = ? AND m.first_token_availability = 'available' "
        "ORDER BY m.first_token_ms",
        (call_id,),
    )
    second = sorted(round(value) for revision, value in revisions if revision == 2)
    assert {revision for revision, _ in revisions} == {1, 2}
    [(started,)] = catalog_rows(tmp_path, "SELECT started_unix_nano FROM call_projections")

    with serve(create_app(store=world.store)) as port:
        reply = _dashboard(port, **_range(int(started), int(started) + HOUR))

    llm = reply.json()["overall"]["llm_first_token"]
    rank = lambda percent: second[(percent * len(second) + 99) // 100 - 1]  # noqa: E731
    assert llm == {
        "providers": [
            {
                "provider": "groq",
                "basis": "provider_stage_direct",
                "p50_ms": rank(50),
                "p95_ms": rank(95),
                "turns": len(second),
                "calls": 1,
            }
        ],
        "calls": 1,
        "of_calls": 1,
    }


# --- buckets and groups -------------------------------------------------------------------------


def test_calls_are_bucketed_and_grouped_by_their_own_start_not_their_receive_time(
    tmp_path,
) -> None:
    # Every call arrives at 03:30, after its evidence started (a webhook, an import).
    world = World(tmp_path, wall=BASE + 3 * HOUR + 30 * MINUTE)
    world.call("a-1", start=BASE + 10 * MINUTE, agent="a")
    world.call("a-2", start=BASE + 50 * MINUTE, agent="a")
    world.call("b-1", start=BASE + 2 * HOUR + 5 * MINUTE, agent="b")
    world.call("anonymous", start=BASE + 3 * HOUR, scope="custom")
    world.call("earlier", start=BASE - HOUR, agent="a")
    world.store.create_project("other", display_name="Other")
    world.call("other", start=BASE + 10 * MINUTE, agent="a", project_id="other")
    world.close()
    world.project()
    # Received in range, not projected yet: the dashboard says so instead of
    # placing it anywhere.
    world.call("open", start=BASE + 20 * MINUTE, agent="a")

    with serve(create_app(store=world.store)) as port:
        hourly = _dashboard(port, **_range(BASE, BASE + 4 * HOUR, bucket="hour", group_by="agent"))
        daily = _dashboard(port, **_range(BASE, BASE + 4 * HOUR, bucket="day"))

    body = hourly.json()
    assert (body["bucket"], body["group_by"]) == ("hour", "agent")
    assert (body["from_unix_nano"], body["to_unix_nano"]) == (str(BASE), str(BASE + 4 * HOUR))
    assert [(item["start_unix_nano"], item["tiles"]["calls"]) for item in body["buckets"]] == [
        (str(BASE), 2),
        (str(BASE + HOUR), 0),
        (str(BASE + 2 * HOUR), 1),
        (str(BASE + 3 * HOUR), 1),
    ]
    assert [(item["value"], item["tiles"]["calls"]) for item in body["groups"]] == [
        ("a", 2),
        ("b", 1),
        (None, 1),
    ]
    assert body["overall"]["calls"] == 4
    assert body["not_projected_calls"] == 1
    # Nothing here reports a provider cost: its coverage is none, never $0.
    assert body["overall"]["cost_per_minute"]["provider"] == {
        "nano_usd_per_minute": None,
        "cost_nano_usd": None,
        "duration_ms": None,
        "calls": 0,
        "of_calls": 4,
        "bases": [],
    }
    by_framework = daily.json()
    assert [
        (item["start_unix_nano"], item["tiles"]["calls"]) for item in by_framework["buckets"]
    ] == [(str(BASE), 4)]
    assert [(item["value"], item["tiles"]["calls"]) for item in by_framework["groups"]] == [
        ("pipecat", 3),
        (None, 1),
    ]


# --- per-call tiles: coverage is the calls that measured them ------------------------------------


def test_per_call_tiles_count_only_calls_that_measured_them_and_never_sum_costs(
    tmp_path,
) -> None:
    world = World(tmp_path)
    ids = [world.call(f"call-{index}", start=BASE + index * MINUTE) for index in range(3)]
    world.close()
    # A two-channel recording on one timeline: dead air and barge-ins measured.
    world.project_with(
        ids[0],
        turn_count=10,
        duration_ms=60_000,
        dead_air_ms=3_000,
        barge_in_count=2,
        cost_lower_bound_nano_usd=1_000,
        cost_estimated_nano_usd=200,
        cost_unknown_items=0,
        provider_cost_nano_usd=5_000,
        provider_cost_basis="vapi.cost",
    )
    world.project_with(
        ids[1],
        turn_count=5,
        duration_ms=120_000,
        dead_air_ms=0,
        barge_in_count=0,
        cost_lower_bound_nano_usd=3_000,
        cost_estimated_nano_usd=None,
        cost_unknown_items=0,
    )
    # No recording, and a cost with unknown items: neither is a zero.
    world.project_with(
        ids[2],
        turn_count=4,
        duration_ms=30_000,
        dead_air_ms=None,
        barge_in_count=None,
        cost_lower_bound_nano_usd=100,
        cost_estimated_nano_usd=None,
        cost_unknown_items=2,
        provider_cost_nano_usd=900,
        provider_cost_basis="retell.combined_cost",
    )

    with serve(create_app(store=world.store)) as port:
        tiles = _dashboard(port, **_range(BASE, BASE + HOUR)).json()["overall"]

    assert tiles["interruption_rate"] == {"barge_ins": 2, "turns": 15, "calls": 2, "of_calls": 3}
    assert tiles["dead_air"] == {
        "dead_air_ms": 3_000,
        "duration_ms": 180_000,
        "calls": 2,
        "of_calls": 3,
    }
    # Earshot's cost: complete calls only, lower bound plus estimate, 4200 over 3 min.
    # The provider's: what the provider reported, 5900 over 1.5 min (half up).
    # Neither includes the other.
    assert tiles["cost_per_minute"] == {
        "earshot": {
            "nano_usd_per_minute": 1_400,
            "cost_nano_usd": 4_200,
            "duration_ms": 180_000,
            "calls": 2,
            "of_calls": 3,
        },
        "provider": {
            "nano_usd_per_minute": 3_933,
            "cost_nano_usd": 5_900,
            "duration_ms": 90_000,
            "calls": 2,
            "of_calls": 3,
            "bases": ["retell.combined_cost", "vapi.cost"],
        },
    }


# --- the request ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "path"),
    [
        ({"to": str(BASE)}, ["query", "from"]),
        ({"from": str(BASE)}, ["query", "to"]),
        (_range(BASE, BASE), ["query", "to"]),
        ({"from": "-1", "to": str(BASE)}, ["query", "from"]),
        ({"from": "1e9", "to": str(BASE)}, ["query", "from"]),
        ({"from": "0", "to": "9223372036854775808"}, ["query", "to"]),
        (_range(BASE, BASE + DAY, bucket="week"), ["query", "bucket"]),
        (_range(BASE, BASE + DAY, group_by="model"), ["query", "group_by"]),
        (_range(BASE, BASE + 1001 * HOUR, bucket="hour"), ["query", "to"]),
    ],
)
def test_a_malformed_range_is_rejected_with_the_parameter_named(tmp_path, params, path) -> None:
    store = make_store(tmp_path)

    with serve(create_app(store=store)) as port:
        reply = _dashboard(port, **params)

    assert reply.status == 422, reply.body[:300]
    assert reply.problem_code() == "EARSHOT_INVALID_REQUEST"
    assert path in [issue["path"] for issue in reply.json()["error"]["issues"]]
