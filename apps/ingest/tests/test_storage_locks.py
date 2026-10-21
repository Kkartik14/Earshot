"""The store write lock as a measured, bounded resource (AC6.10, AC5.7).

Every outermost ``_mutation()`` records how long it waited for the store's locks
and how long it held them, under its operation name, and a hold over one second
logs ``EARSHOT_SLOW_MUTATION``. A mutation given a budget waits at most that long
for every lock it needs (the in-process lock, then ``.store.lock`` or ``K_write``,
then SQLite's own write lock) and otherwise raises ``StorageBusyError``.

Contention comes from real threads and real processes; nothing is simulated.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from earshot.api import create_app
from earshot.codec import encode_incident_protobuf
from earshot.storage import StorageBusyError
from earshot.storage_projection import compute_projection_rows
from incident_factory import make_long_bundle
from lock_holders import (
    SQLITE_WRITER_HOLDER,
    held_by_another_process,
    store_write_lock_held_elsewhere,
)
from store_support import current_dialect, make_store

pytestmark = pytest.mark.integration


def test_a_mutation_records_its_lock_wait_and_hold_under_its_operation_name(tmp_path) -> None:
    store = make_store(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with store.project_access_scope("default"):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock, daemon=True)
    holder.start()
    assert holding.wait(10)
    threading.Timer(0.3, release.set).start()
    store.create_project("waited", display_name="Waited")
    holder.join(10)

    stats = store.mutation_timing_stats()
    assert stats["create_project"].count == 1
    assert stats["create_project"].last_wait_seconds >= 0.25
    assert stats["create_project"].last_hold_seconds < 0.25
    assert stats["project_access_scope"].last_hold_seconds >= 0.25


def test_holding_the_write_lock_over_a_second_logs_the_operation(tmp_path, caplog) -> None:
    store = make_store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="earshot.storage"):
        store.create_project("quick", display_name="Quick")
        with store.project_access_scope("default"):
            time.sleep(1.1)

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert warnings[0].startswith("EARSHOT_SLOW_MUTATION: project_access_scope held")
    assert store.mutation_timing_stats()["project_access_scope"].max_hold_seconds >= 1.1


def test_ingest_projects_a_large_incident_before_it_takes_the_write_lock(tmp_path) -> None:
    if current_dialect() == "postgresql":
        pytest.skip(
            "on Postgres, writing this incident's ~4,800 projected rows takes longer than "
            "computing them, so the hold cannot show where the computation ran"
        )
    bundle = make_long_bundle(turns=400)
    payload = encode_incident_protobuf(bundle)
    store = make_store(tmp_path)
    started = time.perf_counter()
    compute_projection_rows(bundle, "default", canonical_payload=payload)
    projection_seconds = time.perf_counter() - started

    store.ingest(bundle, payload)

    # Computing the analysis and rows under the lock would make the hold longer.
    assert store.mutation_timing_stats()["ingest"].last_hold_seconds < projection_seconds
    assert len(store.list_turn_facts(limit=500)) == 400


def test_a_budgeted_mutation_gives_up_while_another_process_holds_the_write_lock(
    tmp_path,
) -> None:
    store = make_store(tmp_path)

    with store_write_lock_held_elsewhere(tmp_path):
        started = time.monotonic()
        with pytest.raises(StorageBusyError), store._mutation("probe", budget_s=2.0):
            pytest.fail("the mutation ran without the write lock")
        elapsed = time.monotonic() - started

    assert 1.9 <= elapsed < 2.5
    # Without a budget a mutation waits, as before; once the lock is free it runs.
    store.create_project("after", display_name="After")
    assert store.project_exists("after")


def test_a_budgeted_mutation_gives_up_while_another_thread_holds_the_store(tmp_path) -> None:
    store = make_store(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.project_access_scope("default"):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert holding.wait(10)
        started = time.monotonic()
        with pytest.raises(StorageBusyError), store._mutation("probe", budget_s=0.3):
            pytest.fail("the mutation ran without the in-process lock")
        assert 0.25 <= time.monotonic() - started < 1.0
    finally:
        release.set()
        holder.join(10)


@pytest.mark.sqlite_only
def test_a_budgeted_mutation_bounds_its_wait_for_the_sqlite_write_lock(tmp_path) -> None:
    store = make_store(tmp_path)

    # Another process holds SQLite's own write lock (a VACUUM or a foreign
    # writer does), not the store's lock file.
    with held_by_another_process(SQLITE_WRITER_HOLDER, store.database_path):
        started = time.monotonic()
        with pytest.raises(StorageBusyError), store._mutation("probe", budget_s=2.0):
            store.create_project("blocked", display_name="Blocked")
        elapsed = time.monotonic() - started

    assert 1.5 <= elapsed < 2.5
    assert not store.project_exists("blocked")


def test_the_api_answers_a_busy_store_with_503_and_retry_after(tmp_path) -> None:
    store = make_store(tmp_path)
    app = create_app(store=store)

    # A host route that writes with a lock budget, as the OTLP receiver does.
    @app.post("/host/budgeted-write")
    def budgeted_write() -> dict[str, bool]:
        with store._mutation("host_budgeted_write", budget_s=0.2):
            return {"written": True}

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.project_access_scope("default"):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    with TestClient(app) as client:
        holder.start()
        try:
            assert holding.wait(10)
            busy = client.post("/host/budgeted-write")
        finally:
            release.set()
            holder.join(10)
        written = client.post("/host/budgeted-write")

    assert written.json() == {"written": True}
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "3"
    assert busy.json()["error"]["code"] == "EARSHOT_STORAGE_BUSY"
