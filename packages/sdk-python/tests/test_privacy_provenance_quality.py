from __future__ import annotations

import hashlib

import pytest

from earshot.codec import IncidentCodecError, decode_incident_json, encode_incident_json
from earshot.contract import (
    ByteRange,
    CaptureClassPolicy,
    Coverage,
    ErrorRecord,
    Evidence,
    MediaLocator,
    MediaRef,
    QualityMeasurement,
    QualitySample,
    RawOtlpChunk,
    TimeRange,
)
from earshot.contract import Omission as ContractOmission
from earshot.privacy import (
    CaptureClass,
    CapturePolicy,
    classify_attribute,
    contains_secret_sentinel,
    sanitize_attributes,
)
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident
from incident_factory import SECRET_SENTINEL, evidence, point
from test_contract_validation import issue_codes, replace_profile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("key", "capture_class"),
    [
        ("transcript", CaptureClass.TRANSCRIPT),
        ("audio.bytes", CaptureClass.AUDIO),
        ("tool.arguments", CaptureClass.TOOL_PAYLOAD),
        ("gen_ai.input.messages", CaptureClass.MODEL_PAYLOAD),
        ("exception.message", CaptureClass.DIAGNOSTIC_PAYLOAD),
    ],
)
def test_metadata_only_omits_each_sensitive_payload_class(
    key: str, capture_class: CaptureClass
) -> None:
    kept, omissions = sanitize_attributes({key: SECRET_SENTINEL})
    assert kept == {}
    assert [(item.field_key_sha256, item.capture_class) for item in omissions] == [
        (hashlib.sha256(key.encode()).hexdigest(), capture_class)
    ]


def test_metadata_only_is_allowlist_based_for_unknown_keys() -> None:
    kept, omissions = sanitize_attributes(
        {
            "service.name": "voice-service",
            "totally.unknown": SECRET_SENTINEL,
            "earshot.metric.queue_depth": 0,
        }
    )
    assert kept == {"service.name": "voice-service", "earshot.metric.queue_depth": 0}
    assert [item.field_key_sha256 for item in omissions] == [
        hashlib.sha256(b"totally.unknown").hexdigest()
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("lk.response.ttft", SECRET_SENTINEL),
        ("lk.interrupted", SECRET_SENTINEL),
        ("turn.was_interrupted", SECRET_SENTINEL),
        ("earshot.link.type", SECRET_SENTINEL),
        ("earshot.turn.id", [SECRET_SENTINEL]),
        ("service.name", [SECRET_SENTINEL]),
    ],
)
def test_known_metadata_keys_enforce_field_specific_value_shapes(key: str, value: object) -> None:
    kept, omissions = sanitize_attributes({key: value})
    assert kept == {}
    assert len(omissions) == 1


def test_raw_otlp_grant_does_not_authorize_unknown_normalized_attributes() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.RAW_OTLP}))
    kept, omissions = sanitize_attributes(
        {"future.extension": SECRET_SENTINEL},
        policy,
    )
    assert kept == {}
    assert len(omissions) == 1


def test_raw_otlp_class_cannot_label_a_normalized_record(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={"capture_class": "raw_otlp", "attributes": {"future.extension": 1}}
    )
    assert "EARSHOT_STRUCTURAL_INVALID" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )

    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.RAW_OTLP}))
    recorder = IncidentRecorder(config=RecorderConfig(capture_policy=policy))
    with pytest.raises(ValueError, match="only to opaque OTLP chunks"):
        recorder.record_operation(
            operation_id="raw-mislabel",
            operation_name="agent",
            status="ok",
            started_at=recorder._time(),
            capture_class="raw_otlp",
        )
    recorder.close()


def test_extension_payload_grant_is_separate_and_requires_a_governed_key() -> None:
    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.EXTENSION_PAYLOAD})
    )
    kept, omissions = sanitize_attributes(
        {
            "future.extension": {"value": SECRET_SENTINEL},
            "SECRET extension key": 1,
        },
        policy,
    )
    assert kept == {"future.extension": {"value": SECRET_SENTINEL}}
    assert len(omissions) == 1


