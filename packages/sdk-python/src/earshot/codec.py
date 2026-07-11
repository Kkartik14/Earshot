"""Deterministic JSON and protobuf codecs for Earshot v1 incidents."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

import rfc8785
from google.protobuf.message import DecodeError
from pydantic import ValidationError

from .contract import IncidentBundle, IncidentBundleJson, IncidentProfile, RawOtlpChunk
from .generated.earshot.v1.incident_pb2 import IncidentEnvelope as _IncidentEnvelopeMessage
from .validation import IncidentValidationError, assert_valid_incident

JSON_MEDIA_TYPE = "application/vnd.earshot.incident+json"
PROTOBUF_MEDIA_TYPE = "application/vnd.earshot.incident+protobuf"
MAX_PROFILE_DEPTH = 64


class IncidentCodecError(ValueError):
    """Raised when bytes cannot be decoded as a valid Earshot incident."""


class IncidentDepthError(IncidentCodecError):
    """Raised when either representation exceeds its configured profile depth."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IncidentCodecError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise IncidentCodecError(f"non-finite JSON number {value!r} is not allowed")


def _assert_profile_depth(value: Any, maximum: int = MAX_PROFILE_DEPTH) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            raise IncidentDepthError("incident profile exceeds maximum nesting depth")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    try:
        if indent is None:
            rendered = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        else:
            rendered = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=indent,
                sort_keys=True,
            )
        return rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise IncidentCodecError("incident contains non-JSON data") from error


def canonical_profile_json(profile: IncidentProfile) -> bytes:
    """Return RFC 8785/JCS snake_case JSON bytes embedded in protobuf."""

    try:
        return rfc8785.dumps(profile.model_dump(mode="json", exclude_none=True))
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as error:
        raise IncidentCodecError("incident profile is outside the RFC 8785 domain") from error


def _chunk_dict(chunk: RawOtlpChunk) -> dict[str, Any]:
    digest = hashlib.sha256(chunk.payload).hexdigest()
    if chunk.sha256 is not None and chunk.sha256 != digest:
        raise IncidentCodecError("OTLP chunk SHA-256 does not match its payload")
    return {
        "chunk_id": chunk.chunk_id,
        "signal": chunk.signal,
        "content_type": chunk.content_type,
        "compression": chunk.compression,
        "payload_base64": base64.b64encode(chunk.payload).decode("ascii"),
        "sha256": digest,
        "privacy_class": chunk.privacy_class,
    }


def encode_incident_json(bundle: IncidentBundle, *, indent: int | None = None) -> bytes:
    """Encode a complete debug/import JSON representation.

    Raw OTLP bytes are represented only as canonical base64 in JSON.  Exact bytes
    are recovered on decode; the protobuf representation stores them directly.
    """

    assert_valid_incident(bundle)
    value = {
        "profile": bundle.profile.model_dump(mode="json", exclude_none=True),
        "raw_otlp_chunks": [_chunk_dict(chunk) for chunk in bundle.raw_otlp_chunks],
    }
    return _json_bytes(value, indent=indent)


def decode_incident_json(
    data: bytes | bytearray | memoryview | str,
    *,
    max_profile_depth: int = MAX_PROFILE_DEPTH,
    validate: bool = True,
) -> IncidentBundle:
    try:
        text = data if isinstance(data, str) else bytes(data).decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except IncidentCodecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise IncidentCodecError("invalid incident JSON") from error

    _assert_profile_depth(value, max_profile_depth)

    if not isinstance(value, dict):
        raise IncidentCodecError("incident JSON root must be an object")
    unknown_envelope_fields = set(value) - {"profile", "raw_otlp_chunks"}
    if unknown_envelope_fields:
        raise IncidentCodecError("incident JSON contains an unsupported envelope field")
    profile_value = value.get("profile")
    chunks_value = value.get("raw_otlp_chunks", [])
    if not isinstance(profile_value, dict):
        raise IncidentCodecError("incident JSON requires an object at profile")
    if not isinstance(chunks_value, list):
        raise IncidentCodecError("raw_otlp_chunks must be an array")

    try:
        json_bundle = IncidentBundleJson.model_validate(value)
        chunks: list[RawOtlpChunk] = []
        for index, raw_chunk in enumerate(json_bundle.raw_otlp_chunks):
            encoded_payload = raw_chunk.payload_base64
            try:
                payload = base64.b64decode(encoded_payload, validate=True)
            except (binascii.Error, ValueError) as error:
                raise IncidentCodecError(
                    f"raw_otlp_chunks[{index}].payload_base64 is invalid"
                ) from error
            chunks.append(
                RawOtlpChunk(
                    chunk_id=raw_chunk.chunk_id,
                    signal=raw_chunk.signal,
                    content_type=raw_chunk.content_type,
                    compression=raw_chunk.compression,
                    payload=payload,
                    sha256=raw_chunk.sha256,
                    privacy_class=raw_chunk.privacy_class,
                )
            )
        bundle = IncidentBundle(profile=json_bundle.profile, raw_otlp_chunks=tuple(chunks))
        if validate:
            assert_valid_incident(bundle)
        return bundle
    except ValidationError as error:
        raise IncidentCodecError("incident JSON violates the v1 structure") from error
    except IncidentValidationError as error:
        raise IncidentCodecError("incident JSON violates v1 invariants") from error


