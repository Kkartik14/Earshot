# ruff: noqa: RUF005
"""Semantic invariant validation for Earshot incident bundles.

Pydantic owns field shape and lexical constraints.  This module owns invariants
that cross records and returns stable issue codes suitable for other language
implementations and conformance fixtures.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

if TYPE_CHECKING:
    from .explanation import IncidentExplanation

from .contract import (
    SCHEMA_VERSION,
    SEMANTIC_PROFILE_VERSION,
    CausalLink,
    ContractModel,
    DerivedAnalysis,
    Evidence,
    IncidentBundle,
    Operation,
    TimePoint,
    TimeRange,
)
from .measurement_semantics import measurement_value_limitation
from .privacy import (
    CaptureClass,
    classify_attribute,
    is_canonical_otel_schema_url,
    is_locator_attribute_key,
    is_safe_error_label,
    is_safe_event_name,
    is_safe_measurement_label,
    is_safe_measurement_unit,
    is_safe_metadata_key,
    is_safe_operation_name,
    is_safe_provenance_label,
    is_safe_semantic_label,
    is_safe_version_label,
    is_unobservable_heard_key,
    media_locator_safety,
    metadata_value_allowed,
    sanitize_source_label,
)


def _check_schema_url_policy(
    schema_url: str | None,
    policies: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if (
        schema_url is not None
        and not is_canonical_otel_schema_url(schema_url)
        and not _capture_allowed(policies.get(CaptureClass.EXTENSION_PAYLOAD.value))
    ):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_UNKNOWN_METADATA",
                path=path,
                message="third-party schema URLs require explicit extension_payload capture",
            )
        )


def _contract_field_names() -> set[str]:
    names = {"profile", "raw_otlp_chunks", "identity", "attribute"}
    pending = list(ContractModel.__subclasses__())
    seen: set[type[ContractModel]] = set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        names.update(model.model_fields)
        pending.extend(model.__subclasses__())
    return names


_SAFE_PATH_PARTS = _contract_field_names()


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    path: tuple[str | int, ...]
    message: str
    severity: str = "error"

    @model_validator(mode="before")
    @classmethod
    def remove_source_values(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        output = dict(value)
        code = str(output.get("code", "EARSHOT_INVALID"))
        output["message"] = f"{code} invariant failed"
        output["path"] = tuple(
            part if isinstance(part, int) or part in _SAFE_PATH_PARTS else "<key>"
            for part in output.get("path", ())
        )
        return output


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")


class IncidentValidationError(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        summary = "; ".join(
            f"{issue.code} at {_format_path(issue.path)}: {issue.message}"
            for issue in report.errors
        )
        super().__init__(f"invalid Earshot incident bundle: {summary}")


def _format_path(path: tuple[str | int, ...]) -> str:
    if not path:
        return "$"
    result = "$"
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def _id_index(
    records: Iterable[Any],
    field: str,
    base_path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, record in enumerate(records):
        value = getattr(record, field)
        if value in result:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_DUPLICATE_ID",
                    path=base_path + (index, field),
                    message=f"duplicate {field} {value!r}",
                )
            )
        else:
            result[value] = record
    return result


def _session_match(
    actual: str,
    expected: str,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if actual != expected:
        issues.append(
            ValidationIssue(
                code="EARSHOT_SESSION_MISMATCH",
                path=path,
                message=f"expected session_id {expected!r}, got {actual!r}",
            )
        )


def _check_ref(
    value: str | None,
    known: Mapping[str, Any],
    kind: str,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if value is not None and value not in known:
        issues.append(
            ValidationIssue(
                code="EARSHOT_DANGLING_REF",
                path=path,
                message=f"unknown {kind} {value!r}",
            )
        )


def _check_time_range(
    value: TimeRange,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    comparable: tuple[int, int] | None = None
    if (
        value.start.monotonic_time_nano is not None
        and value.end.monotonic_time_nano is not None
        and value.start.clock_domain_id is not None
        and value.start.clock_domain_id == value.end.clock_domain_id
    ):
        comparable = (
            int(value.start.monotonic_time_nano),
            int(value.end.monotonic_time_nano),
        )
    elif (
        value.start.source_time_unix_nano is not None
        and value.end.source_time_unix_nano is not None
        and value.start.clock_domain_id is not None
        and value.start.clock_domain_id == value.end.clock_domain_id
    ):
        # Use a shared source representation even when one endpoint also has a
        # monotonic value. Never compare values from different clock domains.
        comparable = (
            int(value.start.source_time_unix_nano),
            int(value.end.source_time_unix_nano),
        )
    if comparable is not None and comparable[1] < comparable[0]:
        issues.append(
            ValidationIssue(
                code="EARSHOT_TIME_RANGE_REVERSED",
                path=path,
                message="end precedes start within the same clock domain",
            )
        )


def _check_clock_ref(
    point: TimePoint,
    clock_domains: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if point.clock_domain_id is not None and point.clock_domain_id not in clock_domains:
        issues.append(
            ValidationIssue(
                code="EARSHOT_UNKNOWN_CLOCK_DOMAIN",
                path=path + ("clock_domain_id",),
                message=f"unknown clock domain {point.clock_domain_id!r}",
            )
        )


def _check_evidence_clock_refs(
    evidence: Evidence | None,
    clock_domains: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if evidence is None or evidence.sample_window is None:
        return
    _check_clock_ref(
        evidence.sample_window.start,
        clock_domains,
        path + ("sample_window", "start"),
        issues,
    )
    _check_clock_ref(
        evidence.sample_window.end,
        clock_domains,
        path + ("sample_window", "end"),
        issues,
    )
    _check_time_range(evidence.sample_window, path + ("sample_window",), issues)


def _check_evidence_source_label(
    evidence: Evidence | None,
    capture_class: str,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if evidence is None:
        return
    for field_name in ("source", "observer", "method", "confidence", "availability"):
        if not is_safe_provenance_label(str(getattr(evidence, field_name))):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=path + (field_name,),
                    message="provenance labels must use governed semantic identifiers",
                )
            )
    if evidence.method_version is not None and not is_safe_version_label(evidence.method_version):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                path=path + ("method_version",),
                message="provenance versions must use governed version identifiers",
            )
        )
    if (
        evidence.source_field is not None
        and sanitize_source_label(evidence.source_field) != evidence.source_field
        and capture_class
        not in {
            CaptureClass.DIAGNOSTIC_PAYLOAD.value,
        }
    ):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                path=path + ("source_field",),
                message="open source labels must be hashed or governed by payload policy",
            )
        )


def _check_finite_json(
    value: Any,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_NONFINITE_NUMBER",
                    path=path,
                    message="NaN and infinite values are not valid incident data",
                )
            )
    elif isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_IJSON_INTEGER_DOMAIN",
                    path=path,
                    message="JSON numbers must fit the interoperable IEEE-754 integer domain",
                )
            )
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_IJSON_UNICODE_DOMAIN",
                    path=path,
                    message="JSON strings must not contain unpaired Unicode surrogates",
                )
            )
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_NON_JSON_VALUE",
                        path=path + ("<key>",),
                        message="JSON object keys must be strings",
                    )
                )
            else:
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    issues.append(
                        ValidationIssue(
                            code="EARSHOT_IJSON_UNICODE_DOMAIN",
                            path=path + ("<key>",),
                            message="JSON object keys must not contain unpaired surrogates",
                        )
                    )
            _check_finite_json(child, path + (str(key),), issues)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_finite_json(child, path + (index,), issues)
    elif value is not None and not isinstance(value, bool):
        issues.append(
            ValidationIssue(
                code="EARSHOT_NON_JSON_VALUE",
                path=path,
                message="incident profile values must be representable in strict JSON",
            )
        )


_SENSITIVE_ATTRIBUTE_CLASSES = {
    "transcript": "transcript",
    "audio.transcript": "transcript",
    "openinference.audio.transcript": "transcript",
    "prompt": "model_payload",
    "completion": "model_payload",
    "gen_ai.input.messages": "model_payload",
    "gen_ai.output.messages": "model_payload",
    "llm.input_messages": "model_payload",
    "llm.output_messages": "model_payload",
    "tool.arguments": "tool_payload",
    "tool.args": "tool_payload",
    "tool.result": "tool_payload",
    "tool.output": "tool_payload",
    "audio.bytes": "audio",
    "audio.data": "audio",
    "audio.url": "audio",
    "media.uri": "audio",
    "error.message": "diagnostic_payload",
    "exception.message": "diagnostic_payload",
    "exception.stacktrace": "diagnostic_payload",
    "phone_number": "identity",
    "caller_id": "identity",
    "user.email": "identity",
}


def _check_attribute_privacy(
    attributes: Mapping[str, Any],
    capture_class: str,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    for key, value in attributes.items():
        normalized = key.lower()
        expected_class = _SENSITIVE_ATTRIBUTE_CLASSES.get(normalized)
        if expected_class is not None and capture_class != expected_class:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_PAYLOAD_SMUGGLED",
                    path=path + (key,),
                    message=(
                        f"attribute is governed by capture class {expected_class!r}, "
                        f"not {capture_class!r}"
                    ),
                )
            )
        if isinstance(value, Mapping):
            _check_attribute_privacy(value, capture_class, path + (key,), issues)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                if isinstance(child, Mapping):
                    _check_attribute_privacy(
                        child,
                        capture_class,
                        path + (key, index),
                        issues,
                    )


def _check_media_locator(
    uri: str,
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    safety = media_locator_safety(uri)
    if safety == "credential":
        issues.append(
            ValidationIssue(
                code="EARSHOT_MEDIA_LOCATOR_CREDENTIAL",
                path=path,
                message="portable media locators must not embed credentials",
            )
        )
    elif safety == "invalid":
        issues.append(
            ValidationIssue(
                code="EARSHOT_MEDIA_LOCATOR_INVALID",
                path=path,
                message="media locator must be a portable HTTPS reference",
            )
        )


def _capture_allowed(policy: Any | None) -> bool:
    if policy is None or not policy.captured:
        return False
    return policy.decision.lower() in {"allow", "allowed", "grant", "granted"}


_CAPTURE_CLASS_MODELS = {
    "AudioStream",
    "DerivedAnalysis",
    "ErrorRecord",
    "Event",
    "MediaRef",
    "Operation",
    "Participant",
    "QualitySample",
    "RawOtlpChunk",
}


def _check_governed_value(
    key: str,
    value: Any,
    capture_class: str,
    policies: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
    *,
    requires_extension: bool = False,
) -> None:
    if not is_safe_semantic_label(key):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                path=path,
                message="attribute and extension keys must use governed semantic identifiers",
            )
        )
    if is_unobservable_heard_key(key):
        issues.append(
            ValidationIssue(
                code="EARSHOT_UNOBSERVABLE_HEARD_CLAIM",
                path=path,
                message="human hearing is not a directly observable system fact",
            )
        )
    if isinstance(value, str) and is_locator_attribute_key(key):
        locator_safety = media_locator_safety(value)
        if locator_safety != "portable":
            issues.append(
                ValidationIssue(
                    code=(
                        "EARSHOT_MEDIA_LOCATOR_CREDENTIAL"
                        if locator_safety == "credential"
                        else "EARSHOT_MEDIA_LOCATOR_INVALID"
                    ),
                    path=path,
                    message="unsafe locators are not portable evidence",
                )
            )
    extension_allowed = _capture_allowed(policies.get(CaptureClass.EXTENSION_PAYLOAD.value))
    if requires_extension and not extension_allowed:
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_UNKNOWN_METADATA",
                path=path,
                message="unknown contract fields require explicit extension_payload capture",
            )
        )
    expected = classify_attribute(key)
    if expected is not CaptureClass.METADATA:
        if capture_class != expected.value:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_PAYLOAD_SMUGGLED",
                    path=path,
                    message="payload capture class does not match its policy",
                )
            )
        return
    if not is_safe_metadata_key(key):
        if extension_allowed:
            return
        if not requires_extension:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_UNKNOWN_METADATA",
                    path=path,
                    message="unknown metadata requires explicit extension_payload capture",
                )
            )
        return
    allowed = metadata_value_allowed(key, value)
    if not allowed:
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_UNKNOWN_METADATA",
                path=path,
                message=("unknown free-form metadata requires explicit extension_payload capture"),
            )
        )


def _check_recursive_privacy(
    value: Any,
    policies: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
    inherited_class: str = "metadata",
    requires_extension: bool = False,
) -> None:
    if isinstance(value, BaseModel):
        current_class = inherited_class
        if type(value).__name__ in _CAPTURE_CLASS_MODELS:
            current_class = str(getattr(value, "capture_class", inherited_class))

        for key, child in (value.model_extra or {}).items():
            if child is None:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_NULL_EXTENSION_UNSUPPORTED",
                        path=path + ("<key>",),
                        message="null extension values are not preserved; omit the field instead",
                    )
                )
            _check_governed_value(
                key,
                child,
                current_class,
                policies,
                path + ("<key>",),
                issues,
                requires_extension=True,
            )
            _check_recursive_privacy(
                child,
                policies,
                path + ("<key>",),
                issues,
                current_class,
                True,
            )

        for field_name in type(value).model_fields:
            child = getattr(value, field_name)
            child_path = path + (field_name,)
            if field_name in {"payload", "model_extra"}:
                continue
            if field_name in {"attributes", "resource", "projections"} and isinstance(
                child, Mapping
            ):
                for key, item in child.items():
                    _check_governed_value(
                        str(key),
                        item,
                        current_class,
                        policies,
                        child_path + ("<key>",),
                        issues,
                        requires_extension=requires_extension,
                    )
                    if classify_attribute(str(key)) is CaptureClass.METADATA:
                        _check_recursive_privacy(
                            item,
                            policies,
                            child_path + ("<key>",),
                            issues,
                            current_class,
                            requires_extension,
                        )
            else:
                _check_recursive_privacy(
                    child,
                    policies,
                    child_path,
                    issues,
                    current_class,
                    requires_extension,
                )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _check_governed_value(
                str(key),
                child,
                inherited_class,
                policies,
                path + ("<key>",),
                issues,
                requires_extension=requires_extension,
            )
            if classify_attribute(str(key)) is CaptureClass.METADATA:
                _check_recursive_privacy(
                    child,
                    policies,
                    path + ("<key>",),
                    issues,
                    inherited_class,
                    requires_extension,
                )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_recursive_privacy(
                child,
                policies,
                path + (index,),
                issues,
                inherited_class,
                requires_extension,
            )


def _check_capture_class(
    capture_class: str,
    policies: Mapping[str, Any],
    path: tuple[str | int, ...],
    issues: list[ValidationIssue],
) -> None:
    policy = policies.get(capture_class)
    if policy is None:
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_CLASS_UNDECLARED",
                path=path,
                message=f"capture class {capture_class!r} has no privacy policy entry",
            )
        )
    elif not _capture_allowed(policy):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_CAPTURE_DENIED",
                path=path,
                message=f"bundle contains data from denied capture class {capture_class!r}",
            )
        )


def _resolve_link_target(
    link: CausalLink,
    operations: Mapping[str, Any],
    otel_operations: Mapping[tuple[str, str], Any],
) -> str | None:
    if link.target_operation_id is not None and link.target_operation_id in operations:
        return link.target_operation_id
    if link.trace_id is not None and link.span_id is not None:
        target = otel_operations.get((link.trace_id, link.span_id))
        if target is not None:
            return target.operation_id
    return None


def _find_cycle(edges: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    """Find one directed cycle without consuming the Python call stack."""

    state: dict[str, int] = {}
    for start_node in edges:
        if state.get(start_node, 0) != 0:
            continue
        path = [start_node]
        positions = {start_node: 0}
        state[start_node] = 1
        stack: list[tuple[str, Any]] = [(start_node, iter(edges.get(start_node, set())))]
        while stack:
            node, targets = stack[-1]
            try:
                target = next(targets)
            except StopIteration:
                stack.pop()
                path.pop()
                positions.pop(node, None)
                state[node] = 2
                continue
            target_state = state.get(target, 0)
            if target_state == 0:
                state[target] = 1
                positions[target] = len(path)
                path.append(target)
                stack.append((target, iter(edges.get(target, set()))))
            elif target_state == 1:
                cycle_start = positions[target]
                return tuple(path[cycle_start:] + [target])
    return None


def validate_incident(bundle: IncidentBundle) -> ValidationReport:
    """Validate all cross-record invariants without mutating ``bundle``."""

    issues: list[ValidationIssue] = []
    try:
        bundle = IncidentBundle.model_validate(bundle.model_dump(mode="python", warnings=False))
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_STRUCTURAL_INVALID",
                    path=tuple(item.get("loc", ())),
                    message="incident violates the v1alpha1 structural contract",
                )
            )
        return ValidationReport(issues=tuple(issues))
    profile = bundle.profile
    manifest = profile.manifest
    session_id = profile.session.session_id

    if manifest.schema_version != SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                code="EARSHOT_SCHEMA_VERSION_UNSUPPORTED",
                path=("profile", "manifest", "schema_version"),
                message=f"expected {SCHEMA_VERSION!r}, got {manifest.schema_version!r}",
            )
        )
    if manifest.semantic_profile_version != SEMANTIC_PROFILE_VERSION:
        issues.append(
            ValidationIssue(
                code="EARSHOT_SEMANTIC_PROFILE_VERSION_UNSUPPORTED",
                path=("profile", "manifest", "semantic_profile_version"),
                message="unsupported Earshot semantic profile version",
            )
        )
    _session_match(
        manifest.session_id,
        session_id,
        ("profile", "manifest", "session_id"),
        issues,
    )
    if not is_safe_semantic_label(profile.session.status):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                path=("profile", "session", "status"),
                message="session status must be a semantic label rather than free-form text",
            )
        )

    participants = _id_index(
        profile.participants,
        "participant_id",
        ("profile", "participants"),
        issues,
    )
    streams = _id_index(
        profile.audio_streams,
        "stream_id",
        ("profile", "audio_streams"),
        issues,
    )
    clock_domains = _id_index(
        profile.clock_domains,
        "clock_domain_id",
        ("profile", "clock_domains"),
        issues,
    )
    operations = _id_index(
        profile.operations,
        "operation_id",
        ("profile", "operations"),
        issues,
    )
    events = _id_index(profile.events, "event_id", ("profile", "events"), issues)
    quality_samples = _id_index(
        profile.quality_samples,
        "sample_id",
        ("profile", "quality_samples"),
        issues,
    )
    media_refs = _id_index(profile.media_refs, "media_id", ("profile", "media_refs"), issues)
    omissions = _id_index(
        profile.privacy.omissions,
        "omission_id",
        ("profile", "privacy", "omissions"),
        issues,
    )
    chunks = _id_index(
        bundle.raw_otlp_chunks,
        "chunk_id",
        ("raw_otlp_chunks",),
        issues,
    )

    global_ids: dict[str, str] = {}
    for namespace, values in (
        ("participant", participants),
        ("stream", streams),
        ("operation", operations),
        ("event", events),
        ("quality_sample", quality_samples),
        ("media", media_refs),
        ("omission", omissions),
        ("otlp_chunk", chunks),
    ):
        for value in values:
            previous = global_ids.get(value)
            if previous is not None:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_AMBIGUOUS_GLOBAL_ID",
                        path=(namespace, value),
                        message=f"ID {value!r} is already used as a {previous}",
                    )
                )
            else:
                global_ids[value] = namespace

    coverage_signals: set[str] = set()
    for index, coverage in enumerate(profile.coverage):
        for field_name in ("signal", "availability", "reason"):
            field_value = getattr(coverage, field_name)
            if field_value is not None and not is_safe_semantic_label(field_value):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=("profile", "coverage", index, field_name),
                        message="coverage labels and reasons must not contain free-form payload",
                    )
                )
        if coverage.signal in coverage_signals:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_DUPLICATE_COVERAGE_SIGNAL",
                    path=("profile", "coverage", index, "signal"),
                    message=f"duplicate coverage signal {coverage.signal!r}",
                )
            )
        coverage_signals.add(coverage.signal)
        if coverage.availability.lower() != "available" and not coverage.reason:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_AVAILABILITY_REASON_REQUIRED",
                    path=("profile", "coverage", index, "reason"),
                    message="non-available coverage requires a reason",
                )
            )
        _check_evidence_clock_refs(
            coverage.evidence,
            clock_domains,
            ("profile", "coverage", index, "evidence"),
            issues,
        )
        _check_evidence_source_label(
            coverage.evidence,
            CaptureClass.METADATA.value,
            ("profile", "coverage", index, "evidence"),
            issues,
        )

    privacy_policies = _id_index(
        profile.privacy.capture_classes,
        "capture_class",
        ("profile", "privacy", "capture_classes"),
        issues,
    )
    if profile.privacy.default_capture_class not in privacy_policies:
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_DEFAULT_UNDECLARED",
                path=("profile", "privacy", "default_capture_class"),
                message="default capture class has no policy entry",
            )
        )
    if not _capture_allowed(privacy_policies.get("metadata")):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_METADATA_REQUIRED",
                path=("profile", "privacy", "capture_classes"),
                message="the incident envelope requires metadata capture",
            )
        )
    for index, policy in enumerate(profile.privacy.capture_classes):
        if policy.captured and not _capture_allowed(policy):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_POLICY_CONTRADICTION",
                    path=("profile", "privacy", "capture_classes", index),
                    message=(
                        "capture policy says data was captured while its decision denies capture"
                    ),
                )
            )

    for index, participant in enumerate(profile.participants):
        base = ("profile", "participants", index)
        for field_name in ("role", "endpoint_kind"):
            field_value = getattr(participant, field_name)
            if field_value is not None and not is_safe_semantic_label(field_value):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=base + (field_name,),
                        message="participant labels must use governed semantic identifiers",
                    )
                )
        _session_match(
            participant.session_id,
            session_id,
            base + ("session_id",),
            issues,
        )
        _check_capture_class(
            participant.capture_class,
            privacy_policies,
            base + ("capture_class",),
            issues,
        )
        _check_attribute_privacy(
            participant.attributes,
            participant.capture_class,
            base + ("attributes",),
            issues,
        )

    for index, stream in enumerate(profile.audio_streams):
        base = ("profile", "audio_streams", index)
        for field_name in ("direction", "media_kind"):
            if not is_safe_semantic_label(getattr(stream, field_name)):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=base + (field_name,),
                        message="stream labels must use governed semantic identifiers",
                    )
                )
        _session_match(stream.session_id, session_id, base + ("session_id",), issues)
        _check_ref(
            stream.participant_id,
            participants,
            "participant",
            base + ("participant_id",),
            issues,
        )
        participant = participants.get(stream.participant_id)
        if participant is not None and participant.session_id != stream.session_id:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_STREAM_PARTICIPANT_SESSION_MISMATCH",
                    path=base + ("participant_id",),
                    message="audio stream and participant belong to different sessions",
                )
            )
        _check_capture_class(
            stream.capture_class,
            privacy_policies,
            base + ("capture_class",),
            issues,
        )
        _check_attribute_privacy(
            stream.attributes,
            stream.capture_class,
            base + ("attributes",),
            issues,
        )

    _check_attribute_privacy(
        profile.session.attributes,
        "metadata",
        ("profile", "session", "attributes"),
        issues,
    )
    _check_attribute_privacy(
        profile.attributes,
        "metadata",
        ("profile", "attributes"),
        issues,
    )

    _check_clock_ref(
        profile.session.started_at,
        clock_domains,
        ("profile", "session", "started_at"),
        issues,
    )
    if profile.session.ended_at is not None:
        _check_clock_ref(
            profile.session.ended_at,
            clock_domains,
            ("profile", "session", "ended_at"),
            issues,
        )
        _check_time_range(
            TimeRange(start=profile.session.started_at, end=profile.session.ended_at),
            ("profile", "session"),
            issues,
        )

    otel_operations: dict[tuple[str, str], Any] = {}
    for index, operation in enumerate(profile.operations):
        base = ("profile", "operations", index)
        if not is_safe_operation_name(operation.operation_name) and operation.capture_class not in {
            CaptureClass.DIAGNOSTIC_PAYLOAD.value,
        }:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=base + ("operation_name",),
                    message="open operation names require governed payload policy",
                )
            )
        if not is_safe_semantic_label(operation.status):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=base + ("status",),
                    message="operation status must be a semantic label rather than free-form text",
                )
            )
        normalized_operation_name = operation.operation_name.lower().replace("-", "_")
        if "heard_at" in normalized_operation_name or normalized_operation_name.endswith(".heard"):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_UNOBSERVABLE_HEARD_CLAIM",
                    path=base + ("operation_name",),
                    message="human hearing is not a directly observable system fact",
                )
            )
        _session_match(operation.session_id, session_id, base + ("session_id",), issues)
        _check_ref(
            operation.participant_id,
            participants,
            "participant",
            base + ("participant_id",),
            issues,
        )
        _check_ref(
            operation.stream_id,
            streams,
            "stream",
            base + ("stream_id",),
            issues,
        )
        if operation.participant_id is not None and operation.stream_id is not None:
            stream = streams.get(operation.stream_id)
            if stream is not None and stream.participant_id != operation.participant_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_STREAM_PARTICIPANT_MISMATCH",
                        path=base + ("stream_id",),
                        message="operation participant does not own the referenced stream",
                    )
                )
        _check_clock_ref(operation.started_at, clock_domains, base + ("started_at",), issues)
        if operation.ended_at is not None:
            _check_clock_ref(operation.ended_at, clock_domains, base + ("ended_at",), issues)
            _check_time_range(
                TimeRange(start=operation.started_at, end=operation.ended_at),
                base,
                issues,
            )
        _check_evidence_clock_refs(operation.evidence, clock_domains, base + ("evidence",), issues)
        _check_evidence_source_label(
            operation.evidence,
            operation.capture_class,
            base + ("evidence",),
            issues,
        )
        _check_schema_url_policy(
            operation.schema_url,
            privacy_policies,
            base + ("schema_url",),
            issues,
        )
        _check_schema_url_policy(
            operation.resource_schema_url,
            privacy_policies,
            base + ("resource_schema_url",),
            issues,
        )
        _check_capture_class(
            operation.capture_class,
            privacy_policies,
            base + ("capture_class",),
            issues,
        )
        _check_attribute_privacy(
            operation.attributes,
            operation.capture_class,
            base + ("attributes",),
            issues,
        )
        _check_attribute_privacy(
            operation.resource,
            operation.capture_class,
            base + ("resource",),
            issues,
        )
        _check_attribute_privacy(
            operation.instrumentation_scope_attributes,
            operation.capture_class,
            base + ("instrumentation_scope_attributes",),
            issues,
        )
        if operation.error is not None:
            if not is_safe_error_label(operation.error.code):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=base + ("error", "code"),
                        message="error code must use a governed semantic identifier",
                    )
                )
            if not is_safe_semantic_label(operation.error.category):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=base + ("error", "category"),
                        message="error category must use a governed semantic identifier",
                    )
                )
            _check_capture_class(
                operation.error.capture_class,
                privacy_policies,
                base + ("error", "capture_class"),
                issues,
            )
            if (
                operation.error.message is not None
                and operation.error.capture_class != CaptureClass.DIAGNOSTIC_PAYLOAD.value
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_PAYLOAD_SMUGGLED",
                        path=base + ("error", "message"),
                        message="error messages require diagnostic payload capture",
                    )
                )
        if (
            operation.operation_name in {"render", "transport_send", "transport_receive"}
            and operation.evidence is None
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EVIDENCE_REQUIRED",
                    path=base + ("evidence",),
                    message=f"{operation.operation_name!r} claims require provenance",
                )
            )
        elif (
            operation.operation_name in {"render", "transport_send", "transport_receive"}
            and operation.evidence is not None
            and operation.evidence.availability.lower() != "available"
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_UNAVAILABLE_VALUE",
                    path=base + ("evidence", "availability"),
                    message="an asserted UX or transport operation requires available evidence",
                )
            )
        if operation.trace_id is not None and operation.span_id is not None:
            otel_key = (operation.trace_id, operation.span_id)
            if otel_key in otel_operations:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DUPLICATE_OTEL_SPAN",
                        path=base + ("span_id",),
                        message="duplicate trace_id/span_id identity in normalized operations",
                    )
                )
            else:
                otel_operations[otel_key] = operation

    event_evidence_prefixes = (
        "earshot.audio.render.",
        "earshot.transport.",
        "earshot.device.",
    )
    event_evidence_names = {
        "earshot.audio.first_byte_sent",
        "earshot.audio.first_packet_received",
    }
    for index, event in enumerate(profile.events):
        base = ("profile", "events", index)
        if not is_safe_event_name(event.event_name) and event.capture_class not in {
            CaptureClass.DIAGNOSTIC_PAYLOAD.value,
        }:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=base + ("event_name",),
                    message="open event names require governed payload policy",
                )
            )
        normalized_event_name = event.event_name.lower().replace("-", "_")
        if "heard_at" in normalized_event_name or normalized_event_name.endswith(".heard"):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_UNOBSERVABLE_HEARD_CLAIM",
                    path=base + ("event_name",),
                    message="human hearing is not a directly observable system fact",
                )
            )
        _session_match(event.session_id, session_id, base + ("session_id",), issues)
        _check_ref(event.operation_id, operations, "operation", base + ("operation_id",), issues)
        if (
            event.operation_id is not None
            and event.trace_id is not None
            and event.span_id is not None
        ):
            declared_operation = operations.get(event.operation_id)
            if declared_operation is not None and (
                declared_operation.trace_id,
                declared_operation.span_id,
            ) != (event.trace_id, event.span_id):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EVENT_IDENTITY_MISMATCH",
                        path=base,
                        message="event operation_id and trace/span identity name different owners",
                    )
                )
        _check_ref(
            event.participant_id, participants, "participant", base + ("participant_id",), issues
        )
        _check_ref(event.stream_id, streams, "stream", base + ("stream_id",), issues)
        if event.participant_id is not None and event.stream_id is not None:
            stream = streams.get(event.stream_id)
            if stream is not None and stream.participant_id != event.participant_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_STREAM_PARTICIPANT_MISMATCH",
                        path=base + ("stream_id",),
                        message="event participant does not own the referenced stream",
                    )
                )
        _check_clock_ref(event.time, clock_domains, base + ("time",), issues)
        _check_evidence_clock_refs(event.evidence, clock_domains, base + ("evidence",), issues)
        _check_evidence_source_label(
            event.evidence,
            event.capture_class,
            base + ("evidence",),
            issues,
        )
        _check_schema_url_policy(
            event.schema_url,
            privacy_policies,
            base + ("schema_url",),
            issues,
        )
        _check_schema_url_policy(
            event.resource_schema_url,
            privacy_policies,
            base + ("resource_schema_url",),
            issues,
        )
        _check_capture_class(
            event.capture_class, privacy_policies, base + ("capture_class",), issues
        )
        _check_attribute_privacy(
            event.attributes,
            event.capture_class,
            base + ("attributes",),
            issues,
        )
        _check_attribute_privacy(
            event.resource,
            event.capture_class,
            base + ("resource",),
            issues,
        )
        _check_attribute_privacy(
            event.instrumentation_scope_attributes,
            event.capture_class,
            base + ("instrumentation_scope_attributes",),
            issues,
        )
        if (
            event.event_name.startswith(event_evidence_prefixes)
            or event.event_name in event_evidence_names
        ) and event.evidence is None:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EVIDENCE_REQUIRED",
                    path=base + ("evidence",),
                    message=f"event {event.event_name!r} requires provenance",
                )
            )
        elif (
            (
                event.event_name.startswith(event_evidence_prefixes)
                or event.event_name in event_evidence_names
            )
            and event.evidence is not None
            and event.evidence.availability.lower() != "available"
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_UNAVAILABLE_VALUE",
                    path=base + ("evidence", "availability"),
                    message="an asserted UX or transport event requires available evidence",
                )
            )

    for index, sample in enumerate(profile.quality_samples):
        base = ("profile", "quality_samples", index)
        _session_match(sample.session_id, session_id, base + ("session_id",), issues)
        operation_reference = sample.attributes.get("earshot.operation.id")
        if operation_reference is not None:
            if isinstance(operation_reference, str):
                _check_ref(
                    operation_reference,
                    operations,
                    "operation",
                    base + ("attributes", "earshot.operation.id"),
                    issues,
                )
            else:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DANGLING_REF",
                        path=base + ("attributes", "earshot.operation.id"),
                        message="quality operation reference must be a string operation ID",
                    )
                )
        _check_ref(
            sample.participant_id, participants, "participant", base + ("participant_id",), issues
        )
        _check_ref(sample.stream_id, streams, "stream", base + ("stream_id",), issues)
        if sample.participant_id is not None and sample.stream_id is not None:
            stream = streams.get(sample.stream_id)
            if stream is not None and stream.participant_id != sample.participant_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_STREAM_PARTICIPANT_MISMATCH",
                        path=base + ("stream_id",),
                        message="quality participant does not own the referenced stream",
                    )
                )
        _check_clock_ref(
            sample.sample_window.start, clock_domains, base + ("sample_window", "start"), issues
        )
        _check_clock_ref(
            sample.sample_window.end, clock_domains, base + ("sample_window", "end"), issues
        )
        _check_time_range(sample.sample_window, base + ("sample_window",), issues)
        _check_evidence_clock_refs(sample.evidence, clock_domains, base + ("evidence",), issues)
        _check_evidence_source_label(
            sample.evidence,
            sample.capture_class,
            base + ("evidence",),
            issues,
        )
        _check_schema_url_policy(
            sample.schema_url,
            privacy_policies,
            base + ("schema_url",),
            issues,
        )
        _check_schema_url_policy(
            sample.resource_schema_url,
            privacy_policies,
            base + ("resource_schema_url",),
            issues,
        )
        _check_capture_class(
            sample.capture_class, privacy_policies, base + ("capture_class",), issues
        )
        _check_attribute_privacy(
            sample.attributes,
            sample.capture_class,
            base + ("attributes",),
            issues,
        )
        _check_attribute_privacy(
            sample.resource,
            sample.capture_class,
            base + ("resource",),
            issues,
        )
        _check_attribute_privacy(
            sample.instrumentation_scope_attributes,
            sample.capture_class,
            base + ("instrumentation_scope_attributes",),
            issues,
        )
        if not is_safe_semantic_label(sample.quality_kind):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=base + ("quality_kind",),
                    message="quality kind must use a governed semantic identifier",
                )
            )
        for measurement_index, measurement in enumerate(sample.measurements):
            measurement_path = base + ("measurements", measurement_index)
            if not is_safe_measurement_label(measurement.name):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=measurement_path + ("name",),
                        message="measurement name must use a governed semantic identifier",
                    )
                )
            if not is_safe_measurement_unit(measurement.unit):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=measurement_path + ("unit",),
                        message="measurement unit must use a governed semantic identifier",
                    )
                )
            limitation = measurement_value_limitation(
                measurement.name,
                measurement.value,
                measurement.unit,
            )
            if limitation is not None:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_MEASUREMENT_VALUE_OUT_OF_RANGE",
                        path=measurement_path + ("value",),
                        message=limitation,
                    )
                )
        if sample.evidence is None:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EVIDENCE_REQUIRED",
                    path=base + ("evidence",),
                    message="quality samples require provenance",
                )
            )
        elif sample.evidence.availability.lower() != "available" and sample.measurements:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_UNAVAILABLE_VALUE",
                    path=base + ("measurements",),
                    message="unavailable quality evidence must not carry fabricated values",
                )
            )
        if sample.evidence is not None:
            transport_kind = any(
                token in sample.quality_kind.lower() for token in ("transport", "network", "qos")
            )
            transport_measurements = {
                measurement.name.lower()
                for measurement in sample.measurements
                if any(
                    token
                    in "".join(
                        character for character in measurement.name.lower() if character.isalnum()
                    )
                    for token in (
                        "packetloss",
                        "packetslost",
                        "packetdrop",
                        "networkdrop",
                        "jitter",
                        "rtt",
                        "roundtrip",
                    )
                )
            }
            if (transport_kind or transport_measurements) and sample.evidence.source.lower() in {
                "audio_inference",
                "pcm",
                "audio",
            }:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_NETWORK_QOS_SOURCE_INVALID",
                        path=base + ("evidence", "source"),
                        message="packet loss, jitter, and RTT require transport evidence",
                    )
                )
            p563_measurements = {
                measurement.name.lower()
                for measurement in sample.measurements
                if "mos" in measurement.name.lower()
            }
            if (
                p563_measurements
                and "p.563" in sample.evidence.method.lower()
                and "perceptual" not in sample.quality_kind.lower()
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PERCEPTUAL_MOS_MISCLASSIFIED",
                        path=base + ("quality_kind",),
                        message="P.563 MOS-LQO must be classified as audio perceptual quality",
                    )
                )
        if not sample.measurements:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_QUALITY_EMPTY",
                    path=base + ("measurements",),
                    message="quality sample must contain at least one measurement",
                )
            )

    for index, media in enumerate(profile.media_refs):
        base = ("profile", "media_refs", index)
        _session_match(media.session_id, session_id, base + ("session_id",), issues)
        _check_ref(media.stream_id, streams, "stream", base + ("stream_id",), issues)
        if media.capture_class != CaptureClass.AUDIO.value:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_PAYLOAD_SMUGGLED",
                    path=base + ("capture_class",),
                    message="media references require audio capture policy",
                )
            )
        _check_capture_class(
            media.capture_class, privacy_policies, base + ("capture_class",), issues
        )
        if media.byte_range is not None and (
            media.byte_range.offset + media.byte_range.length > media.size_bytes
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_MEDIA_RANGE_OUT_OF_BOUNDS",
                    path=base + ("byte_range",),
                    message="byte range exceeds media size",
                )
            )
        if media.time_range is not None:
            _check_clock_ref(
                media.time_range.start, clock_domains, base + ("time_range", "start"), issues
            )
            _check_clock_ref(
                media.time_range.end, clock_domains, base + ("time_range", "end"), issues
            )
            _check_time_range(media.time_range, base + ("time_range",), issues)
        if media.locator is not None:
            _check_media_locator(media.locator.uri, base + ("locator", "uri"), issues)

    for index, chunk in enumerate(bundle.raw_otlp_chunks):
        base = ("raw_otlp_chunks", index)
        if chunk.privacy_class != CaptureClass.RAW_OTLP.value:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_PAYLOAD_SMUGGLED",
                    path=base + ("privacy_class",),
                    message="opaque OTLP bytes require raw_otlp capture policy",
                )
            )
        _check_capture_class(
            chunk.privacy_class, privacy_policies, base + ("privacy_class",), issues
        )
        actual_digest = hashlib.sha256(chunk.payload).hexdigest()
        if chunk.sha256 is None:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_OTLP_DIGEST_MISSING",
                    path=base + ("sha256",),
                    message="codec will add a digest, but producers should supply one",
                    severity="warning",
                )
            )
        elif chunk.sha256 != actual_digest:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_OTLP_DIGEST_MISMATCH",
                    path=base + ("sha256",),
                    message="OTLP payload SHA-256 does not match exact payload bytes",
                )
            )

    if profile.analysis is not None:
        from .codec import IncidentCodecError, analysis_input_sha256

        issues.append(
            ValidationIssue(
                code="EARSHOT_EMBEDDED_ANALYSIS_UNSUPPORTED",
                path=("profile", "analysis"),
                message=(
                    "v1alpha1 derived analysis is a digest-bound sidecar and must not be embedded"
                ),
            )
        )
        _check_capture_class(
            profile.analysis.capture_class,
            privacy_policies,
            ("profile", "analysis", "capture_class"),
            issues,
        )
        try:
            expected_analysis_input = analysis_input_sha256(bundle)
        except IncidentCodecError:
            expected_analysis_input = None
        if (
            expected_analysis_input is not None
            and profile.analysis.input_sha256 != expected_analysis_input
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_ANALYSIS_INPUT_MISMATCH",
                    path=("profile", "analysis", "input_sha256"),
                    message=(
                        "embedded analysis must bind to the canonical evidence artifact "
                        "with analysis omitted"
                    ),
                )
            )
        evidence_ids = set(operations) | set(events) | set(quality_samples) | set(media_refs)
        for diagnosis_index, diagnosis in enumerate(profile.analysis.diagnoses):
            if not diagnosis.evidence_refs:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DIAGNOSIS_EVIDENCE_REQUIRED",
                        path=("profile", "analysis", "diagnoses", diagnosis_index, "evidence_refs"),
                        message="a diagnosis must cite at least one evidence record",
                    )
                )
            for ref_index, ref in enumerate(diagnosis.evidence_refs):
                if ref not in evidence_ids:
                    issues.append(
                        ValidationIssue(
                            code="EARSHOT_DANGLING_REF",
                            path=(
                                "profile",
                                "analysis",
                                "diagnoses",
                                diagnosis_index,
                                "evidence_refs",
                                ref_index,
                            ),
                            message=f"unknown evidence record {ref!r}",
                        )
                    )

    graph: dict[str, set[str]] = {operation_id: set() for operation_id in operations}
    acyclic_relationships = {
        "produced_by",
        "consumes",
        "supersedes",
        "retries",
        "interrupts",
        "handoff",
    }
    for index, operation in enumerate(profile.operations):
        base = ("profile", "operations", index)
        if operation.parent_span_id is not None and operation.trace_id is not None:
            parent = otel_operations.get((operation.trace_id, operation.parent_span_id))
            if parent is not None:
                graph[operation.operation_id].add(parent.operation_id)
            elif operation.parent_scope == "internal":
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_INTERNAL_PARENT_MISSING",
                        path=base + ("parent_span_id",),
                        message="declared internal parent is absent from this bundle",
                    )
                )
        elif operation.parent_scope == "internal":
            issues.append(
                ValidationIssue(
                    code="EARSHOT_INTERNAL_PARENT_MISSING",
                    path=base + ("parent_scope",),
                    message="internal parent scope requires parent_span_id",
                )
            )

        for link_index, link in enumerate(operation.links):
            link_path = base + ("links", link_index)
            if (
                link.target_operation_id is not None
                and link.trace_id is not None
                and link.span_id is not None
            ):
                declared_target = operations.get(link.target_operation_id)
                if declared_target is not None and (
                    declared_target.trace_id,
                    declared_target.span_id,
                ) != (link.trace_id, link.span_id):
                    issues.append(
                        ValidationIssue(
                            code="EARSHOT_LINK_IDENTITY_MISMATCH",
                            path=link_path,
                            message=(
                                "causal link target_operation_id and trace/span identity "
                                "name different targets"
                            ),
                        )
                    )
            target = _resolve_link_target(link, operations, otel_operations)
            if (
                link.target_operation_id is not None
                and target is None
                and link.target_scope != "external"
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DANGLING_REF",
                        path=link_path + ("target_operation_id",),
                        message=f"unknown target operation {link.target_operation_id!r}",
                    )
                )
            if link.target_scope == "internal" and target is None:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_INTERNAL_LINK_MISSING",
                        path=link_path,
                        message="declared internal link target is absent from this bundle",
                    )
                )
            if link.target_scope == "external" and link.target_operation_id is not None:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXTERNAL_LINK_OWNS_TARGET",
                        path=link_path,
                        message="external links must not use a bundle-owned operation ID",
                    )
                )
            if target is not None and link.relationship in acyclic_relationships:
                graph[operation.operation_id].add(target)

    cycle = _find_cycle(graph)
    if cycle is not None:
        issues.append(
            ValidationIssue(
                code="EARSHOT_CAUSAL_CYCLE",
                path=("profile", "operations"),
                message="causal graph contains a cycle: " + " -> ".join(cycle),
            )
        )

    # Verify arbitrary extension attributes remain serializable and deterministic.
    _check_finite_json(
        profile.model_dump(mode="python", exclude_none=True),
        ("profile",),
        issues,
    )
    _check_recursive_privacy(bundle, privacy_policies, (), issues)

    return ValidationReport(issues=tuple(issues))


def assert_valid_incident(bundle: IncidentBundle) -> None:
    report = validate_incident(bundle)
    if not report.ok:
        raise IncidentValidationError(report)


def validate_derived_analysis(
    bundle: IncidentBundle,
    analysis: DerivedAnalysis,
) -> ValidationReport:
    """Validate a sidecar against the exact evidence graph it claims to analyze."""

    issues: list[ValidationIssue] = []
    try:
        analysis = DerivedAnalysis.model_validate(
            analysis.model_dump(mode="python", warnings=False)
        )
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_ANALYSIS_STRUCTURAL_INVALID",
                    path=("analysis", *tuple(item.get("loc", ()))),
                    message="analysis violates the closed DerivedAnalysis contract",
                )
            )
        return ValidationReport(issues=tuple(issues))
    if not is_safe_version_label(analysis.analyzer_version):
        issues.append(
            ValidationIssue(
                code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                path=("analysis", "analyzer_version"),
                message="analyzer version must use a governed version identifier",
            )
        )
    from .codec import IncidentCodecError, analysis_input_sha256

    try:
        expected_digest = analysis_input_sha256(bundle)
    except IncidentCodecError:
        expected_digest = None
    if expected_digest is not None and analysis.input_sha256 != expected_digest:
        issues.append(
            ValidationIssue(
                code="EARSHOT_ANALYSIS_INPUT_MISMATCH",
                path=("analysis", "input_sha256"),
                message="analysis input digest does not match the immutable evidence artifact",
            )
        )
    projection_session = analysis.projections.session_id
    if projection_session is not None and projection_session != bundle.profile.manifest.session_id:
        issues.append(
            ValidationIssue(
                code="EARSHOT_ANALYSIS_SESSION_MISMATCH",
                path=("analysis", "projections", "session_id"),
                message="analysis projection belongs to a different session",
            )
        )
    summary = analysis.projections.summary
    if summary is not None:
        expected_summary = {
            "turn_count": len(analysis.projections.turns),
            "operation_count": len(bundle.profile.operations),
            "event_count": len(bundle.profile.events),
            "quality_sample_count": len(bundle.profile.quality_samples),
            "failed_operation_count": sum(
                item.status in {"error", "timeout", "failed"} for item in bundle.profile.operations
            ),
        }
        for field_name, expected in expected_summary.items():
            if getattr(summary, field_name) != expected:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_SUMMARY_MISMATCH",
                        path=("analysis", "projections", "summary", field_name),
                        message="analysis summary does not match immutable source facts",
                    )
                )
    operations = {item.operation_id: item for item in bundle.profile.operations}
    events = {item.event_id: item for item in bundle.profile.events}
    quality_samples = {item.sample_id: item for item in bundle.profile.quality_samples}
    media_ids = {item.media_id for item in bundle.profile.media_refs}
    operation_ids = set(operations)
    event_ids = set(events)
    quality_ids = set(quality_samples)
    evidence_ids = operation_ids | event_ids | quality_ids | media_ids

    def check_refs(
        references: tuple[str, ...],
        allowed: set[str],
        path: tuple[str | int, ...],
    ) -> None:
        for reference_index, reference in enumerate(references):
            if reference not in allowed:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DANGLING_REF",
                        path=path + (reference_index,),
                        message="analysis projection cites evidence absent from the input artifact",
                    )
                )

    def check_metric(
        metric: Any,
        path: tuple[str | int, ...],
        allowed: set[str],
    ) -> None:
        check_refs(metric.evidence_ids, allowed, path + ("evidence_ids",))
        if metric.unit is not None and not is_safe_measurement_unit(metric.unit):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                    path=path + ("unit",),
                    message="analysis units must use governed measurement identifiers",
                )
            )

    # Resolve turn ownership through native OTel parentage just as the analyzer
    # does, so a child projection can legitimately inherit its parent's turn.
    by_otel = {
        (item.trace_id, item.span_id): item
        for item in operations.values()
        if item.trace_id is not None and item.span_id is not None
    }
    operation_turns: dict[str, str | None] = {}
    for operation in operations.values():
        chain: list[Operation] = []
        seen: set[str] = set()
        current: Operation | None = operation
        turn_id: str | None = None
        while current is not None and current.operation_id not in seen:
            seen.add(current.operation_id)
            chain.append(current)
            if current.operation_id in operation_turns:
                turn_id = operation_turns[current.operation_id]
                break
            if current.turn_id is not None:
                turn_id = current.turn_id
                break
            if current.trace_id is None or current.parent_span_id is None:
                break
            current = by_otel.get((current.trace_id, current.parent_span_id))
        for member in chain:
            operation_turns[member.operation_id] = turn_id

    event_turns: dict[str, str | None] = {}
    for event in events.values():
        owner = event.turn_id
        if owner is None and event.operation_id is not None:
            owner = operation_turns.get(event.operation_id)
        if owner is None and event.trace_id is not None and event.span_id is not None:
            operation_owner = by_otel.get((event.trace_id, event.span_id))
            if operation_owner is not None:
                owner = operation_turns.get(operation_owner.operation_id)
        event_turns[event.event_id] = owner

    quality_turns: dict[str, str | None] = {}
    for sample in quality_samples.values():
        owner_value = sample.attributes.get("earshot.turn.id")
        owner = (
            str(owner_value)
            if isinstance(owner_value, (str, int)) and not isinstance(owner_value, bool)
            else None
        )
        if owner is None:
            operation_value = sample.attributes.get("earshot.operation.id")
            if isinstance(operation_value, str):
                owner = operation_turns.get(operation_value)
        quality_turns[sample.sample_id] = owner

    source_turn_ids = {
        owner
        for owner in (*operation_turns.values(), *event_turns.values(), *quality_turns.values())
        if owner is not None
    }

    def evidence_turn(reference: str) -> str | None:
        if reference in operation_turns:
            return operation_turns[reference]
        if reference in event_turns:
            return event_turns[reference]
        if reference in quality_turns:
            return quality_turns[reference]
        return None

    def check_turn_evidence(
        references: tuple[str, ...],
        turn_id: str,
        path: tuple[str | int, ...],
    ) -> None:
        for reference_index, reference in enumerate(references):
            if reference in evidence_ids and evidence_turn(reference) != turn_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_TURN_MISMATCH",
                        path=path + (reference_index,),
                        message="analysis evidence does not belong to the projected turn",
                    )
                )

    projected_operations: set[str] = set()
    projected_events: set[str] = set()
    projected_turns: set[str] = set()
    latency_metric_names = (
        "first_token_latency",
        "generated_response_latency",
        "sent_response_latency",
        "received_response_latency",
        "render_start_response_latency",
        "response_latency",
    )
    for turn_index, turn in enumerate(analysis.projections.turns):
        turn_path = ("analysis", "projections", "turns", turn_index)
        if turn.turn_id in projected_turns:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_ANALYSIS_DUPLICATE_TURN",
                    path=turn_path + ("turn_id",),
                    message="turn projection IDs must be unique",
                )
            )
        projected_turns.add(turn.turn_id)
        if turn.turn_id not in source_turn_ids:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_ANALYSIS_TURN_UNBOUND",
                    path=turn_path + ("turn_id",),
                    message="turn projection is absent from immutable source evidence",
                )
            )
        check_refs(turn.operation_ids, operation_ids, turn_path + ("operation_ids",))
        check_refs(turn.event_ids, event_ids, turn_path + ("event_ids",))
        for operation_index, operation_id in enumerate(turn.operation_ids):
            if operation_id in projected_operations:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_DUPLICATE_PROJECTION_REF",
                        path=turn_path + ("operation_ids", operation_index),
                        message="one operation cannot belong to multiple turn projections",
                    )
                )
            projected_operations.add(operation_id)
            owner = operation_turns.get(operation_id)
            if operation_id in operations and owner != turn.turn_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_TURN_MISMATCH",
                        path=turn_path + ("operation_ids", operation_index),
                        message="projected operation belongs to a different source turn",
                    )
                )
        for event_index, event_id in enumerate(turn.event_ids):
            if event_id in projected_events:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_DUPLICATE_PROJECTION_REF",
                        path=turn_path + ("event_ids", event_index),
                        message="one event cannot belong to multiple turn projections",
                    )
                )
            projected_events.add(event_id)
            owner = event_turns.get(event_id)
            if event_id in events and owner != turn.turn_id:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_TURN_MISMATCH",
                        path=turn_path + ("event_ids", event_index),
                        message="projected event belongs to a different source turn",
                    )
                )
        for metric_name in latency_metric_names:
            metric = getattr(turn.metrics, metric_name)
            check_metric(
                metric,
                turn_path + ("metrics", metric_name),
                evidence_ids,
            )
            check_turn_evidence(
                metric.evidence_ids,
                turn.turn_id,
                turn_path + ("metrics", metric_name, "evidence_ids"),
            )
        check_refs(
            turn.metrics.tools.evidence_ids,
            operation_ids,
            turn_path + ("metrics", "tools", "evidence_ids"),
        )
        for tool_index, operation_id in enumerate(turn.metrics.tools.evidence_ids):
            operation = operations.get(operation_id)
            if operation is not None and (
                operation.operation_name != "tool"
                or operation_turns.get(operation_id) != turn.turn_id
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_TOOL_EVIDENCE_INVALID",
                        path=turn_path + ("metrics", "tools", "evidence_ids", tool_index),
                        message="tool analysis must cite tool operations from the same turn",
                    )
                )
        for metric_name, metric in turn.metrics.provider_measurements.items():
            if not is_safe_measurement_label(metric_name):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=turn_path + ("metrics", "provider_measurements", metric_name),
                        message="provider measurement names must be governed identifiers",
                    )
                )
            check_metric(
                metric,
                turn_path + ("metrics", "provider_measurements", metric_name),
                quality_ids,
            )
            check_turn_evidence(
                metric.evidence_ids,
                turn.turn_id,
                turn_path + ("metrics", "provider_measurements", metric_name, "evidence_ids"),
            )
        clock_domain_ids = {domain.clock_domain_id for domain in bundle.profile.clock_domains}
        for clock_key in turn.metrics.tools.elapsed_ms_by_clock_domain:
            matches_source = clock_key in clock_domain_ids or any(
                clock_key.endswith(suffix) and clock_key[: -len(suffix)] in clock_domain_ids
                for suffix in (":monotonic", ":source_wall")
            )
            if not matches_source:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_DANGLING_REF",
                        path=turn_path
                        + ("metrics", "tools", "elapsed_ms_by_clock_domain", clock_key),
                        message="tool elapsed-time key is not an input clock domain",
                    )
                )
        for interruption_index, interruption in enumerate(turn.interruptions):
            check_refs(
                interruption.evidence_ids,
                event_ids,
                turn_path + ("interruptions", interruption_index, "evidence_ids"),
            )
            for event_index, event_id in enumerate(interruption.evidence_ids):
                event = events.get(event_id)
                if event is not None and (
                    event.event_name != interruption.event_name
                    or event_turns.get(event_id) != turn.turn_id
                ):
                    issues.append(
                        ValidationIssue(
                            code="EARSHOT_ANALYSIS_INTERRUPTION_EVIDENCE_INVALID",
                            path=turn_path
                            + (
                                "interruptions",
                                interruption_index,
                                "evidence_ids",
                                event_index,
                            ),
                            message=(
                                "interruption projections must cite same-turn events "
                                "with the projected phase"
                            ),
                        )
                    )

    for sample_id, measurements in analysis.projections.unassigned_provider_measurements.items():
        sample_path = (
            "analysis",
            "projections",
            "unassigned_provider_measurements",
            sample_id,
        )
        if sample_id not in quality_ids:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_DANGLING_REF",
                    path=sample_path,
                    message="unassigned measurement key is not an input quality sample",
                )
            )
        elif quality_turns.get(sample_id) is not None:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_ANALYSIS_TURN_MISMATCH",
                    path=sample_path,
                    message="turn-owned quality cannot appear in the unassigned projection",
                )
            )
        for metric_name, metric in measurements.items():
            if not is_safe_measurement_label(metric_name):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_PRIVACY_TYPED_LABEL_UNGOVERNED",
                        path=sample_path + (metric_name,),
                        message="provider measurement names must be governed identifiers",
                    )
                )
            check_metric(
                metric,
                sample_path + (metric_name,),
                quality_ids,
            )
            if metric.evidence_ids != (sample_id,):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_QUALITY_EVIDENCE_MISMATCH",
                        path=sample_path + (metric_name, "evidence_ids"),
                        message=(
                            "an unassigned provider metric must cite exactly its keyed sample"
                        ),
                    )
                )

    for diagnosis_index, diagnosis in enumerate(analysis.diagnoses):
        check_refs(
            diagnosis.evidence_refs,
            evidence_ids,
            ("analysis", "diagnoses", diagnosis_index, "evidence_refs"),
        )
        if diagnosis.code == "operation.failed":
            cited_operations = [operations.get(item) for item in diagnosis.evidence_refs]
            if not cited_operations or any(
                item is None or item.status not in {"error", "timeout", "failed"}
                for item in cited_operations
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_ANALYSIS_DIAGNOSIS_EVIDENCE_INVALID",
                        path=("analysis", "diagnoses", diagnosis_index, "evidence_refs"),
                        message=(
                            "operation.failed must cite only failed, errored, or timed-out "
                            "source operations"
                        ),
                    )
                )
    return ValidationReport(issues=tuple(issues))


def validate_explanation(
    bundle: IncidentBundle,
    analysis: DerivedAnalysis,
    explanation: IncidentExplanation,
) -> ValidationReport:
    """Validate a UI explanation against its exact evidence graph and analysis.

    The explanation is a read model derived from an already-governed bundle and
    its analysis sidecar. This re-checks the closed shape, then asserts that every
    citation resolves to real evidence, that diagnoses mirror the analysis without
    invention, that no source operation is silently dropped, and that no interval
    or duration is manufactured across incomparable clocks.
    """

    from .explanation import IncidentExplanation as _IncidentExplanation
    from .explanation import _coordinate
    from .explanation import explain_incident as _project_explanation

    issues: list[ValidationIssue] = []
    try:
        explanation = _IncidentExplanation.model_validate(
            explanation.model_dump(mode="python", warnings=False)
        )
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_STRUCTURAL_INVALID",
                    path=("explanation", *tuple(item.get("loc", ()))),
                    message="explanation violates the closed IncidentExplanation contract",
                )
            )
        return ValidationReport(issues=tuple(issues))

    operations = {item.operation_id: item for item in bundle.profile.operations}
    events = {item.event_id: item for item in bundle.profile.events}
    quality_samples = {item.sample_id: item for item in bundle.profile.quality_samples}
    media_ids = {item.media_id for item in bundle.profile.media_refs}
    operation_ids = set(operations)
    evidence_ids = operation_ids | set(events) | set(quality_samples) | media_ids
    expected_explanation = _project_explanation(bundle, analysis)
    expected_operations = {
        operation.operation_id: operation
        for turn in expected_explanation.turns
        for operation in turn.operations
    }
    expected_operations.update(
        {
            operation.operation_id: operation
            for operation in expected_explanation.unassigned_operations
        }
    )
    expected_events = {
        event.event_id: event for turn in expected_explanation.turns for event in turn.events
    }

    def check_refs(
        references: tuple[str, ...],
        allowed: set[str],
        path: tuple[str | int, ...],
    ) -> None:
        for reference_index, reference in enumerate(references):
            if reference not in allowed:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXPLANATION_DANGLING_REF",
                        path=path + (reference_index,),
                        message="explanation cites evidence absent from the input artifact",
                    )
                )

    def check_operation(operation: Any, path: tuple[str | int, ...]) -> None:
        check_refs(operation.evidence_ids, evidence_ids, path + ("evidence_ids",))
        for measurement_index, measurement in enumerate(operation.measurements):
            check_refs(
                measurement.evidence_ids,
                evidence_ids,
                path + ("measurements", measurement_index, "evidence_ids"),
            )
        for link_index, link in enumerate(operation.links):
            if (
                link.target_operation_id is not None
                and link.target_scope != "external"
                and link.target_operation_id not in operation_ids
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXPLANATION_DANGLING_REF",
                        path=path + ("links", link_index, "target_operation_id"),
                        message="explained causal link targets an unknown operation",
                    )
                )
        source = operations.get(operation.operation_id)
        if source is None:
            return
        expected_operation = expected_operations.get(operation.operation_id)
        if expected_operation is not None and operation != expected_operation:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_MISMATCH",
                    path=path,
                    message="explained operation differs from its exact source projection",
                )
            )
        if (
            expected_operation is not None
            and operation.measurements != expected_operation.measurements
        ):
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_MISMATCH",
                    path=path + ("measurements",),
                    message="explained operation measurements differ from owned source evidence",
                )
            )
        if operation.status != source.status:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_MISMATCH",
                    path=path + ("status",),
                    message="explained operation status differs from source evidence",
                )
            )
        expected_error = (
            None
            if source.error is None
            else {
                "code": source.error.code,
                "category": source.error.category,
                "capture_class": source.error.capture_class,
                "message": None,
            }
        )
        actual_error = (
            operation.error.model_dump(mode="python") if operation.error is not None else None
        )
        if actual_error != expected_error:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_MISMATCH",
                    path=path + ("error",),
                    message="explained operation error differs from governed source evidence",
                )
            )
        source_links = tuple(
            (
                link.relationship,
                link.target_scope,
                link.target_operation_id,
                link.trace_id,
                link.span_id,
            )
            for link in source.links
        )
        explained_links = tuple(
            (
                link.relationship,
                link.target_scope,
                link.target_operation_id,
                link.trace_id,
                link.span_id,
            )
            for link in operation.links
        )
        if explained_links != source_links:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_MISMATCH",
                    path=path + ("links",),
                    message="explained causal links differ from source evidence",
                )
            )
        # Never manufacture an interval: an end coordinate is only honest when the
        # source recorded one in the same clock representation and it is not before
        # the start. Recompute from the immutable source rather than trusting the
        # flattened projection.
        start_basis, start_domain, start_value = _coordinate(source.started_at)
        if operation.shape == "interval":
            if (
                source.ended_at is None
                or operation.end_nano is None
                or operation.duration_nano is None
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXPLANATION_MANUFACTURED_INTERVAL",
                        path=path,
                        message="explained interval has no comparable source end boundary",
                    )
                )
                return
            end_basis, end_domain, end_value = _coordinate(source.ended_at)
            if (
                (end_basis, end_domain) != (start_basis, start_domain)
                or int(end_value) < int(start_value)
                or operation.end_nano != end_value
                or operation.duration_nano != str(int(end_value) - int(start_value))
            ):
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXPLANATION_MANUFACTURED_INTERVAL",
                        path=path,
                        message="explained interval is not the exact same-clock source delta",
                    )
                )

    projected_operation_ids: list[str] = []
    for turn_index, turn in enumerate(explanation.turns):
        for operation_index, operation in enumerate(turn.operations):
            projected_operation_ids.append(operation.operation_id)
            check_operation(
                operation,
                ("explanation", "turns", turn_index, "operations", operation_index),
            )
        for event_index, event in enumerate(turn.events):
            path = ("explanation", "turns", turn_index, "events", event_index)
            check_refs(event.evidence_ids, evidence_ids, path + ("evidence_ids",))
            if expected_events.get(event.event_id) != event:
                issues.append(
                    ValidationIssue(
                        code="EARSHOT_EXPLANATION_EVENT_MISMATCH",
                        path=path,
                        message="explained event differs from its exact source projection",
                    )
                )
    for operation_index, operation in enumerate(explanation.unassigned_operations):
        projected_operation_ids.append(operation.operation_id)
        check_operation(
            operation,
            ("explanation", "unassigned_operations", operation_index),
        )

    expected_turn_operation_layout = tuple(
        (turn.turn_id, tuple(operation.operation_id for operation in turn.operations))
        for turn in expected_explanation.turns
    )
    explained_turn_operation_layout = tuple(
        (turn.turn_id, tuple(operation.operation_id for operation in turn.operations))
        for turn in explanation.turns
    )
    expected_unassigned_operation_ids = tuple(
        operation.operation_id for operation in expected_explanation.unassigned_operations
    )
    explained_unassigned_operation_ids = tuple(
        operation.operation_id for operation in explanation.unassigned_operations
    )
    if (
        explained_turn_operation_layout != expected_turn_operation_layout
        or explained_unassigned_operation_ids != expected_unassigned_operation_ids
    ):
        issues.append(
            ValidationIssue(
                code="EARSHOT_EXPLANATION_OPERATION_PLACEMENT_MISMATCH",
                path=("explanation", "operations"),
                message="explained operations differ from exact source turn placement",
            )
        )

    # Completeness: the union of turn-owned and unassigned operations must be
    # exactly the source operation set. Nothing is silently dropped or invented.
    seen_operation_ids: set[str] = set()
    for operation_index, operation_id in enumerate(projected_operation_ids):
        if operation_id in seen_operation_ids:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_OPERATION_PLACEMENT_MISMATCH",
                    path=("explanation", "operations", operation_index),
                    message="source operation appears more than once in the explanation",
                )
            )
        seen_operation_ids.add(operation_id)
    projected_set = set(projected_operation_ids)
    for _missing in sorted(operation_ids - projected_set):
        issues.append(
            ValidationIssue(
                code="EARSHOT_EXPLANATION_OPERATION_DROPPED",
                path=("explanation", "operations"),
                message="source operation is absent from the explanation",
            )
        )
    for extra_index, operation_id in enumerate(projected_operation_ids):
        if operation_id not in operation_ids:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_DANGLING_REF",
                    path=("explanation", "operations", extra_index),
                    message="explained operation is absent from the source bundle",
                )
            )

    for measurement_index, measurement in enumerate(explanation.unassigned_measurements):
        check_refs(
            measurement.evidence_ids,
            evidence_ids,
            ("explanation", "unassigned_measurements", measurement_index, "evidence_ids"),
        )

    # Diagnoses must mirror the analysis exactly: same identities and fields, with
    # no invented and no dropped findings.
    def diagnosis_shape(
        diagnosis_id: str,
        code: str,
        summary: str,
        confidence: str,
        evidence: tuple[str, ...],
        limitations: tuple[str, ...],
    ) -> tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]]:
        return (diagnosis_id, code, summary, confidence, evidence, limitations)

    analysis_diagnoses = {
        diagnosis.diagnosis_id: diagnosis_shape(
            diagnosis.diagnosis_id,
            diagnosis.code,
            diagnosis.summary,
            diagnosis.confidence,
            tuple(diagnosis.evidence_refs),
            tuple(diagnosis.limitations),
        )
        for diagnosis in analysis.diagnoses
    }
    explained_diagnoses: dict[str, tuple] = {}
    for diagnosis_index, diagnosis in enumerate(explanation.diagnoses):
        path = ("explanation", "diagnoses", diagnosis_index)
        check_refs(diagnosis.evidence_ids, evidence_ids, path + ("evidence_ids",))
        shape = diagnosis_shape(
            diagnosis.diagnosis_id,
            diagnosis.code,
            diagnosis.summary,
            diagnosis.confidence,
            tuple(diagnosis.evidence_ids),
            tuple(diagnosis.limitations),
        )
        explained_diagnoses[diagnosis.diagnosis_id] = shape
        if analysis_diagnoses.get(diagnosis.diagnosis_id) != shape:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_DIAGNOSIS_MISMATCH",
                    path=path,
                    message="explanation diagnosis does not mirror the analysis diagnosis",
                )
            )
    for diagnosis_id in analysis_diagnoses:
        if diagnosis_id not in explained_diagnoses:
            issues.append(
                ValidationIssue(
                    code="EARSHOT_EXPLANATION_DIAGNOSIS_MISMATCH",
                    path=("explanation", "diagnoses"),
                    message="analysis diagnosis is missing from the explanation",
                )
            )

    return ValidationReport(issues=tuple(issues))
