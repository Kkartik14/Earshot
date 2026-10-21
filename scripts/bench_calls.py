"""Measure what a long call costs the server: its close, and its detail read. Not run in CI.

The call is the WS7 closer bench's synthetic Pipecat call (Carmack's tribunal
probes): ``--turns`` turns, each a ``turn`` span (``pipecat.turn``) and its
``stt``, ``llm`` and ``tts`` spans (``pipecat``), where every ``llm`` span carries
the whole conversation so far as its ``input``, as Pipecat exports it. A
1000-turn call is 4001 spans and about 237 MiB of stored span payload.

``close``: a real server (the app on uvicorn, in this process, with its own
maintenance loop and closer) serves ``--clients`` OTLP exporters that send
512-span requests back to back. After ``--idle-seconds`` the call's end marker
arrives over OTLP, the closer claims the call one second later (``END_GRACE``
is 1 s here) and closes it into revision 1 while the exporters keep sending. It
reports ack latency idle and during the close, the status codes, the close's
wall time and lock holds, and the server process's RSS.

``detail``: ``CallReads.get_call`` (the read behind ``GET /v1/calls/{id}``) of
the call, timed, with its Python allocation peak (``tracemalloc``), then 1, 4
and 8 reads at once with the RSS peak.

    .venv/bin/python scripts/bench_calls.py close --db sqlite --clients 4
    .venv/bin/python scripts/bench_calls.py detail --db sqlite --db postgresql

Postgres is ``EARSHOT_TEST_POSTGRES_URL`` as in ``bench_otlp.py``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_otlp
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from earshot.call_capture import spans_from_request
from earshot.storage_call_reads import CallReads
from earshot.storage_calls import CallStore, CallTimings

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
TRACE = (7).to_bytes(16, "big")
ROOT_SPAN = 999_999
SESSION_KEY = "big-call"


def _span(span_id: int, name: str, start: int, end: int, parent: int | None, **attributes) -> Span:
    span = Span(
        trace_id=TRACE,
        span_id=span_id.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
    )
    if parent is not None:
        span.parent_span_id = parent.to_bytes(8, "big")
    for key, value in attributes.items():
        item = span.attributes.add()
        item.key = key
        item.value.string_value = value
    return span


def _request(spans: list[Span], scope: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    attribute = resource_spans.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "bench-calls"
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = scope
    scope_spans.spans.extend(spans)
    return request


def call_requests(turns: int) -> list[ExportTraceServiceRequest]:
    """The call's requests in export order, without its end marker."""

    history: list[dict[str, str]] = []
    requests = []
    span_id = 1
    for turn in range(turns):
        started = START + turn * 10 * SECOND
        user = f"user line {turn} " + "words " * 20
        agent = f"agent reply {turn} " + "more words " * 25
        history += [{"role": "user", "content": user}, {"role": "assistant", "content": agent}]
        turn_span = span_id
        requests.append(
            _request(
                [
                    _span(
                        turn_span,
                        "turn",
                        started,
                        started + 8 * SECOND,
                        ROOT_SPAN,
                        **{"conversation.id": SESSION_KEY, "turn.number": str(turn + 1)},
                    )
                ],
                "pipecat.turn",
            )
        )
        requests.append(
            _request(
                [
                    _span(
                        span_id + 1,
                        "stt",
                        started,
                        started + SECOND,
                        turn_span,
                        transcript=user,
                        **{"gen_ai.request.model": "nova-3"},
                    ),
                    _span(
                        span_id + 2,
                        "llm",
                        started + SECOND,
                        started + 3 * SECOND,
                        turn_span,
                        input=json.dumps(history),
                        output=agent,
                        **{"gen_ai.request.model": "m"},
                    ),
                    _span(
                        span_id + 3,
                        "tts",
                        started + 3 * SECOND,
                        started + 6 * SECOND,
                        turn_span,
                        text=agent,
                    ),
                ],
                "pipecat",
            )
        )
        span_id += 4
    return requests


