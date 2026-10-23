"""Crash atomicity of ``POST /v1/traces`` (AC5.10): one request, one transaction.

Real server processes (``uvicorn apps.ingest.app:app``), killed for real:

- **deterministic:** with the crash point ``before_otlp_commit`` armed, the
  server exits inside the request's transaction, before its commit; after a
  restart, no row of that request exists;
- **inert:** the same crash point without ``EARSHOT_TEST_MODE=1`` does nothing;
- **property:** a server is SIGKILLed at random moments, 20 times, while a client
  sends requests that each carry a marker attribute and a known span count. After
  every restart each marker is either fully present (all its spans) or absent; a
  marker whose request was answered 200 is present; and every call's
  ``span_count`` equals the spans stored for it.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from captured_otlp import captured_requests
from earshot.call_capture import attribute_values
from otlp_support import make_request, make_span, post_otlp, string_attribute
from server_process import ServerProcess
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[3]
CRASH_EXIT_CODE = 137
MARKER = "earshot.test.marker"
CALL_TABLES = ("spans", "call_traces", "calls", "otlp_resources", "otlp_counters")
# The rows the real capture's last request makes: two spans of one trace, one call,
# one resource and scope, no rare event.
STORED_ONCE = {"spans": 2, "call_traces": 1, "calls": 1, "otlp_resources": 1, "otlp_counters": 0}


def _row_counts(path: Path) -> dict[str, int]:
    return {
        table: catalog_rows(path, f"SELECT count(*) FROM {table}")[0][0] for table in CALL_TABLES
    }


def _prepare_store(path: Path) -> None:
    """Migrate the store before any server starts (and close it)."""

    make_store(path).close()


def test_a_crash_before_the_otlp_commit_leaves_no_row(tmp_path) -> None:
    _prepare_store(tmp_path)
    # The real capture's last request: the turn and the keyed conversation span.
    request = captured_requests()[-1].SerializeToString()
    crashing = ServerProcess.start(
        tmp_path,
        tmp_path / "server.log",
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_CRASH_AT="before_otlp_commit",
    )
    try:
        with pytest.raises((ConnectionError, OSError)):
            post_otlp(crashing.port, request, timeout=10)
        assert crashing.process.wait(timeout=10) == CRASH_EXIT_CODE
    finally:
        crashing.stop()

    restarted = ServerProcess.start(tmp_path, tmp_path / "server.log")
    try:
        assert _row_counts(tmp_path) == dict.fromkeys(CALL_TABLES, 0)
        # The exporter's retry of the same bytes is then stored once.
        assert post_otlp(restarted.port, request).status == 200
        assert post_otlp(restarted.port, request).status == 200
    finally:
        restarted.stop()
    assert _row_counts(tmp_path) == STORED_ONCE


@pytest.mark.parametrize("test_mode", [None, "true", "0"])
def test_the_otlp_crash_point_is_inert_without_test_mode(tmp_path, test_mode) -> None:
    _prepare_store(tmp_path)
    environment = {"EARSHOT_TEST_CRASH_AT": "before_otlp_commit"}
    if test_mode is not None:
        environment["EARSHOT_TEST_MODE"] = test_mode
    server = ServerProcess.start(tmp_path, tmp_path / "server.log", **environment)
    try:
        reply = post_otlp(server.port, captured_requests()[-1].SerializeToString())
        alive = server.process.poll() is None
    finally:
        server.stop()

    assert reply.status == 200
    assert alive
    assert _row_counts(tmp_path) == STORED_ONCE


# --- the SIGKILL property test -----------------------------------------------------------------


class _Client(threading.Thread):
    """Sends marked requests until the server dies; remembers what it sent."""

    def __init__(self, port: int, iteration: int, rng: random.Random, traces) -> None:
        super().__init__(daemon=True)
        self.port, self.iteration, self.rng, self.traces = port, iteration, rng, traces
        self.sent: dict[str, int] = {}  # marker -> span count
        self.acknowledged: set[str] = set()

    def run(self) -> None:
        for sequence in range(10_000):
            marker = f"m{self.iteration}-{sequence}"
            spans = []
            for _ in range(self.rng.randint(1, 3)):  # one to three traces per request
                trace = next(self.traces)
                attributes = [
                    string_attribute(MARKER, marker),
                    string_attribute("earshot.session.id", f"call-{marker}"),
                ]
                for span in range(self.rng.randint(1, 30)):
                    spans.append(make_span(trace, span + 1, attributes=attributes))
            body = make_request(spans).SerializeToString()
            self.sent[marker] = len(spans)
            try:
                reply = post_otlp(self.port, body, timeout=10)
            except OSError:
                return  # the server was killed
            if reply.status == 200:
                self.acknowledged.add(marker)


def _stored_markers(path: Path) -> Counter[str]:
    stored: Counter[str] = Counter()
    for (payload,) in catalog_rows(path, "SELECT payload FROM spans"):
        marker = attribute_values(Span.FromString(bytes(payload)).attributes)[MARKER]
        stored[marker] += 1
    return stored


def _span_counts_disagree(path: Path) -> list[tuple]:
    return catalog_rows(
        path,
        "SELECT calls.call_id, calls.span_count, "
        "(SELECT count(*) FROM spans JOIN call_traces ON call_traces.project_id = "
        "spans.project_id AND call_traces.trace_id = spans.trace_id "
        "WHERE call_traces.call_id = calls.call_id) AS stored FROM calls "
        "WHERE calls.span_count <> (SELECT count(*) FROM spans JOIN call_traces ON "
        "call_traces.project_id = spans.project_id AND call_traces.trace_id = spans.trace_id "
        "WHERE call_traces.call_id = calls.call_id)",
    )


@pytest.mark.slow
def test_every_marked_request_is_all_or_nothing_across_random_sigkills(tmp_path) -> None:
    seed = int(os.environ.get("EARSHOT_OTLP_CRASH_SEED", time.time_ns() % 2**32))
    rng = random.Random(seed)
    _prepare_store(tmp_path)
    log = tmp_path / "server.log"
    traces = iter(range(1, 10**9))
    sent: dict[str, int] = {}
    acknowledged: set[str] = set()
    server = ServerProcess.start(tmp_path, log)
    try:
        for iteration in range(20):
            client = _Client(server.port, iteration, rng, traces)
            client.start()
            time.sleep(rng.uniform(0.05, 0.6))
            server.kill()
            client.join(timeout=30)
            assert not client.is_alive()
            sent.update(client.sent)
            acknowledged |= client.acknowledged
            server = ServerProcess.start(tmp_path, log)  # recovery runs at open

            stored = _stored_markers(tmp_path)
            torn = {
                marker: (stored[marker], count)
                for marker, count in sent.items()
                if stored[marker] not in (0, count)
            }
            lost = {marker for marker in acknowledged if stored[marker] != sent[marker]}
            assert not torn, f"seed {seed}, iteration {iteration}: torn requests {torn}"
            assert not lost, f"seed {seed}, iteration {iteration}: acknowledged but lost {lost}"
            assert set(stored) <= set(sent)
            assert _span_counts_disagree(tmp_path) == [], f"seed {seed}"
    finally:
        server.stop()

    stored = _stored_markers(tmp_path)
    unanswered = set(sent) - acknowledged
    print(  # the summary the implementation log quotes
        f"seed={seed} requests={len(sent)} acknowledged={len(acknowledged)} "
        f"unanswered={len(unanswered)} unanswered_but_stored="
        f"{sum(1 for marker in unanswered if stored[marker])} spans={sum(stored.values())}"
    )
    # The kills landed both between and inside requests.
    assert len(acknowledged) > 20, f"seed {seed}"
    assert len(sent) > len(acknowledged), f"seed {seed}"
