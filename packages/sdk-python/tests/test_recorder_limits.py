from __future__ import annotations

import pytest

import earshot
from earshot.clock import ManualClock
from earshot.contract import MediaRef
from earshot.exporter import ExportDiagnostic
from earshot.privacy import CaptureClass, CapturePolicy
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident

pytestmark = pytest.mark.unit


def _event(recorder: IncidentRecorder, event_id: str, **attributes: object):
    return recorder.record_event(
        "voice.fact",
        event_id=event_id,
        attributes=attributes,
    )


def test_record_limit_admits_exact_boundary_then_freezes_the_suffix() -> None:
    diagnostics = []
    recorder = IncidentRecorder(
        config=RecorderConfig(
            max_records=1,
            max_capture_bytes=1024 * 1024,
            max_raw_otlp_bytes=1024,
            max_value_bytes=1024,
        ),
        diagnostic=diagnostics.append,
    )

    _event(recorder, "kept", **{"service.name": "voice"})
    attempted = _event(recorder, "dropped", **{"service.name": "voice"})
    recorder.add_participant("participant-after-freeze", role="user")
    recorder.record_coverage("after-freeze", "available")

    status = recorder.status()
    assert attempted.event_id == "dropped"  # compatible attempted-record return
    assert status.truncated
    assert status.first_limit_reason == "max_records"
    assert status.truncated_records == 3
    assert status.captured_records == 1
    assert status.omitted_records_by_kind == (
        ("coverage", 1),
        ("event", 1),
        ("participant", 1),
    )
    assert diagnostics == [ExportDiagnostic("recorder.capture_truncated", recorder.bundle_id)]

    bundle = recorder.close()
    assert [item.event_id for item in bundle.profile.events] == ["kept"]
    assert bundle.profile.participants == ()
    assert bundle.profile.manifest.completeness == "incomplete"
    assert any(item.signal == "recorder.capture" for item in bundle.profile.coverage)
    assert any(
        item.reason == "recorder_capture_truncated" for item in bundle.profile.privacy.omissions
    )
    assert validate_incident(bundle).ok


def test_total_byte_limit_has_an_exact_admission_boundary() -> None:
    probe = IncidentRecorder(
        session_id="same-session",
        config=RecorderConfig(
            max_records=10,
            max_capture_bytes=1024 * 1024,
            max_raw_otlp_bytes=1024,
            max_value_bytes=1024,
        ),
        clock=ManualClock(wall=1000, monotonic=100),
    )
    _event(probe, "same", **{"service.name": "voice"})
    exact_size = probe.status().captured_bytes

    recorder = IncidentRecorder(
        session_id="same-session",
        config=RecorderConfig(
            max_records=10,
            max_capture_bytes=exact_size,
            max_raw_otlp_bytes=1024,
            max_value_bytes=1024,
        ),
        clock=ManualClock(wall=1000, monotonic=100),
    )
    _event(recorder, "same", **{"service.name": "voice"})
    _event(recorder, "later", **{"service.name": "voice"})

    status = recorder.status()
    assert status.captured_bytes == exact_size
    assert status.first_limit_reason == "max_capture_bytes"
    assert [item.event_id for item in recorder.close().profile.events] == ["same"]


def test_oversized_value_is_omitted_before_copy_without_freezing_enclosing_record() -> None:
    recorder = IncidentRecorder(
        config=RecorderConfig(
            max_records=10,
            max_capture_bytes=1024 * 1024,
            max_raw_otlp_bytes=1024,
            max_value_bytes=8,
        )
    )

    _event(recorder, "trimmed", **{"service.name": "x" * 1_000_000})
    _event(recorder, "still-admitted", **{"service.name": "ok"})

    status = recorder.status()
    assert status.truncated
    assert not status.admission_frozen
    assert status.first_limit_reason == "max_value_bytes"
    assert status.truncated_records == 0
    assert status.estimated_omitted_bytes > 8
    bundle = recorder.close()
    assert [item.event_id for item in bundle.profile.events] == [
        "trimmed",
        "still-admitted",
    ]
    assert bundle.profile.events[0].attributes == {}
    assert bundle.profile.manifest.completeness == "incomplete"
    assert validate_incident(bundle).ok


