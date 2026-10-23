"""Startup never re-projects; stale projections converge afterwards (AC1.10).

An incident's projections (graph rows, turn facts, and the expiry and export
columns) carry ``incidents.projection_version``. Opening a store re-projects
nothing, however many rows are stale. ``reproject_stale`` brings them up to date
in bounded batches, each in its own short mutation; the server runs it in the
background after it starts serving, and ``/readyz`` reports what is left.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from bulk_ingest import ingest_in_parallel
from earshot.api import create_app
from earshot.cli import main
from earshot.contract import ExportPolicy, RetentionPolicy
from earshot.maintenance import FAILED_REPROJECTION_RETRY_SECONDS, reprojection_job
from incident_factory import make_valid_bundle
from store_support import (
    TEST_CORRELATION_KEY_BASE64,
    catalog,
    catalog_rows,
    current_dialect,
    make_store,
    postgres_cluster,
)

pytestmark = pytest.mark.integration

PROJECTION_QUERIES = {
    "operations": "SELECT * FROM operations ORDER BY bundle_id, operation_id",
    "causal_links": (
        "SELECT * FROM causal_links ORDER BY bundle_id, source_operation_id, link_index"
    ),
    "events": "SELECT * FROM events ORDER BY bundle_id, event_id",
    "turn_metrics": "SELECT * FROM turn_metrics ORDER BY bundle_id, turn_index",
    "incident_columns": (
        "SELECT bundle_id, expires_at_unix_nano, export_allowed_local_api, "
        "export_allowed_local_cli FROM incidents ORDER BY bundle_id"
    ),
}


def projection_snapshot(path: Path) -> dict[str, list[tuple]]:
    return {name: catalog_rows(path, query) for name, query in PROJECTION_QUERIES.items()}


def mark_stale(path: Path) -> None:
    """Make every incident's projections stale and lose them, as an old release would."""

    with catalog(path) as connection:
        connection.execute("UPDATE incidents SET projection_version = NULL")
        for table in ("turn_metrics", "events", "causal_links", "operations"):
            connection.execute(f"DELETE FROM {table}")


def ingest_incidents(path: Path, count: int) -> dict[str, list[tuple]]:
    """Ingest ``count`` incidents and return their synchronous projections."""

    store = make_store(path)
    for index in range(count):
        store.ingest(make_valid_bundle(bundle_id=f"incident-{index}"))
    store.close()
    return projection_snapshot(path)


def corrupt_artifact(path: Path, bundle_id: str) -> bytes:
    """Overwrite one incident's stored artifact; return the original bytes."""

    [(digest,)] = catalog_rows(
        path, "SELECT object_digest FROM incidents WHERE bundle_id = ?", (bundle_id,)
    )
    if current_dialect() == "sqlite":
        artifact = path / "objects" / "sha256" / digest[:2] / digest[2:]
        original = artifact.read_bytes()
        artifact.write_bytes(b"not the canonical incident")
        return original
    [(original,)] = catalog_rows(path, "SELECT payload FROM artifacts WHERE digest = ?", (digest,))
    with catalog(path) as connection:
        connection.execute(
            "UPDATE artifacts SET payload = ? WHERE digest = ?",
            (b"not the canonical incident", digest),
        )
    return bytes(original)


def restore_artifact(path: Path, bundle_id: str, original: bytes) -> None:
    [(digest,)] = catalog_rows(
        path, "SELECT object_digest FROM incidents WHERE bundle_id = ?", (bundle_id,)
    )
    if current_dialect() == "sqlite":
        (path / "objects" / "sha256" / digest[:2] / digest[2:]).write_bytes(original)
        return
    with catalog(path) as connection:
        connection.execute("UPDATE artifacts SET payload = ? WHERE digest = ?", (original, digest))


def test_ingested_incidents_are_current_and_none_is_pending(tmp_path) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="fresh"))

    assert store.projections_pending() == 0


def test_opening_a_store_with_stale_projections_re_projects_nothing(tmp_path) -> None:
    ingest_incidents(tmp_path, 3)
    mark_stale(tmp_path)

    store = make_store(tmp_path)

    assert store.projections_pending() == 3
    assert store.list_turn_facts() == ()
    assert catalog_rows(tmp_path, "SELECT COUNT(*) FROM operations") == [(0,)]


