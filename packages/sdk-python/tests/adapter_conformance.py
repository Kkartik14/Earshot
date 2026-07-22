"""Shared public-seam assertions for adapter compatibility lanes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from earshot.codec import (
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from earshot.contract import IncidentBundle
from earshot.privacy import CaptureClass
from earshot.validation import validate_incident


def _without_generated_clock_identity(bundle: IncidentBundle) -> Any:
    """Normalize the recorder's per-process clock identifier, not its timestamps."""

    value = bundle.model_dump(mode="json")
    clock_ids = {item["clock_domain_id"] for item in value["profile"]["clock_domains"]}
    assert len(clock_ids) == 1
    [clock_id] = clock_ids

    def replace(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "<runtime-clock>"
                if key == "clock_domain_id" and child == clock_id
                else replace(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [replace(child) for child in item]
        return item

    return replace(value)


def assert_capture_conforms(
    capture: Callable[[], IncidentBundle],
    *,
    forbidden_values: Iterable[str] = (),
    expected_completeness: str = "complete",
    require_omission: bool = True,
) -> IncidentBundle:
    """Assert validity, privacy, canonical round-trip, and deterministic output."""

    first = capture()
    second = capture()
    first_protobuf = encode_incident_protobuf(first)
    second_protobuf = encode_incident_protobuf(second)
    first_json = encode_incident_json(first)
    second_json = encode_incident_json(second)

    result = validate_incident(first)
    assert result.ok, result.issues
    assert first.profile.manifest.completeness == expected_completeness
    assert decode_incident_protobuf(first_protobuf) == first
    assert first_protobuf == second_protobuf
    assert first_json == second_json

    private_values = tuple(forbidden_values)
    if require_omission:
        assert first.profile.privacy.omissions
    for value in private_values:
        encoded = value.encode("utf-8")
        assert encoded not in first_protobuf
        assert encoded not in first_json
        assert value not in repr(first)
    return first


def assert_canonical_payload_conforms(
    payload: bytes,
    *,
    deterministic_peer: bytes,
    forbidden_values: Iterable[str] = (),
) -> IncidentBundle:
    """Assert conformance at a finalized-delivery canonical payload boundary."""

    bundle = decode_incident_protobuf(payload)
    peer = decode_incident_protobuf(deterministic_peer)
    result = validate_incident(bundle)
    assert result.ok, result.issues
    assert encode_incident_protobuf(bundle) == payload
    assert _without_generated_clock_identity(bundle) == _without_generated_clock_identity(peer)
    assert bundle.profile.manifest.completeness == "complete"

    # Finalized providers are normalized before their values enter the recorder.
    # Consequently there is no recorder field-level omission to enumerate, but
    # the canonical manifest must still prove that sensitive classes were denied
    # and not captured. Streaming adapters exercise explicit omission records.
    policies = {item.capture_class: item for item in bundle.profile.privacy.capture_classes}
    for capture_class in (
        CaptureClass.TRANSCRIPT,
        CaptureClass.AUDIO,
        CaptureClass.TOOL_PAYLOAD,
        CaptureClass.MODEL_PAYLOAD,
        CaptureClass.DIAGNOSTIC_PAYLOAD,
        CaptureClass.RAW_OTLP,
    ):
        policy = policies[capture_class.value]
        assert policy.decision == "deny"
        assert not policy.captured

    canonical_json = encode_incident_json(bundle)
    for value in forbidden_values:
        encoded = value.encode("utf-8")
        assert encoded not in payload
        assert encoded not in canonical_json
        assert value not in repr(bundle)
    return bundle


def assert_native_trace_topology(
    bundle: IncidentBundle,
    *,
    trace_id: str,
    root_span_id: str,
    child_span_ids: Iterable[str],
) -> None:
    """Assert native trace identity and direct child parentage are unchanged."""

    operations = {
        operation.span_id: operation
        for operation in bundle.profile.operations
        if operation.trace_id == trace_id and operation.span_id is not None
    }
    assert root_span_id in operations
    assert operations[root_span_id].parent_span_id is None
    for span_id in child_span_ids:
        assert span_id in operations
        assert operations[span_id].trace_id == trace_id
        assert operations[span_id].parent_span_id == root_span_id
