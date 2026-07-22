from __future__ import annotations

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import earshot
import earshot.sdk as sdk
from earshot.clock import ManualClock
from earshot.privacy import (
    CaptureClass,
    CaptureGovernance,
    CapturePolicy,
    ConsentConfig,
    ExportConfig,
    RedactionConfig,
    RetentionConfig,
)
from earshot.validation import validate_incident
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_global_sdk_configuration():
    earshot.shutdown()
    earshot.configure()
    yield
    earshot.shutdown()
    earshot.configure()


def test_one_line_default_session_is_metadata_only_and_publicly_exported() -> None:
    assert callable(earshot.configure)
    assert callable(earshot.session)
    clock = ManualClock(wall=1_800_000_000_000_000_000, monotonic=100)
    with earshot.session(
        session_id="one-line", bundle_id="one-line-bundle", clock=clock
    ) as recorder:
        recorder.record_event(
            "custom.event",
            attributes={"transcript": SECRET_SENTINEL, "service.name": "safe"},
        )
    bundle = recorder.close()
    assert validate_incident(bundle).ok
    assert bundle.profile.events[0].attributes == {"service.name": "safe"}
    assert SECRET_SENTINEL not in str(bundle.model_dump(mode="python"))


def test_explicit_client_configuration_never_exposes_its_token() -> None:
    client = earshot.Client(endpoint="http://localhost:4319", token=SECRET_SENTINEL)
    try:
        assert client.config.endpoint == "http://localhost:4319"
        assert not hasattr(client.config, "token")
        assert SECRET_SENTINEL not in repr(client)
        assert SECRET_SENTINEL not in repr(client.config)
    finally:
        assert client.shutdown()


def test_configured_capture_policy_is_applied_to_new_sessions() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}))
    config = earshot.configure(capture_policy=policy)
    recorder = earshot.session()
    recorder.record_event("stt.final", attributes={"transcript": "allowed"})
    bundle = recorder.close()
    assert config.capture_policy is policy
    assert bundle.profile.events[0].capture_class == "transcript"
    assert bundle.profile.events[0].attributes == {"transcript": "allowed"}
    assert validate_incident(bundle).ok


def test_sensitive_capture_governance_is_emitted_without_manual_bundle_rewriting() -> None:
    governance = CaptureGovernance(
        consent=ConsentConfig(
            status="granted",
            legal_basis="consent",
            recorded_at_unix_nano="1800000000000000000",
            authority="application",
        ),
        redaction=RedactionConfig(
            policy_id="transcript-redaction",
            policy_version="2",
            status="completed",
            findings_count=1,
            redacted_count=1,
        ),
        retention=RetentionConfig(ttl_nano="3600000000000", policy_id="one-hour"),
        export=ExportConfig(
            allowed=False,
            destinations=("local_api", "local_cli"),
            policy_id="local-private",
        ),
    )
    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
        governance={CaptureClass.TRANSCRIPT: governance},
    )
    earshot.configure(capture_policy=policy)
    recorder = earshot.session()
    recorder.record_event("stt.final", attributes={"transcript": "allowed"})
    bundle = recorder.close()
    transcript = next(
        item
        for item in bundle.profile.privacy.capture_classes
        if item.capture_class == "transcript"
    )
    assert transcript.consent is not None and transcript.consent.status == "granted"
    assert transcript.redaction is not None and transcript.redaction.redacted_count == 1
    assert transcript.retention is not None and transcript.retention.ttl_nano == "3600000000000"
    assert transcript.export is not None and not transcript.export.allowed
    assert validate_incident(bundle).ok


