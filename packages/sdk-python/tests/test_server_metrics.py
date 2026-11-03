"""The server's durable-work gauges and bounded labels."""

from __future__ import annotations

import asyncio
import time

import pytest

from earshot.storage import IncidentStore
from earshot.storage_calls import CallStore
from earshot.storage_closer import MATERIALIZE_WORKER_FAILED, CloseOutcome, call_closer_job
from earshot.storage_recordings import RecordingManifest, RecordingStore
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.unit


def _metrics_by_name(reader):
    data = reader.get_metrics_data()
    return {
        metric.name: metric.data.data_points
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_queue_depth_snapshot_reads_each_durable_queue(tmp_path) -> None:
    store = IncidentStore(tmp_path / "catalog")
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
        with store._connect() as connection:
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
            "call_closer": 1,
            "recording_analysis": 1,
            "recording_fetch": 1,
            "blob_deletions": 1,
            "incident_reprojection": 1,
            "parked_traces": 1,
        }
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
    store = IncidentStore(tmp_path / "catalog")
    try:
        blob_key = "recordings/default/private-object"
        with store._connect() as connection:
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