def end_marker(turns: int) -> ExportTraceServiceRequest:
    return _request(
        [
            _span(
                ROOT_SPAN,
                "conversation",
                START,
                START + turns * 10 * SECOND,
                None,
                **{"conversation.id": SESSION_KEY},
            )
        ],
        "pipecat.turn",
    )


def _record_call(calls: CallStore, turns: int) -> str:
    started = time.perf_counter()
    call_ids: set[str] = set()
    for request in call_requests(turns):
        call_ids.update(calls.record_spans("default", spans_from_request(request)).call_ids)
    [call_id] = call_ids
    print(
        f"# recorded a {turns}-turn call in {time.perf_counter() - started:.1f} s",
        file=sys.stderr,
        flush=True,
    )
    return call_id


def _rss_mib(pid: int) -> float:
    output = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return int(output) / 1024 if output else 0.0


def _children_rss_mib() -> float:
    output = subprocess.run(
        ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False
    ).stdout.split()
    return sum(_rss_mib(int(pid)) for pid in output)


class _RssPeak:
    """The peak RSS of this process (and, separately, its children) while running."""

    def __init__(self) -> None:
        self.before = _rss_mib(os.getpid())
        self.peak = self.before
        self.children_peak = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, _rss_mib(os.getpid()))
            self.children_peak = max(self.children_peak, _children_rss_mib())
            self._stop.wait(0.05)

    def __enter__(self) -> _RssPeak:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