def test_sdk_http_export_obeys_capture_governance_before_queueing() -> None:
    class ExporterSpy:
        def __init__(self) -> None:
            self.items = []

        def submit(self, item) -> bool:
            self.items.append(item)
            return True

    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
        governance={
            CaptureClass.TRANSCRIPT: CaptureGovernance(
                export=ExportConfig(allowed=False, destinations=("sdk_http",))
            )
        },
    )
    exporter = ExporterSpy()
    recorder = earshot.IncidentRecorder(
        config=earshot.RecorderConfig(capture_policy=policy),
        exporter=exporter,  # type: ignore[arg-type]
    )
    recorder.record_event("stt.final", attributes={"transcript": "private"})
    recorder.close()
    assert exporter.items == []
    assert recorder.export_accepted is False
    assert isinstance(recorder.last_export_error, earshot.ExportPolicyError)


def test_reconfigure_cannot_route_an_active_session_to_a_different_endpoint(monkeypatch) -> None:
    instances = []

    class FakeExporter:
        def __init__(self, transport, *, capacity, **_kwargs):
            self.transport = transport
            self.capacity = capacity
            self.shutdown_calls = 0
            self.items = []
            instances.append(self)

        def submit(self, item):
            self.items.append(item)
            return True

        def shutdown(self, timeout=5.0):
            del timeout
            self.shutdown_calls += 1
            return True

        def status(self):
            return sdk.ExporterStatus(
                state="closed",
                pid=1,
                accepted=0,
                sent=0,
                dropped=0,
                failed=0,
                rejected=0,
                pending=0,
                queued_bytes=0,
            )

    class FakeTransport:
        def __init__(self, endpoint, *, token=None, **_kwargs):
            self.endpoint = endpoint
            self.token = token

    monkeypatch.setattr(sdk, "BoundedAsyncExporter", FakeExporter)
    monkeypatch.setattr(sdk, "HttpExportTransport", FakeTransport)
    earshot.configure(endpoint="http://one.invalid", token="one", queue_capacity=3)
    first = instances[0]
    recorder = earshot.session()
    with pytest.raises(RuntimeError, match="active recorder"):
        earshot.configure(endpoint="http://two.invalid", token="two", queue_capacity=7)
    recorder.close()
    assert recorder.export_accepted is True
    assert len(first.items) == 1
    assert len(instances) == 1
    earshot.configure(endpoint="http://two.invalid", token="two", queue_capacity=7)
    assert first.shutdown_calls == 1
    assert instances[1].capacity == 7
    assert instances[1].transport.token == "two"
    assert earshot.shutdown()
    assert instances[1].shutdown_calls == 1
    assert earshot.shutdown()


def test_init_is_idempotent_for_identical_configuration(monkeypatch) -> None:
    instances = []

    class FakeExporter:
        def __init__(self, _transport, *, capacity, **_kwargs):
            self.capacity = capacity
            instances.append(self)

        def shutdown(self, timeout=5.0):
            del timeout
            return True

        def status(self):
            return sdk.ExporterStatus(
                state="closed",
                pid=1,
                accepted=0,
                sent=0,
                dropped=0,
                failed=0,
                rejected=0,
                pending=0,
                queued_bytes=0,
            )

    monkeypatch.setattr(sdk, "BoundedAsyncExporter", FakeExporter)
    monkeypatch.setattr(
        sdk,
        "HttpExportTransport",
        lambda endpoint, *, token=None, **_kwargs: object(),
    )
    first = earshot.init(endpoint="http://localhost:4319", token="secret")
    second = earshot.init(endpoint="http://localhost:4319", token="secret")
    assert first is second
    assert earshot.get_client() is first
    assert len(instances) == 1


