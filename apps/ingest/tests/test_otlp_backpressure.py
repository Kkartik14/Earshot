"""Back-pressure on ``POST /v1/traces`` (AC5.7): 503 with ``Retry-After: 3``, never 429.

- An admission limiter caps requests in flight at the pool size; a request past
  it is refused at once.
- One request waits at most 2 s in total for every lock it needs; a held
  ``.store.lock`` (SQLite), a running ``VACUUM`` (SQLite) or a held ``K_write``
  (Postgres) in another process makes it answer 503 within 2.5 s, anonymous or
  authenticated by an API key.
- A pool timeout (Postgres) is a 503 too.

Real servers, real concurrent sockets and real lock-holding processes.
"""

from __future__ import annotations

import socket
import time

import pytest

from earshot.api import ApiConfig, create_app
from lock_holders import (
    SQLITE_VACUUM_HOLDER,
    held_by_another_process,
    store_write_lock_held_elsewhere,
)
from otlp_support import PROTOBUF, keyed_request, post_otlp, rpc_status, serve
from store_support import catalog_rows, current_dialect, make_store

pytestmark = pytest.mark.integration


def _assert_busy(reply, elapsed: float | None = None) -> None:
    assert (reply.status, reply.media_type) == (503, PROTOBUF), reply.body
    assert reply.headers["retry-after"] == "3"
    assert rpc_status(reply)[0] == 14  # UNAVAILABLE
    if elapsed is not None:
        assert elapsed < 2.5


def _stalled_export(port: int, body: bytes) -> socket.socket:
    """A request whose headers and first body bytes are sent, and whose rest is not."""

    connection = socket.create_connection(("127.0.0.1", port), timeout=30)
    head = (
        "POST /v1/traces HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/x-protobuf\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    connection.sendall(head + body[:10])
    return connection


def _finish(connection: socket.socket, body: bytes) -> int:
    connection.sendall(body[10:])
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = connection.recv(4096)
        assert chunk, "the server closed the connection"
        response += chunk
    connection.close()
    return int(response.split(b" ", 2)[1])


def test_requests_past_the_concurrency_cap_are_refused_at_once(tmp_path) -> None:
    config = ApiConfig(otlp_max_concurrent_requests=2)
    bodies = [keyed_request(f"call-{n}", n, 3) for n in (1, 2)]

    with serve(create_app(store=make_store(tmp_path), config=config)) as port:
        stalled = [_stalled_export(port, body) for body in bodies]
        # Both stalled requests hold a slot while their bodies trickle in; an empty
        # export (which stores nothing) is admitted until they do.
        deadline = time.monotonic() + 10
        while True:
            started = time.monotonic()
            refused = post_otlp(port, b"")
            if refused.status == 503 or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        refused_after = time.monotonic() - started
        statuses = [
            _finish(connection, body) for connection, body in zip(stalled, bodies, strict=True)
        ]
        admitted = post_otlp(port, keyed_request("call-3", 3, 1))

    _assert_busy(refused)
    assert refused_after < 0.5  # refused at once, not after a wait
    assert statuses == [200, 200]
    assert admitted.status == 200
    assert catalog_rows(tmp_path, "SELECT count(*) FROM spans") == [(7,)]


def test_the_admission_cap_follows_the_pool_size_setting() -> None:
    from earshot.server_config import load_server_settings

    settings = load_server_settings(
        {
            "EARSHOT_DB_POOL_SIZE": "5",
            "EARSHOT_OTLP_MAX_PROTOBUF_BYTES": "1000",
            "EARSHOT_OTLP_MAX_JSON_BYTES": "500",
        }
    )
    config = settings.api_config()

    assert (
        config.otlp_max_concurrent_requests,
        config.otlp_max_protobuf_bytes,
        config.otlp_max_json_bytes,
    ) == (5, 1000, 500)
    assert ApiConfig().otlp_max_concurrent_requests == 8


@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "api-key"])
def test_a_write_lock_held_by_another_process_answers_503_within_the_budget(
    tmp_path, authenticated
) -> None:
    store = make_store(tmp_path)
    credential = store.issue_api_key("default", label="bot").credential if authenticated else None

    with serve(create_app(store=store)) as port, store_write_lock_held_elsewhere(tmp_path):
        started = time.monotonic()
        reply = post_otlp(port, keyed_request("call-1", 1, 2), credential=credential, timeout=15)
        elapsed = time.monotonic() - started

    _assert_busy(reply, elapsed)
    assert elapsed >= 1.9  # it waited for its budget, then gave up
    assert catalog_rows(tmp_path, "SELECT count(*) FROM spans") == [(0,)]


@pytest.mark.sqlite_only
@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "api-key"])
def test_a_running_vacuum_in_another_process_answers_503_within_the_budget(
    tmp_path, authenticated
) -> None:
    store = make_store(tmp_path)
    credential = store.issue_api_key("default", label="bot").credential if authenticated else None
    with serve(create_app(store=store)) as port:
        # Something for the VACUUM to copy.
        assert post_otlp(port, keyed_request("call-0", 9, 50), credential=credential).status == 200
        with held_by_another_process(SQLITE_VACUUM_HOLDER, store.database_path):
            started = time.monotonic()
            reply = post_otlp(port, keyed_request("call-1", 1, 2), credential=credential)
            elapsed = time.monotonic() - started
        after = post_otlp(port, keyed_request("call-1", 1, 2), credential=credential)

    _assert_busy(reply, elapsed)
    assert after.status == 200
    assert catalog_rows(tmp_path, "SELECT count(*) FROM spans") == [(52,)]


@pytest.mark.postgres_only
def test_a_pool_timeout_answers_503_with_retry_after(tmp_path) -> None:
    store = make_store(tmp_path)
    with serve(create_app(store=store)) as port:
        # Every pooled connection is checked out (and held) by this process.
        held = [store._database.connect() for _ in range(store._database.pool_size)]
        try:
            reply = post_otlp(port, keyed_request("call-1", 1, 1), timeout=30)
        finally:
            for connection in held:
                connection.close()
        after = post_otlp(port, keyed_request("call-1", 1, 1))

    _assert_busy(reply)
    assert after.status == 200
    assert current_dialect() == "postgresql"
