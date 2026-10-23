"""The closer in real server processes: both entry points, and crash recovery (AC6.1, AC6.4).

Real ``earshot serve`` and ``uvicorn apps.ingest.app:app`` processes, configured
only through the environment, receive synthetic Pipecat-shaped OTLP requests
over HTTP; the closer in their maintenance loop turns them into revision 1. Short call timings
(``EARSHOT_CALL_*_SECONDS``) keep each wait under a few seconds.

The crash tests arm one of the closer's crash points (``after_claim_commit``,
``after_catalog_commit``; active only with ``EARSHOT_TEST_MODE=1``), let the
process die there, and restart it without them.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from synthetic_otlp import synthetic_requests
from otlp_support import make_request, make_span, post_otlp, serve, string_attribute
from server_process import ServerProcess
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.e2e

CRASH_EXIT_CODE = 137
FAST = {
    "EARSHOT_CALL_END_GRACE_SECONDS": "0.2",
    "EARSHOT_CALL_LATE_DEBOUNCE_SECONDS": "0.2",
    "EARSHOT_CALL_LONG_QUIET_SECONDS": "1",
}


def _wait_for(check: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _replay(port: int) -> None:
    for request in synthetic_requests():
        reply = post_otlp(port, request.SerializeToString())
        assert reply.status == 200, reply.body


def _revisions(path: Path) -> list[tuple]:
    return catalog_rows(
        path, "SELECT revision, close_reason, finality FROM call_revisions ORDER BY revision"
    )


def _call(path: Path) -> tuple:
    [row] = catalog_rows(
        path,
        "SELECT state, dirty, pending_revision, latest_revision, lease_until_unix_nano IS NULL, "
        "materialize_attempts, materialize_error FROM calls",
    )
    return row


def _incidents(path: Path) -> int:
    return catalog_rows(path, "SELECT count(*) FROM incidents")[0][0]


def _start(path: Path, log: str, entry_point: str = "uvicorn", **environment: str):
    return ServerProcess.start(path, path / log, entry_point=entry_point, **FAST, **environment)


def _materializers(pid: int) -> list[int]:
    """The closer's materializer children of process ``pid`` (spawned interpreters)."""

    found = subprocess.run(
        ["pgrep", "-P", str(pid), "-f", "spawn_main"], capture_output=True, text=True
    )
    return [int(line) for line in found.stdout.split()]


