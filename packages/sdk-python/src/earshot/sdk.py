"""Small one-line configuration surface for application code."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .clock import Clock
from .exporter import BoundedAsyncExporter, HttpExportTransport
from .privacy import CapturePolicy
from .recorder import IncidentRecorder, RecorderConfig


@dataclass(frozen=True)
class SdkConfig:
    endpoint: str | None = None
    token: str | None = None
    queue_capacity: int = 128
    capture_policy: CapturePolicy = field(default_factory=CapturePolicy.metadata_only)
    producer_name: str = "earshot"
    producer_version: str = "0.1.0"


_lock = threading.RLock()
_config = SdkConfig()
_exporter: BoundedAsyncExporter | None = None


def _runtime_snapshot() -> tuple[SdkConfig, BoundedAsyncExporter | None]:
    """Return one consistent process-configuration snapshot for SDK facades."""

    with _lock:
        return _config, _exporter


def configure(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    queue_capacity: int = 128,
    capture_policy: CapturePolicy | None = None,
) -> SdkConfig:
    """Configure the process once; metadata-only is always the default."""

    global _config, _exporter
    if queue_capacity < 1:
        raise ValueError("queue_capacity must be positive")
    next_config = SdkConfig(
        endpoint=endpoint,
        token=token,
        queue_capacity=queue_capacity,
        capture_policy=capture_policy or CapturePolicy.metadata_only(),
    )
    next_exporter = (
        BoundedAsyncExporter(
            HttpExportTransport(endpoint, token=token),
            capacity=queue_capacity,
        )
        if endpoint
        else None
    )
    with _lock:
        previous = _exporter
        _config = next_config
        _exporter = next_exporter
    if previous is not None:
        previous.shutdown()
    return next_config


def session(
    *,
    session_id: str | None = None,
    bundle_id: str | None = None,
    clock: Clock | None = None,
) -> IncidentRecorder:
    """Create a recorder from process configuration.

    Example::

        earshot.configure(endpoint="http://127.0.0.1:4319")
        with earshot.session() as incident:
            ...
    """

    config, exporter = _runtime_snapshot()
    return IncidentRecorder(
        session_id=session_id,
        bundle_id=bundle_id,
        config=RecorderConfig(
            producer_name=config.producer_name,
            producer_version=config.producer_version,
            capture_policy=config.capture_policy,
        ),
        clock=clock,
        exporter=exporter,
    )


def shutdown(timeout: float = 5.0) -> bool:
    global _exporter
    with _lock:
        exporter = _exporter
        _exporter = None
    return True if exporter is None else exporter.shutdown(timeout)
