from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

import earshot.codec as codec
from earshot.codec import (
    IncidentCodecError,
    IncidentDepthError,
    canonical_profile_json,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from earshot.contract import CaptureClassPolicy, IncidentBundle, RawOtlpChunk
from earshot.validation import validate_incident
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def test_json_encoding_is_deterministic_and_snake_case(valid_bundle: IncidentBundle) -> None:
    first = encode_incident_json(valid_bundle)
    second = encode_incident_json(valid_bundle)
    assert first == second
    assert b'"created_at_unix_nano"' in first
    assert b'"createdAtUnixNano"' not in first


def test_json_roundtrip_preserves_profile_and_exact_otlp_bytes(
    valid_bundle: IncidentBundle,
) -> None:
    decoded = decode_incident_json(encode_incident_json(valid_bundle))
    assert decoded.profile == valid_bundle.profile
    assert decoded.raw_otlp_chunks[0].payload == valid_bundle.raw_otlp_chunks[0].payload
    assert decoded.raw_otlp_chunks[0].sha256 == hashlib.sha256(b"\x0a\x00").hexdigest()


def test_protobuf_encoding_is_deterministic(valid_bundle: IncidentBundle) -> None:
    assert encode_incident_protobuf(valid_bundle) == encode_incident_protobuf(valid_bundle)


def test_protobuf_roundtrip_preserves_profile_and_exact_otlp_bytes(
    valid_bundle: IncidentBundle,
) -> None:
    encoded = encode_incident_protobuf(valid_bundle)
    decoded = decode_incident_protobuf(encoded)
    assert decoded.profile == valid_bundle.profile
    assert decoded.raw_otlp_chunks[0].payload == b"\x0a\x00"
    assert encode_incident_protobuf(decoded) == encoded


def test_large_nanoseconds_remain_exact_decimal_strings_across_both_codecs(valid_bundle) -> None:
    maximum = "18446744073709551615"
    manifest = valid_bundle.profile.manifest.model_copy(update={"created_at_unix_nano": maximum})
    bundle = valid_bundle.model_copy(
        update={"profile": valid_bundle.profile.model_copy(update={"manifest": manifest})}
    )
    json_decoded = decode_incident_json(encode_incident_json(bundle))
    proto_decoded = decode_incident_protobuf(encode_incident_protobuf(bundle))
    assert json_decoded.profile.manifest.created_at_unix_nano == maximum
    assert proto_decoded.profile.manifest.created_at_unix_nano == maximum


def test_unknown_profile_extensions_survive_json_and_protobuf(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(
        update={"future_profile_extension": {"version": "future", "list": [1, 2]}}
    )
    operation = profile.operations[0].model_copy(
        update={"future_operation_extension": {"kept": True}}
    )
    profile = profile.model_copy(update={"operations": (operation, *profile.operations[1:])})
    policies = tuple(
        CaptureClassPolicy(
            capture_class=item.capture_class,
            decision=("allow" if item.capture_class == "extension_payload" else item.decision),
            captured=True if item.capture_class == "extension_payload" else item.captured,
        )
        if item.capture_class == "extension_payload"
        else item
        for item in profile.privacy.capture_classes
    )
    profile = profile.model_copy(
        update={"privacy": profile.privacy.model_copy(update={"capture_classes": policies})}
    )
    bundle = valid_bundle.model_copy(update={"profile": profile})
    for decoded in (
        decode_incident_json(encode_incident_json(bundle)),
        decode_incident_protobuf(encode_incident_protobuf(bundle)),
    ):
        value = decoded.model_dump(mode="python")
        assert value["profile"]["future_profile_extension"]["version"] == "future"
        assert value["profile"]["operations"][0]["future_operation_extension"] == {"kept": True}


def test_json_rejects_unknown_outer_or_raw_chunk_fields(valid_bundle) -> None:
    value = json.loads(encode_incident_json(valid_bundle))
    value["future_envelope"] = True
    with pytest.raises(IncidentCodecError, match="unsupported envelope"):
        decode_incident_json(json.dumps(value))

    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0]["future_chunk"] = True
    with pytest.raises(IncidentCodecError, match="violates the v1 structure"):
        decode_incident_json(json.dumps(value))

    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0]["payload"] = "shadow-payload"
    with pytest.raises(IncidentCodecError, match="violates the v1 structure"):
        decode_incident_json(json.dumps(value))


def test_json_wire_requires_the_declared_otlp_digest(valid_bundle) -> None:
    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0].pop("sha256")
    with pytest.raises(IncidentCodecError, match="violates the v1 structure"):
        decode_incident_json(json.dumps(value))


