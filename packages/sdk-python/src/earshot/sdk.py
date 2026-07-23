"""Small one-line and explicit-client configuration surfaces."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .clock import Clock
from .context import _conversation_scope, is_instrumentation_suppressed
from .exporter import (
    BoundedAsyncExporter,
    ExportDiagnostic,
    ExporterStatus,
    ExportItem,
    HttpExportTransport,
)
from .privacy import CapturePolicy
from .recorder import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_MAX_RAW_OTLP_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_VALUE_BYTES,
    IncidentRecorder,
    RecorderConfig,
    RecorderStatus,
)
from .versions import PACKAGE_VERSION

if TYPE_CHECKING:  # pragma: no cover - the projection seam stays a lazy import
    from .contract import IncidentBundle
    from .exporters.registry import IncidentExporter, RegisteredExporter


@dataclass(frozen=True)
class SdkConfig:
    """Non-secret SDK configuration safe to print and log."""

    endpoint: str | None = None
    project_id: str = "default"
    queue_capacity: int = 128
    max_queue_bytes: int = 16 * 1024 * 1024
    compression_threshold_bytes: int | None = 16 * 1024
    sampling_rate: float = 1.0
    delivery_mode: str = "async"
    spool_dir: str | None = None
    sync_deadline_seconds: float = 10.0
    max_spool_items: int = 1024
    max_spool_bytes: int = 256 * 1024 * 1024
    permanent_rejection_policy: str = "retain"
    max_records: int = DEFAULT_MAX_RECORDS
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES
    max_raw_otlp_bytes: int = DEFAULT_MAX_RAW_OTLP_BYTES
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES
    capture_policy: CapturePolicy = field(default_factory=CapturePolicy.metadata_only)
    producer_name: str = "earshot"
    producer_version: str = PACKAGE_VERSION


@dataclass(frozen=True)
class ClientStatus:
    state: str
    pid: int
    accepted: int = 0
    sent: int = 0
    dropped: int = 0
    failed: int = 0
    rejected: int = 0
    pending: int = 0
    queued_bytes: int = 0
    in_flight_bytes: int = 0
    high_water_bytes: int = 0
    oldest_age_seconds: float | None = None
    retried: int = 0
    overflow: int = 0
    last_success_at_unix_nano: int | None = None
    last_failure: str | None = None
    spool_depth: int = 0
    spool_bytes: int = 0
    replayed: int = 0
    abandoned: int = 0
    retained_rejections: int = 0
    sampled_conversations: int = 0
    unsampled_conversations: int = 0
    suppressed_conversations: int = 0
    last_sampling_reason: str | None = None
    truncated_conversations: int = 0
    truncated_records: int = 0

    @property
    def lost(self) -> int:
        return self.dropped + self.failed + max(0, self.rejected - self.retained_rejections)

    @property
    def healthy(self) -> bool:
        return self.lost == 0 and self.abandoned == 0


@dataclass(frozen=True)
class SamplingDecision:
    sampled: bool
    reason: str


class _ExportRouter:
    """Stable recorder target whose active exporter may be replaced safely."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._pid = os.getpid()
        self._exporter: BoundedAsyncExporter | None = None
        self._retiring: list[BoundedAsyncExporter] = []
        self._history = {
            "accepted": 0,
            "sent": 0,
            "dropped": 0,
            "failed": 0,
            "rejected": 0,
            "retried": 0,
            "overflow": 0,
            "replayed": 0,
            "abandoned": 0,
            "retained_rejections": 0,
        }
        self._history_high_water_bytes = 0
        self._history_spool_depth = 0
        self._history_spool_bytes = 0
        self._history_spools: dict[str, tuple[int, int, int, int]] = {}
        self._history_last_success_at_unix_nano: int | None = None
        self._history_last_failure: str | None = None

    def _ensure_pid(self) -> None:
        current_pid = os.getpid()
        if current_pid != self._pid:
            self._pid = current_pid
            self._lock = threading.RLock()
            self._lifecycle_lock = threading.RLock()
            self._retiring = []
            self._history = {key: 0 for key in self._history}
            self._history_high_water_bytes = 0
            self._history_spool_depth = 0
            self._history_spool_bytes = 0
            self._history_spools = {}
            self._history_last_success_at_unix_nano = None
            self._history_last_failure = None

    def _record_completed_status(self, status: ExporterStatus) -> None:
        for key in self._history:
            if status.spool_root_fingerprint is None or key not in {
                "abandoned",
                "retained_rejections",
            }:
                self._history[key] += getattr(status, key)
        self._history_high_water_bytes = max(
            self._history_high_water_bytes,
            status.high_water_bytes,
        )
        if status.spool_root_fingerprint is None:
            self._history_spool_depth += status.spool_depth
            self._history_spool_bytes += status.spool_bytes
        else:
            self._history_spools[status.spool_root_fingerprint] = (
                status.spool_depth,
                status.spool_bytes,
                status.abandoned + status.pending,
                status.retained_rejections,
            )
        if status.last_success_at_unix_nano is not None:
            self._history_last_success_at_unix_nano = max(
                self._history_last_success_at_unix_nano or 0,
                status.last_success_at_unix_nano,
            )
        if status.last_failure is not None:
            self._history_last_failure = status.last_failure

    def submit(self, item: ExportItem) -> bool:
        self._ensure_pid()
        with self._lock:
            exporter = self._exporter
            if exporter is None:
                self._history["dropped"] += 1
                return False
            return exporter.submit(item)

    def replace(self, exporter: BoundedAsyncExporter | None, timeout: float = 5.0) -> bool:
        self._ensure_pid()
        with self._lifecycle_lock:
            with self._lock:
                previous = self._exporter
                self._exporter = exporter
                retiring = self._retiring
                self._retiring = []
            if previous is not None:
                retiring.append(previous)
            deadline = time.monotonic() + max(0.0, timeout)
            incomplete: list[BoundedAsyncExporter] = []
            completed: list[ExporterStatus] = []
            for candidate in retiring:
                remaining = max(0.0, deadline - time.monotonic())
                if not candidate.shutdown(remaining):
                    incomplete.append(candidate)
                else:
                    completed.append(candidate.status())
            with self._lock:
                self._retiring.extend(incomplete)
                for candidate_status in completed:
                    self._record_completed_status(candidate_status)
            return not incomplete

    def flush(self, timeout: float | None = None) -> bool:
        self._ensure_pid()
        with self._lifecycle_lock:
            with self._lock:
                exporter = self._exporter
                retiring = tuple(self._retiring)
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            complete = True
            if exporter is not None:
                complete = exporter.flush(timeout)
            incomplete: list[BoundedAsyncExporter] = []
            completed: list[ExporterStatus] = []
            for candidate in retiring:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                drained = candidate.flush(remaining)
                if drained and candidate.shutdown(1.0 if remaining is None else remaining):
                    completed.append(candidate.status())
                else:
                    incomplete.append(candidate)
                    complete = False
            with self._lock:
                self._retiring = incomplete
                for candidate_status in completed:
                    self._record_completed_status(candidate_status)
            return complete

    def shutdown(self, timeout: float = 5.0) -> bool:
        return self.replace(None, timeout)

    def status(self) -> ExporterStatus | None:
        self._ensure_pid()
        with self._lock:
            exporter = self._exporter
            retiring = tuple(self._retiring)
            history = dict(self._history)
            history_high_water = self._history_high_water_bytes
            history_spool_depth = self._history_spool_depth
            history_spool_bytes = self._history_spool_bytes
            history_spools = dict(self._history_spools)
            history_last_success = self._history_last_success_at_unix_nano
            history_last_failure = self._history_last_failure
        active = [candidate.status() for candidate in retiring]
        if exporter is not None:
            active.append(exporter.status())
        if not active and not any(history.values()):
            return None
        state = "closing" if retiring else "running" if exporter is not None else "closed"
        active_spool_roots = {
            item.spool_root_fingerprint
            for item in active
            if item.spool_root_fingerprint is not None
        }
        inactive_spool_depth = history_spool_depth + sum(
            depth
            for root, (depth, _, _, _) in history_spools.items()
            if root not in active_spool_roots
        )
        inactive_spool_bytes = history_spool_bytes + sum(
            byte_count
            for root, (_, byte_count, _, _) in history_spools.items()
            if root not in active_spool_roots
        )
        inactive_abandoned = sum(
            abandoned
            for root, (_, _, abandoned, _) in history_spools.items()
            if root not in active_spool_roots
        )
        inactive_retained_rejections = sum(
            retained
            for root, (_, _, _, retained) in history_spools.items()
            if root not in active_spool_roots
        )
        return ExporterStatus(
            state=state,
            pid=os.getpid(),
            accepted=history["accepted"] + sum(item.accepted for item in active),
            sent=history["sent"] + sum(item.sent for item in active),
            dropped=history["dropped"] + sum(item.dropped for item in active),
            failed=history["failed"] + sum(item.failed for item in active),
            rejected=history["rejected"] + sum(item.rejected for item in active),
            pending=sum(item.pending for item in active),
            queued_bytes=sum(item.queued_bytes for item in active),
            in_flight_bytes=sum(item.in_flight_bytes for item in active),
            high_water_bytes=max([history_high_water, *(item.high_water_bytes for item in active)]),
            oldest_age_seconds=max(
                (item.oldest_age_seconds for item in active if item.oldest_age_seconds is not None),
                default=None,
            ),
            retried=history["retried"] + sum(item.retried for item in active),
            overflow=history["overflow"] + sum(item.overflow for item in active),
            spool_depth=inactive_spool_depth + sum(item.spool_depth for item in active),
            spool_bytes=inactive_spool_bytes + sum(item.spool_bytes for item in active),
            replayed=history["replayed"] + sum(item.replayed for item in active),
            abandoned=history["abandoned"]
            + inactive_abandoned
            + sum(item.abandoned for item in active),
            retained_rejections=history["retained_rejections"]
            + inactive_retained_rejections
            + sum(item.retained_rejections for item in active),
            last_success_at_unix_nano=max(
                (
                    value
                    for value in [
                        history_last_success,
                        *(item.last_success_at_unix_nano for item in active),
                    ]
                    if value is not None
                ),
                default=None,
            ),
            last_failure=(
                next(
                    (
                        item.last_failure
                        for item in reversed(active)
                        if item.last_failure is not None
                    ),
                    None,
                )
                or history_last_failure
            ),
        )


