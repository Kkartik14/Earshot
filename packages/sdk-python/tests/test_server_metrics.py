"""The server's durable-work gauges and bounded labels."""

from __future__ import annotations

import asyncio
import time

import pytest

from earshot.clock import ManualClock
from earshot.storage_calls import REVISION_CAP, CallStore
from earshot.storage_closer import (
    CALL_INTEGRITY,
    MATERIALIZE_WORKER_FAILED,
    CloseOutcome,
    call_closer_job,
)
from earshot.storage_recordings import RecordingManifest, RecordingStore
from incident_factory import make_valid_bundle
from store_support import make_store

pytestmark = pytest.mark.unit


def _metrics_by_name(reader):
    data = reader.get_metrics_data()
    return {
        metric.name: metric.data.data_points
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def _make_call(store, calls: CallStore, project_id: str, session_key: str, now: int, **updates):
    with store._mutation("test_metrics_call"), store._connect() as connection:
        connection.begin_write()
        call_id = calls.recording_call(connection, project_id, session_key, now)
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(
                f"UPDATE calls SET {assignments} WHERE project_id = ? AND call_id = ?",
                (*updates.values(), project_id, call_id),
            )
        connection.commit()
    return call_id


def test_queue_depth_snapshot_reads_each_durable_queue(tmp_path) -> None:
    store = make_store(tmp_path / "catalog", clock=ManualClock(wall=1_800_000_000_000_000_000))
    try:
        store.set_capture_preset("default", "full")
        calls = CallStore(store)
        recordings = RecordingStore(store, calls=calls, blobs=None)
        recordings.create(
            "default",
            RecordingManifest(
                recording_id="queue-test",
                session_key="queue-session",
                speaker="user",
                content_type="audio/wav",
                started_at_unix_nano="1800000000000000000",
            ),
        )
        store.ingest(make_valid_bundle(bundle_id="queue-incident"))
        assert store.mark_projections_stale() == 1
        with store._mutation("test_seed_queue_depths"), store._connect() as connection:
            connection.begin_write()
            connection.execute(
                "INSERT INTO audio_analysis(project_id, recording_id, state, attempts, "
                "algorithm_version, updated_unix_nano) VALUES (?, ?, 'pending', 0, 0, ?)",
                ("default", "queue-test", 1),
            )
            connection.execute(
                "INSERT INTO connectors(endpoint_id, project_id, provider, secret_ref, "
                "enabled, normalizer_version, created_at_unix_nano) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                ("queue-endpoint", "default", "test", "secret-ref", "1", 1),
            )
            connection.execute(
                "INSERT INTO recording_fetches(project_id, recording_id, endpoint_id, "
                "source_kind, provider_call_id, track, state, attempts, lease_token, "
                "created_unix_nano, updated_unix_nano) "
                "VALUES (?, ?, ?, 'provider_api', ?, ?, 'pending', 0, 0, ?, ?)",
                ("default", "queue-test", "queue-endpoint", "provider-call", "mixed", 1, 1),
            )
            connection.execute(
                "INSERT INTO pending_blob_deletions(project_id, blob_key, "
                "queued_at_unix_nano, attempts) VALUES (?, ?, ?, 0)",
                ("default", "recordings/default/orphan", 1),
            )
            connection.execute(
                "INSERT INTO call_traces(project_id, trace_id, call_id, has_session_span, "
                "first_span_received_unix_nano, last_span_received_unix_nano) "
                "VALUES (?, ?, NULL, 1, ?, ?)",
                ("default", bytes(range(16)), 1, 1),
            )
            connection.commit()

        assert store.operational_queue_depths() == {
            "call_closer": 0,
            "recording_analysis": 1,
            "recording_fetch": 1,
            "blob_deletions": 1,
            "incident_reprojection": 1,
            "parked_traces": 1,
        }
    finally:
        store.close()


def test_call_closer_queue_depth_counts_only_claimable_calls(tmp_path) -> None:
    now = 1_800_000_000_000_000_000
    clock = ManualClock(wall=now)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=None)
    try:
        due_call = _make_call(store, calls, "default", "metrics-due", now, close_due_unix_nano=now)
        _make_call(store, calls, "default", "metrics-not-due", now)
        _make_call(
            store,
            calls,
            "default",
            "metrics-live-lease",
            now,
            pending_revision=1,
            lease_until_unix_nano=now + 1,
            dirty=0,
        )
        _make_call(
            store,
            calls,
            "default",
            "metrics-expired-lease",
            now,
            pending_revision=1,
            lease_until_unix_nano=now - 1,
            dirty=0,
        )
        _make_call(
            store,
            calls,
            "default",
            "metrics-pending-and-dirty",
            now,
            pending_revision=1,
            lease_until_unix_nano=now - 1,
            close_due_unix_nano=now,
            dirty=1,
        )
        _make_call(
            store,
            calls,
            "default",
            "metrics-revision-cap",
            now,
            latest_revision=REVISION_CAP,
            close_due_unix_nano=now,
        )
        _make_call(
            store,
            calls,
            "default",
            "metrics-final-cap-exception",
            now,
            latest_revision=REVISION_CAP,
            end_signal_unix_nano=now - 1,
            close_due_unix_nano=now,
        )
        _make_call(
            store,
            calls,
            "default",
            "metrics-integrity-error",
            now,
            close_due_unix_nano=now,
            materialize_error=CALL_INTEGRITY,
        )
        stored_pending_revision = _make_call(
            store,
            calls,
            "default",
            "metrics-stored-pending-revision",
            now,
            pending_revision=1,
            lease_until_unix_nano=now - 1,
            dirty=0,
        )
        with (
            store._mutation("test_metrics_stored_pending_revision"),
            store._connect() as connection,
        ):
            connection.begin_write()
            connection.execute(
                "INSERT INTO call_revisions(project_id, call_id, revision, bundle_id, "
                "close_reason, finality, span_count, recording_count, created_unix_nano) "
                "VALUES (?, ?, 1, ?, 'quiet_period', 'provisional', 0, 0, ?)",
                ("default", stored_pending_revision, f"{stored_pending_revision}.r1", now),
            )
            connection.commit()
        held, _ = recordings.create(
            "default",
            RecordingManifest(
                recording_id="metrics-held-recording",
                session_key="metrics-held-call",
                speaker="user",
                content_type="audio/wav",
                started_at_unix_nano=str(now),
            ),
        )
        with store._mutation("test_metrics_held_call"), store._connect() as connection:
            connection.begin_write()
            connection.execute(
                "UPDATE calls SET close_due_unix_nano = ? WHERE project_id = ? AND call_id = ?",
                (now, "default", held.call_id),
            )
            connection.commit()

        store.create_project("metrics_deleting", display_name="Deleting test project")
        _make_call(
            store,
            calls,
            "metrics_deleting",
            "metrics-deleting-call",
            now,
            close_due_unix_nano=now,
        )
        assert store.begin_project_deletion("metrics_deleting") == "deleting"

        depths = store.operational_queue_depths()

        # The due dirty call, expired pending revisions, and first-final-close
        # exception are claimable. The overlapping pending+dirty call counts once.
        assert depths["call_closer"] == 4
        assert (
            store.operational_queue_depths(uploading_calls={("default", due_call)})["call_closer"]
            == 3
        )
    finally:
        store.close()