def test_null_profile_extensions_are_rejected_instead_of_silently_dropped(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(update={"future_nullable": None})
    bundle = valid_bundle.model_copy(update={"profile": profile})
    assert "EARSHOT_NULL_EXTENSION_UNSUPPORTED" in {
        issue.code for issue in validate_incident(bundle).errors
    }
    with pytest.raises(Exception, match="EARSHOT_NULL_EXTENSION_UNSUPPORTED"):
        encode_incident_protobuf(bundle)


def test_arbitrary_binary_otlp_payload_is_not_utf8_normalized(valid_bundle) -> None:
    payload = bytes(range(256)) + b"\x00\xff\x80"
    chunk = RawOtlpChunk(
        chunk_id="binary",
        signal="traces",
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    bundle = valid_bundle.model_copy(update={"raw_otlp_chunks": (chunk,)})
    assert decode_incident_json(encode_incident_json(bundle)).raw_otlp_chunks[0].payload == payload
    assert (
        decode_incident_protobuf(encode_incident_protobuf(bundle)).raw_otlp_chunks[0].payload
        == payload
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"profile":{},"profile":{}}',
        b'{"profile":{"manifest":{"bundle_id":"one","bundle_id":"two"}}}',
    ],
)
def test_json_decoder_rejects_duplicate_keys_at_any_depth(payload: bytes) -> None:
    with pytest.raises(IncidentCodecError, match="duplicate JSON object key"):
        decode_incident_json(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_decoder_rejects_nonfinite_constants(constant: str) -> None:
    payload = '{"profile":{"extension":' + constant + "}}"
    with pytest.raises(IncidentCodecError, match="non-finite"):
        decode_incident_json(payload)


def test_json_decoder_rejects_invalid_utf8() -> None:
    with pytest.raises(IncidentCodecError, match="invalid incident JSON"):
        decode_incident_json(b"\xff\xfe")


def test_json_decoder_normalizes_oversized_integer_parse_failure() -> None:
    payload = '{"profile":{"future":' + ("1" * 5_000) + "}}"

    with pytest.raises(IncidentCodecError, match="invalid incident JSON") as caught:
        decode_incident_json(payload)

    assert type(caught.value.__cause__) is ValueError


def test_protobuf_decoder_normalizes_oversized_integer_parse_failure(valid_bundle) -> None:
    def mutate(envelope) -> None:
        envelope.canonical_profile_json = b'{"future":' + (b"1" * 5_000) + b"}"
        envelope.profile_sha256 = hashlib.sha256(envelope.canonical_profile_json).hexdigest()

    with pytest.raises(IncidentCodecError, match="invalid canonical profile JSON") as caught:
        decode_incident_protobuf(_mutated_envelope(valid_bundle, mutate))

    assert type(caught.value.__cause__) is ValueError


@pytest.mark.parametrize("depth", [codec.MAX_PROFILE_DEPTH + 1, 500, 1_000])
def test_both_encoders_reject_profiles_their_decoders_cannot_read(valid_bundle, depth: int) -> None:
    value: object = True
    for _ in range(depth):
        value = {"next": value}
    policies = tuple(
        policy.model_copy(update={"decision": "allow", "captured": True})
        if policy.capture_class == "extension_payload"
        else policy
        for policy in valid_bundle.profile.privacy.capture_classes
    )
    privacy = valid_bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    profile = valid_bundle.profile.model_copy(update={"privacy": privacy, "vendor.deep": value})
    bundle = valid_bundle.model_copy(update={"profile": profile})

    with pytest.raises(IncidentDepthError):
        encode_incident_json(bundle)
    with pytest.raises(IncidentDepthError):
        encode_incident_protobuf(bundle)


def test_json_decoder_rejects_invalid_base64_without_echoing_value(valid_bundle) -> None:
    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0]["payload_base64"] = SECRET_SENTINEL
    with pytest.raises(IncidentCodecError) as caught:
        decode_incident_json(json.dumps(value))
    assert SECRET_SENTINEL not in str(caught.value)


def test_json_decoder_rejects_chunk_digest_mismatch(valid_bundle) -> None:
    value = json.loads(encode_incident_json(valid_bundle))
    value["raw_otlp_chunks"][0]["sha256"] = "f" * 64
    with pytest.raises(IncidentCodecError, match="violates v1 invariants"):
        decode_incident_json(json.dumps(value))


