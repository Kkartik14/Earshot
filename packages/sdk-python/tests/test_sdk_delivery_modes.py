from __future__ import annotations

import threading
import time

import pytest

import earshot
import earshot.sdk as sdk
from earshot.exporter import PermanentExportError, RetryableExportError
from earshot.privacy import CaptureClass, CaptureGovernance, CapturePolicy, ExportConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_global_sdk_configuration():
    earshot.shutdown()
    earshot.configure()
    yield
    earshot.shutdown()
    earshot.configure()


class _AcceptingTransport:
    def __init__(self, *_args, **_kwargs) -> None:
        self.items = []

    def send(self, item) -> None:
        self.items.append(item)


class _RejectingTransport:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def send(self, _item) -> None:
        raise PermanentExportError("rejected")


class _UnavailableTransport:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def send(self, _item) -> None:
        raise RetryableExportError("unavailable")


def test_sync_delivery_reports_remote_acknowledgement_without_worker(monkeypatch) -> None:
    monkeypatch.setattr(sdk, "HttpExportTransport", _AcceptingTransport)
    before = {thread.ident for thread in threading.enumerate() if thread.name == "earshot-export"}
    client = earshot.Client(endpoint="http://localhost:4319", delivery_mode="sync")
    try:
        recorder = client.session(bundle_id="sync-accepted")
        recorder.close()

        assert recorder.export_accepted is True
        assert client.status().sent == 1
        assert {
            thread.ident for thread in threading.enumerate() if thread.name == "earshot-export"
        } == before
    finally:
        assert client.shutdown()


def test_sync_delivery_reports_permanent_remote_rejection(monkeypatch) -> None:
    monkeypatch.setattr(sdk, "HttpExportTransport", _RejectingTransport)
    client = earshot.Client(endpoint="http://localhost:4319", delivery_mode="sync")
    try:
        recorder = client.session(bundle_id="sync-rejected")
        recorder.close()

        assert recorder.export_accepted is False
        assert client.status().rejected == 1
    finally:
        assert client.shutdown()


def test_delivery_configuration_can_be_loaded_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EARSHOT_ENDPOINT", "http://localhost:4319")
    monkeypatch.setenv("EARSHOT_DELIVERY_MODE", "durable")
    monkeypatch.setenv("EARSHOT_SPOOL_DIR", str(tmp_path))
    monkeypatch.setenv("EARSHOT_MAX_SPOOL_ITEMS", "17")
    monkeypatch.setenv("EARSHOT_MAX_SPOOL_BYTES", "4096")
    monkeypatch.setenv("EARSHOT_PERMANENT_REJECTION_POLICY", "delete")
    monkeypatch.setattr(sdk, "HttpExportTransport", _UnavailableTransport)

    client = earshot.init()

    assert client.config.delivery_mode == "durable"
    assert client.config.spool_dir == str(tmp_path)
    assert client.config.max_spool_items == 17
    assert client.config.max_spool_bytes == 4096
    assert client.config.permanent_rejection_policy == "delete"


def test_durable_delivery_requires_explicit_plaintext_spool_directory() -> None:
    with pytest.raises(ValueError, match="explicit spool_dir"):
        earshot.Client(endpoint="http://localhost:4319", delivery_mode="durable")


def test_privacy_rejection_happens_before_durable_spooling(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sdk, "HttpExportTransport", _UnavailableTransport)
    policy = CapturePolicy(
        governance={
            CaptureClass.METADATA: CaptureGovernance(
                export=ExportConfig(allowed=False, destinations=("sdk_http",))
            )
        }
    )
    client = earshot.Client(
        endpoint="http://localhost:4319",
        delivery_mode="durable",
        spool_dir=tmp_path,
        capture_policy=policy,
    )
    try:
        recorder = client.session(bundle_id="private")
        recorder.close()

        assert recorder.export_accepted is False
        assert client.status().spool_depth == 0
    finally:
        assert client.shutdown()