def test_shutdown_detaches_old_recorders_from_the_next_global_project(monkeypatch) -> None:
    instances = []

    class FakeExporter:
        def __init__(self, _transport, **_kwargs):
            self.items = []
            self.closed = False
            instances.append(self)

        def submit(self, item):
            if self.closed:
                return False
            self.items.append(item)
            return True

        def shutdown(self, timeout=5.0):
            del timeout
            self.closed = True
            return True

        def status(self):
            return sdk.ExporterStatus(
                state="closed" if self.closed else "running",
                pid=1,
                accepted=len(self.items),
                sent=len(self.items),
                dropped=0,
                failed=0,
                rejected=0,
                pending=0,
                queued_bytes=0,
            )

    monkeypatch.setattr(sdk, "BoundedAsyncExporter", FakeExporter)
    monkeypatch.setattr(sdk, "HttpExportTransport", lambda *_args, **_kwargs: object())
    first_client = earshot.init(endpoint="http://localhost:4319", project_id="project-a")
    old_recorder = earshot.session(bundle_id="old-project")
    assert earshot.shutdown()
    second_client = earshot.init(endpoint="http://localhost:4320", project_id="project-b")
    assert second_client is not first_client
    old_recorder.close()
    assert old_recorder.export_accepted is False
    assert instances[0].items == []
    assert instances[1].items == []


def test_client_exposes_conversation_flush_health_and_explicit_shutdown() -> None:
    client = earshot.Client()
    with client.conversation(session_id="conversation") as recorder:
        recorder.record_event("voice.connected")
    status = client.status()
    assert status.state == "disabled"
    assert status.healthy
    assert status.lost == 0
    assert client.flush(timeout=0.1)
    assert client.shutdown()
    assert client.status().state == "closed"
    with pytest.raises(RuntimeError, match="shut down"):
        client.session()


def test_module_facade_exposes_flush_status_and_conversation() -> None:
    assert callable(earshot.flush)
    assert callable(earshot.status)
    with earshot.conversation(session_id="global-conversation") as recorder:
        recorder.record_event("voice.connected")
    assert earshot.flush(timeout=0.1)
    assert earshot.status().healthy


@pytest.mark.parametrize("explicit_client", [False, True])
def test_normal_interpreter_exit_flushes_every_live_client(explicit_client: bool) -> None:
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers["Content-Length"])
            self.rfile.read(content_length)
            received.set()
            self.send_response(201)
            self.end_headers()

        def log_message(self, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        if explicit_client:
            script = (
                "import earshot; "
                f"client=earshot.Client(endpoint={endpoint!r}); "
                "client.session(session_id='atexit-explicit').close()"
            )
        else:
            script = (
                "import earshot; "
                f"earshot.configure(endpoint={endpoint!r}); "
                "earshot.session(session_id='atexit-global').close()"
            )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        assert received.wait(2)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_client_health_and_diagnostics_report_lost_incidents() -> None:
    diagnostics = []
    client = earshot.Client(
        endpoint="http://localhost:4319",
        max_queue_bytes=1,
        diagnostic=diagnostics.append,
    )
    try:
        recorder = client.session(bundle_id="oversized")
        recorder.close()
        health = client.status()
        assert recorder.export_accepted is False
        assert health.dropped == 1
        assert health.lost == 1
        assert not health.healthy
        assert diagnostics[-1].code == "exporter.queue_bytes_full"
    finally:
        assert client.shutdown()


def test_client_retains_timed_out_worker_until_shutdown_completes(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingTransport:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def send(self, _item) -> None:
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(sdk, "HttpExportTransport", BlockingTransport)
    client = earshot.Client(endpoint="http://localhost:4319")
    recorder = client.session(bundle_id="blocking-shutdown")
    recorder.close()
    assert started.wait(2)
    assert not client.shutdown(timeout=0.02)
    assert client.status().state == "closing"
    release.set()
    assert client.shutdown(timeout=2)
    assert client.status().state == "closed"


def test_session_closed_after_client_shutdown_is_reported_as_lost() -> None:
    client = earshot.Client(endpoint="http://localhost:4319")
    recorder = client.session(bundle_id="late-close")
    assert client.shutdown()
    recorder.close()
    assert recorder.export_accepted is False
    assert client.status().lost == 1


@pytest.mark.parametrize("capacity", [0, -1])
def test_queue_capacity_is_validated_even_without_an_endpoint(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        earshot.configure(queue_capacity=capacity)
