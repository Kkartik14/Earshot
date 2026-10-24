"""Revision ``0006_connectors``: what an upgrade keeps, and what its keys delete.

The catalog-parity module proves the schema on every catalog. These tests prove
the revision's data rules on a real catalog of the dialect under test:

- a catalog at ``0005_projections`` keeps every delivery receipt it holds, each
  marked as digested the 0.3.x way (``digest_kind = 'body_v1'``: the sha256 of
  the raw body) and bound to no call, and every connector, with no configuration;
- a recording's fetch row goes with its recording (a call purge), and with its
  connector.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earshot.blobstore import LocalBlobStore
from earshot.clock import ManualClock
from earshot.storage_calls import CallStore
from earshot.storage_migrations import head_revision
from earshot.storage_recordings import RecordingManifest, RecordingStore
from store_support import catalog, catalog_rows, drop_connector_objects, make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND


def _connector(store, endpoint_id: str = "connector_migration_0006", project: str = "default"):
    store.create_connector(
        project, provider="vapi", secret_ref="env:VAPI_SECRET", endpoint_id=endpoint_id
    )
    return endpoint_id


def test_the_head_revision_is_0006() -> None:
    assert head_revision() == "0006_connectors"


def test_an_upgrade_marks_every_receipt_as_a_raw_body_digest_bound_to_no_call(tmp_path) -> None:
    store = make_store(tmp_path)
    endpoint_id = _connector(store)
    store.close()
    with catalog(tmp_path) as connection:  # back to 0005_projections, rows kept
        drop_connector_objects(connection)
        connection.execute("UPDATE alembic_version SET version_num = ?", ("0005_projections",))
        connection.execute(
            "INSERT INTO delivery_receipts(receipt_id, project_id, endpoint_id, "
            "delivery_key_hmac, body_sha256, event_type, state, first_received_at_unix_nano, "
            "attempt_count) VALUES ('receipt_legacy', 'default', ?, ?, ?, "
            "'end-of-call-report', 'applied', 1, 1)",
            (endpoint_id, "k" * 64, "b" * 64),
        )

    make_store(tmp_path).close()

    assert catalog_rows(tmp_path, "SELECT version_num FROM alembic_version") == [
        ("0006_connectors",)
    ]
    assert catalog_rows(
        tmp_path, "SELECT receipt_id, body_sha256, digest_kind, call_id FROM delivery_receipts"
    ) == [("receipt_legacy", "b" * 64, "body_v1", None)]
    assert catalog_rows(tmp_path, "SELECT endpoint_id, config_json FROM connectors") == [
        (endpoint_id, None)
    ]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM recording_fetches") == [(0,)]


def _fetching_recording(tmp_path: Path, endpoint_id: str, project: str) -> RecordingStore:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    recordings = RecordingStore(
        store, calls=CallStore(store), blobs=LocalBlobStore(tmp_path / "blobs")
    )
    if project != "default":
        store.create_project(project, display_name=project)
    _connector(store, endpoint_id, project)
    recordings.create(
        project,
        RecordingManifest(
            recording_id="conn_fetch",
            session_key="vapi:call-1",
            speaker="mixed",
            content_type="audio/wav",
            started_at_unix_nano=str(START),
        ),
    )
    with catalog(tmp_path) as connection:
        connection.execute(
            "INSERT INTO recording_fetches(project_id, recording_id, endpoint_id, source_kind, "
            "provider_call_id, track, url, state, attempts, lease_token, created_unix_nano, "
            "updated_unix_nano) VALUES (?, 'conn_fetch', ?, 'payload_url', 'call-1', "
            "'mixed', 'https://storage.example/rec.wav', 'pending', 0, 0, ?, ?)",
            (project, endpoint_id, START, START),
        )
    return recordings


def test_purging_a_call_deletes_its_recordings_fetches(tmp_path) -> None:
    from earshot.storage_call_purge import CallPurger

    recordings = _fetching_recording(tmp_path, "connector_purge_0006", "default")
    [(call_id,)] = catalog_rows(tmp_path, "SELECT call_id FROM recordings")

    CallPurger(recordings.store, calls=recordings.calls, recordings=recordings).purge_call(
        "default", call_id
    )

    assert catalog_rows(tmp_path, "SELECT count(*) FROM recording_fetches") == [(0,)]


def test_deleting_a_project_deletes_its_fetches(tmp_path) -> None:
    recordings = _fetching_recording(tmp_path, "connector_project_0006", "fetches")

    assert recordings.store.delete_project_data("fetches") is True

    assert catalog_rows(tmp_path, "SELECT count(*) FROM recording_fetches") == [(0,)]
