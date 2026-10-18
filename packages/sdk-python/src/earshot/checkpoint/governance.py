"""Validate and classify checkpoint facts before an egress decision.

Journal envelopes are an on-disk format, not proof that a producer applied the
recorder's privacy rules. This module is the shared boundary for record-kind
validation and capture-class derivation used by live tailing and assembly.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from ..contract import (
    Adapter,
    AudioStream,
    CausalLink,
    ClockDomain,
    ClockRelation,
    Coverage,
    ErrorRecord,
    Event,
    Evidence,
    MediaRef,
    Operation,
    Participant,
    QualitySample,
    RawOtlpChunk,
    TimePoint,
)
from ..privacy import (
    CaptureClass,
    CapturePolicy,
    classify_attribute,
    is_safe_metadata_key,
    is_safe_semantic_label,
    is_unobservable_heard_key,
    media_locator_safety,
    normalize_event_name,
    normalize_operation_name,
    sanitize_attributes,
    sanitize_error_label,
    sanitize_measurement_label,
    sanitize_measurement_unit,
    sanitize_provenance_label,
    sanitize_schema_url,
    sanitize_semantic_label,
    sanitize_source_label,
    sanitize_version_label,
)
from .records import (
    JournalExhausted,
    JournalFinalize,
    JournalLimitEntry,
    JournalOperationOpen,
    JournalRecordEntry,
)

RECORD_MODELS: dict[str, type[BaseModel]] = {
    "adapter": Adapter,
    "participant": Participant,
    "stream": AudioStream,
    "coverage": Coverage,
    "operation": Operation,
    "event": Event,
    "quality_sample": QualitySample,
    "media": MediaRef,
    "clock_domain": ClockDomain,
    "clock_relation": ClockRelation,
}

_ATTRIBUTE_FIELDS = frozenset(
    {
        "attributes",
        "resource",
        "instrumentation_scope_attributes",
    }
)
_SCHEMA_FIELDS = frozenset({"schema_url", "resource_schema_url"})
_RECORD_KINDS = frozenset((*RECORD_MODELS, "raw_otlp", "omission", "policy"))
_LIMIT_REASONS = frozenset(
    {"max_capture_bytes", "max_raw_otlp_bytes", "max_records", "max_value_bytes"}
)
_OMISSION_REASONS = frozenset(
    {
        "adapter_payload_omitted",
        "capture_class_disabled",
        "credential_bearing_locator",
        "invalid_media_locator",
        "typed_field_normalized",
        "unobservable_claim",
        "unsafe_attribute_key_or_value",
    }
)


def decode_raw_otlp_value(value: dict[str, Any]) -> RawOtlpChunk | None:
    """Rebuild the opaque chunk stored in a journal record, rejecting extras."""

    allowed_fields = {
        "chunk_id",
        "signal",
        "content_type",
        "compression",
        "sha256",
        "privacy_class",
        "payload_base64",
    }
    if set(value) - allowed_fields:
        return None
    if value.get("privacy_class", "raw_otlp") != "raw_otlp":
        return None
    encoded = value.get("payload_base64")
    if not isinstance(encoded, str):
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
        return RawOtlpChunk(
            chunk_id=value["chunk_id"],
            signal=value["signal"],
            content_type=value.get("content_type", "application/x-protobuf"),
            compression=value.get("compression", "identity"),
            payload=payload,
            sha256=value.get("sha256"),
        )
    except (KeyError, binascii.Error, TypeError, ValueError):
        return None


def validate_and_classify_record(
    entry: JournalRecordEntry,
    policy: CapturePolicy,
) -> tuple[BaseModel | RawOtlpChunk | None, frozenset[CaptureClass]]:
    """Validate a journal record as a recorder would and derive what it retained.

    ``retained_classes`` is a producer-maintained journal ledger. It remains useful
    for replay consistency, but it cannot be the live export gate's authority:
    classify the record body independently before any subscriber sees it.
    """

    if entry.value is None:
        return None, frozenset()
    if entry.kind == "raw_otlp":
        chunk = decode_raw_otlp_value(entry.value)
        if chunk is None:
            raise ValueError("raw OTLP journal record is invalid")
        return chunk, frozenset({CaptureClass.RAW_OTLP})

    model_type = RECORD_MODELS.get(entry.kind)
    if model_type is None:
        raise ValueError("journal record kind has no contract model")
    try:
        record = model_type.model_validate(entry.value)
    except (TypeError, ValueError) as error:
        raise ValueError("journal record is not a valid contract record") from error

    _validate_normalized_fields(record, policy)
    classes: set[CaptureClass] = set()
    _collect_classes(record, policy, classes)
    if isinstance(record, MediaRef):
        classes.add(CaptureClass.AUDIO)
        if record.locator is not None and media_locator_safety(record.locator.uri) != "portable":
            raise ValueError("media record contains a non-portable locator")
    if any(not policy.allows(capture_class) for capture_class in classes):
        raise ValueError("journal record retained a capture class disabled by policy")
    return record, frozenset(classes)


def validate_operation_open(entry: JournalOperationOpen) -> None:
    """Keep the early operation event on the recorder's sanitized surface."""

    normalized, digest = normalize_operation_name(entry.operation_name)
    # A non-safe source name is represented only by the stable placeholder and
    # its digest; the original string is never a live-tail field.
    if (normalized, digest) != (entry.operation_name, entry.operation_name_sha256) and not (
        entry.operation_name == "framework_operation"
        and entry.operation_name_sha256 is not None
        and len(entry.operation_name_sha256) == 64
        and all(character in "0123456789abcdef" for character in entry.operation_name_sha256)
    ):
        raise ValueError("operation-open name is not normalized")
    try:
        TimePoint.model_validate(entry.started_at)
    except (TypeError, ValueError) as error:
        raise ValueError("operation-open timestamp is not a contract time point") from error