def test_flush_does_not_hold_router_lock_during_diagnostic_callback(monkeypatch) -> None:
    monkeypatch.setattr(sdk, "HttpExportTransport", _UnavailableTransport)
    statuses = []
    client = None

    def diagnostic(_event) -> None:
        assert client is not None
        statuses.append(client.status())

    client = earshot.Client(
        endpoint="http://localhost:4319",
        delivery_mode="async",
        diagnostic=diagnostic,
    )
    try:
        client.session(bundle_id="reentrant-status").close()
        assert client.flush(2)
        assert statuses
    finally:
        assert client.shutdown()


@pytest.mark.parametrize(
    ("next_endpoint", "next_project"),
    [
        ("http://localhost:4320", "project-a"),
        ("http://localhost:4319", "project-b"),
    ],
)
def test_route_rotation_never_replays_previous_destination_spool(
    monkeypatch,
    tmp_path,
    next_endpoint: str,
    next_project: str,
) -> None:
    deliveries = []

    class RoutedTransport:
        def __init__(self, endpoint, *, project_id=None, **_kwargs) -> None:
            self.route = (endpoint, project_id)

        def send(self, item) -> None:
            if self.route == ("http://localhost:4319", "project-a"):
                raise RetryableExportError("route a unavailable")
            deliveries.append((self.route, item.bundle_id))

    monkeypatch.setattr(sdk, "HttpExportTransport", RoutedTransport)
    client = earshot.init(
        endpoint="http://localhost:4319",
        project_id="project-a",
        delivery_mode="durable",
        spool_dir=tmp_path,
    )
    client.session(bundle_id="private-route-a").close()
    deadline = time.monotonic() + 2
    while client.status().spool_depth < 1 and time.monotonic() < deadline:
        time.sleep(0.005)

    client = earshot.init(
        endpoint=next_endpoint,
        project_id=next_project,
        delivery_mode="durable",
        spool_dir=tmp_path,
    )
    assert deliveries == []
    client.session(bundle_id="route-b").close()
    assert client.flush(2)

    assert deliveries == [((next_endpoint, next_project), "route-b")]
    assert client.status().spool_depth == 1
    assert client.status().abandoned >= 1


def test_recovered_durable_outage_is_not_reported_as_terminal_loss(monkeypatch, tmp_path) -> None:
    class RecoveringTransport:
        available = False

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def send(self, _item) -> None:
            if not self.available:
                raise RetryableExportError("temporarily unavailable")

    monkeypatch.setattr(sdk, "HttpExportTransport", RecoveringTransport)
    client = earshot.Client(
        endpoint="http://localhost:4319",
        delivery_mode="durable",
        spool_dir=tmp_path,
    )
    try:
        client.session(bundle_id="recoverable").close()
        deadline = time.monotonic() + 2
        while client.status().retried < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert client.status().failed == 0
        assert client.status().lost == 0

        RecoveringTransport.available = True
        assert client.flush(2)
        assert client.status().sent == 1
        assert client.status().lost == 0
        assert client.status().healthy
    finally:
        assert client.shutdown()


def test_same_route_credential_rotation_replays_with_new_transport(monkeypatch, tmp_path) -> None:
    deliveries = []

    class CredentialTransport:
        def __init__(self, _endpoint, *, token=None, **_kwargs) -> None:
            self.token = token

        def send(self, item) -> None:
            if self.token == "revoked":
                raise RetryableExportError("credential revoked")
            deliveries.append((self.token, item.bundle_id))

    monkeypatch.setattr(sdk, "HttpExportTransport", CredentialTransport)
    client = earshot.init(
        endpoint="http://localhost:4319",
        project_id="project-a",
        token="revoked",
        delivery_mode="durable",
        spool_dir=tmp_path,
    )
    client.session(bundle_id="credential-rotation").close()
    deadline = time.monotonic() + 2
    while client.status().spool_depth < 1 and time.monotonic() < deadline:
        time.sleep(0.005)

    client = earshot.init(
        endpoint="http://localhost:4319",
        project_id="project-a",
        token="replacement",
        delivery_mode="durable",
        spool_dir=tmp_path,
    )
    assert client.flush(2)
    assert deliveries == [("replacement", "credential-rotation")]
    assert client.status().spool_depth == 0
    assert client.status().abandoned == 0