def _quantiles(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    if len(ordered) < 2:
        return {"n": 1, "p50": round(ordered[0], 1), "max": round(ordered[0], 1)}
    cuts = statistics.quantiles(ordered, n=100, method="inclusive")
    return {
        "n": len(ordered),
        "p50": round(cuts[49], 1),
        "p95": round(cuts[94], 1),
        "max": round(ordered[-1], 1),
    }


def bench_close(dialect: str, *, turns: int, clients: int, idle_seconds: float) -> dict:
    with bench_otlp._store(dialect) as store:
        call_id = _record_call(CallStore(store), turns)
        credential = store.issue_api_key("default", label="bench").credential
        payloads = bench_otlp._payloads(4000, 512)
        marker = end_marker(turns).SerializeToString()
        timings = CallTimings(end_grace_seconds=1.0)
        with bench_otlp._serving(store, call_timings=timings) as port:
            samples: list[tuple[float, float, int]] = []
            stop = threading.Event()
            lock = threading.Lock()
            cursor = [0]

            def exporter() -> None:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
                try:
                    while not stop.is_set():
                        with lock:
                            body = payloads[cursor[0] % len(payloads)]
                            cursor[0] += 1
                        started = time.perf_counter()
                        elapsed, status = bench_otlp._send(connection, body, credential)
                        with lock:
                            samples.append((started, elapsed * 1000, status))
                finally:
                    connection.close()

            def state() -> tuple[object, object]:
                with store._connect() as connection:
                    row = connection.execute(
                        "SELECT pending_revision, latest_revision FROM calls WHERE call_id = ?",
                        (call_id,),
                    ).fetchone()
                return row["pending_revision"], row["latest_revision"]

            threads = [threading.Thread(target=exporter) for _ in range(clients)]
            for thread in threads:
                thread.start()
            time.sleep(idle_seconds)
            idle_end = time.perf_counter()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            bench_otlp._post(connection, marker, credential)
            connection.close()
            claimed = done = None
            with _RssPeak() as rss:
                deadline = time.perf_counter() + 600
                while done is None and time.perf_counter() < deadline:
                    pending, latest = state()
                    now = time.perf_counter()
                    if claimed is None and (pending is not None or latest is not None):
                        claimed = now
                    if latest is not None:
                        done = now
                    time.sleep(0.01)
                time.sleep(0.5)
            stop.set()
            for thread in threads:
                thread.join()
        stats = store.mutation_timing_stats()
    if done is None or claimed is None:
        raise SystemExit("the call was not closed within 600 s")
    idle = [ms for started, ms, _ in samples if started < idle_end]
    during = [(ms, status) for started, ms, status in samples if claimed - 0.02 <= started <= done]

    def hold(name: str) -> dict[str, float] | None:
        stat = stats.get(name)
        if stat is None:
            return None
        return {
            "count": stat.count,
            "max_hold_ms": round(stat.max_hold_seconds * 1000, 1),
            "max_wait_ms": round(stat.max_wait_seconds * 1000, 1),
        }

    return {
        "bench": "close",
        "dialect": dialect,
        "turns": turns,
        "clients": clients,
        "ack_idle_ms": _quantiles(idle),
        "ack_during_close_ms": _quantiles([ms for ms, _ in during]),
        "status_during_close": dict(Counter(status for _, status in during)),
        "status_all": dict(Counter(status for _, _, status in samples)),
        "close_wall_s": round(done - claimed, 2),
        # Acks over 100 ms during the close: (start, seconds after the claim was seen; ms).
        "slow_acks_during_close": [
            (round(started - claimed, 2), round(ms))
            for started, ms, _ in samples
            if claimed - 0.02 <= started <= done and ms > 100
        ],
        "record_spans": hold("record_spans"),
        "claim_call": hold("claim_call"),
        "ingest": hold("ingest"),
        "server_rss_mib": {
            "before": round(rss.before),
            "peak_delta": round(rss.peak - rss.before),
            "children_peak": round(rss.children_peak),
        },
        "load_average": [round(value, 2) for value in os.getloadavg()],
    }


def bench_detail(dialect: str, *, turns: int, runs: int) -> dict:
    with bench_otlp._store(dialect) as store:
        call_id = _record_call(CallStore(store), turns)
        reads = CallReads(store)
        reads.get_call("default", call_id)  # warm
        timings = []
        for _ in range(runs):
            started = time.perf_counter()
            detail = reads.get_call("default", call_id)
            timings.append((time.perf_counter() - started) * 1000)
        tracemalloc.start()
        reads.get_call("default", call_id)
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        concurrent = {}
        for count in (1, 4, 8):
            with _RssPeak() as rss:
                started = time.perf_counter()
                threads = [
                    threading.Thread(target=reads.get_call, args=("default", call_id))
                    for _ in range(count)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                wall = time.perf_counter() - started
            concurrent[str(count)] = {
                "wall_ms": round(wall * 1000, 1),
                "rss_peak_delta_mib": round(rss.peak - rss.before),
            }
        with store._connect() as connection:
            payload = connection.execute("SELECT sum(length(payload)) FROM spans").fetchone()[0]
    return {
        "bench": "detail",
        "dialect": dialect,
        "turns": turns,
        "span_payload_mib": round(int(payload) / 2**20, 1),
        "transcript_lines": len(detail.transcript),
        "get_call_ms": [round(value, 1) for value in timings],
        "get_call_p50_ms": round(statistics.median(timings), 1),
        "python_alloc_peak_mib": round(traced_peak / 2**20, 2),
        "concurrent": concurrent,
        "load_average": [round(value, 2) for value in os.getloadavg()],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bench", choices=("close", "detail"))
    parser.add_argument("--db", action="append", choices=("sqlite", "postgresql"))
    parser.add_argument("--turns", type=int, default=1000)
    parser.add_argument("--clients", type=int, default=1, help="close: exporters sending")
    parser.add_argument("--idle-seconds", type=float, default=3.0, help="close: idle baseline")
    parser.add_argument("--runs", type=int, default=5, help="detail: timed reads")
    arguments = parser.parse_args()
    for dialect in arguments.db or ["sqlite"]:
        if arguments.bench == "close":
            result = bench_close(
                dialect,
                turns=arguments.turns,
                clients=arguments.clients,
                idle_seconds=arguments.idle_seconds,
            )
        else:
            result = bench_detail(dialect, turns=arguments.turns, runs=arguments.runs)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