def test_stale_projections_converge_in_bounded_batches_to_the_ingest_result(tmp_path) -> None:
    synchronous = ingest_incidents(tmp_path, 5)
    mark_stale(tmp_path)
    store = make_store(tmp_path)

    first = store.reproject_stale(limit=2)
    second = store.reproject_stale(limit=2, after=first.resume_after)
    last = store.reproject_stale(limit=2, after=second.resume_after)

    assert (first.reprojected, first.failed, first.resume_after) == (2, (), "incident-1")
    assert (second.reprojected, second.failed, second.resume_after) == (2, (), "incident-3")
    assert (last.reprojected, last.failed, last.resume_after) == (1, (), None)
    assert store.projections_pending() == 0
    assert projection_snapshot(tmp_path) == synchronous


def test_a_reprojection_batch_does_its_decoding_and_analysis_outside_the_write_lock(
    tmp_path,
) -> None:
    make_store(tmp_path).close()
    database_url = (
        None if current_dialect() == "sqlite" else postgres_cluster().database_for(tmp_path)
    )
    ingest_in_parallel(tmp_path, 100, database_url=database_url)
    mark_stale(tmp_path)
    store = make_store(tmp_path)

    started = time.perf_counter()
    assert store.reproject_stale(limit=100).reprojected == 100
    elapsed = time.perf_counter() - started

    hold = store.mutation_timing_stats()["reproject_stale"].last_hold_seconds
    assert hold < elapsed - hold


def test_an_unreadable_artifact_stays_pending_without_blocking_the_others(tmp_path) -> None:
    synchronous = ingest_incidents(tmp_path, 3)
    mark_stale(tmp_path)
    original = corrupt_artifact(tmp_path, "incident-1")
    store = make_store(tmp_path)

    batch = store.reproject_stale()

    assert (batch.reprojected, batch.failed) == (2, ("incident-1",))
    assert store.projections_pending() == 1

    restore_artifact(tmp_path, "incident-1", original)
    assert store.reproject_stale().reprojected == 1
    assert store.projections_pending() == 0
    assert projection_snapshot(tmp_path) == synchronous


def test_reprojection_leaves_expired_incidents_to_the_retention_reaper(tmp_path) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="current"))
    expired = make_valid_bundle(bundle_id="expired")
    policies = tuple(
        policy.model_copy(update={"retention": RetentionPolicy(expires_at_unix_nano="0")})
        if policy.capture_class == "metadata"
        else policy
        for policy in expired.profile.privacy.capture_classes
    )
    privacy = expired.profile.privacy.model_copy(update={"capture_classes": policies})
    store.ingest(
        expired.model_copy(
            update={"profile": expired.profile.model_copy(update={"privacy": privacy})}
        )
    )
    mark_stale(tmp_path)
    corrupt_artifact(tmp_path, "expired")

    batch = store.reproject_stale()

    # Never decoded, so its damaged bytes are not a failure; the reaper owns it.
    assert (batch.reprojected, batch.failed) == (1, ())
    assert store.purge_expired() == 1
    assert store.projections_pending() == 0


def test_readiness_reports_pending_projections_without_failing_on_them(tmp_path) -> None:
    ingest_incidents(tmp_path, 2)
    mark_stale(tmp_path)
    store = make_store(tmp_path)

    # No lifespan: the background reprojection does not run in this client.
    response = TestClient(create_app(store=store)).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "projections_pending": 2}


def test_the_background_job_drains_stale_projections_batch_by_batch(tmp_path) -> None:
    synchronous = ingest_incidents(tmp_path, 5)
    mark_stale(tmp_path)
    store = make_store(tmp_path)
    job = reprojection_job(store, batch_size=2)

    # Two full batches ask to run again at once; the pass then ends, and an idle
    # job waits for the next tick.
    delays = [asyncio.run(job(0)) for _ in range(4)]

    assert delays == [0.0, 0.0, None, None]
    assert store.projections_pending() == 0
    assert projection_snapshot(tmp_path) == synchronous


def test_the_background_job_backs_off_when_only_failures_remain(tmp_path) -> None:
    ingest_incidents(tmp_path, 2)
    mark_stale(tmp_path)
    corrupt_artifact(tmp_path, "incident-0")
    store = make_store(tmp_path)
    job = reprojection_job(store)

    delays = [asyncio.run(job(0)) for _ in range(2)]

    assert delays == [None, FAILED_REPROJECTION_RETRY_SECONDS]
    assert store.projections_pending() == 1