def test_protobuf_decoder_rejects_truncated_payload(valid_bundle) -> None:
    encoded = encode_incident_protobuf(valid_bundle)
    with pytest.raises(IncidentCodecError):
        decode_incident_protobuf(encoded[: len(encoded) // 2])


def _mutated_envelope(valid_bundle, mutate) -> bytes:
    envelope = codec._IncidentEnvelopeMessage()
    envelope.ParseFromString(encode_incident_protobuf(valid_bundle))
    mutate(envelope)
    return envelope.SerializeToString(deterministic=True)


def test_protobuf_decoder_rejects_profile_digest_tampering(valid_bundle) -> None:
    payload = _mutated_envelope(
        valid_bundle, lambda envelope: setattr(envelope, "profile_sha256", "0" * 64)
    )
    with pytest.raises(IncidentCodecError, match="profile SHA-256"):
        decode_incident_protobuf(payload)


def test_protobuf_decoder_rejects_digest_valid_noncanonical_profile_json(valid_bundle) -> None:
    def mutate(envelope) -> None:
        canonical = bytes(envelope.canonical_profile_json)
        envelope.canonical_profile_json = b" \n" + canonical + b"\n"
        envelope.profile_sha256 = hashlib.sha256(envelope.canonical_profile_json).hexdigest()

    with pytest.raises(IncidentCodecError, match="not RFC 8785/JCS canonical"):
        decode_incident_protobuf(_mutated_envelope(valid_bundle, mutate))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile", "privacy", "capture_classes", 0, "captured"), "true"),
        (("profile", "privacy", "capture_classes", 0, "captured"), 1),
        (("profile", "audio_streams", 0, "format", "sample_rate_hz"), "16000"),
    ],
)
def test_json_decoder_rejects_coercible_scalar_spellings(valid_bundle, path, value) -> None:
    document = json.loads(encode_incident_json(valid_bundle))
    target = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(IncidentCodecError, match="violates the v1 structure"):
        decode_incident_json(json.dumps(document))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile", "operations", 0, "parent_scope"), SECRET_SENTINEL),
        (("profile", "operations", 0, "instrumentation_scope_name"), SECRET_SENTINEL),
        (("profile", "operations", 0, "instrumentation_scope_version"), SECRET_SENTINEL),
        (
            ("profile", "operations", 0, "schema_url"),
            f"https://opentelemetry.io/schemas/{SECRET_SENTINEL}",
        ),
        (("profile", "privacy", "capture_classes", 0, "capture_class"), "invented"),
    ],
)
def test_untrusted_json_rejects_unsafe_governance_fields_without_echoing_secret(
    valid_bundle,
    path,
    value,
) -> None:
    document = json.loads(encode_incident_json(valid_bundle))
    target = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(IncidentCodecError) as caught:
        decode_incident_json(json.dumps(document))
    assert SECRET_SENTINEL not in str(caught.value)


def test_untrusted_json_rejects_unsafe_link_labels_without_echoing_secret(valid_bundle) -> None:
    document = json.loads(encode_incident_json(valid_bundle))
    document["profile"]["operations"][0]["links"] = [
        {
            "relationship": SECRET_SENTINEL,
            "target_scope": SECRET_SENTINEL,
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
        }
    ]

    with pytest.raises(IncidentCodecError) as caught:
        decode_incident_json(json.dumps(document))
    assert SECRET_SENTINEL not in str(caught.value)


def test_untrusted_json_rejects_raw_otlp_as_a_normalized_capture_class(valid_bundle) -> None:
    document = json.loads(encode_incident_json(valid_bundle))
    document["profile"]["operations"][0]["capture_class"] = "raw_otlp"
    with pytest.raises(IncidentCodecError, match="violates the v1 structure"):
        decode_incident_json(json.dumps(document))


@pytest.mark.parametrize("field", ["bundle_id", "session_id", "schema_version"])
def test_protobuf_envelope_identity_must_match_profile(valid_bundle, field: str) -> None:
    payload = _mutated_envelope(
        valid_bundle, lambda envelope: setattr(envelope, field, "different")
    )
    with pytest.raises(IncidentCodecError, match=f"{field} differ"):
        decode_incident_protobuf(payload)


def test_protobuf_decoder_rejects_otlp_chunk_digest_tampering(valid_bundle) -> None:
    def mutate(envelope) -> None:
        envelope.raw_otlp_chunks[0].sha256 = "f" * 64

    with pytest.raises(IncidentCodecError, match="SHA-256"):
        decode_incident_protobuf(_mutated_envelope(valid_bundle, mutate))


def test_canonical_profile_hash_changes_when_a_fact_changes(valid_bundle) -> None:
    original = hashlib.sha256(canonical_profile_json(valid_bundle.profile)).hexdigest()
    session = valid_bundle.profile.session.model_copy(update={"status": "failed"})
    changed = valid_bundle.profile.model_copy(update={"session": session})
    assert hashlib.sha256(canonical_profile_json(changed)).hexdigest() != original