def test_raw_byte_cap_rejects_huge_chunk_before_hashing_and_freezes_all_evidence() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.RAW_OTLP}))
    recorder = IncidentRecorder(
        config=RecorderConfig(
            capture_policy=policy,
            max_records=10,
            max_capture_bytes=1024,
            max_raw_otlp_bytes=3,
            max_value_bytes=1024,
        )
    )

    assert recorder.add_raw_otlp_chunk(chunk_id="one", signal="traces", payload=b"abc")
    assert not recorder.add_raw_otlp_chunk(
        chunk_id="huge", signal="traces", payload=b"z" * 1_000_000
    )
    _event(recorder, "after-freeze", **{"service.name": "voice"})

    status = recorder.status()
    assert status.raw_otlp_bytes == 3
    assert status.first_limit_reason == "max_raw_otlp_bytes"
    assert status.truncated_records == 2
    bundle = recorder.close()
    assert [chunk.chunk_id for chunk in bundle.raw_otlp_chunks] == ["one"]
    assert bundle.profile.events == ()
    assert validate_incident(bundle).ok


def test_truncation_bookkeeping_is_fixed_size_and_diagnostic_is_non_reentrant() -> None:
    diagnostics = []
    recorder: IncidentRecorder

    def diagnostic(item) -> None:
        diagnostics.append(item)
        recorder.record_coverage("reentrant", "available")
        raise RuntimeError("application callback failure")

    recorder = IncidentRecorder(
        config=RecorderConfig(
            max_records=1,
            max_capture_bytes=1024 * 1024,
            max_raw_otlp_bytes=1024,
            max_value_bytes=1024,
        ),
        diagnostic=diagnostic,
    )
    _event(recorder, "kept")
    for index in range(10_000):
        _event(recorder, f"drop-{index}")

    status = recorder.status()
    assert status.truncated_records == 10_001  # includes callback's reentrant coverage
    assert status.omitted_records_by_kind == (("coverage", 1), ("event", 10_000))
    assert len(diagnostics) == 1
    bundle = recorder.close()
    capacity_omissions = [
        item
        for item in bundle.profile.privacy.omissions
        if item.reason == "recorder_capture_truncated"
    ]
    assert len(capacity_omissions) <= len(CaptureClass)
    assert len(bundle.profile.coverage) == 1


def test_client_threads_limits_and_aggregates_recorder_loss_separately() -> None:
    client = earshot.Client(max_records=1, max_capture_bytes=1024 * 1024)
    try:
        recorder = client.session(session_id="bounded")
        _event(recorder, "kept")
        _event(recorder, "dropped")
        recorder.close()

        assert client.config.max_records == 1
        status = client.status()
        assert status.truncated_conversations == 1
        assert status.truncated_records == 1
        assert status.lost == 0  # exporter loss is a distinct channel
    finally:
        assert client.shutdown()


def test_limit_configuration_and_environment_are_validated(monkeypatch) -> None:
    for field in (
        "max_records",
        "max_capture_bytes",
        "max_raw_otlp_bytes",
        "max_value_bytes",
    ):
        with pytest.raises(ValueError, match=field):
            IncidentRecorder(config=RecorderConfig(**{field: 0}))

    monkeypatch.setenv("EARSHOT_MAX_RECORDS", "7")
    monkeypatch.setenv("EARSHOT_MAX_CAPTURE_BYTES", "7000")
    monkeypatch.setenv("EARSHOT_MAX_RAW_OTLP_BYTES", "700")
    monkeypatch.setenv("EARSHOT_MAX_VALUE_BYTES", "70")
    client = earshot.init()
    assert client.config.max_records == 7
    assert client.config.max_capture_bytes == 7000
    assert client.config.max_raw_otlp_bytes == 700
    assert client.config.max_value_bytes == 70

    monkeypatch.setenv("EARSHOT_MAX_RECORDS", "invalid")
    with pytest.raises(ValueError, match="EARSHOT_MAX_RECORDS"):
        earshot.init()


def test_media_after_freeze_uses_its_existing_boolean_failure_surface() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.AUDIO}))
    recorder = IncidentRecorder(
        session_id="session",
        config=RecorderConfig(capture_policy=policy, max_records=1),
    )
    _event(recorder, "kept")
    assert not recorder.add_media_ref(
        MediaRef(
            media_id="media",
            session_id="session",
            stream_id="stream",
            media_kind="audio",
            content_type="audio/wav",
            sha256="a" * 64,
            size_bytes=1,
            capture_class="audio",
        )
    )