def _store_arguments(path: Path, monkeypatch) -> list[str]:
    """CLI arguments for the store at ``path`` on the test's dialect."""

    if current_dialect() == "sqlite":
        return ["--data-dir", str(path)]
    monkeypatch.setenv("EARSHOT_CORRELATION_KEY", TEST_CORRELATION_KEY_BASE64)
    return ["--data-dir", str(path), "--database-url", postgres_cluster().database_for(path)]


def _restricted_from_cli(bundle_id: str):
    bundle = make_valid_bundle(bundle_id=bundle_id)
    policies = tuple(
        policy.model_copy(
            update={"export": ExportPolicy(allowed=True, destinations=("local_api",))}
        )
        if policy.capture_class == "metadata"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def test_the_cli_reprojects_stale_incidents_without_a_server(tmp_path, capsys, monkeypatch) -> None:
    synchronous = ingest_incidents(tmp_path, 3)
    mark_stale(tmp_path)

    assert main(["db", "reproject", *_store_arguments(tmp_path, monkeypatch)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "failed": [],
        "projections_pending": 0,
        "reprojected": 3,
    }
    assert projection_snapshot(tmp_path) == synchronous


def test_reproject_all_rebuilds_damaged_projections_and_names_unreadable_artifacts(
    tmp_path, capsys, monkeypatch
) -> None:
    synchronous = ingest_incidents(tmp_path, 3)
    # Damage that no release change explains: the rows are current but lost.
    with catalog(tmp_path) as connection:
        connection.execute("DELETE FROM turn_metrics")
    corrupt_artifact(tmp_path, "incident-2")

    returncode = main(["db", "reproject", "--all", *_store_arguments(tmp_path, monkeypatch)])

    assert returncode == 1
    assert json.loads(capsys.readouterr().out) == {
        "failed": ["incident-2"],
        "projections_pending": 1,
        "reprojected": 2,
    }
    rebuilt = projection_snapshot(tmp_path)
    assert [row[1] for row in rebuilt["turn_metrics"]] == ["incident-0", "incident-1"]
    assert rebuilt["operations"] == synchronous["operations"]


def test_cli_list_reprojects_before_it_filters_on_projected_export_columns(
    tmp_path, capsys, monkeypatch
) -> None:
    store = make_store(tmp_path)
    store.ingest(make_valid_bundle(bundle_id="exportable"))
    store.ingest(_restricted_from_cli("api-only"))
    store.close()
    # A projection from before the export columns were derived: still stale,
    # and permissive.
    with catalog(tmp_path) as connection:
        connection.execute(
            "UPDATE incidents SET projection_version = NULL, export_allowed_local_cli = 1"
        )

    assert main(["list", *_store_arguments(tmp_path, monkeypatch)]) == 0

    listed = json.loads(capsys.readouterr().out)["items"]
    assert [item["bundle_id"] for item in listed] == ["exportable"]


def _readiness(port: int) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.slow
def test_a_boot_with_ten_thousand_stale_incidents_is_ready_within_a_second(tmp_path) -> None:
    count = 10_000
    make_store(tmp_path).close()
    database_url = (
        None if current_dialect() == "sqlite" else postgres_cluster().database_for(tmp_path)
    )
    ingest_in_parallel(tmp_path, count, database_url=database_url)
    synchronous = projection_snapshot(tmp_path)
    assert len(synchronous["turn_metrics"]) == count
    mark_stale(tmp_path)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    booted = time.perf_counter()
    store = make_store(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(create_app(store=store), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while True:
            try:
                status, first_ready = _readiness(port)
            except OSError:
                status = 0
            if status == 200:
                break
            assert time.perf_counter() - booted < 5, "the server never became ready"
            time.sleep(0.01)
        ready_after = time.perf_counter() - booted

        deadline = time.monotonic() + 180
        while _readiness(port)[1]["projections_pending"]:
            assert time.monotonic() < deadline, "projections never converged"
            time.sleep(0.2)
    finally:
        server.should_exit = True
        thread.join(30)

    assert ready_after < 1.0
    assert first_ready["projections_pending"] > 0
    assert projection_snapshot(tmp_path) == synchronous