def test_canonical_profile_uses_rfc8785_number_spelling(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(
        update={
            "attributes": {
                "tiny": 1e-7,
                "large": 1e20,
                "negative_zero": -0.0,
            }
        }
    )
    encoded = canonical_profile_json(profile)
    assert b'"tiny":1e-7' in encoded
    assert b'"large":100000000000000000000' in encoded
    assert b'"negative_zero":0' in encoded


def test_rfc8785_vector_matches_javascript_runtime() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    value = {
        "z": -0.0,
        "tiny": 1e-7,
        "large": 1e20,
        "nested": {"z": 2, "a": "é"},
    }
    python_bytes = codec.rfc8785.dumps(value)
    script = r"""
const value = JSON.parse(process.argv[1]);
const canonical = (item) => {
  if (Array.isArray(item)) return item.map(canonical);
  if (item !== null && typeof item === 'object') {
    return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canonical(item[key])]));
  }
  return item;
};
process.stdout.write(JSON.stringify(canonical(value)));
"""
    result = subprocess.run(
        [node, "-e", script, json.dumps(value)],
        check=True,
        capture_output=True,
    )
    assert result.stdout == python_bytes
    assert hashlib.sha256(python_bytes).hexdigest() == (
        "8572ef353bc65b2b5ca2a2b9ea5346346768c278d338aedcaab0278d3478c2cd"
    )


def test_full_contract_canonical_vector_matches_profile_and_envelope_digests() -> None:
    fixture = ROOT / "fixtures" / "conformance"
    source = (fixture / "canonical-vector.input.json").read_bytes()
    expected = json.loads((fixture / "canonical-vector.expected.json").read_text())
    bundle = decode_incident_json(source)
    envelope_bytes = encode_incident_protobuf(bundle)
    envelope = codec._IncidentEnvelopeMessage()
    envelope.ParseFromString(envelope_bytes)
    profile_bytes = bytes(envelope.canonical_profile_json)
    assert profile_bytes.decode("utf-8") == expected["canonical_profile_json"]
    assert hashlib.sha256(profile_bytes).hexdigest() == expected["profile_sha256"]
    assert hashlib.sha256(envelope_bytes).hexdigest() == expected["envelope_sha256"]


def test_validation_rejects_numbers_outside_ijson_domain(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(update={"attributes": {"count": 2**60}})
    report = validate_incident(valid_bundle.model_copy(update={"profile": profile}))
    assert "EARSHOT_IJSON_INTEGER_DOMAIN" in {issue.code for issue in report.errors}
    with pytest.raises(IncidentCodecError, match="RFC 8785 domain"):
        canonical_profile_json(profile)


def test_validation_rejects_non_unicode_object_keys_before_canonicalization(valid_bundle) -> None:
    profile = valid_bundle.profile.model_copy(update={"future_extension": {"\ud800": 1}})
    invalid = valid_bundle.model_copy(update={"profile": profile})
    report = validate_incident(invalid)
    assert "EARSHOT_IJSON_UNICODE_DOMAIN" in {issue.code for issue in report.errors}
    with pytest.raises(Exception, match="EARSHOT_IJSON_UNICODE_DOMAIN"):
        encode_incident_protobuf(invalid)


def test_model_copy_cannot_bypass_structural_decimal_constraints(valid_bundle) -> None:
    manifest = valid_bundle.profile.manifest.model_copy(
        update={"created_at_unix_nano": SECRET_SENTINEL}
    )
    invalid = valid_bundle.model_copy(
        update={"profile": valid_bundle.profile.model_copy(update={"manifest": manifest})}
    )
    with warnings.catch_warnings(record=True) as caught:
        report = validate_incident(invalid)
    assert SECRET_SENTINEL not in " ".join(str(item.message) for item in caught)
    assert "EARSHOT_STRUCTURAL_INVALID" in {issue.code for issue in report.errors}
    with pytest.raises(Exception, match="EARSHOT_STRUCTURAL_INVALID"):
        encode_incident_protobuf(invalid)


def test_codec_rejects_semantically_invalid_bundle_before_serialization(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(update={"participant_id": "missing"})
    invalid = valid_bundle.model_copy(
        update={
            "profile": valid_bundle.profile.model_copy(update={"operations": tuple(operations)})
        }
    )
    assert not validate_incident(invalid).ok
    with pytest.raises(Exception, match="EARSHOT_DANGLING_REF"):
        encode_incident_protobuf(invalid)
