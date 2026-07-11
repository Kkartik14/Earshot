from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from earshot.codec import decode_incident_json
from earshot.validation import validate_incident

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def _replace_pointer(document: object, pointer: str, value: object) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    target = document
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
    final = parts[-1]
    if isinstance(target, list):
        target[int(final)] = value
    else:
        target[final] = value  # type: ignore[index]


def test_all_shared_valid_json_fixtures_decode_and_validate() -> None:
    paths = sorted((ROOT / "fixtures" / "valid").glob("*.json"))
    assert paths
    for path in paths:
        bundle = decode_incident_json(path.read_bytes())
        assert validate_incident(bundle).ok, path


def test_invalid_mutation_ledger_breaks_exactly_the_named_invariant() -> None:
    manifest_path = ROOT / "fixtures" / "invalid" / "mutations.json"
    manifest = json.loads(manifest_path.read_text())
    cases = manifest["mutations"]
    base = json.loads((manifest_path.parent / manifest["base"]).read_text())
    assert cases
    for case in cases:
        value = deepcopy(base)
        _replace_pointer(value, case["pointer"], case["value"])
        bundle = decode_incident_json(json.dumps(value), validate=False)
        codes = {issue.code for issue in validate_incident(bundle).errors}
        assert case["expected_code"] in codes, case["name"]


def test_fault_catalog_has_unique_ids_and_covers_the_plan_categories() -> None:
    scenarios = json.loads((ROOT / "fixtures" / "faults" / "scenarios.json").read_text())
    identifiers = [item["id"] for item in scenarios]
    assert len(identifiers) == len(set(identifiers))
    assert {
        "fast_endpointing",
        "slow_endpointing",
        "barge_in",
        "tool_timeout_retry",
        "webrtc_degradation",
        "native_s2s_interruption",
        "privacy_opt_out",
        "telephony_handoff",
    } <= set(identifiers)
    assert all(item["required_signals"] for item in scenarios)
    for item in scenarios:
        artifact = ROOT / "fixtures" / "faults" / f"{item['id']}.incident.json"
        bundle = decode_incident_json(artifact.read_bytes())
        assert validate_incident(bundle).ok, artifact
        signals = {
            *(operation.operation_name for operation in bundle.profile.operations),
            *(event.event_name.removeprefix("earshot.") for event in bundle.profile.events),
            *(
                link.relationship
                for operation in bundle.profile.operations
                for link in operation.links
            ),
            *(sample.quality_kind for sample in bundle.profile.quality_samples),
            *(
                sample.evidence.source
                for sample in bundle.profile.quality_samples
                if sample.evidence is not None
            ),
            *(
                measurement.name
                for sample in bundle.profile.quality_samples
                for measurement in sample.measurements
            ),
            *(coverage.signal for coverage in bundle.profile.coverage),
            *(coverage.availability for coverage in bundle.profile.coverage),
            *(
                omission.reason.removesuffix("_disabled")
                for omission in bundle.profile.privacy.omissions
            ),
            *(omission.capture_class for omission in bundle.profile.privacy.omissions),
            *("omission" for _ in bundle.profile.privacy.omissions),
            *(
                policy.capture_class
                for policy in bundle.profile.privacy.capture_classes
                if policy.captured
            ),
        }
        assert set(item["required_signals"]) <= signals, item["id"]


def _fault_bundle(identifier: str):
    artifact = ROOT / "fixtures" / "faults" / f"{identifier}.incident.json"
    return decode_incident_json(artifact.read_bytes())


def _event_mono(bundle, event_name: str) -> int:
    event = next(item for item in bundle.profile.events if item.event_name == event_name)
    assert event.time.monotonic_time_nano is not None
    return int(event.time.monotonic_time_nano)


def test_endpointing_faults_distinguish_fast_and_slow_commit_delay() -> None:
    fast = _fault_bundle("fast_endpointing")
    slow = _fault_bundle("slow_endpointing")
    fast_delay = _event_mono(fast, "earshot.turn.committed") - _event_mono(
        fast, "earshot.speech.ended"
    )
    slow_delay = _event_mono(slow, "earshot.turn.committed") - _event_mono(
        slow, "earshot.speech.ended"
    )
    assert fast_delay == 80_000_000
    assert slow_delay == 1_300_000_000
    assert slow_delay > fast_delay * 10


def test_barge_in_fault_orders_each_distinct_cancellation_boundary() -> None:
    bundle = _fault_bundle("barge_in")
    ordered_names = [
        "earshot.interruption.detected",
        "earshot.interruption.accepted",
        "earshot.model.cancelled",
        "earshot.audio.queued.discarded",
        "earshot.audio.render.stopped",
    ]
    assert [_event_mono(bundle, name) for name in ordered_names] == sorted(
        _event_mono(bundle, name) for name in ordered_names
    )
    assert {
        operation.operation_name: operation.status for operation in bundle.profile.operations
    } == {"agent": "cancelled", "tts": "cancelled", "render": "cancelled"}


@pytest.mark.parametrize(
    ("identifier", "expected_bottleneck"),
    [("stt_delay", "stt"), ("llm_delay", "llm"), ("tts_delay", "tts")],
)
def test_pipeline_delay_fault_localizes_named_stage_and_output_boundaries(
    identifier: str,
    expected_bottleneck: str,
) -> None:
    bundle = _fault_bundle(identifier)
    durations: dict[str, int] = {}
    for operation in bundle.profile.operations:
        assert operation.started_at.monotonic_time_nano is not None
        assert operation.ended_at is not None
        assert operation.ended_at.monotonic_time_nano is not None
        durations[operation.operation_name] = int(
            operation.ended_at.monotonic_time_nano
        ) - int(operation.started_at.monotonic_time_nano)
    assert max(durations, key=durations.__getitem__) == expected_bottleneck
    boundary_names = [
        "earshot.response.first_audio_generated",
        "earshot.audio.first_byte_sent",
        "earshot.audio.first_packet_received",
        "earshot.audio.render.started",
    ]
    assert [_event_mono(bundle, name) for name in boundary_names] == sorted(
        _event_mono(bundle, name) for name in boundary_names
    )


