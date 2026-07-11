from __future__ import annotations

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


def test_reconfigure_replaces_and_shuts_down_previous_exporter(monkeypatch) -> None:
    instances = []

    class FakeExporter:
        def __init__(self, transport, *, capacity):
            self.transport = transport
            self.capacity = capacity
            self.shutdown_calls = 0
            instances.append(self)

        def shutdown(self, timeout=5.0):
            del timeout
            self.shutdown_calls += 1
            return True

    class FakeTransport:
        def __init__(self, endpoint, *, token=None):
            self.endpoint = endpoint
            self.token = token

    monkeypatch.setattr(sdk, "BoundedAsyncExporter", FakeExporter)
    monkeypatch.setattr(sdk, "HttpExportTransport", FakeTransport)
    earshot.configure(endpoint="http://one.invalid", token="one", queue_capacity=3)
    first = instances[0]
    recorder = earshot.session()
    assert recorder.exporter is first
    earshot.configure(endpoint="http://two.invalid", token="two", queue_capacity=7)
    assert first.shutdown_calls == 1
    assert instances[1].capacity == 7
    assert instances[1].transport.token == "two"
    assert earshot.shutdown()
    assert instances[1].shutdown_calls == 1
    assert earshot.shutdown()


@pytest.mark.parametrize("capacity", [0, -1])
def test_queue_capacity_is_validated_even_without_an_endpoint(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        earshot.configure(queue_capacity=capacity)
