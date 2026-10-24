"""A real Earshot server holding projected calls, for the viewer's dashboard to read.

Nothing here simulates Earshot. The server is the real app (``create_app``) on a
real uvicorn server over a real SQLite catalog. Every call's evidence starts within
the few hours before this script's "now", which the test uses as the browser's clock,
so the dashboard's "last 24 hours" range holds it:

- the real Pipecat 1.5 capture, its span times moved to three hours ago, recorded
  through ``CallStore.record_spans``, closed by the real ``Closer`` (which stores
  the revision's incident and its LLM first-token rows) and projected by the real
  projection job;
- three one-span calls whose projections carry chosen per-turn and per-call values
  (latency, audible gap, barge-ins, dead air, Earshot's and a provider's cost): the
  real job's projection with those values set, written with the job's own
  compare-and-set write (``CallProjector.write``);
- one open call, received in range and never projected.

The script prints one JSON line (the port and "now") when the server is ready and
serves until its standard input closes. "Now" is a day after the wall clock: the
served app's own closer runs on the real clock, so evidence dated before the real
time would fall due and the open call would be closed and projected while the test
reads it.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[6]
for path in (ROOT / "packages" / "sdk-python" / "src", ROOT / "packages" / "sdk-python" / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from captured_otlp import captured_requests  # noqa: E402
from earshot.api import create_app  # noqa: E402
from earshot.call_capture import spans_from_request  # noqa: E402
from earshot.clock import ManualClock  # noqa: E402
from earshot.pricing_defaults import BUILTIN_PRICES  # noqa: E402
from earshot.storage_call_projections import CallProjector, Computed  # noqa: E402
from earshot.storage_calls import CallStore, CallTimings  # noqa: E402
from earshot.storage_closer import Closer  # noqa: E402
from earshot.storage_prices import PriceStore  # noqa: E402
from earshot.storage_recordings import RecordingStore  # noqa: E402
from otlp_support import make_request, make_span, serve, string_attribute  # noqa: E402
from store_support import make_store  # noqa: E402

SECOND = 1_000_000_000
HOUR = 3_600 * SECOND
DAY = 24 * HOUR
# (key, agent, hours ago, (reported_ms, audible_gap_ms) per turn, projection columns)
CHOSEN: list[tuple[str, str | None, int, list[tuple[int | None, int | None]], dict[str, Any]]] = [
    (
        "measured",
        "support-agent",
        2,
        [(900, 1000), (1100, 1200)],
        {
            "duration_ms": 60_000,
            "dead_air_ms": 3_000,
            "barge_in_count": 2,
            "cost_lower_bound_nano_usd": 50_000_000,
            "cost_estimated_nano_usd": 10_000_000,
            "cost_unknown_items": 0,
            "provider_cost_nano_usd": 90_000_000,
            "provider_cost_basis": "vapi.cost",
        },
    ),
    (
        "quiet",
        "support-agent",
        2,
        [(700, None)],
        {
            "duration_ms": 120_000,
            "dead_air_ms": 0,
            "barge_in_count": 0,
            "cost_lower_bound_nano_usd": 120_000_000,
            "cost_estimated_nano_usd": None,
            "cost_unknown_items": 0,
        },
    ),
    (
        "hosted",
        None,
        1,
        [(None, None)],
        {
            "duration_ms": 30_000,
            "dead_air_ms": None,
            "barge_in_count": None,
            "cost_lower_bound_nano_usd": None,
            "cost_estimated_nano_usd": None,
            "cost_unknown_items": 1,
            "provider_cost_nano_usd": 45_000_000,
            "provider_cost_basis": "retell.combined_cost",
        },
    ),
]


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


def _moved_capture(start: int) -> list[Any]:
    """The Pipecat capture with every span time moved so its evidence starts at
    ``start``."""

    requests = captured_requests()
    spans = [
        span
        for item in requests
        for resource in item.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    shift = start - min(span.start_time_unix_nano for span in spans)
    for span in spans:
        span.start_time_unix_nano += shift
        span.end_time_unix_nano += shift
        for event in span.events:
            event.time_unix_nano += shift
    return requests


def _one_span_call(calls: CallStore, clock: ManualClock, key: str, agent: str | None, start: int):
    attributes = [string_attribute("earshot.session.id", key)]
    if agent is not None:
        attributes.append(string_attribute("earshot.agent.name", agent))
    trace = int.from_bytes(key.encode().ljust(16, b"\0")[:16], "big")
    request = make_request([make_span(trace, 1, start=start, attributes=attributes)], resource=())
    calls.record_spans("default", spans_from_request(request))
    clock.advance(SECOND)
    return calls.call_id("default", key, 0)


def _write_chosen(projector: CallProjector, call_id: str, turns, columns) -> None:
    snapshot = projector.snapshot("default", call_id)
    assert snapshot is not None
    projection = projector.compute(snapshot).projection
    assert projection is not None
    detail = {**projection.detail, "turns": [_turn(i, *turn) for i, turn in enumerate(turns)]}
    columns = {
        **projection.columns,
        "turn_count": len(turns),
        "measurable_turns": sum(gap is not None for _, gap in turns),
        **columns,
    }
    projection = replace(projection, columns=columns, detail=detail)
    assert projector.write(
        snapshot, Computed(projection=projection, digest=projection.input_digest())
    )


def build(work: Path) -> None:
    now = time.time_ns() + DAY
    clock = ManualClock(wall=now - HOUR, monotonic=now - HOUR)
    store = make_store(work, clock=clock)
    # Seeded before projecting, so the served app's seeding changes no price version
    # and its projection job finds every projection current.
    PriceStore(store).seed_builtins(BUILTIN_PRICES)
    calls = CallStore(store)
    for item in _moved_capture(now - 3 * HOUR):
        calls.record_spans("default", spans_from_request(item))
        clock.advance(5 * SECOND)
    chosen = {
        _one_span_call(calls, clock, key, agent, now - hours_ago * HOUR): (turns, columns)
        for key, agent, hours_ago, turns, columns in CHOSEN
    }
    clock.advance(CallTimings().long_quiet_nano)
    closer = Closer(store, calls=calls, recordings=RecordingStore(store, calls=calls, blobs=None))
    while closer.close_next() is not None:
        pass
    projector = CallProjector(store)
    for call_id, (turns, columns) in chosen.items():
        _write_chosen(projector, call_id, turns, columns)
    # The Pipecat call, as the server's job projects it.
    while projector.run_pending().more:
        pass
    _one_span_call(calls, clock, "open", "support-agent", now - HOUR)

    with serve(create_app(store=store)) as port:
        print(json.dumps({"port": port, "now_ms": now // 1_000_000}), flush=True)
        sys.stdin.read()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="earshot-viewer-dashboard-"))
    try:
        build(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