def test_raw_policy_cannot_hide_unknown_profile_attribute_or_key(valid_bundle) -> None:
    unknown_value = replace_profile(
        valid_bundle,
        attributes={"future.extension": SECRET_SENTINEL},
    )
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in issue_codes(unknown_value)

    unknown_key = replace_profile(
        valid_bundle,
        attributes={"SECRET_IN_ATTRIBUTE_KEY": 1},
    )
    assert "EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED" in issue_codes(unknown_key)


def test_unknown_semantic_looking_numeric_fields_still_require_extension_policy(
    valid_bundle,
) -> None:
    unknown_attribute = replace_profile(valid_bundle, attributes={"customer.ssn": 123})
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in issue_codes(unknown_attribute)

    profile = valid_bundle.profile.model_copy(update={"customer.ssn": 123})
    unknown_extra = valid_bundle.model_copy(update={"profile": profile})
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in issue_codes(unknown_extra)

    policies = tuple(
        policy.model_copy(update={"decision": "allow", "captured": True})
        if policy.capture_class == "extension_payload"
        else policy
        for policy in valid_bundle.profile.privacy.capture_classes
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    allowed_profile = valid_bundle.profile.model_copy(
        update={
            "privacy": privacy,
            "attributes": {"customer.ssn": 123},
            "customer.ssn": 123,
        }
    )
    assert validate_incident(
        valid_bundle.model_copy(update={"profile": allowed_profile})
    ).ok


def test_nonmetadata_records_and_model_extras_cannot_bypass_extension_policy(
    valid_bundle,
) -> None:
    policies = tuple(
        policy.model_copy(update={"decision": "allow", "captured": True})
        if policy.capture_class == "model_payload"
        else policy
        for policy in valid_bundle.profile.privacy.capture_classes
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    operation = valid_bundle.profile.operations[0].model_copy(
        update={
            "capture_class": "model_payload",
            "attributes": {"customer.ssn": 123},
            "service.name": "misplaced-model-extra",
        }
    )
    broken = replace_profile(
        valid_bundle,
        privacy=privacy,
        operations=(operation, *valid_bundle.profile.operations[1:]),
    )
    codes = issue_codes(broken)
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in codes


def test_recorder_rejects_model_extras_before_close_and_allows_explicit_extension() -> None:
    extended_evidence = evidence().model_copy(update={"customer.ssn": 123})
    recorder = IncidentRecorder()
    with pytest.raises(ValueError, match="require extension_payload"):
        recorder.record_operation(
            operation_id="rejected-extension",
            operation_name="agent",
            status="ok",
            started_at=recorder._time(),
            evidence=extended_evidence,
        )
    assert recorder.close().profile.operations == ()

    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.EXTENSION_PAYLOAD})
    )
    allowed = IncidentRecorder(config=RecorderConfig(capture_policy=policy))
    allowed.record_operation(
        operation_id="allowed-extension",
        operation_name="agent",
        status="ok",
        started_at=allowed._time(),
        evidence=extended_evidence,
    )
    bundle = allowed.close()
    assert bundle.profile.operations[0].evidence is not None
    assert bundle.profile.operations[0].evidence.model_extra == {"customer.ssn": 123}
    extension = next(
        item
        for item in bundle.profile.privacy.capture_classes
        if item.capture_class == "extension_payload"
    )
    assert extension.captured and extension.decision == "allow"
    assert validate_incident(bundle).ok


def test_metadata_numeric_and_decimal_values_are_bounded_without_overflow() -> None:
    huge_integer = 10**400
    for key, value in (
        ("lk.response.ttft", huge_integer),
        ("earshot.metric.future_count", huge_integer),
        ("earshot.time.monotonic_nano", "18446744073709551616"),
    ):
        kept, omissions = sanitize_attributes({key: value})
        assert kept == {}
        assert len(omissions) == 1


