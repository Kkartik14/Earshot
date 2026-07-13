"""FastAPI application for the local, immutable Earshot incident store."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from .analysis import ANALYZER_VERSION
from .codec import (
    JSON_MEDIA_TYPE,
    PROTOBUF_MEDIA_TYPE,
    IncidentCodecError,
    IncidentDepthError,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from .contract import DerivedAnalysis, IncidentBundle, IncidentBundleJson
from .privacy import ExportPolicyError, assert_export_allowed
from .storage import (
    ArtifactCorruptionError,
    IncidentConflictError,
    IncidentNotFoundError,
    IncidentPurgedError,
    IncidentStore,
    InvalidCursorError,
    StorageError,
)
from .validation import (
    IncidentValidationError,
    ValidationIssue,
    validate_derived_analysis,
    validate_incident,
)

Analyzer = Callable[..., DerivedAnalysis]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiIssue(ApiModel):
    code: str
    path: list[str | int]
    message: str
    severity: str


class ProblemDetail(ApiModel):
    code: str
    message: str
    issues: list[ApiIssue] | None = None


class ProblemResponse(ApiModel):
    error: ProblemDetail


class HealthResponse(ApiModel):
    status: str


class ValidateResponse(ApiModel):
    valid: bool
    bundle_id: str
    session_id: str
    canonical_sha256: str
    warnings: list[ApiIssue]


class IncidentRecordResponse(ApiModel):
    bundle_id: str
    session_id: str
    schema_version: str
    digest: str
    size_bytes: int
    status: str
    finality: str
    completeness: str
    framework: str | None
    created_at_unix_nano: str
    ingested_at_unix_nano: str


class IngestResponse(IncidentRecordResponse):
    created: bool
    warnings: list[ApiIssue]


class IncidentPageResponse(ApiModel):
    items: list[IncidentRecordResponse]
    next_cursor: str | None


class StoredAnalysisResponse(ApiModel):
    bundle_id: str
    analyzer_version: str
    input_digest: str
    generated_at_unix_nano: str
    analysis: DerivedAnalysis


_ERROR_RESPONSES = {
    status: {"model": ProblemResponse}
    for status in (400, 401, 403, 404, 409, 410, 413, 415, 422, 500, 503)
}

_INCIDENT_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            JSON_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            "application/json": {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            PROTOBUF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
            "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    token: str | None = None
    max_body_bytes: int = 16 * 1024 * 1024
    max_json_depth: int = 64
    default_page_size: int = 50
    analyzer_version: str = ANALYZER_VERSION
    behind_tls_proxy: bool = False

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if self.max_json_depth < 1:
            raise ValueError("max_json_depth must be positive")
        if not _is_loopback(self.host):
            raise ValueError(
                "the M1 API must bind to loopback; terminate remote TLS in a local proxy"
            )
        if self.behind_tls_proxy and not self.token:
            raise ValueError("a bearer token is required behind a TLS proxy")


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        issues: list[dict[str, object]] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues
        super().__init__(message)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_header_is_loopback(host_header: str) -> bool:
    candidate = host_header.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        hostname = candidate[1:closing] if closing > 0 else ""
    else:
        hostname = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    hostname = hostname.rstrip(".").lower()
    return hostname == "testserver" or _is_loopback(hostname)


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _response_media_type(request: Request) -> str:
    accept = request.headers.get("accept", JSON_MEDIA_TYPE).lower()
    if PROTOBUF_MEDIA_TYPE in accept or "application/x-protobuf" in accept:
        return PROTOBUF_MEDIA_TYPE
    return JSON_MEDIA_TYPE


def _issue_dict(issue: ValidationIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "path": list(issue.path),
        # The internal message may mention source identifiers. API errors remain
        # useful through stable code/path without reflecting incident values.
        "message": "incident violates an Earshot contract invariant",
        "severity": issue.severity,
    }


async def _read_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise ApiProblem(413, "EARSHOT_BODY_TOO_LARGE", "incident body exceeds limit")
        except ValueError as error:
            raise ApiProblem(
                400,
                "EARSHOT_INVALID_CONTENT_LENGTH",
                "invalid Content-Length header",
            ) from error

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise ApiProblem(413, "EARSHOT_BODY_TOO_LARGE", "incident body exceeds limit")
    if not body:
        raise ApiProblem(400, "EARSHOT_EMPTY_BODY", "incident body is empty")
    return bytes(body)


def _strict_json_preflight(payload: bytes, maximum_depth: int) -> None:
    class DuplicateKey(ValueError):
        pass

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in values:
            if key in output:
                raise DuplicateKey
            output[key] = value
        return output

    def reject_constant(_: str) -> None:
        raise ValueError

    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except DuplicateKey as error:
        raise ApiProblem(
            400,
            "EARSHOT_DUPLICATE_JSON_KEY",
            "incident JSON contains a duplicate object key",
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ApiProblem(400, "EARSHOT_MALFORMED_JSON", "incident JSON is malformed") from error

    stack: list[tuple[object, int]] = [(parsed, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > maximum_depth:
            raise ApiProblem(
                400,
                "EARSHOT_JSON_TOO_DEEP",
                "incident JSON nesting exceeds limit",
            )
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)


def _decode_request(payload: bytes, content_type: str, config: ApiConfig) -> IncidentBundle:
    try:
        if content_type in {JSON_MEDIA_TYPE, "application/json"}:
            _strict_json_preflight(payload, config.max_json_depth)
            return decode_incident_json(
                payload,
                max_profile_depth=config.max_json_depth,
            )
        if content_type in {PROTOBUF_MEDIA_TYPE, "application/x-protobuf"}:
            return decode_incident_protobuf(
                payload,
                max_profile_depth=config.max_json_depth,
            )
    except IncidentDepthError as error:
        raise ApiProblem(
            400,
            "EARSHOT_JSON_TOO_DEEP",
            "incident JSON nesting exceeds limit",
        ) from error
    except IncidentCodecError as error:
        if isinstance(error.__cause__, IncidentValidationError):
            raise ApiProblem(
                422,
                "EARSHOT_INVALID_INCIDENT",
                "incident does not satisfy the Earshot contract",
                issues=[_issue_dict(issue) for issue in error.__cause__.report.errors],
            ) from error
        # Codec errors can contain provider-originated identifiers. Keep the API
        # response stable and non-reflective instead of echoing exception text.
        raise ApiProblem(
            422,
            "EARSHOT_INVALID_INCIDENT",
            "incident does not satisfy the Earshot contract",
        ) from error
    raise ApiProblem(415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE", "unsupported incident media type")


def _analysis_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, warnings=False)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


def create_app(
    *,
    store: IncidentStore | None = None,
    data_dir: str | Path = ".earshot",
    analyzer: Analyzer | None = None,
    config: ApiConfig | None = None,
) -> FastAPI:
    settings = config or ApiConfig()
    repository = store or IncidentStore(data_dir)
    app = FastAPI(
        title="Earshot local ingest",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.store = repository
    app.state.config = settings
    app.state.analyzer = analyzer

    def openapi_schema() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            description=(
                "Local immutable ingest, retrieval, purge, and digest-bound analysis "
                "for Earshot v1 incidents."
            ),
        )
        incident_schema = IncidentBundleJson.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        definitions = incident_schema.pop("$defs", {})
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definitions)
        components["IncidentBundleJson"] = incident_schema
        schema["components"].setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
        }
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/v1/"):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and "responses" in operation:
                    # Loopback deployments may omit a token; remote deployments
                    # require BearerAuth behind a trusted TLS proxy.
                    operation["security"] = (
                        [{"BearerAuth": []}] if settings.token else [{"BearerAuth": []}, {}]
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema  # type: ignore[method-assign]

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(_: Request, error: ApiProblem) -> JSONResponse:
        error_body: dict[str, object] = {"code": error.code, "message": error.message}
        value: dict[str, object] = {"error": error_body}
        if error.issues is not None:
            error_body["issues"] = error.issues
        return JSONResponse(value, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, error: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "code": "EARSHOT_INVALID_REQUEST_FIELD",
                "path": [str(part) for part in item.get("loc", ())],
                "message": "request field is invalid",
                "severity": "error",
            }
            for item in error.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_INVALID_REQUEST",
                    "message": "request parameters are invalid",
                    "issues": issues,
                }
            },
            status_code=422,
        )

    @app.exception_handler(IncidentNotFoundError)
    async def handle_not_found(_: Request, __: IncidentNotFoundError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "EARSHOT_INCIDENT_NOT_FOUND", "message": "incident not found"}},
            status_code=404,
        )

    @app.exception_handler(IncidentPurgedError)
    async def handle_purged(_: Request, __: IncidentPurgedError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "EARSHOT_INCIDENT_PURGED", "message": "incident was purged"}},
            status_code=410,
        )

    @app.exception_handler(IncidentConflictError)
    async def handle_conflict(_: Request, __: IncidentConflictError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_INCIDENT_CONFLICT",
                    "message": "bundle identifier already exists with different content",
                }
            },
            status_code=409,
        )

    @app.exception_handler(ArtifactCorruptionError)
    async def handle_corruption(_: Request, __: ArtifactCorruptionError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_ARTIFACT_CORRUPT",
                    "message": "stored incident failed an integrity check",
                }
            },
            status_code=500,
        )

    @app.exception_handler(StorageError)
    async def handle_storage(_: Request, __: StorageError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_STORAGE_UNAVAILABLE",
                    "message": "incident storage is temporarily unavailable",
                }
            },
            status_code=503,
        )

    @app.exception_handler(ExportPolicyError)
    async def handle_export_policy(_: Request, __: ExportPolicyError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_EXPORT_DENIED",
                    "message": "incident policy denies this export",
                }
            },
            status_code=403,
        )

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def handle_storage_system_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_STORAGE_UNAVAILABLE",
                    "message": "incident storage is temporarily unavailable",
                }
            },
            status_code=503,
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[..., Any]) -> Response:
        runtime_server = request.scope.get("server")
        runtime_host = str(runtime_server[0]) if runtime_server else ""
        test_transport = runtime_host == "testserver"
        unsafe_runtime_binding = (
            runtime_host and not test_transport and not _is_loopback(runtime_host)
        )
        if unsafe_runtime_binding and request.url.path.startswith("/v1/"):
            return JSONResponse(
                {
                    "error": {
                        "code": "EARSHOT_REMOTE_BINDING_UNSAFE",
                        "message": (
                            "runtime listener is non-loopback; the M1 API permits only "
                            "a loopback listener behind any remote TLS proxy"
                        ),
                    }
                },
                status_code=503,
            )
        if (
            not settings.token
            and _is_loopback(settings.host)
            and request.url.path.startswith("/v1/")
            and not _host_header_is_loopback(request.headers.get("host", ""))
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "EARSHOT_UNTRUSTED_HOST",
                        "message": "loopback API requires a loopback Host header",
                    }
                },
                status_code=400,
            )
        if settings.token and request.url.path.startswith("/v1/"):
            authorization = request.headers.get("authorization", "")
            scheme, separator, credential = authorization.partition(" ")
            supplied = credential if separator and scheme.lower() == "bearer" else ""
            if not supplied or not hmac.compare_digest(supplied, settings.token):
                return JSONResponse(
                    {
                        "error": {
                            "code": "EARSHOT_UNAUTHORIZED",
                            "message": "valid bearer token required",
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.get("/healthz", response_model=HealthResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    def ready() -> JSONResponse:
        is_ready = repository.ready()
        return JSONResponse(
            {"status": "ready" if is_ready else "unavailable"},
            status_code=200 if is_ready else 503,
        )

    async def decode_and_validate(
        request: Request,
    ) -> tuple[IncidentBundle, list[dict[str, object]], bytes]:
        if request.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_CONTENT_ENCODING",
                "compressed incident requests are not supported",
            )
        payload = await _read_body(request, settings.max_body_bytes)
        content_type = _content_type(request)

        def decode_validate_and_canonicalize() -> tuple[
            IncidentBundle, list[dict[str, object]], bytes
        ]:
            bundle = _decode_request(payload, content_type, settings)
            report = validate_incident(bundle)
            if not report.ok:
                raise ApiProblem(
                    422,
                    "EARSHOT_INVALID_INCIDENT",
                    "incident does not satisfy the Earshot contract",
                    issues=[_issue_dict(issue) for issue in report.errors],
                )
            canonical = encode_incident_protobuf(bundle)
            return bundle, [_issue_dict(issue) for issue in report.warnings], canonical

        return await run_in_threadpool(decode_validate_and_canonicalize)

    @app.post(
        "/v1/incidents/validate",
        response_model=ValidateResponse,
        responses=_ERROR_RESPONSES,
        openapi_extra=_INCIDENT_REQUEST_BODY,
    )
    async def validate_endpoint(request: Request) -> JSONResponse:
        bundle, warnings, canonical = await decode_and_validate(request)
        return JSONResponse(
            {
                "valid": True,
                "bundle_id": bundle.profile.manifest.bundle_id,
                "session_id": bundle.profile.manifest.session_id,
                "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
                "warnings": warnings,
            }
        )

    @app.post(
        "/v1/incidents",
        response_model=IngestResponse,
        status_code=201,
        responses={200: {"model": IngestResponse}, **_ERROR_RESPONSES},
        openapi_extra=_INCIDENT_REQUEST_BODY,
    )
    async def ingest_endpoint(request: Request) -> JSONResponse:
        bundle, warnings, canonical = await decode_and_validate(request)
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key and idempotency_key != bundle.profile.manifest.bundle_id:
            raise ApiProblem(
                400,
                "EARSHOT_IDEMPOTENCY_KEY_MISMATCH",
                "Idempotency-Key must match the incident bundle identifier",
            )
        result = await run_in_threadpool(repository.ingest, bundle, canonical)
        value = result.record.as_dict()
        value["created"] = result.created
        value["warnings"] = warnings
        return JSONResponse(
            value,
            status_code=201 if result.created else 200,
            headers={
                "Location": f"/v1/incidents/{quote(result.record.bundle_id, safe='')}",
                "ETag": f'"sha256:{result.record.digest}"',
            },
        )

    @app.get(
        "/v1/incidents",
        response_model=IncidentPageResponse,
        responses=_ERROR_RESPONSES,
    )
    def list_endpoint(
        session_id: str | None = None,
        limit: int = Query(default=settings.default_page_size, ge=1, le=100),
        cursor: str | None = None,
    ) -> JSONResponse:
        try:
            page = repository.list_incidents(
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                destination="local_api",
            )
        except InvalidCursorError as error:
            raise ApiProblem(
                400,
                "EARSHOT_INVALID_CURSOR",
                "invalid incident pagination cursor",
            ) from error
        visible = []
        for item in page.items:
            _, payload = repository.get_artifact(item.bundle_id)
            try:
                bundle = decode_incident_protobuf(payload)
            except IncidentCodecError as error:
                raise ArtifactCorruptionError("stored incident cannot be decoded") from error
            try:
                assert_export_allowed(bundle, "local_api")
            except ExportPolicyError:
                continue
            visible.append(item.as_dict())
        return JSONResponse(
            {
                "items": visible,
                "next_cursor": page.next_cursor,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}",
        responses={
            200: {
                "content": {
                    JSON_MEDIA_TYPE: {
                        "schema": {"$ref": "#/components/schemas/IncidentBundleJson"}
                    },
                    PROTOBUF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
                }
            },
            **_ERROR_RESPONSES,
        },
    )
    def get_endpoint(bundle_id: str, request: Request) -> Response:
        record, payload = repository.get_artifact(bundle_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        assert_export_allowed(bundle, "local_api")
        common_headers = {
            "X-Earshot-Digest": record.digest,
            "Vary": "Accept",
            "Cache-Control": "no-store",
        }
        if _response_media_type(request) == PROTOBUF_MEDIA_TYPE:
            headers = dict(common_headers)
            headers["ETag"] = f'"sha256:{hashlib.sha256(payload).hexdigest()}"'
            return Response(payload, media_type=PROTOBUF_MEDIA_TYPE, headers=headers)
        rendered = encode_incident_json(bundle, indent=2)
        headers = dict(common_headers)
        headers["ETag"] = f'"sha256:{hashlib.sha256(rendered).hexdigest()}"'
        return Response(rendered, media_type=JSON_MEDIA_TYPE, headers=headers)

    @app.get(
        "/v1/incidents/{bundle_id}/analysis",
        response_model=StoredAnalysisResponse,
        responses=_ERROR_RESPONSES,
    )
    def analysis_endpoint(bundle_id: str) -> JSONResponse:
        record, payload = repository.get_artifact(bundle_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        assert_export_allowed(bundle, "local_api")
        stored = repository.get_analysis(bundle_id, settings.analyzer_version)
        if stored is not None:
            return JSONResponse(
                stored.as_dict(),
                headers={"Cache-Control": "no-store"},
            )
        if analyzer is None:
            raise ApiProblem(
                404,
                "EARSHOT_ANALYSIS_NOT_AVAILABLE",
                "analysis is not available for this incident",
            )

        generated_at = time.time_ns()
        value = analyzer(
            bundle,
            input_sha256=record.digest,
            generated_at_unix_nano=generated_at,
        )
        try:
            bound_analysis = DerivedAnalysis.model_validate(_analysis_value(value))
        except ValidationError as error:
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_CONTRACT",
                "analyzer returned an invalid DerivedAnalysis value",
            ) from error
        if (
            bound_analysis.input_sha256 != record.digest
            or bound_analysis.analyzer_version != settings.analyzer_version
            or int(bound_analysis.generated_at_unix_nano) != generated_at
        ):
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_BINDING_MISMATCH",
                "analyzer output does not match its requested input/version/time binding",
            )
        analysis_report = validate_derived_analysis(bundle, bound_analysis)
        if not analysis_report.ok:
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_CONTRACT",
                "analyzer output is inconsistent with the source evidence graph",
                issues=[_issue_dict(issue) for issue in analysis_report.errors],
            )
        stored = repository.put_analysis(
            bundle_id,
            settings.analyzer_version,
            bound_analysis,
        )
        return JSONResponse(
            stored.as_dict(),
            headers={"Cache-Control": "no-store"},
        )

    @app.delete(
        "/v1/incidents/{bundle_id}",
        status_code=204,
        responses=_ERROR_RESPONSES,
    )
    def delete_endpoint(bundle_id: str) -> Response:
        repository.purge(bundle_id)
        return Response(status_code=204)

    return app
