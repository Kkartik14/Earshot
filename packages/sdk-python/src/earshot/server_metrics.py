"""OpenTelemetry instruments for Earshot server operations.

This module depends on the OpenTelemetry API only. The embedding process owns the
MeterProvider, readers, exporters, resource and shutdown lifecycle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping

from opentelemetry import metrics
from opentelemetry.metrics import Meter, Observation

from .storage_closer import (
    CALL_INTEGRITY,
    MATERIALIZE_CONFLICT,
    MATERIALIZE_FINISH_FAILED,
    MATERIALIZE_INVALID_EVIDENCE,
    MATERIALIZE_SNAPSHOT_GONE,
    MATERIALIZE_WORKER_FAILED,
)

_CLOSER_FAILURE_CODES = frozenset(
    {
        CALL_INTEGRITY,
        MATERIALIZE_CONFLICT,
        MATERIALIZE_FINISH_FAILED,
        MATERIALIZE_INVALID_EVIDENCE,
        MATERIALIZE_SNAPSHOT_GONE,
        MATERIALIZE_WORKER_FAILED,
    }
)
_MATERIALIZER_FAILURE_CODES = frozenset(
    {
        MATERIALIZE_CONFLICT,
        MATERIALIZE_FINISH_FAILED,
        MATERIALIZE_INVALID_EVIDENCE,
        MATERIALIZE_SNAPSHOT_GONE,
        MATERIALIZE_WORKER_FAILED,
    }
)
_QUEUE_NAMES = frozenset(
    {
        "call_closer",
        "recording_analysis",
        "recording_fetch",
        "blob_deletions",
        "incident_reprojection",
        "parked_traces",
    }
)
_JOB_ERROR_TYPES = frozenset({"io", "storage", "timeout", "other"})
_JOB_NAMES = frozenset(
    {
        "database-server-lock",
        "incident-reprojection",
        "blob-store-health",
        "blob-spool-sweep",
        "blob-deletions",
        "recording-upload-sweep",
        "recording-analysis",
        "recording-fetch",
        "call-closer",
        "parked-trace-collection",
        "call-retention",
        "call-projection",
        "queue-depth-snapshot",
    }
)
_DURATION_BUCKET_BOUNDARIES_SECONDS = (
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


class ServerMetrics:
    """Bounded server measurements; no identifiers or free-form errors are labels."""

    _QUEUE_SNAPSHOT_MAX_AGE_SECONDS = 90.0

    def __init__(self, *, meter: Meter | None = None) -> None:
        meter = meter or metrics.get_meter("earshot.server")
        self._maintenance_duration = meter.create_histogram(
            "earshot.maintenance.job.duration",
            unit="s",
            description="Time a maintenance job spent running.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKET_BOUNDARIES_SECONDS,
        )
        self._maintenance_failures = meter.create_counter(
            "earshot.maintenance.job.failures",
            unit="1",
            description="Maintenance job runs that raised an uncaught exception.",
        )
        self._maintenance_delay = meter.create_histogram(
            "earshot.maintenance.scheduler.wakeup_delay",
            unit="s",
            description=(
                "Delay between a completion-relative maintenance deadline and the job's start."
            ),
            explicit_bucket_boundaries_advisory=_DURATION_BUCKET_BOUNDARIES_SECONDS,
        )
        self._closer_failures = meter.create_counter(
            "earshot.call_closer.failures",
            unit="1",
            description="Call-closing attempts that failed with a stable Earshot failure code.",
        )
        self._materializer_failures = meter.create_counter(
            "earshot.materializer.failures",
            unit="1",
            description="Call materialization attempts that failed with a stable Earshot code.",
        )
        self._blob_deletion_failures = meter.create_counter(
            "earshot.blob_deletion.failures",
            unit="1",
            description="Journaled blob deletion attempts that failed and will be retried.",
        )
        self._queue_lock = threading.Lock()
        self._queue_depths: dict[str, int] = {name: 0 for name in _QUEUE_NAMES}
        self._queue_snapshot_at: float | None = None
        meter.create_observable_gauge(
            "earshot.storage.queue.depth",
            callbacks=[self._observe_queue_depths],
            unit="{item}",
            description=(
                "Sampled operational work by queue; call closer estimates claim-eligible work."
            ),
        )
        meter.create_observable_gauge(
            "earshot.storage.queue.snapshot.age",
            callbacks=[self._observe_queue_snapshot_age],
            unit="s",
            description="Age of the last successful operational queue depth snapshot.",
        )

    def maintenance_job(
        self,
        job_name: str,
        duration_seconds: float,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        bounded_job = job_name if job_name in _JOB_NAMES else "other"
        bounded_outcome = outcome if outcome in {"success", "failure"} else "other"
        attributes = {"job.name": bounded_job}
        if bounded_outcome == "failure":
            bounded_type = error_type if error_type in _JOB_ERROR_TYPES else "other"
            attributes["error.type"] = bounded_type
            self._maintenance_failures.add(
                1,
                {"job.name": bounded_job, "error.type": bounded_type},
            )
        self._maintenance_duration.record(duration_seconds, attributes)

    def maintenance_delay(self, job_name: str, delay_seconds: float) -> None:
        bounded_job = job_name if job_name in _JOB_NAMES else "other"
        self._maintenance_delay.record(max(0.0, delay_seconds), {"job.name": bounded_job})

    def closer_failure(self, failure_code: str | None) -> None:
        code = failure_code if failure_code in _CLOSER_FAILURE_CODES else "other"
        self._closer_failures.add(1, {"failure.code": code})
        if code in _MATERIALIZER_FAILURE_CODES:
            self._materializer_failures.add(1, {"failure.code": code})

    def blob_deletion_failure(self, count: int = 1) -> None:
        if count > 0:
            self._blob_deletion_failures.add(count)

    def set_queue_depths(self, depths: Mapping[str, int]) -> None:
        unknown = set(depths) - _QUEUE_NAMES
        if unknown:
            raise ValueError("unknown operational queue name")
        normalized = {name: int(depth) for name, depth in depths.items()}
        if any(depth < 0 for depth in normalized.values()):
            raise ValueError("queue depths cannot be negative")
        with self._queue_lock:
            self._queue_depths = {name: normalized.get(name, 0) for name in _QUEUE_NAMES}
            self._queue_snapshot_at = time.monotonic()

    def _observe_queue_depths(self, _options: object) -> list[Observation[int]]:
        with self._queue_lock:
            checked_at = self._queue_snapshot_at
            depths = tuple(self._queue_depths.items())
        if (
            checked_at is None
            or time.monotonic() - checked_at > self._QUEUE_SNAPSHOT_MAX_AGE_SECONDS
        ):
            return []
        return [Observation(value, {"queue.name": name}) for name, value in depths]

    def _observe_queue_snapshot_age(self, _options: object) -> list[Observation[float]]:
        with self._queue_lock:
            checked_at = self._queue_snapshot_at
        if checked_at is None:
            return []
        return [Observation(max(0.0, time.monotonic() - checked_at))]