def test_schema_url_port_is_digested_and_third_party_urls_require_extension_policy(
    valid_bundle,
) -> None:
    for index, unsafe in enumerate(
        (
            f"https://opentelemetry.io:{SECRET_SENTINEL}/schemas/1.30.0",
            "https://opentelemetry.io:443/schemas/1.30.0",
        )
    ):
        recorder = IncidentRecorder()
        recorder.record_operation(
            operation_id=f"schema-port-{index}",
            operation_name="agent",
            status="ok",
            started_at=recorder._time(),
            schema_url=unsafe,
        )
        sanitized = recorder.close()
        operation = sanitized.profile.operations[0]
        assert operation.schema_url is None
        assert operation.attributes["earshot.source.schema_url_sha256"] == hashlib.sha256(
            unsafe.encode()
        ).hexdigest()
        assert SECRET_SENTINEL not in encode_incident_json(sanitized).decode()
        assert validate_incident(sanitized).ok

    third_party = "https://telemetry.example.com/schemas/1.2.3"
    source_operation = valid_bundle.profile.operations[0].model_copy(
        update={"schema_url": third_party}
    )
    denied = replace_profile(
        valid_bundle,
        operations=(source_operation, *valid_bundle.profile.operations[1:]),
    )
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in issue_codes(denied)
    resource_source = valid_bundle.profile.operations[0].model_copy(
        update={"resource_schema_url": third_party}
    )
    resource_denied = replace_profile(
        valid_bundle,
        operations=(resource_source, *valid_bundle.profile.operations[1:]),
    )
    assert "EARSHOT_PRIVACY_UNKNOWN_METADATA" in issue_codes(resource_denied)

    extension_policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.EXTENSION_PAYLOAD})
    )
    extension_recorder = IncidentRecorder(
        config=RecorderConfig(capture_policy=extension_policy)
    )
    extension_recorder.record_operation(
        operation_id="third-party-schema",
        operation_name="agent",
        status="ok",
        started_at=extension_recorder._time(),
        schema_url=third_party,
        resource_schema_url="https://resource.example.com/schemas/2.0.0",
        instrumentation_scope_attributes={"vendor.scope.option": {"enabled": True}},
    )
    retained = extension_recorder.close()
    assert retained.profile.operations[0].schema_url == third_party
    assert retained.profile.operations[0].resource_schema_url == (
        "https://resource.example.com/schemas/2.0.0"
    )
    assert retained.profile.operations[0].instrumentation_scope_attributes == {
        "vendor.scope.option": {"enabled": True}
    }
    assert validate_incident(retained).ok


def test_coverage_evidence_uses_the_same_governed_provenance_rules(valid_bundle) -> None:
    coverage = Coverage(
        signal="render",
        availability="not_observed",
        reason="collector_unavailable",
        evidence=Evidence(
            source=SECRET_SENTINEL,
            observer=SECRET_SENTINEL,
            method=SECRET_SENTINEL,
            method_version=SECRET_SENTINEL,
            confidence=SECRET_SENTINEL,
            availability=SECRET_SENTINEL,
            source_field=SECRET_SENTINEL,
        ),
    )
    codes = issue_codes(replace_profile(valid_bundle, coverage=(coverage,)))
    assert "EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED" in codes


def test_omission_reason_is_a_non_sensitive_semantic_code(valid_bundle) -> None:
    omission = ContractOmission(
        omission_id="omission-safe",
        capture_class="transcript",
        reason="capture_class_disabled",
    ).model_copy(update={"reason": SECRET_SENTINEL})
    privacy = valid_bundle.profile.privacy.model_copy(update={"omissions": (omission,)})
    assert "EARSHOT_STRUCTURAL_INVALID" in issue_codes(
        replace_profile(valid_bundle, privacy=privacy)
    )


def test_explicit_capture_class_opt_in_keeps_only_that_class() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}))
    kept, omissions = sanitize_attributes(
        {"transcript": SECRET_SENTINEL, "tool.arguments": SECRET_SENTINEL}, policy
    )
    assert kept == {"transcript": SECRET_SENTINEL}
    assert [item.capture_class for item in omissions] == [CaptureClass.TOOL_PAYLOAD]


def test_participant_identity_attributes_carry_and_declare_identity_class() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.IDENTITY}))
    recorder = IncidentRecorder(
        session_id="identity-session",
        config=RecorderConfig(capture_policy=policy),
    )
    participant = recorder.add_participant(
        "participant-identity",
        role="user",
        attributes={"phone_number": SECRET_SENTINEL},
        capture_class="identity",
    )

    bundle = recorder.close()

    assert participant.capture_class == "identity"
    assert participant.attributes == {"phone_number": SECRET_SENTINEL}
    identity_policy = next(
        item for item in bundle.profile.privacy.capture_classes if item.capture_class == "identity"
    )
    assert identity_policy.captured and identity_policy.decision == "allow"
    assert validate_incident(bundle).ok