def test_call_closer_queue_depth_uses_configured_upload_hold(tmp_path) -> None:
    now = 1_800_000_000_000_000_000
    clock = ManualClock(wall=now)
    store = make_store(tmp_path, clock=clock)
    calls = CallStore(store)
    recordings = RecordingStore(store, calls=calls, blobs=None)
    try:
        recording, _ = recordings.create(
            "default",
            RecordingManifest(
                recording_id="metrics-hold-override",
                session_key="metrics-hold-override",
                speaker="user",
                content_type="audio/wav",
                started_at_unix_nano=str(now),
            ),
        )
        with store._mutation("test_metrics_hold_override"), store._connect() as connection:
            connection.begin_write()
            connection.execute(
                "UPDATE calls SET close_due_unix_nano = ? WHERE project_id = ? AND call_id = ?",
                (now, "default", recording.call_id),
            )
            connection.commit()

        clock.advance(11_000_000_000)

        assert store.operational_queue_depths()["call_closer"] == 0
        assert store.operational_queue_depths(upload_hold_nano=10_000_000_000)["call_closer"] == 1
    finally:
        store.close()


def test_server_metrics_export_queue_depths_and_bounded_failure_labels() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.test"))
    metrics.set_queue_depths({"call_closer": 4, "recording_analysis": 2})
    metrics.maintenance_job("private-call-id", 0.125, "failure", "private exception detail")
    metrics.maintenance_delay("call-closer", 0.25)
    metrics.closer_failure("private-error-code")
    metrics.blob_deletion_failure(3)

    observed = _metrics_by_name(reader)
    depth_points = observed["earshot.storage.queue.depth"]
    assert {point.attributes["queue.name"]: point.value for point in depth_points} == {
        "call_closer": 4,
        "recording_analysis": 2,
        "recording_fetch": 0,
        "blob_deletions": 0,
        "incident_reprojection": 0,
        "parked_traces": 0,
    }
    failure_points = observed["earshot.maintenance.job.failures"]
    assert [(dict(point.attributes), point.value) for point in failure_points] == [
        ({"error.type": "other", "job.name": "other"}, 1)
    ]
    assert dict(observed["earshot.call_closer.failures"][0].attributes) == {"failure.code": "other"}
    assert observed["earshot.blob_deletion.failures"][0].value == 3
    assert observed["earshot.storage.queue.snapshot.age"][0].value >= 0
    expected_bounds = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        120.0,
    )
    for name in (
        "earshot.maintenance.job.duration",
        "earshot.maintenance.scheduler.wakeup_delay",
    ):
        assert observed[name][0].explicit_bounds == expected_bounds
    assert "private" not in str(observed)
    provider.shutdown()