def validate_nonrecord_entry(entry: Any) -> None:
    """Validate the small metadata-only entries that are also sent over SSE."""

    from .records import JOURNAL_FORMAT_VERSION, JournalOpen

    if isinstance(entry, JournalOpen):
        if entry.journal_format_version != JOURNAL_FORMAT_VERSION:
            raise ValueError("journal header has an unsupported format version")
        try:
            entry.capture_policy()
        except (TypeError, ValueError) as error:
            raise ValueError("journal header has invalid capture governance") from error
        if (
            min(
                entry.max_records,
                entry.max_capture_bytes,
                entry.max_raw_otlp_bytes,
                entry.max_value_bytes,
            )
            < 1
        ):
            raise ValueError("journal header declares invalid recorder limits")
        return
    if isinstance(entry, JournalOperationOpen):
        validate_operation_open(entry)
        return
    if isinstance(entry, JournalLimitEntry):
        try:
            CaptureClass(entry.capture_class)
        except ValueError as error:
            raise ValueError("limit entry has an unsupported capture class") from error
        if entry.kind not in _RECORD_KINDS or entry.reason not in _LIMIT_REASONS:
            raise ValueError("limit entry contains an unsupported reason or record kind")
        return
    if isinstance(entry, JournalExhausted):
        if entry.reason != "checkpoint_journal_full":
            raise ValueError("exhausted entry has an unsupported reason")
        return
    if isinstance(entry, JournalFinalize):
        if sanitize_semantic_label(entry.status) != entry.status:
            raise ValueError("finalize status is not normalized")
        safe_attributes, omissions = sanitize_attributes(
            entry.status_attributes,
            CapturePolicy.metadata_only(),
        )
        if omissions or safe_attributes != entry.status_attributes:
            raise ValueError("finalize status attributes are not recorder-governed")
        try:
            TimePoint.model_validate(entry.ended)
        except (TypeError, ValueError) as error:
            raise ValueError("finalize timestamp is not a contract time point") from error
        if entry.first_limit_reason is not None and entry.first_limit_reason not in _LIMIT_REASONS:
            raise ValueError("finalize entry has an unsupported limit reason")
        return
    if isinstance(entry, JournalRecordEntry):
        for omission in entry.omissions:
            try:
                CaptureClass(omission.capture_class)
            except ValueError as error:
                raise ValueError("omission has an unsupported capture class") from error
            if (
                len(omission.field_key_sha256) != 64
                or any(
                    character not in "0123456789abcdef" for character in omission.field_key_sha256
                )
                or omission.reason not in _OMISSION_REASONS
            ):
                raise ValueError("omission contains unsupported metadata")