def test_audio_stream_attributes_carry_and_declare_audio_class() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.AUDIO}))
    recorder = IncidentRecorder(
        session_id="audio-session",
        config=RecorderConfig(capture_policy=policy),
    )
    recorder.add_participant("participant-agent", role="agent")
    stream = recorder.add_stream(
        "stream-output",
        participant_id="participant-agent",
        direction="output",
        attributes={"audio.codec_config": SECRET_SENTINEL},
    )

    bundle = recorder.close()

    assert stream.capture_class == "audio"
    assert stream.attributes == {"audio.codec_config": SECRET_SENTINEL}
    audio_policy = next(
        item for item in bundle.profile.privacy.capture_classes if item.capture_class == "audio"
    )
    assert audio_policy.captured and audio_policy.decision == "allow"
    assert validate_incident(bundle).ok


def test_participant_explicit_class_cannot_mislabel_retained_identity() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.IDENTITY}))
    recorder = IncidentRecorder(config=RecorderConfig(capture_policy=policy))

    with pytest.raises(ValueError, match="does not match retained payload"):
        recorder.add_participant(
            "participant-identity",
            role="user",
            attributes={"phone_number": SECRET_SENTINEL},
            capture_class="metadata",
        )


def test_raw_otlp_is_intrinsically_raw_and_requires_raw_policy(valid_bundle) -> None:
    payload = b"opaque-otlp"
    with pytest.raises(ValueError):
        RawOtlpChunk(
            chunk_id="downgraded",
            signal="traces",
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            privacy_class="metadata",
        )

    downgraded = valid_bundle.raw_otlp_chunks[0].model_copy(update={"privacy_class": "metadata"})
    broken = valid_bundle.model_copy(update={"raw_otlp_chunks": (downgraded,)})
    assert "EARSHOT_STRUCTURAL_INVALID" in issue_codes(broken)


def test_recorder_raw_otlp_opt_in_always_records_raw_class() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.RAW_OTLP}))
    recorder = IncidentRecorder(config=RecorderConfig(capture_policy=policy))
    assert recorder.add_raw_otlp_chunk(
        chunk_id="raw",
        signal="traces",
        payload=b"opaque-otlp",
    )

    bundle = recorder.close()

    assert bundle.raw_otlp_chunks[0].privacy_class == "raw_otlp"
    raw_policy = next(
        item for item in bundle.profile.privacy.capture_classes if item.capture_class == "raw_otlp"
    )
    assert raw_policy.captured and raw_policy.decision == "allow"
    assert validate_incident(bundle).ok


def test_recursive_secret_scanner_checks_keys_and_values() -> None:
    assert contains_secret_sentinel(
        {"outer": [{"nested": f"prefix-{SECRET_SENTINEL}-suffix"}]}, [SECRET_SENTINEL]
    )
    assert contains_secret_sentinel({SECRET_SENTINEL: "value"}, [SECRET_SENTINEL])
    assert not contains_secret_sentinel({"safe": [1, False, None]}, [SECRET_SENTINEL])


@pytest.mark.parametrize(
    ("key", "expected_class"),
    [
        ("transcript", "transcript"),
        ("tool.arguments", "tool_payload"),
        ("prompt", "model_payload"),
        ("audio.data", "audio"),
        ("exception.stacktrace", "diagnostic_payload"),
    ],
)
def test_validator_rejects_payload_smuggled_under_metadata(
    valid_bundle, key: str, expected_class: str
) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={"attributes": {key: SECRET_SENTINEL}})
    broken = replace_profile(valid_bundle, operations=tuple(operations))
    report = validate_incident(broken)
    issue = next(item for item in report.errors if item.code == "EARSHOT_PRIVACY_PAYLOAD_SMUGGLED")
    assert classify_attribute(key).value == expected_class
    assert issue.path[-1] == "<key>"
    assert SECRET_SENTINEL not in issue.message


@pytest.mark.parametrize("key", ["heard_at", "audio.heard_at", "agent.heard"])
def test_unobservable_human_hearing_claim_is_rejected(valid_bundle, key: str) -> None:
    broken = replace_profile(valid_bundle, attributes={key: "1800000000000000000"})
    assert "EARSHOT_UNOBSERVABLE_HEARD_CLAIM" in issue_codes(broken)