def test_queue_depth_observations_expire_but_snapshot_age_remains(monkeypatch) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.freshness-test"))
    monkeypatch.setattr(ServerMetrics, "_QUEUE_SNAPSHOT_MAX_AGE_SECONDS", 0.01)
    metrics.set_queue_depths({"blob_deletions": 2})
    assert _metrics_by_name(reader)["earshot.storage.queue.depth"]

    time.sleep(0.02)
    observed = _metrics_by_name(reader)
    assert observed.get("earshot.storage.queue.depth", []) == []
    assert observed["earshot.storage.queue.snapshot.age"][0].value >= 0.01
    provider.shutdown()


def test_call_closer_job_counts_a_handled_materializer_failure() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    class FailedCloser:
        def release_leases(self, **_kwargs) -> int:
            return 0

        def start(self) -> None:
            pass

        def close_next(self, **_kwargs) -> CloseOutcome:
            return CloseOutcome(
                "private-project", "private-call", 1, "failed", MATERIALIZE_WORKER_FAILED
            )

        def close(self) -> None:
            pass

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.test"))
    asyncio.run(call_closer_job(FailedCloser(), metrics=metrics)(0))

    observed = _metrics_by_name(reader)
    for name in ("earshot.call_closer.failures", "earshot.materializer.failures"):
        assert [(dict(point.attributes), point.value) for point in observed[name]] == [
            ({"failure.code": MATERIALIZE_WORKER_FAILED}, 1)
        ]
    assert "private" not in str(observed)
    provider.shutdown()


def test_recording_store_counts_failed_journaled_blob_deletion(tmp_path) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from earshot.server_metrics import ServerMetrics

    class FailingBlobs:
        def delete(self, _key: str) -> None:
            raise OSError("secret bucket and object details")

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ServerMetrics(meter=provider.get_meter("earshot.server.test"))
    store = make_store(tmp_path / "catalog")
    try:
        blob_key = "recordings/default/private-object"
        with store._mutation("test_seed_blob_deletion_metric"), store._connect() as connection:
            connection.begin_write()
            connection.execute(
                "INSERT INTO pending_blob_deletions(project_id, blob_key, "
                "queued_at_unix_nano, attempts) VALUES (?, ?, ?, 0)",
                ("default", blob_key, 1),
            )
            connection.commit()
        recordings = RecordingStore(
            store,
            calls=CallStore(store),
            blobs=FailingBlobs(),
            metrics=metrics,
        )

        result = recordings.delete_blobs("default", [blob_key])

        assert result.failed == 1
        observed = _metrics_by_name(reader)
        assert observed["earshot.blob_deletion.failures"][0].value == 1
        assert "secret bucket" not in str(observed)
    finally:
        store.close()
        provider.shutdown()
