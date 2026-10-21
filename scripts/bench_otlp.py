"""Measure the OTLP receiver's acknowledgement latency (final-plan §6). Not run in CI.

For each backend, a real server (the app on uvicorn, in this process) receives
``--requests`` OTLP/HTTP protobuf exports of ``--spans`` spans each, authenticated
by a project API key whose verdict is already cached (a warm-up runs first). Each
request is new evidence: the real Pipecat 1.5 capture's spans (attributes, LLM
input history and all) under fresh trace and span IDs. The ack time is from the
first byte sent to the last byte of the 200 read, on one keep-alive connection
per client.

The batch shape is the one a voice worker's ``BatchSpanProcessor`` sends:
``--traces N`` spreads each request's spans over N traces (N calls), each keyed by
its first span. With ``--follow-on`` every trace is bound once and later requests
add spans to the bound traces (the steady state of long calls); otherwise every
request opens new traces. ``--clients`` exporters send at once. The store's
``record_spans`` lock statistics are reported with the latencies.

    .venv/bin/python scripts/bench_otlp.py --db sqlite --db postgresql
    .venv/bin/python scripts/bench_otlp.py --db postgresql --traces 512 --requests 60

Postgres is ``EARSHOT_TEST_POSTGRES_URL`` (an admin URL that may create a
database), by default the ``earshot-dev-postgres`` container; a throwaway
database is created and dropped. URLs are never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import secrets
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

from earshot.api import create_app  # noqa: E402
from earshot.storage import IncidentStore  # noqa: E402

FIXTURE = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5"
DEFAULT_POSTGRES_URL = "postgresql://earshot:earshot@127.0.0.1:55460/earshot"


def _captured_spans() -> tuple[ExportTraceServiceRequest, list]:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    requests = [
        ExportTraceServiceRequest.FromString((FIXTURE / entry["file"]).read_bytes())
        for entry in manifest["requests"]
    ]
    spans = [
        span
        for request in requests
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]
    return requests[0], spans


def _payloads(
    count: int,
    spans_per_request: int,
    *,
    traces: int = 1,
    follow_on: bool = False,
    client: int = 0,
) -> list[bytes]:
    """``count`` requests of ``spans_per_request`` captured spans under new IDs,
    spread over ``traces`` traces each (``follow_on``: the same traces every time,
    keyed in the first request only)."""

    if not 1 <= traces <= spans_per_request:
        raise SystemExit("--traces must be between 1 and --spans")
    template, spans = _captured_spans()
    resource = template.resource_spans[0].resource
    per_trace = spans_per_request // traces
    bound = [secrets.token_bytes(16) for _ in range(traces)]
    payloads = []
    for number in range(count):
        request = ExportTraceServiceRequest()
        resource_spans = request.resource_spans.add()
        resource_spans.resource.CopyFrom(resource)
        scope_spans = resource_spans.scope_spans.add()
        scope_spans.scope.name = "pipecat"
        for trace in range(traces):
            trace_id = bound[trace] if follow_on else secrets.token_bytes(16)
            keyed = number == 0 if follow_on else True
            for index in range(per_trace):
                position = (number if follow_on else 0) * per_trace + index
                span = scope_spans.spans.add()
                span.CopyFrom(spans[position % len(spans)])
                span.trace_id = trace_id
                span.span_id = (position + 1).to_bytes(8, "big")
                span.parent_span_id = b""
                if index == 0 and keyed:
                    key = span.attributes.add()
                    key.key = "earshot.session.id"
                    key.value.string_value = f"bench-{client}-{number}-{trace}"
        payloads.append(request.SerializeToString())
    return payloads


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def _serving(store: IncidentStore, **options: Any) -> Iterator[int]:
    """Serve ``create_app(store=store, **options)`` on a free port; yield the port."""

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(store=store, **options), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=30)


@contextlib.contextmanager
def _store(dialect: str) -> Iterator[IncidentStore]:
    with tempfile.TemporaryDirectory(prefix="earshot-bench-otlp-") as directory:
        if dialect == "sqlite":
            store = IncidentStore(directory)
            try:
                yield store
            finally:
                store.close()
            return
        import psycopg
        from sqlalchemy.engine import make_url

        admin_url = os.environ.get("EARSHOT_TEST_POSTGRES_URL") or DEFAULT_POSTGRES_URL
        name = f"bench_otlp_{secrets.token_hex(4)}"
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')
        url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
        try:
            store = IncidentStore(directory, database_url=url, correlation_key=os.urandom(32))
            try:
                yield store
            finally:
                store.close()
        finally:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _send(
    connection: http.client.HTTPConnection, body: bytes, credential: str
) -> tuple[float, int]:
    started = time.perf_counter()
    connection.request(
        "POST",
        "/v1/traces",
        body,
        {"Content-Type": "application/x-protobuf", "Authorization": f"Bearer {credential}"},
    )
    response = connection.getresponse()
    response.read()
    return time.perf_counter() - started, response.status


def _post(connection: http.client.HTTPConnection, body: bytes, credential: str) -> float:
    elapsed, status = _send(connection, body, credential)
    if status != 200:
        raise SystemExit(f"the receiver answered {status}")
    return elapsed


def _client_run(port: int, credential: str, payloads: list[bytes], warmup: int) -> list[float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        for body in payloads[:warmup]:
            _post(connection, body, credential)
        return [_post(connection, body, credential) for body in payloads[warmup:]]
    finally:
        connection.close()


def bench(
    dialect: str,
    *,
    requests: int,
    spans: int,
    warmup: int,
    traces: int = 1,
    follow_on: bool = False,
    clients: int = 1,
) -> dict[str, object]:
    batches = [
        _payloads(requests + warmup, spans, traces=traces, follow_on=follow_on, client=client)
        for client in range(clients)
    ]
    with _store(dialect) as store:
        credential = store.issue_api_key("default", label="bench").credential
        with _serving(store) as port:
            results: list[list[float]] = [[] for _ in range(clients)]
            threads = [
                threading.Thread(
                    target=lambda index=index: results[index].extend(
                        _client_run(port, credential, batches[index], warmup)
                    )
                )
                for index in range(clients)
            ]
            started = time.perf_counter()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            wall = time.perf_counter() - started
        stored = store.authentication_stats()
        lock = store.mutation_timing_stats().get("record_spans")
    milliseconds = sorted(sample * 1000 for samples in results for sample in samples)
    if len(milliseconds) != requests * clients:
        raise SystemExit("a client failed")
    quantiles = statistics.quantiles(milliseconds, n=100, method="inclusive")
    return {
        "dialect": dialect,
        "requests": requests,
        "clients": clients,
        "spans_per_request": spans,
        "traces_per_request": traces,
        "follow_on": follow_on,
        "request_bytes": round(statistics.mean(len(body) for body in batches[0])),
        "p50_ms": round(quantiles[49], 1),
        "p95_ms": round(quantiles[94], 1),
        "p99_ms": round(quantiles[98], 1),
        "max_ms": round(milliseconds[-1], 1),
        "requests_per_second": round(len(milliseconds) / wall, 1),
        "lock_max_hold_ms": None if lock is None else round(lock.max_hold_seconds * 1000, 1),
        "lock_max_wait_ms": None if lock is None else round(lock.max_wait_seconds * 1000, 1),
        "secret_verifications": stored.secret_verifications,
        "verdict_cache_hits": stored.verdict_cache_hits,
        "load_average": [round(value, 2) for value in os.getloadavg()],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", action="append", choices=("sqlite", "postgresql"))
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--spans", type=int, default=512)
    parser.add_argument("--traces", type=int, default=1, help="traces (calls) per request")
    parser.add_argument(
        "--follow-on", action="store_true", help="add spans to traces bound by the first request"
    )
    parser.add_argument("--clients", type=int, default=1, help="exporters sending at once")
    parser.add_argument("--warmup", type=int, default=10)
    arguments = parser.parse_args()
    for dialect in arguments.db or ["sqlite"]:
        result = bench(
            dialect,
            requests=arguments.requests,
            spans=arguments.spans,
            warmup=arguments.warmup,
            traces=arguments.traces,
            follow_on=arguments.follow_on,
            clients=arguments.clients,
        )
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