def test_unknown_capture_class_is_structurally_rejected(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={"capture_class": "new-sensitive-class"})
    assert "EARSHOT_STRUCTURAL_INVALID" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_denied_capture_class_cannot_have_payload(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={"capture_class": "transcript", "attributes": {"transcript": SECRET_SENTINEL}}
    )
    assert "EARSHOT_PRIVACY_CAPTURE_DENIED" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_raw_diagnostic_error_message_obeys_its_capture_policy(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "error": ErrorRecord(
                code="failure",
                category="application",
                message=SECRET_SENTINEL,
                capture_class="diagnostic_payload",
            )
        }
    )
    assert "EARSHOT_PRIVACY_CAPTURE_DENIED" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


@pytest.mark.parametrize(
    "uri",
    [
        f"https://user:{SECRET_SENTINEL}@example.invalid/audio.wav",
        f"https://example.invalid/audio.wav?token={SECRET_SENTINEL}",
        f"https://example.invalid/audio.wav?X-Amz-Signature={SECRET_SENTINEL}",
        f"https://storage.googleapis.com/audio.wav?X-Goog-Credential={SECRET_SENTINEL}",
        f"https://storage.googleapis.com/audio.wav?X-Goog-Signature={SECRET_SENTINEL}",
    ],
)
def test_media_locator_rejects_embedded_credentials(valid_bundle, uri: str) -> None:
    allowed_audio = CaptureClassPolicy(capture_class="audio", decision="allow", captured=True)
    policies = tuple(
        allowed_audio if policy.capture_class == "audio" else policy
        for policy in valid_bundle.profile.privacy.capture_classes
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    media = MediaRef(
        media_id="media-1",
        session_id="session-1",
        stream_id="stream-output",
        media_kind="audio",
        content_type="audio/wav",
        sha256="a" * 64,
        size_bytes=42,
        locator=MediaLocator(uri=uri),
    )
    broken = replace_profile(valid_bundle, privacy=privacy, media_refs=(media,))
    report = validate_incident(broken)
    assert "EARSHOT_MEDIA_LOCATOR_CREDENTIAL" in {item.code for item in report.errors}
    assert SECRET_SENTINEL not in str(report)


def test_public_media_locator_is_not_dereferenced_during_validation(
    valid_bundle, monkeypatch
) -> None:
    import urllib.request

    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("validator must not perform network I/O")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)
    allowed_audio = CaptureClassPolicy(capture_class="audio", decision="allow", captured=True)
    privacy = valid_bundle.profile.privacy.model_copy(
        update={
            "capture_classes": tuple(
                allowed_audio if item.capture_class == "audio" else item
                for item in valid_bundle.profile.privacy.capture_classes
            )
        }
    )
    media = MediaRef(
        media_id="media-public",
        session_id="session-1",
        stream_id="stream-output",
        media_kind="audio",
        content_type="audio/wav",
        sha256="b" * 64,
        size_bytes=0,
        locator=MediaLocator(uri="https://example.invalid/audio.wav"),
    )
    assert validate_incident(replace_profile(valid_bundle, privacy=privacy, media_refs=(media,))).ok
    assert not called


def test_media_byte_range_must_fit_inside_declared_object(valid_bundle) -> None:
    allowed_audio = CaptureClassPolicy(capture_class="audio", decision="allow", captured=True)
    privacy = valid_bundle.profile.privacy.model_copy(
        update={
            "capture_classes": tuple(
                allowed_audio if item.capture_class == "audio" else item
                for item in valid_bundle.profile.privacy.capture_classes
            )
        }
    )
    media = MediaRef(
        media_id="media-range",
        session_id="session-1",
        stream_id="stream-output",
        media_kind="audio",
        content_type="audio/wav",
        sha256="b" * 64,
        size_bytes=10,
        byte_range=ByteRange(offset=9, length=2),
    )
    assert "EARSHOT_MEDIA_RANGE_OUT_OF_BOUNDS" in issue_codes(
        replace_profile(valid_bundle, privacy=privacy, media_refs=(media,))
    )