def _running(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


def _wait_for_exit(server: ServerProcess) -> int:
    """The crashed server's exit status; a server that does not exit is stopped (a
    process left running would keep writing to a later test's catalog at this path)."""

    deadline = time.monotonic() + 20
    while server.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    server.stop()
    return server.process.returncode


@pytest.mark.parametrize("entry_point", ["serve", "uvicorn"])
def test_each_entry_point_closes_calls_and_collects_parked_traces(tmp_path, entry_point) -> None:
    make_store(tmp_path).close()  # migrate before the server starts
    server = _start(tmp_path, f"{entry_point}.log", entry_point)
    try:
        _replay(server.port)
        stray = make_request([make_span(99, 1, name="GET /health")]).SerializeToString()
        assert post_otlp(server.port, stray).status == 200

        _wait_for(lambda: _revisions(tmp_path), "revision 1")
        _wait_for(
            lambda: catalog_rows(tmp_path, "SELECT count(*) FROM call_traces")[0][0] == 1,
            "the parked trace's collection",
        )
        # The revision was built in the one materializer child the lifespan started.
        materializers = _materializers(server.process.pid)
    finally:
        server.stop()

    # Stopping the server left no child behind.
    assert len(materializers) == 1
    assert not _running(materializers[0])
    assert _revisions(tmp_path) == [(1, "end_signal", "final")]
    assert _call(tmp_path) == ("closed", 0, None, 1, True, 0, None)
    counters = catalog_rows(tmp_path, "SELECT name, value FROM otlp_counters")
    assert counters == [("parked_traces_discarded", 1)]


def test_a_crash_after_the_claim_commits_is_recovered_by_the_next_start(tmp_path) -> None:
    make_store(tmp_path).close()
    crashing = _start(
        tmp_path,
        "crash.log",
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_CRASH_AT="after_claim_commit",
    )
    _replay(crashing.port)

    assert _wait_for_exit(crashing) == CRASH_EXIT_CODE
    # Claimed (dirty cleared, revision 1 pending, leased), nothing materialized.
    assert _call(tmp_path) == ("open", 0, 1, None, False, 0, None)
    assert _revisions(tmp_path) == []

    server = _start(tmp_path, "restart.log")
    try:
        _wait_for(lambda: _revisions(tmp_path), "revision 1 after the restart")
    finally:
        server.stop()

    assert _revisions(tmp_path) == [(1, "end_signal", "final")]
    assert _call(tmp_path) == ("closed", 0, None, 1, True, 0, None)
    assert _incidents(tmp_path) == 1


def test_a_crash_after_the_catalog_commit_leaves_one_revision_and_no_conflict(tmp_path) -> None:
    make_store(tmp_path).close()
    crashing = _start(
        tmp_path,
        "crash.log",
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_CRASH_AT="after_catalog_commit",
    )
    _replay(crashing.port)

    assert _wait_for_exit(crashing) == CRASH_EXIT_CODE
    # The revision and the finish committed together.
    assert _revisions(tmp_path) == [(1, "end_signal", "final")]
    assert _call(tmp_path) == ("closed", 0, None, 1, True, 0, None)

    server = _start(tmp_path, "restart.log")
    try:
        time.sleep(2.5)  # a few closer ticks: nothing is claimable
        assert (_incidents(tmp_path), _call(tmp_path)) == (
            1,
            ("closed", 0, None, 1, True, 0, None),
        )
        trace = synthetic_requests()[0].resource_spans[0].scope_spans[0].spans[0].trace_id
        late = make_request([make_span(int.from_bytes(trace, "big"), 900)])
        assert post_otlp(server.port, late.SerializeToString()).status == 200
        _wait_for(lambda: len(_revisions(tmp_path)) == 2, "revision 2 from late data")
    finally:
        server.stop()

    assert _revisions(tmp_path) == [(1, "end_signal", "final"), (2, "late_data", "final")]
    assert _call(tmp_path)[5:] == (0, None)
    assert _incidents(tmp_path) == 2


def test_the_apps_lifespan_starts_its_materializer_and_stops_it_at_shutdown(tmp_path) -> None:
    # Served in this process, which outlives the app: only the lifespan's shutdown
    # (the maintenance loop closing its jobs) can stop the child.
    import multiprocessing

    from earshot.api import create_app

    def materializers() -> list:
        return [
            child
            for child in multiprocessing.active_children()
            if child.name == "earshot-materializer"
        ]

    store = make_store(tmp_path)
    try:
        with serve(create_app(store=store)):
            _wait_for(lambda: len(materializers()) == 1, "the materializer process")
        stopped = materializers()
    finally:
        store.close()

    assert stopped == []


@pytest.mark.parametrize("entry_point", ["serve", "uvicorn"])
def test_a_server_whose_materializer_cannot_start_refuses_to_start(tmp_path, entry_point) -> None:
    # Carmack CR2-5: the docs promised this, and the server instead served without a
    # closer (every call stayed open). The materializer child dies before it is ready
    # (the env-gated crash point ``materializer_starting``).
    make_store(tmp_path).close()
    log = tmp_path / f"no-materializer-{entry_point}.log"
    server = ServerProcess(
        tmp_path,
        log,
        entry_point=entry_point,
        **FAST,
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_CRASH_AT="materializer_starting",
    )
    try:
        served = server.wait_ready(time.monotonic() + 30)
        try:
            status = server.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            status = None
    finally:
        server.stop()

    assert not served
    assert status == 3  # uvicorn's start-up failure, under both entry points
    assert "EARSHOT_MATERIALIZER_START_FAILED" in log.read_text(errors="replace")


@pytest.mark.parametrize(
    "crash_at", ["after_claim_commit", "after_catalog_commit", "materializer_starting"]
)
def test_the_closer_crash_points_are_inert_without_test_mode(tmp_path, crash_at) -> None:
    make_store(tmp_path).close()
    server = _start(tmp_path, "inert.log", EARSHOT_TEST_CRASH_AT=crash_at)
    try:
        _replay(server.port)
        _wait_for(lambda: _revisions(tmp_path), "revision 1")
        assert server.process.poll() is None
    finally:
        server.stop()

    assert _revisions(tmp_path) == [(1, "end_signal", "final")]


@pytest.mark.slow
@pytest.mark.parametrize("entry_point", ["serve", "uvicorn"])
def test_a_server_whose_materializer_stopped_reading_still_stops_when_asked(
    tmp_path, entry_point
) -> None:
    # Lamport LAM2-2: a materializer child that is alive but reads nothing blocked the
    # closer in a send; the shutdown waited for that build, so SIGTERM never stopped
    # the server. The env-gated stall point stops the real child mid-snapshot.
    make_store(tmp_path).close()
    server = _start(
        tmp_path,
        f"stalled-{entry_point}.log",
        entry_point,
        EARSHOT_TEST_MODE="1",
        EARSHOT_TEST_STALL_AT="materializer_reading_snapshot",
    )
    try:
        _replay(server.port)
        trace = synthetic_requests()[0].resource_spans[0].scope_spans[0].spans[0].trace_id
        padding = [string_attribute("padding", "x" * (4 << 20))]  # past any pipe buffer
        padded = make_request([make_span(int.from_bytes(trace, "big"), 950, attributes=padding)])
        assert post_otlp(server.port, padded.SerializeToString()).status == 200
        _wait_for(lambda: _call(tmp_path)[2] == 1, "the claim of revision 1")
        time.sleep(1.0)  # the closer now sends to a child that reads nothing
        children = _materializers(server.process.pid)

        started = time.monotonic()
        server.process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            server.process.wait(timeout=40)
        stopped_after = time.monotonic() - started
        still_running = server.process.poll() is None
    finally:
        server.stop()
    left = [child for child in children if _running(child)]
    for child in left:  # a stalled orphan never reads its pipe's EOF
        os.kill(child, signal.SIGKILL)

    assert not still_running, "SIGTERM did not stop the server within 40 s"
    assert stopped_after < 20  # the materializer's 10 s close grace, then shutdown
    assert len(children) == 1 and left == []
    assert _revisions(tmp_path) == []  # the claim is retried by the next start