def test_tool_retry_fault_links_attempt_and_downstream_resume() -> None:
    bundle = _fault_bundle("tool_timeout_retry")
    operations = {item.operation_id: item for item in bundle.profile.operations}
    retry = operations["op-tool-attempt-2"]
    downstream = operations["op-downstream-agent"]
    assert operations["op-tool-attempt-1"].status == "timeout"
    assert [(item.relationship, item.target_operation_id) for item in retry.links] == [
        ("retries", "op-tool-attempt-1")
    ]
    assert [(item.relationship, item.target_operation_id) for item in downstream.links] == [
        ("consumes", "op-tool-attempt-2")
    ]
    assert retry.ended_at is not None
    assert retry.ended_at.monotonic_time_nano is not None
    assert downstream.started_at.monotonic_time_nano is not None
    assert int(downstream.started_at.monotonic_time_nano) > int(
        retry.ended_at.monotonic_time_nano
    )


def test_webrtc_fault_has_direct_loss_jitter_rtt_stats_provenance() -> None:
    sample = _fault_bundle("webrtc_degradation").profile.quality_samples[0]
    assert sample.evidence is not None
    assert sample.evidence.source == "webrtc_stats"
    assert {item.name for item in sample.measurements} == {
        "packet_loss_ratio",
        "jitter",
        "round_trip_time",
    }


def test_websocket_fault_preserves_duplicate_and_out_of_order_identity_links() -> None:
    bundle = _fault_bundle("websocket_reconnect")
    operations = {item.operation_id: item for item in bundle.profile.operations}
    assert operations["op-ws-message-duplicate"].links[0].relationship == "duplicates"
    assert operations["op-ws-message-out-of-order"].links[0].relationship == "supersedes"
    assert len({item.event_id for item in bundle.profile.events}) == len(bundle.profile.events)
    assert _event_mono(bundle, "earshot.transport.message.duplicate") < _event_mono(
        bundle, "earshot.transport.message.out_of_order"
    )


def test_device_fault_keeps_capture_render_and_permission_absence_explicit() -> None:
    bundle = _fault_bundle("device_unavailable")
    coverage = {
        item.signal: (item.availability, item.reason) for item in bundle.profile.coverage
    }
    assert coverage == {
        "device.microphone": ("not_observed", "permission_denied"),
        "capture": ("not_observed", "device_unavailable"),
        "client.render": ("not_observed", "app_backgrounded"),
    }
    assert {item.event_name for item in bundle.profile.events} == {
        "earshot.device.permission_denied",
        "earshot.device.audio_context_suspended",
    }


def test_native_s2s_fault_never_invents_cascaded_stages() -> None:
    bundle = _fault_bundle("native_s2s_interruption")
    names = {item.operation_name for item in bundle.profile.operations}
    assert "agent" in names
    assert names.isdisjoint({"stt", "llm", "tts"})


def test_telephony_fault_models_distinct_legs_and_handoff_ownership() -> None:
    artifact = ROOT / "fixtures" / "faults" / "telephony_handoff.incident.json"
    bundle = decode_incident_json(artifact.read_bytes())
    participants = {
        participant.participant_id: (participant.role, participant.endpoint_kind)
        for participant in bundle.profile.participants
    }
    streams = {
        stream.stream_id: (stream.participant_id, stream.direction, stream.transport_ref)
        for stream in bundle.profile.audio_streams
    }
    operations = {operation.operation_id: operation for operation in bundle.profile.operations}

    assert participants == {
        "participant-caller": ("user", "pstn"),
        "participant-bot": ("agent", "sip"),
        "participant-human": ("human_operator", "pstn"),
    }
    assert streams == {
        "stream-caller-inbound": (
            "participant-caller",
            "input",
            "sip-leg-inbound",
        ),
        "stream-bot-outbound": ("participant-bot", "output", "sip-leg-bot"),
        "stream-human-outbound": (
            "participant-human",
            "output",
            "sip-leg-human",
        ),
    }
    human_leg = operations["op-human-leg"]
    assert human_leg.participant_id == "participant-human"
    assert human_leg.stream_id == "stream-human-outbound"
    assert [(link.relationship, link.target_operation_id) for link in human_leg.links] == [
        ("handoff", "op-bot-leg")
    ]


def test_security_regression_catalog_is_complete_and_language_neutral() -> None:
    cases = json.loads((ROOT / "fixtures" / "faults" / "security_regressions.json").read_text())
    identifiers = [item["id"] for item in cases]
    assert len(identifiers) == len(set(identifiers))
    assert {
        "error_message_metadata_smuggling",
        "cas_cleanup_during_inflight_ingest",
        "restricted_list_and_analysis_export",
        "cross_origin_bearer_redirect",
        "reentrant_diagnostic_callback",
        "application_exception_masking",
        "protobuf_configured_depth_bypass",
        "negotiated_representation_etag",
        "secure_purge_residual_bytes",
        "stream_participant_owner_mismatch",
        "mixed_clock_representation_reversal",
    } == set(identifiers)
    assert all(item["layer"] and item["expected"] for item in cases)