def test_media_reference_cannot_be_mislabeled_as_metadata(valid_bundle) -> None:
    media = MediaRef(
        media_id="media-smuggled",
        session_id="session-1",
        stream_id="stream-output",
        media_kind="audio",
        content_type="audio/wav",
        sha256="b" * 64,
        size_bytes=10,
        capture_class="metadata",
    )
    assert "EARSHOT_PRIVACY_PAYLOAD_SMUGGLED" in issue_codes(
        replace_profile(valid_bundle, media_refs=(media,))
    )


def _quality_sample(
    *,
    name: str,
    value: int | float,
    quality_kind: str,
    provenance: Evidence,
    unit: str = "ms",
) -> QualitySample:
    return QualitySample(
        sample_id="quality-1",
        session_id="session-1",
        quality_kind=quality_kind,
        sample_window=TimeRange(start=point(0), end=point(1_000_000)),
        measurements=(QualityMeasurement(name=name, value=value, unit=unit),),
        evidence=provenance,
        stream_id="stream-input",
    )


def test_quality_sample_requires_provenance(valid_bundle) -> None:
    sample = _quality_sample(
        name="jitter", value=1, quality_kind="transport", provenance=evidence()
    ).model_copy(update={"evidence": None})
    assert "EARSHOT_EVIDENCE_REQUIRED" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_quality_sample_cannot_be_empty(valid_bundle) -> None:
    sample = _quality_sample(
        name="jitter",
        value=0,
        quality_kind="transport",
        provenance=evidence(source="webrtc_stats", method="getStats"),
    ).model_copy(update={"measurements": ()})
    assert "EARSHOT_QUALITY_EMPTY" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_unavailable_quality_evidence_cannot_carry_zero_as_a_fake_value(valid_bundle) -> None:
    sample = _quality_sample(
        name="jitter",
        value=0,
        quality_kind="transport",
        provenance=evidence(availability="not_observed"),
    )
    assert "EARSHOT_UNAVAILABLE_VALUE" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_observed_zero_quality_measurement_is_valid(valid_bundle) -> None:
    sample = _quality_sample(
        name="jitter",
        value=0,
        quality_kind="transport",
        provenance=evidence(source="webrtc_stats", method="getStats"),
    )
    assert validate_incident(replace_profile(valid_bundle, quality_samples=(sample,))).ok


@pytest.mark.parametrize("name", ["packet_loss", "packets_lost", "jitter", "rtt_ms"])
def test_network_quality_cannot_be_inferred_from_pcm(valid_bundle, name: str) -> None:
    sample = _quality_sample(
        name=name,
        value=1,
        quality_kind="transport",
        provenance=evidence(source="audio_inference", method="pcm_analysis"),
    )
    assert "EARSHOT_NETWORK_QOS_SOURCE_INVALID" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_p563_mos_must_be_classified_as_perceptual_not_network_quality(valid_bundle) -> None:
    sample = _quality_sample(
        name="mos_lqo",
        value=3.8,
        quality_kind="transport",
        provenance=evidence(source="audio", method="ITU-T P.563", confidence="estimated"),
        unit="MOS-LQO",
    )
    assert "EARSHOT_PERCEPTUAL_MOS_MISCLASSIFIED" in issue_codes(
        replace_profile(valid_bundle, quality_samples=(sample,))
    )