def _validate_normalized_fields(record: BaseModel, policy: CapturePolicy) -> None:
    """Refuse values the recorder would hash, strip, or classify differently."""

    for model in _walk_models(record):
        for field_name in _ATTRIBUTE_FIELDS:
            attributes = getattr(model, field_name, None)
            if attributes is None:
                continue
            safe_attributes, omissions = sanitize_attributes(attributes, policy)
            if omissions or safe_attributes != attributes:
                raise ValueError("record contains an unnormalized attribute map")
        for field_name in _SCHEMA_FIELDS:
            value = getattr(model, field_name, None)
            if value is None:
                continue
            safe, digest = sanitize_schema_url(
                value,
                allow_extension=policy.allows(CaptureClass.EXTENSION_PAYLOAD),
            )
            if safe != value or digest is not None:
                raise ValueError("record contains an unnormalized schema URL")
        if isinstance(model, Event):
            if normalize_event_name(model.event_name) != (model.event_name, None):
                raise ValueError("event name is not normalized")
        elif isinstance(model, Operation):
            if normalize_operation_name(model.operation_name) != (model.operation_name, None):
                raise ValueError("operation name is not normalized")
            if sanitize_semantic_label(model.status) != model.status:
                raise ValueError("operation status is not normalized")
        elif isinstance(model, Participant):
            for field_name in ("role", "endpoint_kind"):
                value = getattr(model, field_name)
                if value is not None and sanitize_semantic_label(value) != value:
                    raise ValueError("participant label is not normalized")
        elif isinstance(model, AudioStream):
            for field_name in ("direction", "media_kind"):
                value = getattr(model, field_name)
                if sanitize_semantic_label(value) != value:
                    raise ValueError("audio stream label is not normalized")
        elif isinstance(model, Coverage):
            for field_name in ("signal", "availability", "reason"):
                value = getattr(model, field_name)
                if value is not None and sanitize_semantic_label(value) != value:
                    raise ValueError("coverage label is not normalized")
        elif isinstance(model, QualitySample):
            if sanitize_semantic_label(model.quality_kind) != model.quality_kind:
                raise ValueError("quality kind is not normalized")
            for measurement in model.measurements:
                if sanitize_measurement_label(measurement.name) != measurement.name:
                    raise ValueError("measurement name is not normalized")
                if sanitize_measurement_unit(measurement.unit) != measurement.unit:
                    raise ValueError("measurement unit is not normalized")
        elif isinstance(model, ErrorRecord):
            if sanitize_error_label(model.code) != model.code:
                raise ValueError("error code is not normalized")
            if sanitize_semantic_label(model.category) != model.category:
                raise ValueError("error category is not normalized")
        elif isinstance(model, Evidence):
            for field_name in ("source", "observer", "method", "confidence", "availability"):
                value = getattr(model, field_name)
                if sanitize_provenance_label(value) != value:
                    raise ValueError("evidence label is not normalized")
            if sanitize_version_label(model.method_version) != model.method_version:
                raise ValueError("evidence method version is not normalized")
            if sanitize_source_label(model.source_field) != model.source_field:
                raise ValueError("evidence source field is not normalized")
        elif isinstance(model, CausalLink):
            if sanitize_semantic_label(model.relationship) != model.relationship:
                raise ValueError("causal link relationship is not normalized")
        elif isinstance(model, ClockDomain):
            for field_name in ("kind", "observer", "scope", "synchronization_method"):
                value = getattr(model, field_name)
                if sanitize_semantic_label(value) != value:
                    raise ValueError("clock domain label is not normalized")
        elif isinstance(model, ClockRelation):
            if sanitize_semantic_label(model.method) != model.method:
                raise ValueError("clock relation method is not normalized")
        elif isinstance(model, MediaRef):
            if sanitize_semantic_label(model.media_kind) != model.media_kind:
                raise ValueError("media kind is not normalized")
            if (
                model.custodian is not None
                and sanitize_semantic_label(model.custodian) != model.custodian
            ):
                raise ValueError("media custodian is not normalized")


def _walk_models(value: Any) -> tuple[BaseModel, ...]:
    models: list[BaseModel] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseModel):
            models.append(current)
            pending.extend(
                getattr(current, field_name)
                for field_name in type(current).model_fields
                if field_name != "payload"
            )
            pending.extend((current.model_extra or {}).values())
        elif isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return tuple(models)


def _collect_classes(
    value: Any,
    policy: CapturePolicy,
    classes: set[CaptureClass],
    *,
    attribute_map: bool = False,
) -> None:
    if isinstance(value, BaseModel):
        extra = value.model_extra or {}
        if extra:
            classes.add(CaptureClass.EXTENSION_PAYLOAD)
            _validate_model_extra(extra, policy)
            _collect_classes(extra, policy, classes)
        for field_name in type(value).model_fields:
            field_value = getattr(value, field_name)
            if field_name == "capture_class" and field_value is not None:
                try:
                    classes.add(CaptureClass(field_value))
                except ValueError as error:
                    raise ValueError("record has an unsupported capture class") from error
            if field_name == "message" and field_value is not None:
                classes.add(CaptureClass.DIAGNOSTIC_PAYLOAD)
            _collect_classes(
                field_value,
                policy,
                classes,
                attribute_map=field_name in _ATTRIBUTE_FIELDS,
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("record contains a non-string object key")
            capture_class = classify_attribute(key)
            if capture_class is not CaptureClass.METADATA:
                classes.add(capture_class)
            elif attribute_map and (
                not is_safe_metadata_key(key) or is_unobservable_heard_key(key)
            ):
                classes.add(CaptureClass.EXTENSION_PAYLOAD)
            _collect_classes(item, policy, classes)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_classes(item, policy, classes)


def _validate_model_extra(extra: Mapping[str, Any], policy: CapturePolicy) -> None:
    if not policy.allows(CaptureClass.EXTENSION_PAYLOAD):
        raise ValueError("model extension requires extension_payload capture")
    for key, value in extra.items():
        if (
            not isinstance(key, str)
            or not is_safe_semantic_label(key)
            or is_unobservable_heard_key(key)
            or classify_attribute(key) is not CaptureClass.METADATA
        ):
            raise ValueError("model extension contains a sensitive or unsafe field")
        kept, omissions = sanitize_attributes({key: value}, policy)
        if omissions or key not in kept:
            raise ValueError("model extension contains an unsafe value")


__all__ = [
    "RECORD_MODELS",
    "decode_raw_otlp_value",
    "validate_and_classify_record",
    "validate_nonrecord_entry",
    "validate_operation_open",
]