_live_clients: set[Client] = set()


class _Conversation:
    def __init__(self, recorder: IncidentRecorder, *, client_id: str, project_id: str) -> None:
        self._recorder = recorder
        self._context_scope = _conversation_scope(
            client_id=client_id,
            project_id=project_id,
            conversation_id=recorder.session_id,
        )

    def __enter__(self) -> IncidentRecorder:
        self._context_scope.__enter__()
        try:
            return self._recorder.__enter__()
        except BaseException:
            self._context_scope.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self._recorder.__exit__(exc_type, exc, traceback)
        finally:
            self._context_scope.__exit__(None, None, None)

    async def __aenter__(self) -> IncidentRecorder:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.__exit__(exc_type, exc, traceback)


class Client:
    """Owner of one Earshot runtime and its background delivery resources."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        project_id: str = "default",
        queue_capacity: int = 128,
        max_queue_bytes: int = 16 * 1024 * 1024,
        compression_threshold_bytes: int | None = 16 * 1024,
        sampling_rate: float = 1.0,
        sampling_seed: str = "earshot",
        delivery_mode: str = "async",
        spool_dir: str | Path | None = None,
        sync_deadline_seconds: float = 10.0,
        max_spool_items: int = 1024,
        max_spool_bytes: int = 256 * 1024 * 1024,
        permanent_rejection_policy: str = "retain",
        max_records: int = DEFAULT_MAX_RECORDS,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        max_raw_otlp_bytes: int = DEFAULT_MAX_RAW_OTLP_BYTES,
        max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
        capture_policy: CapturePolicy | None = None,
        diagnostic: Callable[[ExportDiagnostic], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._router = _ExportRouter()
        self.client_id = f"client-{uuid.uuid4().hex}"
        self._active_recorders: set[int] = set()
        self._next_recorder_token = 0
        self._reconfiguring = False
        self._token: str | None = None
        self._sampling_seed = sampling_seed
        self._sampled_conversations = 0
        self._unsampled_conversations = 0
        self._suppressed_conversations = 0
        self._last_sampling_reason: str | None = None
        self._truncated_conversations = 0
        self._truncated_records = 0
        self.config = SdkConfig()
        self._closed = False
        self._diagnostic = diagnostic
        self._reconfigure(
            endpoint=endpoint,
            token=token,
            project_id=project_id,
            queue_capacity=queue_capacity,
            max_queue_bytes=max_queue_bytes,
            compression_threshold_bytes=compression_threshold_bytes,
            sampling_rate=sampling_rate,
            sampling_seed=sampling_seed,
            delivery_mode=delivery_mode,
            spool_dir=spool_dir,
            sync_deadline_seconds=sync_deadline_seconds,
            max_spool_items=max_spool_items,
            max_spool_bytes=max_spool_bytes,
            permanent_rejection_policy=permanent_rejection_policy,
            max_records=max_records,
            max_capture_bytes=max_capture_bytes,
            max_raw_otlp_bytes=max_raw_otlp_bytes,
            max_value_bytes=max_value_bytes,
            capture_policy=capture_policy,
            diagnostic=diagnostic,
        )

    def _ensure_pid(self) -> None:
        current_pid = os.getpid()
        if current_pid != self._pid:
            self._pid = current_pid
            self._lock = threading.RLock()
            self.client_id = f"client-{uuid.uuid4().hex}"
            self._active_recorders = set()
            self._next_recorder_token = 0
            self._reconfiguring = False
            self._sampled_conversations = 0
            self._unsampled_conversations = 0
            self._suppressed_conversations = 0
            self._last_sampling_reason = None
            self._truncated_conversations = 0
            self._truncated_records = 0

    def _matches(
        self,
        *,
        endpoint: str | None,
        token: str | None,
        project_id: str,
        queue_capacity: int,
        max_queue_bytes: int,
        compression_threshold_bytes: int | None,
        sampling_rate: float,
        sampling_seed: str,
        delivery_mode: str,
        spool_dir: str | Path | None,
        sync_deadline_seconds: float,
        max_spool_items: int,
        max_spool_bytes: int,
        permanent_rejection_policy: str,
        max_records: int,
        max_capture_bytes: int,
        max_raw_otlp_bytes: int,
        max_value_bytes: int,
        capture_policy: CapturePolicy | None,
        diagnostic: Callable[[ExportDiagnostic], None] | None,
    ) -> bool:
        self._ensure_pid()
        desired_policy = capture_policy or CapturePolicy.metadata_only()
        with self._lock:
            return (
                not self._closed
                and self.config.endpoint == endpoint
                and self._token == token
                and self.config.project_id == project_id
                and self.config.queue_capacity == queue_capacity
                and self.config.max_queue_bytes == max_queue_bytes
                and self.config.compression_threshold_bytes == compression_threshold_bytes
                and self.config.sampling_rate == sampling_rate
                and self._sampling_seed == sampling_seed
                and self.config.delivery_mode == delivery_mode
                and self.config.spool_dir == (None if spool_dir is None else str(Path(spool_dir)))
                and self.config.sync_deadline_seconds == sync_deadline_seconds
                and self.config.max_spool_items == max_spool_items
                and self.config.max_spool_bytes == max_spool_bytes
                and self.config.permanent_rejection_policy == permanent_rejection_policy
                and self.config.max_records == max_records
                and self.config.max_capture_bytes == max_capture_bytes
                and self.config.max_raw_otlp_bytes == max_raw_otlp_bytes
                and self.config.max_value_bytes == max_value_bytes
                and self.config.capture_policy == desired_policy
                and self._diagnostic is diagnostic
            )

    def _reconfigure(
        self,
        *,
        endpoint: str | None,
        token: str | None,
        project_id: str,
        queue_capacity: int,
        max_queue_bytes: int,
        compression_threshold_bytes: int | None,
        sampling_rate: float,
        sampling_seed: str,
        delivery_mode: str,
        spool_dir: str | Path | None,
        sync_deadline_seconds: float,
        max_spool_items: int,
        max_spool_bytes: int,
        permanent_rejection_policy: str,
        max_records: int,
        max_capture_bytes: int,
        max_raw_otlp_bytes: int,
        max_value_bytes: int,
        capture_policy: CapturePolicy | None,
        diagnostic: Callable[[ExportDiagnostic], None] | None,
    ) -> SdkConfig:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if max_queue_bytes < 1:
            raise ValueError("max_queue_bytes must be positive")
        if compression_threshold_bytes is not None and compression_threshold_bytes < 1:
            raise ValueError("compression_threshold_bytes must be positive or None")
        if not 0 <= sampling_rate <= 1:
            raise ValueError("sampling_rate must be between zero and one")
        if not sampling_seed:
            raise ValueError("sampling_seed must not be empty")
        if delivery_mode not in {"async", "sync", "durable"}:
            raise ValueError("delivery_mode must be async, sync, or durable")
        if sync_deadline_seconds <= 0:
            raise ValueError("sync_deadline_seconds must be positive")
        if max_spool_items < 1 or max_spool_bytes < 1:
            raise ValueError("spool item and byte caps must be positive")
        if permanent_rejection_policy not in {"retain", "delete"}:
            raise ValueError("permanent_rejection_policy must be retain or delete")
        for name, value in (
            ("max_records", max_records),
            ("max_capture_bytes", max_capture_bytes),
            ("max_raw_otlp_bytes", max_raw_otlp_bytes),
            ("max_value_bytes", max_value_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        resolved_spool_dir = None if spool_dir is None else str(Path(spool_dir))
        if delivery_mode == "durable" and (endpoint is None or resolved_spool_dir is None):
            raise ValueError("durable delivery requires both endpoint and explicit spool_dir")
        if not project_id or project_id != project_id.strip():
            raise ValueError("project_id must be a non-empty trimmed string")
        desired_policy = capture_policy or CapturePolicy.metadata_only()
        if self._matches(
            endpoint=endpoint,
            token=token,
            project_id=project_id,
            queue_capacity=queue_capacity,
            max_queue_bytes=max_queue_bytes,
            compression_threshold_bytes=compression_threshold_bytes,
            sampling_rate=sampling_rate,
            sampling_seed=sampling_seed,
            delivery_mode=delivery_mode,
            spool_dir=resolved_spool_dir,
            sync_deadline_seconds=sync_deadline_seconds,
            max_spool_items=max_spool_items,
            max_spool_bytes=max_spool_bytes,
            permanent_rejection_policy=permanent_rejection_policy,
            max_records=max_records,
            max_capture_bytes=max_capture_bytes,
            max_raw_otlp_bytes=max_raw_otlp_bytes,
            max_value_bytes=max_value_bytes,
            capture_policy=desired_policy,
            diagnostic=diagnostic,
        ):
            return self.config
        with self._lock:
            if self._active_recorders:
                raise RuntimeError(
                    "cannot reconfigure Earshot while an active recorder uses the current "
                    "endpoint or privacy policy"
                )
            if self._reconfiguring:
                raise RuntimeError("Earshot client is already being reconfigured")
            self._reconfiguring = True
        try:
            transport = (
                HttpExportTransport(
                    endpoint,
                    token=token,
                    project_id=project_id,
                    compression_threshold_bytes=compression_threshold_bytes,
                )
                if endpoint
                else None
            )
            if transport is None:
                next_exporter = None
            elif delivery_mode == "async":
                next_exporter = BoundedAsyncExporter(
                    transport,
                    capacity=queue_capacity,
                    max_queue_bytes=max_queue_bytes,
                    diagnostic=diagnostic,
                )
            elif delivery_mode == "sync":
                from .exporter import SynchronousExporter

                next_exporter = SynchronousExporter(
                    transport,
                    max_elapsed=sync_deadline_seconds,
                    diagnostic=diagnostic,
                )
            else:
                from .exporter import DurableExporter

                normalized_destination = endpoint.rstrip("/")
                if not normalized_destination.endswith("/v1/incidents"):
                    normalized_destination += "/v1/incidents"
                destination_fingerprint = hashlib.sha256(
                    f"{normalized_destination}\0{project_id}".encode()
                ).hexdigest()
                next_exporter = DurableExporter(
                    transport,
                    spool_dir=Path(resolved_spool_dir or ""),
                    destination_fingerprint=destination_fingerprint,
                    max_spool_items=max_spool_items,
                    max_spool_bytes=max_spool_bytes,
                    permanent_rejection_policy=permanent_rejection_policy,
                    diagnostic=diagnostic,
                )
            next_config = SdkConfig(
                endpoint=endpoint,
                project_id=project_id,
                queue_capacity=queue_capacity,
                max_queue_bytes=max_queue_bytes,
                compression_threshold_bytes=compression_threshold_bytes,
                sampling_rate=sampling_rate,
                delivery_mode=delivery_mode,
                spool_dir=resolved_spool_dir,
                sync_deadline_seconds=sync_deadline_seconds,
                max_spool_items=max_spool_items,
                max_spool_bytes=max_spool_bytes,
                permanent_rejection_policy=permanent_rejection_policy,
                max_records=max_records,
                max_capture_bytes=max_capture_bytes,
                max_raw_otlp_bytes=max_raw_otlp_bytes,
                max_value_bytes=max_value_bytes,
                capture_policy=desired_policy,
            )
            retirement_complete = self._router.replace(next_exporter)
            with self._lock:
                self.config = next_config
                self._token = token
                self._sampling_seed = sampling_seed
                self._diagnostic = diagnostic
                self._closed = False
            if not retirement_complete:
                raise RuntimeError(
                    "new Earshot configuration is active, but the previous exporter "
                    "did not shut down before the lifecycle deadline"
                )
        finally:
            with self._lock:
                self._reconfiguring = False
        if endpoint is not None:
            _live_clients.add(self)
        return next_config

    def sampling_decision(self, conversation_id: str) -> SamplingDecision:
        if not conversation_id:
            raise ValueError("conversation_id must not be empty")
        with self._lock:
            rate = self.config.sampling_rate
            project_id = self.config.project_id
            seed = self._sampling_seed
        digest = hashlib.sha256(f"{seed}\0{project_id}\0{conversation_id}".encode()).digest()
        score = int.from_bytes(digest[:8], "big") / (1 << 64)
        sampled = score < rate
        return SamplingDecision(
            sampled=sampled,
            reason="sampled_by_root_rate" if sampled else "dropped_by_root_rate",
        )

    def _runtime_for_recorder(
        self, conversation_id: str
    ) -> tuple[SdkConfig, _ExportRouter | None, Callable[[], None]]:
        release = self._reserve_recorder()
        with self._lock:
            config = self.config
        if is_instrumentation_suppressed():
            decision = SamplingDecision(False, "instrumentation_suppressed")
            with self._lock:
                self._suppressed_conversations += 1
                self._last_sampling_reason = decision.reason
        else:
            decision = self.sampling_decision(conversation_id)
            with self._lock:
                if decision.sampled:
                    self._sampled_conversations += 1
                else:
                    self._unsampled_conversations += 1
                self._last_sampling_reason = decision.reason
        exporter = self._router if config.endpoint and decision.sampled else None
        return config, exporter, release

    def _reserve_recorder(self) -> Callable[[], None]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Earshot client is shut down")
            if self._reconfiguring:
                raise RuntimeError("Earshot client is being reconfigured")
            self._next_recorder_token += 1
            token = self._next_recorder_token
            self._active_recorders.add(token)

        def release() -> None:
            with self._lock:
                self._active_recorders.discard(token)

        return release

    def _record_recorder_status(self, recorder_status: RecorderStatus) -> None:
        if not recorder_status.truncated:
            return
        with self._lock:
            self._truncated_conversations += 1
            self._truncated_records += recorder_status.truncated_records

    def session(
        self,
        *,
        session_id: str | None = None,
        bundle_id: str | None = None,
        clock: Clock | None = None,
    ) -> IncidentRecorder:
        self._ensure_pid()
        resolved_session_id = session_id or f"session-{uuid.uuid4().hex}"
        config, exporter, release = self._runtime_for_recorder(resolved_session_id)
        try:
            recorder = IncidentRecorder(
                session_id=resolved_session_id,
                bundle_id=bundle_id,
                config=RecorderConfig(
                    producer_name=config.producer_name,
                    producer_version=config.producer_version,
                    capture_policy=config.capture_policy,
                    max_records=config.max_records,
                    max_capture_bytes=config.max_capture_bytes,
                    max_raw_otlp_bytes=config.max_raw_otlp_bytes,
                    max_value_bytes=config.max_value_bytes,
                ),
                clock=clock,
                exporter=exporter,  # type: ignore[arg-type]
                on_close=release,
                on_status=self._record_recorder_status,
                diagnostic=self._diagnostic,
            )
        except BaseException:
            release()
            raise
        weakref.finalize(recorder, release)
        return recorder

    def conversation(
        self,
        *,
        session_id: str | None = None,
        bundle_id: str | None = None,
        clock: Clock | None = None,
    ) -> _Conversation:
        recorder = self.session(session_id=session_id, bundle_id=bundle_id, clock=clock)
        return _Conversation(
            recorder,
            client_id=self.client_id,
            project_id=self.config.project_id,
        )

    def export(self, bundle: IncidentBundle, *, format: str = "otlp") -> Mapping[str, Any]:
        """Project a finished incident with a registered exporter, by name.

        This is the seam that keeps a backend integration out of application code:
        the caller names an exporter (``"otlp"``, ``"openinference"``, or one their
        own process registered) and never imports a projection module. The named
        export is policy-checked against the exporter's declared destination, so an
        incident whose capture policy forbids it is refused before projection.

        The projection is pure and needs none of the client's delivery runtime,
        which is why it neither reserves a recorder nor cares whether the client is
        configured with an endpoint or already shut down.
        """

        from .exporters.registry import default_registry

        return default_registry().export(bundle, format=format)

    def register_exporter(
        self,
        name: str,
        exporter: IncidentExporter,
        *,
        destination: str | None = None,
        replace: bool = False,
    ) -> RegisteredExporter:
        """Register a user exporter so :meth:`export` can select it by name."""

        from .exporters.registry import default_registry

        return default_registry().register(name, exporter, destination=destination, replace=replace)

    def exporter_formats(self) -> tuple[str, ...]:
        """Every exporter name :meth:`export` accepts, sorted."""

        from .exporters.registry import default_registry

        return default_registry().names()

    def flush(self, timeout: float | None = 5.0) -> bool:
        return self._router.flush(timeout)

    def status(self) -> ClientStatus:
        self._ensure_pid()
        with self._lock:
            closed = self._closed
            endpoint = self.config.endpoint
            sampled_conversations = self._sampled_conversations
            unsampled_conversations = self._unsampled_conversations
            suppressed_conversations = self._suppressed_conversations
            last_sampling_reason = self._last_sampling_reason
            truncated_conversations = self._truncated_conversations
            truncated_records = self._truncated_records
        exporter = self._router.status()
        if exporter is None:
            return ClientStatus(
                state="closed" if closed else "disabled",
                pid=os.getpid(),
                sampled_conversations=sampled_conversations,
                unsampled_conversations=unsampled_conversations,
                suppressed_conversations=suppressed_conversations,
                last_sampling_reason=last_sampling_reason,
                truncated_conversations=truncated_conversations,
                truncated_records=truncated_records,
            )
        return ClientStatus(
            state=(
                "closing"
                if exporter.state == "closing"
                else "closed"
                if closed
                else "disabled"
                if endpoint is None
                else exporter.state
            ),
            pid=exporter.pid,
            accepted=exporter.accepted,
            sent=exporter.sent,
            dropped=exporter.dropped,
            failed=exporter.failed,
            rejected=exporter.rejected,
            pending=exporter.pending,
            queued_bytes=exporter.queued_bytes,
            in_flight_bytes=exporter.in_flight_bytes,
            high_water_bytes=exporter.high_water_bytes,
            oldest_age_seconds=exporter.oldest_age_seconds,
            retried=exporter.retried,
            overflow=exporter.overflow,
            last_success_at_unix_nano=exporter.last_success_at_unix_nano,
            last_failure=exporter.last_failure,
            spool_depth=exporter.spool_depth,
            spool_bytes=exporter.spool_bytes,
            replayed=exporter.replayed,
            abandoned=exporter.abandoned,
            retained_rejections=exporter.retained_rejections,
            sampled_conversations=sampled_conversations,
            unsampled_conversations=unsampled_conversations,
            suppressed_conversations=suppressed_conversations,
            last_sampling_reason=last_sampling_reason,
            truncated_conversations=truncated_conversations,
            truncated_records=truncated_records,
        )

    def shutdown(self, timeout: float = 5.0) -> bool:
        self._ensure_pid()
        with self._lock:
            self._closed = True
        complete = self._router.shutdown(timeout)
        if complete:
            _live_clients.discard(self)
        return complete

    def __repr__(self) -> str:
        return f"Client(config={self.config!r})"


_lock = threading.RLock()
_global_pid = os.getpid()
_client = Client()


def _ensure_global_pid() -> None:
    global _global_pid, _lock
    current_pid = os.getpid()
    if current_pid != _global_pid:
        _global_pid = current_pid
        _lock = threading.RLock()


def _runtime_snapshot(
    conversation_id: str,
) -> tuple[
    SdkConfig,
    _ExportRouter | None,
    Callable[[], None],
    Callable[[RecorderStatus], None],
    Callable[[ExportDiagnostic], None] | None,
]:
    """Compatibility snapshot used by higher-level SDK facades."""

    _ensure_global_pid()
    _client._ensure_pid()
    config, exporter, release = _client._runtime_for_recorder(conversation_id)
    return config, exporter, release, _client._record_recorder_status, _client._diagnostic


def _initialize(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    project_id: str = "default",
    queue_capacity: int = 128,
    max_queue_bytes: int = 16 * 1024 * 1024,
    compression_threshold_bytes: int | None = 16 * 1024,
    sampling_rate: float = 1.0,
    sampling_seed: str = "earshot",
    delivery_mode: str = "async",
    spool_dir: str | Path | None = None,
    sync_deadline_seconds: float = 10.0,
    max_spool_items: int = 1024,
    max_spool_bytes: int = 256 * 1024 * 1024,
    permanent_rejection_policy: str = "retain",
    max_records: int = DEFAULT_MAX_RECORDS,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    max_raw_otlp_bytes: int = DEFAULT_MAX_RAW_OTLP_BYTES,
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    capture_policy: CapturePolicy | None = None,
    diagnostic: Callable[[ExportDiagnostic], None] | None = None,
) -> Client:
    global _client
    _ensure_global_pid()
    with _lock:
        with _client._lock:
            closed = _client._closed
        if closed:
            _client = Client(
                endpoint=endpoint,
                token=token,
                project_id=project_id,
                queue_capacity=queue_capacity,
                max_queue_bytes=max_queue_bytes,
                compression_threshold_bytes=compression_threshold_bytes,
                sampling_rate=sampling_rate,
                sampling_seed=sampling_seed,
                delivery_mode=delivery_mode,
                spool_dir=spool_dir,
                sync_deadline_seconds=sync_deadline_seconds,
                max_spool_items=max_spool_items,
                max_spool_bytes=max_spool_bytes,
                permanent_rejection_policy=permanent_rejection_policy,
                max_records=max_records,
                max_capture_bytes=max_capture_bytes,
                max_raw_otlp_bytes=max_raw_otlp_bytes,
                max_value_bytes=max_value_bytes,
                capture_policy=capture_policy,
                diagnostic=diagnostic,
            )
            return _client
        if not _client._matches(
            endpoint=endpoint,
            token=token,
            project_id=project_id,
            queue_capacity=queue_capacity,
            max_queue_bytes=max_queue_bytes,
            compression_threshold_bytes=compression_threshold_bytes,
            sampling_rate=sampling_rate,
            sampling_seed=sampling_seed,
            delivery_mode=delivery_mode,
            spool_dir=spool_dir,
            sync_deadline_seconds=sync_deadline_seconds,
            max_spool_items=max_spool_items,
            max_spool_bytes=max_spool_bytes,
            permanent_rejection_policy=permanent_rejection_policy,
            max_records=max_records,
            max_capture_bytes=max_capture_bytes,
            max_raw_otlp_bytes=max_raw_otlp_bytes,
            max_value_bytes=max_value_bytes,
            capture_policy=capture_policy,
            diagnostic=diagnostic,
        ):
            _client._reconfigure(
                endpoint=endpoint,
                token=token,
                project_id=project_id,
                queue_capacity=queue_capacity,
                max_queue_bytes=max_queue_bytes,
                compression_threshold_bytes=compression_threshold_bytes,
                sampling_rate=sampling_rate,
                sampling_seed=sampling_seed,
                delivery_mode=delivery_mode,
                spool_dir=spool_dir,
                sync_deadline_seconds=sync_deadline_seconds,
                max_spool_items=max_spool_items,
                max_spool_bytes=max_spool_bytes,
                permanent_rejection_policy=permanent_rejection_policy,
                max_records=max_records,
                max_capture_bytes=max_capture_bytes,
                max_raw_otlp_bytes=max_raw_otlp_bytes,
                max_value_bytes=max_value_bytes,
                capture_policy=capture_policy,
                diagnostic=diagnostic,
            )
        return _client


_UNSET = object()


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _environment_optional_integer(name: str, default: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.lower() in {"0", "off", "none", "disabled"}:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer or off") from None


def init(
    *,
    endpoint: str | None | object = _UNSET,
    token: str | None | object = _UNSET,
    project_id: str | object = _UNSET,
    queue_capacity: int | object = _UNSET,
    max_queue_bytes: int | object = _UNSET,
    compression_threshold_bytes: int | None | object = _UNSET,
    sampling_rate: float | object = _UNSET,
    sampling_seed: str | object = _UNSET,
    delivery_mode: str | object = _UNSET,
    spool_dir: str | Path | None | object = _UNSET,
    sync_deadline_seconds: float | object = _UNSET,
    max_spool_items: int | object = _UNSET,
    max_spool_bytes: int | object = _UNSET,
    permanent_rejection_policy: str | object = _UNSET,
    max_records: int | object = _UNSET,
    max_capture_bytes: int | object = _UNSET,
    max_raw_otlp_bytes: int | object = _UNSET,
    max_value_bytes: int | object = _UNSET,
    capture_policy: CapturePolicy | None = None,
    diagnostic: Callable[[ExportDiagnostic], None] | None = None,
) -> Client:
    """Initialize the global client, using ``EARSHOT_*`` for omitted values."""

    resolved_endpoint = (
        os.environ.get("EARSHOT_ENDPOINT") or None if endpoint is _UNSET else endpoint
    )
    resolved_token = os.environ.get("EARSHOT_TOKEN") or None if token is _UNSET else token
    resolved_project = (
        os.environ.get("EARSHOT_PROJECT_ID", "default") if project_id is _UNSET else project_id
    )
    resolved_capacity = (
        _environment_integer("EARSHOT_QUEUE_CAPACITY", 128)
        if queue_capacity is _UNSET
        else queue_capacity
    )
    resolved_max_bytes = (
        _environment_integer("EARSHOT_MAX_QUEUE_BYTES", 16 * 1024 * 1024)
        if max_queue_bytes is _UNSET
        else max_queue_bytes
    )
    resolved_compression = (
        _environment_optional_integer("EARSHOT_COMPRESSION_THRESHOLD_BYTES", 16 * 1024)
        if compression_threshold_bytes is _UNSET
        else compression_threshold_bytes
    )
    resolved_rate = (
        _environment_float("EARSHOT_SAMPLING_RATE", 1.0)
        if sampling_rate is _UNSET
        else sampling_rate
    )
    resolved_seed = (
        os.environ.get("EARSHOT_SAMPLING_SEED", "earshot")
        if sampling_seed is _UNSET
        else sampling_seed
    )
    resolved_delivery_mode = (
        os.environ.get("EARSHOT_DELIVERY_MODE", "async")
        if delivery_mode is _UNSET
        else delivery_mode
    )
    resolved_spool_dir = os.environ.get("EARSHOT_SPOOL_DIR") if spool_dir is _UNSET else spool_dir
    resolved_sync_deadline = (
        _environment_float("EARSHOT_SYNC_DEADLINE_SECONDS", 10.0)
        if sync_deadline_seconds is _UNSET
        else sync_deadline_seconds
    )
    resolved_max_spool_items = (
        _environment_integer("EARSHOT_MAX_SPOOL_ITEMS", 1024)
        if max_spool_items is _UNSET
        else max_spool_items
    )
    resolved_max_spool_bytes = (
        _environment_integer("EARSHOT_MAX_SPOOL_BYTES", 256 * 1024 * 1024)
        if max_spool_bytes is _UNSET
        else max_spool_bytes
    )
    resolved_rejection_policy = (
        os.environ.get("EARSHOT_PERMANENT_REJECTION_POLICY", "retain")
        if permanent_rejection_policy is _UNSET
        else permanent_rejection_policy
    )
    resolved_max_records = (
        _environment_integer("EARSHOT_MAX_RECORDS", DEFAULT_MAX_RECORDS)
        if max_records is _UNSET
        else max_records
    )
    resolved_max_capture_bytes = (
        _environment_integer("EARSHOT_MAX_CAPTURE_BYTES", DEFAULT_MAX_CAPTURE_BYTES)
        if max_capture_bytes is _UNSET
        else max_capture_bytes
    )
    resolved_max_raw_otlp_bytes = (
        _environment_integer("EARSHOT_MAX_RAW_OTLP_BYTES", DEFAULT_MAX_RAW_OTLP_BYTES)
        if max_raw_otlp_bytes is _UNSET
        else max_raw_otlp_bytes
    )
    resolved_max_value_bytes = (
        _environment_integer("EARSHOT_MAX_VALUE_BYTES", DEFAULT_MAX_VALUE_BYTES)
        if max_value_bytes is _UNSET
        else max_value_bytes
    )
    if resolved_endpoint is not None and not isinstance(resolved_endpoint, str):
        raise TypeError("endpoint must be a string or None")
    if resolved_token is not None and not isinstance(resolved_token, str):
        raise TypeError("token must be a string or None")
    if not isinstance(resolved_project, str):
        raise TypeError("project_id must be a string")
    if not isinstance(resolved_capacity, int):
        raise TypeError("queue_capacity must be an integer")
    if not isinstance(resolved_max_bytes, int):
        raise TypeError("max_queue_bytes must be an integer")
    if resolved_compression is not None and not isinstance(resolved_compression, int):
        raise TypeError("compression_threshold_bytes must be an integer or None")
    if not isinstance(resolved_rate, (int, float)):
        raise TypeError("sampling_rate must be a number")
    if not isinstance(resolved_seed, str):
        raise TypeError("sampling_seed must be a string")
    if not isinstance(resolved_delivery_mode, str):
        raise TypeError("delivery_mode must be a string")
    if resolved_spool_dir is not None and not isinstance(resolved_spool_dir, (str, Path)):
        raise TypeError("spool_dir must be a path or None")
    if not isinstance(resolved_sync_deadline, (int, float)):
        raise TypeError("sync_deadline_seconds must be a number")
    if not isinstance(resolved_max_spool_items, int):
        raise TypeError("max_spool_items must be an integer")
    if not isinstance(resolved_max_spool_bytes, int):
        raise TypeError("max_spool_bytes must be an integer")
    if not isinstance(resolved_rejection_policy, str):
        raise TypeError("permanent_rejection_policy must be a string")
    for name, value in (
        ("max_records", resolved_max_records),
        ("max_capture_bytes", resolved_max_capture_bytes),
        ("max_raw_otlp_bytes", resolved_max_raw_otlp_bytes),
        ("max_value_bytes", resolved_max_value_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an integer")
    return _initialize(
        endpoint=resolved_endpoint,
        token=resolved_token,
        project_id=resolved_project,
        queue_capacity=resolved_capacity,
        max_queue_bytes=resolved_max_bytes,
        compression_threshold_bytes=resolved_compression,
        sampling_rate=float(resolved_rate),
        sampling_seed=resolved_seed,
        delivery_mode=resolved_delivery_mode,
        spool_dir=resolved_spool_dir,
        sync_deadline_seconds=float(resolved_sync_deadline),
        max_spool_items=resolved_max_spool_items,
        max_spool_bytes=resolved_max_spool_bytes,
        permanent_rejection_policy=resolved_rejection_policy,
        max_records=resolved_max_records,
        max_capture_bytes=resolved_max_capture_bytes,
        max_raw_otlp_bytes=resolved_max_raw_otlp_bytes,
        max_value_bytes=resolved_max_value_bytes,
        capture_policy=capture_policy,
        diagnostic=diagnostic,
    )


def configure(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    project_id: str = "default",
    queue_capacity: int = 128,
    max_queue_bytes: int = 16 * 1024 * 1024,
    compression_threshold_bytes: int | None = 16 * 1024,
    sampling_rate: float = 1.0,
    sampling_seed: str = "earshot",
    delivery_mode: str = "async",
    spool_dir: str | Path | None = None,
    sync_deadline_seconds: float = 10.0,
    max_spool_items: int = 1024,
    max_spool_bytes: int = 256 * 1024 * 1024,
    permanent_rejection_policy: str = "retain",
    max_records: int = DEFAULT_MAX_RECORDS,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    max_raw_otlp_bytes: int = DEFAULT_MAX_RAW_OTLP_BYTES,
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    capture_policy: CapturePolicy | None = None,
    diagnostic: Callable[[ExportDiagnostic], None] | None = None,
) -> SdkConfig:
    """Backward-compatible process configuration returning only non-secret values."""

    return _initialize(
        endpoint=endpoint,
        token=token,
        project_id=project_id,
        queue_capacity=queue_capacity,
        max_queue_bytes=max_queue_bytes,
        compression_threshold_bytes=compression_threshold_bytes,
        sampling_rate=sampling_rate,
        sampling_seed=sampling_seed,
        delivery_mode=delivery_mode,
        spool_dir=spool_dir,
        sync_deadline_seconds=sync_deadline_seconds,
        max_spool_items=max_spool_items,
        max_spool_bytes=max_spool_bytes,
        permanent_rejection_policy=permanent_rejection_policy,
        max_records=max_records,
        max_capture_bytes=max_capture_bytes,
        max_raw_otlp_bytes=max_raw_otlp_bytes,
        max_value_bytes=max_value_bytes,
        capture_policy=capture_policy,
        diagnostic=diagnostic,
    ).config


def get_client() -> Client:
    """Return the current process-global client without changing its configuration."""

    _ensure_global_pid()
    with _lock:
        _client._ensure_pid()
        return _client


def session(
    *,
    session_id: str | None = None,
    bundle_id: str | None = None,
    clock: Clock | None = None,
) -> IncidentRecorder:
    """Create an incident recorder from the process-global client."""

    return _client.session(session_id=session_id, bundle_id=bundle_id, clock=clock)


def conversation(
    *,
    session_id: str | None = None,
    bundle_id: str | None = None,
    clock: Clock | None = None,
) -> _Conversation:
    return _client.conversation(session_id=session_id, bundle_id=bundle_id, clock=clock)


def export(bundle: IncidentBundle, *, format: str = "otlp") -> Mapping[str, Any]:
    """Project a finished incident with a registered exporter, by name."""

    return _client.export(bundle, format=format)


def register_exporter(
    name: str,
    exporter: IncidentExporter,
    *,
    destination: str | None = None,
    replace: bool = False,
) -> RegisteredExporter:
    """Register a user exporter so :func:`export` can select it by name."""

    return _client.register_exporter(name, exporter, destination=destination, replace=replace)


def exporter_formats() -> tuple[str, ...]:
    """Every exporter name :func:`export` accepts, sorted."""

    return _client.exporter_formats()


def flush(timeout: float | None = 5.0) -> bool:
    return _client.flush(timeout)


def status() -> ClientStatus:
    return _client.status()


def shutdown(timeout: float = 5.0) -> bool:
    _ensure_global_pid()
    with _lock:
        return _client.shutdown(timeout)


def _shutdown_at_exit() -> None:
    deadline = time.monotonic() + 2.0
    for client in tuple(_live_clients):
        remaining = max(0.0, deadline - time.monotonic())
        with contextlib.suppress(Exception):
            client.shutdown(timeout=remaining)


atexit.register(_shutdown_at_exit)