def test_render_claim_requires_provenance(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    render_index = next(
        index for index, item in enumerate(operations) if item.operation_name == "render"
    )
    operations[render_index] = operations[render_index].model_copy(update={"evidence": None})
    assert "EARSHOT_EVIDENCE_REQUIRED" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_render_claim_cannot_use_explicitly_unavailable_evidence(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    render_index = next(
        index for index, item in enumerate(operations) if item.operation_name == "render"
    )
    render = operations[render_index]
    assert render.evidence is not None
    operations[render_index] = render.model_copy(
        update={"evidence": render.evidence.model_copy(update={"availability": "not_observed"})}
    )
    assert "EARSHOT_UNAVAILABLE_VALUE" in issue_codes(
        replace_profile(valid_bundle, operations=tuple(operations))
    )


def test_default_recorder_never_leaks_sensitive_payloads_or_exception_message() -> None:
    recorder = IncidentRecorder(session_id="safe-session", bundle_id="safe-bundle")
    with (
        pytest.raises(RuntimeError, match="public error wrapper"),
        recorder.operation(
            "tool",
            attributes={
                "transcript": SECRET_SENTINEL,
                "tool.arguments": SECRET_SENTINEL,
                "exception.message": SECRET_SENTINEL,
                "service.name": "safe-service",
            },
        ),
    ):
        raise RuntimeError(f"public error wrapper {SECRET_SENTINEL}")
    bundle = recorder.close("failed")
    encoded = encode_incident_json(bundle)
    assert SECRET_SENTINEL.encode() not in encoded
    assert not contains_secret_sentinel(bundle.model_dump(mode="python"), [SECRET_SENTINEL])
    assert bundle.profile.operations[0].error is not None
    assert bundle.profile.operations[0].error.message is None
    assert len(bundle.profile.privacy.omissions) == 3


def test_codec_validation_error_does_not_reflect_secret_input(valid_bundle) -> None:
    raw = encode_incident_json(valid_bundle).decode("utf-8")
    poisoned = raw.replace('"session_id":"session-1"', f'"session_id":"{SECRET_SENTINEL}"', 1)
    with pytest.raises(IncidentCodecError) as caught:
        decode_incident_json(poisoned)
    assert SECRET_SENTINEL not in str(caught.value)


def test_enabled_transcript_capture_is_represented_by_matching_record_class() -> None:
    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}))
    recorder = IncidentRecorder(config=RecorderConfig(capture_policy=policy))
    recorder.record_operation(
        operation_id="op-transcript",
        operation_name="stt",
        status="ok",
        started_at=recorder._time(),
        attributes={"transcript": "allowed text"},
    )
    bundle = recorder.close()
    # Opt-in capture must produce a self-consistent bundle instead of retaining a
    # transcript under the default metadata classification.
    assert validate_incident(bundle).ok
    assert bundle.profile.operations[0].capture_class == "transcript"


def test_recorder_filters_nested_quality_measurement_attributes() -> None:
    recorder = IncidentRecorder(session_id="session-1")
    now = recorder._time()
    sample = _quality_sample(
        name="jitter",
        value=0,
        quality_kind="transport",
        provenance=evidence(source="webrtc_stats", method="getStats"),
    ).model_copy(
        update={
            "stream_id": None,
            "sample_window": TimeRange(start=now, end=now),
            "measurements": (
                QualityMeasurement(
                    name="jitter",
                    value=0,
                    unit="ms",
                    attributes={"transcript": SECRET_SENTINEL},
                ),
            ),
        }
    )
    retained = recorder.record_quality_sample(sample)
    bundle = recorder.close()
    assert retained.measurements[0].attributes == {}
    assert bundle.profile.quality_samples == (retained,)
    assert len(bundle.profile.privacy.omissions) == 1
    assert SECRET_SENTINEL not in encode_incident_json(bundle).decode()


def test_quality_measurement_scalars_cannot_carry_free_form_payload() -> None:
    with pytest.raises(ValueError):
        QualityMeasurement(name="jitter", value=SECRET_SENTINEL, unit="ms")
    with pytest.raises(ValueError):
        QualityMeasurement(
            name="jitter",
            value=1,
            unit="ms",
            raw_counter=SECRET_SENTINEL,
        )


def test_recorder_media_policy_omits_by_default_and_strips_credentials_when_allowed() -> None:
    media = MediaRef(
        media_id="media-1",
        session_id="session-media",
        stream_id="stream-1",
        media_kind="audio",
        content_type="audio/wav",
        sha256="c" * 64,
        size_bytes=10,
        locator=MediaLocator(
            uri=(f"https://storage.googleapis.com/audio.wav?X-Goog-Signature={SECRET_SENTINEL}")
        ),
    )
    denied = IncidentRecorder(session_id="session-media")
    assert not denied.add_media_ref(media)
    assert denied.close().profile.media_refs == ()

    policy = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.AUDIO}))
    allowed = IncidentRecorder(
        session_id="session-media",
        config=RecorderConfig(capture_policy=policy),
    )
    allowed.add_participant("participant-1", role="agent")
    allowed.add_stream(
        "stream-1",
        participant_id="participant-1",
        direction="output",
    )
    assert allowed.add_media_ref(media)
    bundle = allowed.close()
    assert bundle.profile.media_refs[0].locator is None
    assert SECRET_SENTINEL not in encode_incident_json(bundle).decode()
    assert validate_incident(bundle).ok