def _encode_incident_protobuf_unchecked(bundle: IncidentBundle) -> bytes:
    """Encode an already-validated bundle for internal digest construction."""

    profile_json = canonical_profile_json(bundle.profile)
    manifest = bundle.profile.manifest
    envelope = _IncidentEnvelopeMessage()
    envelope.schema_version = manifest.schema_version
    envelope.bundle_id = manifest.bundle_id
    envelope.session_id = manifest.session_id
    envelope.canonical_profile_json = profile_json
    envelope.profile_sha256 = hashlib.sha256(profile_json).hexdigest()

    for chunk in bundle.raw_otlp_chunks:
        digest = hashlib.sha256(chunk.payload).hexdigest()
        if chunk.sha256 is not None and chunk.sha256 != digest:
            raise IncidentCodecError("OTLP chunk SHA-256 does not match its payload")
        encoded = envelope.raw_otlp_chunks.add()
        encoded.chunk_id = chunk.chunk_id
        encoded.signal = chunk.signal
        encoded.content_type = chunk.content_type
        encoded.compression = chunk.compression
        encoded.payload = chunk.payload
        encoded.sha256 = digest
        encoded.privacy_class = chunk.privacy_class
    return envelope.SerializeToString(deterministic=True)


def analysis_input_sha256(bundle: IncidentBundle) -> str:
    """Digest the immutable evidence artifact with embedded analysis omitted."""

    evidence_profile = bundle.profile.model_copy(update={"analysis": None})
    evidence_bundle = bundle.model_copy(update={"profile": evidence_profile})
    return hashlib.sha256(_encode_incident_protobuf_unchecked(evidence_bundle)).hexdigest()


def encode_incident_protobuf(bundle: IncidentBundle) -> bytes:
    """Encode a deterministic protobuf envelope with exact OTLP payload bytes."""

    assert_valid_incident(bundle)
    return _encode_incident_protobuf_unchecked(bundle)


def decode_incident_protobuf(
    data: bytes | bytearray | memoryview,
    *,
    max_profile_depth: int = MAX_PROFILE_DEPTH,
    validate: bool = True,
) -> IncidentBundle:
    envelope = _IncidentEnvelopeMessage()
    try:
        envelope.ParseFromString(bytes(data))
    except DecodeError as error:
        raise IncidentCodecError(f"invalid incident protobuf: {error}") from error

    profile_json = bytes(envelope.canonical_profile_json)
    expected_profile_digest = hashlib.sha256(profile_json).hexdigest()
    if envelope.profile_sha256 != expected_profile_digest:
        raise IncidentCodecError("canonical profile SHA-256 does not match envelope")
    try:
        profile_value = json.loads(
            profile_json.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        _assert_profile_depth(profile_value, max_profile_depth)
        profile = IncidentProfile.model_validate(profile_value)
    except IncidentCodecError:
        raise
    except ValidationError as error:
        raise IncidentCodecError("invalid canonical profile structure") from error
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise IncidentCodecError("invalid canonical profile JSON") from error

    if canonical_profile_json(profile) != profile_json:
        raise IncidentCodecError("canonical profile JSON is not RFC 8785/JCS canonical")

    manifest = profile.manifest
    if envelope.schema_version != manifest.schema_version:
        raise IncidentCodecError("envelope and profile schema_version differ")
    if envelope.bundle_id != manifest.bundle_id:
        raise IncidentCodecError("envelope and profile bundle_id differ")
    if envelope.session_id != manifest.session_id:
        raise IncidentCodecError("envelope and profile session_id differ")

    chunks: list[RawOtlpChunk] = []
    try:
        for index, encoded in enumerate(envelope.raw_otlp_chunks):
            payload = bytes(encoded.payload)
            digest = hashlib.sha256(payload).hexdigest()
            if encoded.sha256 != digest:
                raise IncidentCodecError(f"raw_otlp_chunks[{index}] SHA-256 does not match payload")
            chunks.append(
                RawOtlpChunk(
                    chunk_id=encoded.chunk_id,
                    signal=encoded.signal,
                    content_type=encoded.content_type,
                    compression=encoded.compression,
                    payload=payload,
                    sha256=digest,
                    privacy_class=encoded.privacy_class,
                )
            )
        bundle = IncidentBundle(profile=profile, raw_otlp_chunks=tuple(chunks))
        if validate:
            assert_valid_incident(bundle)
        return bundle
    except ValidationError as error:
        raise IncidentCodecError("incident protobuf violates the v1 structure") from error
    except IncidentValidationError as error:
        raise IncidentCodecError("incident protobuf violates v1 invariants") from error


# Short aliases keep exporter integrations readable while the explicit names make
# content type unambiguous at API boundaries.
encode_json = encode_incident_json
decode_json = decode_incident_json
encode_protobuf = encode_incident_protobuf
decode_protobuf = decode_incident_protobuf
