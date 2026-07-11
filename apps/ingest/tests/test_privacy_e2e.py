from __future__ import annotations

from pathlib import Path

import pytest

from earshot.analysis import analyze_incident
from earshot.codec import encode_incident_json, encode_incident_protobuf
from earshot.recorder import IncidentRecorder
from earshot.storage import IncidentStore, StorageError
from earshot.validation import validate_incident
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.integration


def _assert_no_secret_in_tree(root: Path) -> None:
    needle = SECRET_SENTINEL.encode()
    for path in root.rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), path
        assert SECRET_SENTINEL not in path.name


def test_metadata_only_sentinel_never_reaches_artifact_index_analysis_or_files(tmp_path) -> None:
    recorder = IncidentRecorder(session_id="privacy-session", bundle_id="privacy-bundle")
    recorder.add_participant(
        "user",
        role="user",
        attributes={
            "phone_number": SECRET_SENTINEL,
            f"unknown.{SECRET_SENTINEL}": SECRET_SENTINEL,
        },
    )
    recorder.record_operation(
        operation_id="private-operation",
        operation_name="stt",
        status="ok",
        started_at=recorder._time(),
        parent_scope=SECRET_SENTINEL,
        instrumentation_scope_name=SECRET_SENTINEL,
        instrumentation_scope_version=SECRET_SENTINEL,
        instrumentation_scope_attributes={"vendor.scope.secret": SECRET_SENTINEL},
        schema_url=f"https://opentelemetry.io/schemas/{SECRET_SENTINEL}",
        resource_schema_url=(
            f"https://opentelemetry.io:{SECRET_SENTINEL}/schemas/1.30.0"
        ),
        attributes={
            "transcript": SECRET_SENTINEL,
            "audio.bytes": SECRET_SENTINEL.encode(),
            "tool.arguments": {"secret": SECRET_SENTINEL},
            "gen_ai.input.messages": [{"content": SECRET_SENTINEL}],
            "exception.message": SECRET_SENTINEL,
            "lk.response.ttft": SECRET_SENTINEL,
            "lk.interrupted": SECRET_SENTINEL,
            "earshot.turn.id": [SECRET_SENTINEL],
            "service.name": "safe-service",
        },
    )
    assert not recorder.add_raw_otlp_chunk(
        chunk_id="private-raw",
        signal="traces",
        payload=SECRET_SENTINEL.encode(),
    )
    bundle = recorder.close()
    assert validate_incident(bundle).ok
    assert SECRET_SENTINEL.encode() not in encode_incident_json(bundle)
    protobuf = encode_incident_protobuf(bundle)
    assert SECRET_SENTINEL.encode() not in protobuf

    data_dir = tmp_path / "store"
    store = IncidentStore(data_dir)
    result = store.ingest(bundle, protobuf)
    assert SECRET_SENTINEL not in str(result.record.as_dict())
    analysis = analyze_incident(
        bundle,
        input_sha256=result.record.digest,
        generated_at_unix_nano="1800000000000000000",
    )
    assert SECRET_SENTINEL not in str(analysis.model_dump(mode="python"))
    store.put_analysis(
        "privacy-bundle",
        analysis.analyzer_version,
        analysis.model_dump(mode="json"),
    )
    _assert_no_secret_in_tree(data_dir)


def test_analysis_sidecar_cannot_add_an_untyped_payload_channel(tmp_path) -> None:
    recorder = IncidentRecorder(session_id="privacy-session", bundle_id="privacy-bundle")
    bundle = recorder.close()
    store = IncidentStore(tmp_path)
    result = store.ingest(bundle, encode_incident_protobuf(bundle))
    analysis = analyze_incident(
        bundle,
        input_sha256=result.record.digest,
        generated_at_unix_nano="1800000000000000000",
    ).model_dump(mode="json")
    analysis["projections"]["secret"] = SECRET_SENTINEL

    with pytest.raises(StorageError, match="DerivedAnalysis contract"):
        store.put_analysis("privacy-bundle", analysis["analyzer_version"], analysis)
    _assert_no_secret_in_tree(tmp_path)
