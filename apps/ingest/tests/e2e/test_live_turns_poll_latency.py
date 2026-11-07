"""Measure host-profile turn visibility through a running server and 2 s poller.

The emitter and poller use real local HTTP requests. Run once with ``pytest --db sqlite``
and once with ``pytest --db postgresql`` to exercise both local database backends.
"""

from __future__ import annotations

import json
import math
import time
import uuid

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans

from host_profile_support import HostTurn, host_request
from otlp_support import JSON, post_otlp, request
from server_process import ServerProcess

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

_POLL_INTERVAL_SECONDS = 2.0
_TURN_COUNT = 20
_VISIBILITY_DEADLINE_SECONDS = 5.0
_NANOSECOND = 1_000_000_000


def _one_span_export(span, *, resource, scope) -> bytes:
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=resource,
                scope_spans=[ScopeSpans(scope=scope, spans=[span])],
            )
        ]
    ).SerializeToString()


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def test_synthetic_host_turns_reach_the_two_second_polling_viewer(tmp_path, _earshot_db) -> None:
    session_key = f"e3-live-turns-{uuid.uuid4()}"
    first_turn = HostTurn("turn-1", 1_800_000_000 * _NANOSECOND, 1_800_000_001 * _NANOSECOND)
    seed = host_request([first_turn], ended=False, session_key=session_key)
    source = seed.resource_spans[0]
    scope_spans = source.scope_spans[0]
    call_span = scope_spans.spans[0]
    call_span.end_time_unix_nano += 180 * _NANOSECOND
    call_payload = _one_span_export(call_span, resource=source.resource, scope=scope_spans.scope)
    server = ServerProcess.start(tmp_path / "data", tmp_path / "server.log", entry_point="serve")
    try:
        root_reply = post_otlp(server.port, call_payload)
        assert root_reply.status == 200, root_reply.body[:300]
        resolved = request(
            server.port,
            "POST",
            "/v1/calls:resolve",
            json.dumps({"session_key": session_key}).encode(),
            headers={"Content-Type": JSON},
        )
        assert resolved.status == 200, resolved.body[:300]
        call_id = resolved.json()["call_id"]
        live_path = f"/v1/calls/{call_id}/live-turns"
        etag: str | None = None
        viewer_turn_count = 0
        turns: list[HostTurn] = []
        route_visibility_ms: list[float] = []
        viewer_visibility_ms: list[float] = []

        def viewer_poll() -> None:
            nonlocal etag, viewer_turn_count
            response = request(
                server.port,
                "GET",
                live_path,
                headers={} if etag is None else {"If-None-Match": etag},
            )
            if response.status == 304:
                return
            assert response.status == 200, (response.status, response.body[:300])
            etag = response.headers["etag"]
            body = response.json()
            assert body["state"] == "open"
            turns_view = body["turns"]
            for index, visible_turn in enumerate(turns_view, start=1):
                assert visible_turn["turn_id"] == f"turn-{index}"
                assert visible_turn["reported_latency_ms"] == 250
            viewer_turn_count = len(turns_view)

        viewer_poll()  # Establish the viewer's initial ETag and empty view.
        assert viewer_turn_count == 0
        next_viewer_poll = time.monotonic() + _POLL_INTERVAL_SECONDS

        base_time = 1_800_000_000 * _NANOSECOND
        for number in range(1, _TURN_COUNT + 1):
            start = base_time + number * 5 * _NANOSECOND
            turn = HostTurn(f"turn-{number}", start, start + _NANOSECOND, reported_latency_ms=250.0)
            turns.append(turn)
            emission_started = time.monotonic()
            export = host_request(turns, ended=False, session_key=session_key)
            export_source = export.resource_spans[0]
            export_scope = export_source.scope_spans[0]
            turn_span = export_scope.spans[number]
            turn_payload = _one_span_export(
                turn_span, resource=export_source.resource, scope=export_scope.scope
            )
            accepted = post_otlp(server.port, turn_payload)
            accepted_at = time.monotonic()
            assert accepted.status == 200, accepted.body[:300]

            route_deadline = accepted_at + 1.0
            while True:
                route_reply = request(server.port, "GET", live_path)
                assert route_reply.status == 200, route_reply.body[:300]
                route_body = route_reply.json()
                assert route_body["state"] == "open"
                if len(route_body["turns"]) >= number:
                    assert route_body["turns"][number - 1]["turn_id"] == f"turn-{number}"
                    assert route_body["turns"][number - 1]["reported_latency_ms"] == 250
                    route_visibility_ms.append((time.monotonic() - accepted_at) * 1000)
                    break
                assert time.monotonic() < route_deadline, (
                    f"turn {number} was not visible on the route within 1 s"
                )
                time.sleep(0.05)

            viewer_deadline = emission_started + _VISIBILITY_DEADLINE_SECONDS
            while viewer_turn_count < number:
                remaining = next_viewer_poll - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                viewer_poll()
                observed_at = time.monotonic()
                assert observed_at < viewer_deadline, (
                    f"turn {number} did not reach the 2 s viewer poll within 5 s"
                )
                next_viewer_poll = observed_at + _POLL_INTERVAL_SECONDS
            viewer_visibility_ms.append((observed_at - emission_started) * 1000)
    finally:
        server.stop()

    route_p95 = _percentile95(route_visibility_ms)
    viewer_p95 = _percentile95(viewer_visibility_ms)
    print(
        "E-3 synthetic live-turn timing: "
        + json.dumps(
            {
                "database": "postgresql" if _earshot_db == "postgresql" else "sqlite",
                "turns": _TURN_COUNT,
                "route_visibility_ms_p95": round(route_p95, 2),
                "route_visibility_ms_max": round(max(route_visibility_ms), 2),
                "viewer_visibility_ms_p95": round(viewer_p95, 2),
                "viewer_visibility_ms_max": round(max(viewer_visibility_ms), 2),
                "poll_interval_ms": int(_POLL_INTERVAL_SECONDS * 1000),
            },
            sort_keys=True,
        )
    )
    assert route_p95 <= 1000, "accepted host-profile turns exceed the 1 s route visibility target"
    assert viewer_p95 <= 5000, "host-profile turns exceed the 5 s 2-second-poll viewer target"
