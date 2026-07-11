from __future__ import annotations

import hashlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from earshot.analysis import analyze_incident
from earshot.codec import (
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from earshot.contract import Operation, RawOtlpChunk
from earshot.validation import validate_incident
from incident_factory import TRACE_ID, make_valid_bundle, point
from test_contract_validation import replace_profile

pytestmark = pytest.mark.property


@given(
    nano=st.integers(min_value=0, max_value=2**64 - 1),
    payload=st.binary(min_size=1, max_size=1024),
)
@settings(max_examples=40, deadline=None)
def test_json_and_protobuf_roundtrip_arbitrary_uint64_time_and_binary_otlp(
    nano: int, payload: bytes
) -> None:
    bundle = make_valid_bundle()
    manifest = bundle.profile.manifest.model_copy(update={"created_at_unix_nano": str(nano)})
    chunk = RawOtlpChunk(
        chunk_id="property-chunk",
        signal="traces",
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    bundle = replace_profile(bundle, manifest=manifest).model_copy(
        update={"raw_otlp_chunks": (chunk,)}
    )
    from_json = decode_incident_json(encode_incident_json(bundle))
    from_protobuf = decode_incident_protobuf(encode_incident_protobuf(bundle))
    assert from_json.profile.manifest.created_at_unix_nano == str(nano)
    assert from_protobuf.profile.manifest.created_at_unix_nano == str(nano)
    assert from_json.raw_otlp_chunks[0].payload == payload
    assert from_protobuf.raw_otlp_chunks[0].payload == payload


@given(
    operation_order=st.permutations(tuple(range(5))),
    event_order=st.permutations(tuple(range(6))),
)
@settings(max_examples=30, deadline=None)
def test_validation_and_analysis_are_invariant_to_arrival_order(
    operation_order: list[int], event_order: list[int]
) -> None:
    bundle = make_valid_bundle()
    operations = tuple(bundle.profile.operations[index] for index in operation_order)
    events = tuple(bundle.profile.events[index] for index in event_order)
    shuffled = replace_profile(bundle, operations=operations, events=events)
    assert validate_incident(shuffled).ok
    expected = analyze_incident(bundle, input_sha256="a" * 64, generated_at_unix_nano="1")
    actual = analyze_incident(shuffled, input_sha256="a" * 64, generated_at_unix_nano="1")
    assert actual.model_dump(mode="python") == expected.model_dump(mode="python")


@given(length=st.integers(min_value=2, max_value=20))
@settings(max_examples=19, deadline=None)
def test_parent_dag_accepts_any_chain_and_rejects_single_injected_cycle(length: int) -> None:
    bundle = make_valid_bundle(include_render=False)
    operations: list[Operation] = []
    span_ids = [f"{index + 1:016x}" for index in range(length)]
    for index, span_id in enumerate(span_ids):
        operations.append(
            Operation(
                operation_id=f"chain-{index}",
                session_id="session-1",
                operation_name="tool",
                status="ok",
                started_at=point(index),
                ended_at=point(index + 1),
                trace_id=TRACE_ID,
                span_id=span_id,
                parent_span_id=span_ids[index - 1] if index else None,
                parent_scope="internal" if index else "external",
            )
        )
    chain = replace_profile(bundle, operations=tuple(operations), events=())
    assert validate_incident(chain).ok

    operations[0] = operations[0].model_copy(
        update={"parent_span_id": span_ids[-1], "parent_scope": "internal"}
    )
    cycle = replace_profile(chain, operations=tuple(operations))
    assert "EARSHOT_CAUSAL_CYCLE" in {issue.code for issue in validate_incident(cycle).errors}


@given(index=st.integers(min_value=0, max_value=4), missing=st.text(min_size=1, max_size=30))
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.filter_too_much,),
)
def test_one_dangling_owned_reference_is_never_silently_ignored(index: int, missing: str) -> None:
    bundle = make_valid_bundle()
    if missing in {item.stream_id for item in bundle.profile.audio_streams}:
        return
    operations = list(bundle.profile.operations)
    operations[index] = operations[index].model_copy(update={"stream_id": missing})
    report = validate_incident(replace_profile(bundle, operations=tuple(operations)))
    assert any(issue.code == "EARSHOT_DANGLING_REF" for issue in report.errors)