def test_failed_finalization_is_terminal_releases_client_and_never_reroutes(monkeypatch) -> None:
    exporters = []

    class FakeTransport:
        def __init__(self, endpoint, **_kwargs) -> None:
            self.endpoint = endpoint

    class FakeExporter:
        def __init__(self, transport, **_kwargs) -> None:
            self.transport = transport
            self.items = []
            exporters.append(self)

        def submit(self, item) -> bool:
            self.items.append(item)
            return True

        def flush(self, _timeout=None) -> bool:
            return True

        def shutdown(self, _timeout=5.0) -> bool:
            return True

        def status(self):
            return sdk.ExporterStatus(
                state="closed",
                pid=1,
                accepted=len(self.items),
                sent=0,
                dropped=0,
                failed=0,
                rejected=0,
                pending=0,
                queued_bytes=0,
            )

    monkeypatch.setattr(sdk, "HttpExportTransport", FakeTransport)
    monkeypatch.setattr(sdk, "BoundedAsyncExporter", FakeExporter)
    earshot.init(endpoint="http://localhost:4319", project_id="before")
    recorder = earshot.session(bundle_id="invalid-finalization")
    recorder.record_event("voice.event", operation_id="missing-operation")

    with pytest.raises(earshot.IncidentValidationError) as first:
        recorder.close()
    with pytest.raises(earshot.IncidentValidationError) as second:
        recorder.close()
    assert second.value is first.value
    with pytest.raises(RuntimeError, match="closed"):
        recorder.record_event("voice.event")

    earshot.init(endpoint="http://localhost:4320", project_id="after")
    with pytest.raises(earshot.IncidentValidationError):
        recorder.close()
    assert [exporter.items for exporter in exporters] == [[], []]


def test_context_manager_finalization_error_does_not_leak_active_client_token() -> None:
    client = earshot.Client()
    with client.session() as recorder:
        recorder.record_event("voice.event", operation_id="missing-operation")

    assert isinstance(recorder.last_export_error, earshot.IncidentValidationError)
    assert client.shutdown()


def test_failed_finalization_notifies_owner_once() -> None:
    notifications = []
    recorder = earshot.IncidentRecorder(on_close=lambda: notifications.append("closed"))
    recorder.record_event("voice.event", operation_id="missing-operation")

    with pytest.raises(earshot.IncidentValidationError):
        recorder.close()
    with pytest.raises(earshot.IncidentValidationError):
        recorder.close()
    assert notifications == ["closed"]


def test_timed_out_reconfiguration_reports_explicit_partial_success(monkeypatch) -> None:
    instances = []

    class FakeTransport:
        def __init__(self, endpoint, **_kwargs) -> None:
            self.endpoint = endpoint

    class SlowRetirementExporter:
        def __init__(self, _transport, **_kwargs) -> None:
            self.shutdown_calls = 0
            instances.append(self)

        def shutdown(self, _timeout=5.0) -> bool:
            self.shutdown_calls += 1
            return self.shutdown_calls > 1

        def flush(self, _timeout=None) -> bool:
            return True

        def status(self):
            return sdk.ExporterStatus(
                state="closing",
                pid=1,
                accepted=0,
                sent=0,
                dropped=0,
                failed=0,
                rejected=0,
                pending=0,
                queued_bytes=0,
            )

    monkeypatch.setattr(sdk, "HttpExportTransport", FakeTransport)
    monkeypatch.setattr(sdk, "BoundedAsyncExporter", SlowRetirementExporter)
    earshot.init(endpoint="http://localhost:4319")

    with pytest.raises(RuntimeError, match="new Earshot configuration is active"):
        earshot.init(endpoint="http://localhost:4320")

    assert earshot.get_client().config.endpoint == "http://localhost:4320"
    assert earshot.status().state == "closing"
    assert not earshot.shutdown()
    assert earshot.shutdown()
