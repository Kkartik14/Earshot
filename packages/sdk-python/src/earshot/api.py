"""FastAPI application for the local, immutable Earshot incident store."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from .analysis import ANALYZER_VERSION
from .browser_session import BrowserSessionStore
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
from .connectors import (
    DeliveryError,
    DeliveryTooLargeError,
    HostedProviderIngestion,
    RawProviderDelivery,
)
from .contract import DerivedAnalysis, IncidentBundle, IncidentBundleJson
from .explanation import IncidentExplanation, explain_incident
from .privacy import ExportPolicyError, assert_export_allowed
from .storage import (
    DEFAULT_PROJECT_ID,
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
from .versions import API_VERSION

Analyzer = Callable[..., DerivedAnalysis]
_VIEWER_SESSION_COOKIE = "earshot_session"
_CSRF_HEADER = "x-earshot-csrf"
_PROJECT_HEADER = "x-earshot-project-id"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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


class ConnectorProblemDetail(ProblemDetail):
    retryable: bool


class ConnectorProblemResponse(ApiModel):
    error: ConnectorProblemDetail


class HealthResponse(ApiModel):
    status: str


class BrowserSessionResponse(ApiModel):
    project_id: str
    csrf_token: str
    expires_in_seconds: int


class BrowserSessionStatusResponse(ApiModel):
    authenticated: bool
    authentication_required: bool
    project_id: str
    csrf_token: str | None
    expires_in_seconds: int | None


class ValidateResponse(ApiModel):
    valid: bool
    bundle_id: str
    session_id: str
    canonical_sha256: str
    warnings: list[ApiIssue]


class IncidentRecordResponse(ApiModel):
    project_id: str
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


class TurnMetricGroupResponse(ApiModel):
    group: str
    availability: str
    basis: str
    confidence: str
    limitation: str | None
    turn_count: int
    available_count: int
    average_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None
    p50_ms: float | None
    p95_ms: float | None


class TurnMetricSummaryResponse(ApiModel):
    metric: str
    group_by: str
    groups: list[TurnMetricGroupResponse]


class ConnectorDeliveryResponse(ApiModel):
    receipt_id: str
    disposition: Literal["applied", "replayed", "ignored"]
    bundle_id: str | None
    canonical_sha256: str | None


_ERROR_RESPONSES = {
    status: {"model": ProblemResponse}
    for status in (400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 500, 503)
}

_CONNECTOR_ERROR_RESPONSES = {
    status: {
        "model": ProblemResponse | ConnectorProblemResponse,
        **(
            {
                "headers": {
                    "Retry-After": {
                        "description": "Whole seconds before the delivery should be retried.",
                        "schema": {"type": "integer", "minimum": 1},
                    }
                }
            }
            if status in {429, 503}
            else {}
        ),
    }
    for status in (400, 401, 404, 409, 413, 415, 429, 500, 503)
}

_INCIDENT_REQUEST_BODY = {
    "parameters": [
        {
            "name": "Content-Encoding",
            "in": "header",
            "required": False,
            "schema": {
                "type": "string",
                "enum": ["identity", "gzip"],
                "default": "identity",
            },
        },
        {
            "name": "X-Earshot-Project-Id",
            "in": "header",
            "required": False,
            "description": (
                "SDK assertion checked against the project selected by the credential."
            ),
            "schema": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    ],
    "requestBody": {
        "required": True,
        "content": {
            JSON_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            "application/json": {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            PROTOBUF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
            "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    token: str | None = None
    max_body_bytes: int = 16 * 1024 * 1024
    max_connector_body_bytes: int = 2 * 1024 * 1024
    max_connector_deliveries_per_minute: int = 120
    max_json_depth: int = 64
    default_page_size: int = 50
    analyzer_version: str = ANALYZER_VERSION
    behind_tls_proxy: bool = False
    # Opt-in for a single-machine container: permits an unauthenticated
    # non-loopback bind on the promise that the listener is confined to a
    # trusted boundary (e.g. `docker run -p 127.0.0.1:PORT`). Off by default.
    trust_local_network: bool = False
    viewer_session_capacity: int = 256
    viewer_session_ttl_seconds: int = 8 * 60 * 60

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if self.max_connector_body_bytes < 1:
            raise ValueError("max_connector_body_bytes must be positive")
        if self.max_connector_deliveries_per_minute < 1:
            raise ValueError("max_connector_deliveries_per_minute must be positive")
        if self.max_json_depth < 1:
            raise ValueError("max_json_depth must be positive")
        if self.viewer_session_capacity < 1:
            raise ValueError("viewer_session_capacity must be positive")
        if self.viewer_session_ttl_seconds < 1:
            raise ValueError("viewer_session_ttl_seconds must be positive")
        if (
            not _is_loopback(self.host)
            and not self.behind_tls_proxy
            and not self.trust_local_network
        ):
            raise ValueError("a non-loopback listener requires an explicitly trusted TLS proxy")


def _remote_access(settings: ApiConfig) -> bool:
    """Whether the listener is reachable beyond the loopback interface."""
    return not _is_loopback(settings.host) or settings.behind_tls_proxy


def _authentication_required(settings: ApiConfig) -> bool:
    """Single source of truth for whether ``/v1`` demands a credential.

    Loopback — or an explicitly trusted local network (a loopback-mapped
    container) — with no configured token serves anonymously; every other
    binding requires a bearer key or a viewer session. Middleware enforcement,
    the ``/v1/auth/session`` gate, and the generated OpenAPI security all read
    from this one predicate so they cannot drift.
    """
    return (
        _remote_access(settings) and not settings.trust_local_network
    ) or settings.token is not None


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


def _decompress_gzip(payload: bytes, maximum: int) -> bytes:
    """Decode one strict gzip member without allocating beyond the body limit."""

    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = bytearray(decompressor.decompress(payload, maximum + 1))
        if len(decoded) > maximum or decompressor.unconsumed_tail:
            raise ApiProblem(
                413,
                "EARSHOT_BODY_TOO_LARGE",
                "incident body exceeds limit after decompression",
            )
        decoded.extend(decompressor.flush(maximum + 1 - len(decoded)))
    except zlib.error as error:
        raise ApiProblem(
            400,
            "EARSHOT_MALFORMED_GZIP",
            "incident gzip body is malformed",
        ) from error
    if len(decoded) > maximum:
        raise ApiProblem(
            413,
            "EARSHOT_BODY_TOO_LARGE",
            "incident body exceeds limit after decompression",
        )
    if not decompressor.eof or decompressor.unused_data or not decoded:
        raise ApiProblem(
            400,
            "EARSHOT_MALFORMED_GZIP",
            "incident gzip body is malformed",
        )
    return bytes(decoded)


async def _read_connector_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise DeliveryTooLargeError
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
            raise DeliveryTooLargeError
    if not body:
        raise ApiProblem(400, "EARSHOT_EMPTY_BODY", "connector body is empty")
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


_SPA_RESERVED_PREFIXES = (
    "v1/",
    "hooks/",
    "healthz",
    "readyz",
    "openapi.json",
    "docs",
    "redoc",
)


def _resolve_web_dir(web_dir: str | Path | None) -> Path | None:
    """Locate a built single-page viewer to serve alongside the API, if any.

    Precedence: explicit argument, ``EARSHOT_WEB_DIR``, then a ``web`` directory
    packaged next to this module. Returns ``None`` when no ``index.html`` exists,
    so the API runs headless without a bundled UI.
    """
    candidate: str | Path | None = web_dir or os.environ.get("EARSHOT_WEB_DIR")
    if candidate is None:
        packaged = Path(__file__).resolve().parent / "web"
        candidate = packaged if packaged.is_dir() else None
    if candidate is None:
        return None
    root = Path(candidate).expanduser().resolve()
    return root if (root / "index.html").is_file() else None


def _mount_spa(app: FastAPI, web_root: Path) -> None:
    """Serve the viewer: hashed assets are cached by the static handler, client
    routes fall back to ``index.html``, and API prefixes are never shadowed."""
    assets = web_root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def index() -> FileResponse:
        return FileResponse(
            web_root / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa(spa_path: str) -> Response:
        if spa_path.startswith(_SPA_RESERVED_PREFIXES):
            return JSONResponse(
                {"error": {"code": "EARSHOT_NOT_FOUND", "message": "not found"}},
                status_code=404,
            )
        if spa_path:
            candidate = (web_root / spa_path).resolve()
            within = candidate == web_root or web_root in candidate.parents
            if within and candidate.is_file():
                return FileResponse(candidate)
        return index()


def create_app(
    *,
    store: IncidentStore | None = None,
    data_dir: str | Path = ".earshot",
    analyzer: Analyzer | None = None,
    config: ApiConfig | None = None,
    connector_ingestion: HostedProviderIngestion | None = None,
    web_dir: str | Path | None = None,
) -> FastAPI:
    settings = config or ApiConfig()
    repository = store or IncidentStore(data_dir)
    remote_access = _remote_access(settings)
    if (
        remote_access
        and not settings.trust_local_network
        and not settings.token
        and not repository.has_active_api_keys()
    ):
        raise ValueError("remote access requires a bearer token or an active project API key")
    app = FastAPI(
        title="Earshot local ingest",
        version=API_VERSION,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.store = repository
    app.state.config = settings
    app.state.analyzer = analyzer
    app.state.connector_ingestion = connector_ingestion or HostedProviderIngestion(
        repository,
        max_body_bytes=settings.max_connector_body_bytes,
        max_json_depth=settings.max_json_depth,
        max_deliveries_per_minute=settings.max_connector_deliveries_per_minute,
    )
    app.state.browser_sessions = BrowserSessionStore(
        capacity=settings.viewer_session_capacity,
        ttl_seconds=settings.viewer_session_ttl_seconds,
    )

    def openapi_schema() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            description=(
                "Local immutable ingest, retrieval, purge, and digest-bound analysis "
                "for Earshot v1alpha1 incidents."
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
        schema["components"]["securitySchemes"]["BrowserSession"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": _VIEWER_SESSION_COOKIE,
        }
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/v1/"):
                continue
            for method, operation in path_item.items():
                if isinstance(operation, dict) and "responses" in operation:
                    # Loopback deployments may omit auth; remote deployments
                    # accept bearer clients or the same-origin viewer session.
                    authenticated = [{"BearerAuth": []}, {"BrowserSession": []}]
                    auth_needed = _authentication_required(settings)
                    if path == "/v1/auth/session" and method == "post":
                        operation["security"] = [{"BearerAuth": []}]
                    elif path == "/v1/auth/session" and method == "get":
                        operation["security"] = (
                            [{"BrowserSession": []}]
                            if auth_needed
                            else [{"BrowserSession": []}, {}]
                        )
                    elif path == "/v1/auth/logout":
                        operation["security"] = [{"BrowserSession": []}]
                    else:
                        operation["security"] = (
                            authenticated if auth_needed else [*authenticated, {}]
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

    @app.exception_handler(DeliveryError)
    async def handle_delivery_error(_: Request, error: DeliveryError) -> JSONResponse:
        headers = (
            {"Retry-After": str(error.retry_after_seconds)}
            if error.retry_after_seconds is not None
            else None
        )
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": error.public_message,
                    "retryable": error.retryable,
                }
            },
            status_code=error.http_status,
            headers=headers,
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
        transport_protected_path = request.url.path.startswith(("/v1/", "/hooks/v1/"))
        if (
            unsafe_runtime_binding
            and transport_protected_path
            and not settings.behind_tls_proxy
            and not settings.trust_local_network
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "EARSHOT_REMOTE_BINDING_UNSAFE",
                        "message": (
                            "runtime listener is non-loopback; the backend permits only "
                            "a loopback listener unless a remote TLS proxy is trusted"
                        ),
                    }
                },
                status_code=503,
            )
        local_only_host_boundary = settings.trust_local_network or (
            not settings.token and _is_loopback(settings.host)
        )
        if (
            local_only_host_boundary
            and transport_protected_path
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
        if request.url.path.startswith("/v1/"):
            authorization = request.headers.get("authorization", "")
            scheme, separator, credential = authorization.partition(" ")
            supplied = credential if separator and scheme.lower() == "bearer" else ""
            principal = None
            browser_session = None
            browser_token = ""
            if supplied:
                principal = await run_in_threadpool(
                    repository.authenticate_api_key,
                    supplied,
                )
            elif not authorization:
                browser_token = request.cookies.get(_VIEWER_SESSION_COOKIE, "")
                if browser_token:
                    browser_session = app.state.browser_sessions.authenticate(browser_token)
                    if browser_session is not None and browser_session.key_id is not None:
                        issuer_active = await run_in_threadpool(
                            repository.api_key_is_active,
                            browser_session.project_id,
                            browser_session.key_id,
                        )
                        if not issuer_active:
                            app.state.browser_sessions.revoke(browser_token)
                            browser_session = None
            legacy_valid = bool(
                supplied and settings.token and hmac.compare_digest(supplied, settings.token)
            )
            auth_required = _authentication_required(settings)
            bearer_valid = principal is not None or legacy_valid
            session_valid = browser_session is not None
            invalid_supplied = bool(authorization or browser_token) and not (
                bearer_valid or session_valid
            )
            if (auth_required and not bearer_valid and not session_valid) or invalid_supplied:
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
            if browser_session is not None and request.method.upper() in _UNSAFE_METHODS:
                csrf = request.headers.get(_CSRF_HEADER, "")
                if not app.state.browser_sessions.csrf_matches(browser_session, csrf):
                    return JSONResponse(
                        {
                            "error": {
                                "code": "EARSHOT_CSRF_REQUIRED",
                                "message": "valid CSRF token required",
                            }
                        },
                        status_code=403,
                    )
            request.state.project_id = (
                principal.project_id
                if principal is not None
                else (
                    browser_session.project_id
                    if browser_session is not None
                    else DEFAULT_PROJECT_ID
                )
            )
            request.state.auth_method = (
                "bearer" if bearer_valid else ("session" if session_valid else "anonymous")
            )
            request.state.auth_key_id = principal.key_id if principal is not None else None
            request.state.browser_session = browser_session
            request.state.browser_session_token = browser_token
            asserted_project_id = request.headers.get(_PROJECT_HEADER)
            if asserted_project_id is not None and asserted_project_id != request.state.project_id:
                return JSONResponse(
                    {
                        "error": {
                            "code": "EARSHOT_PROJECT_MISMATCH",
                            "message": (
                                "asserted SDK project does not match the authenticated project"
                            ),
                        }
                    },
                    status_code=403,
                )
        return await call_next(request)

    @app.post(
        "/v1/auth/session",
        response_model=BrowserSessionResponse,
        status_code=201,
        responses=_ERROR_RESPONSES,
    )
    def create_browser_session(request: Request) -> JSONResponse:
        if request.state.auth_method != "bearer":
            raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "valid bearer token required")
        previous = request.cookies.get(_VIEWER_SESSION_COOKIE, "")
        if previous:
            app.state.browser_sessions.revoke(previous)
        issued = app.state.browser_sessions.issue(
            project_id=request.state.project_id,
            key_id=request.state.auth_key_id,
        )
        response = JSONResponse(
            {
                "project_id": issued.session.project_id,
                "csrf_token": issued.session.csrf_token,
                "expires_in_seconds": settings.viewer_session_ttl_seconds,
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            _VIEWER_SESSION_COOKIE,
            issued.token,
            max_age=settings.viewer_session_ttl_seconds,
            path="/",
            secure=settings.behind_tls_proxy,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get(
        "/v1/auth/session",
        response_model=BrowserSessionStatusResponse,
        responses=_ERROR_RESPONSES,
    )
    def get_browser_session(request: Request) -> JSONResponse:
        session = request.state.browser_session
        if request.state.auth_method != "session" or session is None:
            # A trusted local network (e.g. a loopback-mapped container) needs no
            # viewer login, so the SPA can load without a project key it does not
            # yet have. Same predicate as the middleware and OpenAPI security.
            if _authentication_required(settings):
                raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "viewer session required")
            return JSONResponse(
                {
                    "authenticated": False,
                    "authentication_required": False,
                    "project_id": DEFAULT_PROJECT_ID,
                    "csrf_token": None,
                    "expires_in_seconds": None,
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "authenticated": True,
                "authentication_required": True,
                "project_id": session.project_id,
                "csrf_token": session.csrf_token,
                "expires_in_seconds": max(
                    0,
                    int(session.expires_at - time.monotonic()),
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/v1/auth/logout",
        status_code=204,
        responses=_ERROR_RESPONSES,
    )
    def logout_browser_session(request: Request) -> Response:
        token = request.state.browser_session_token
        if request.state.auth_method != "session" or not token:
            raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "viewer session required")
        app.state.browser_sessions.revoke(token)
        response = Response(status_code=204)
        response.delete_cookie(
            _VIEWER_SESSION_COOKIE,
            path="/",
            secure=settings.behind_tls_proxy,
            httponly=True,
            samesite="strict",
        )
        return response

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

    @app.post(
        "/hooks/v1/connectors/{endpoint_id}",
        response_model=ConnectorDeliveryResponse,
        responses=_CONNECTOR_ERROR_RESPONSES,
    )
    async def connector_delivery_endpoint(endpoint_id: str, request: Request) -> JSONResponse:
        if _content_type(request) != "application/json":
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_MEDIA_TYPE",
                "connector delivery requires application/json",
            )
        payload = await _read_connector_body(request, settings.max_connector_body_bytes)
        raw = RawProviderDelivery(
            endpoint_id=endpoint_id,
            headers=tuple(request.headers.raw),
            body=payload,
        )
        outcome = await run_in_threadpool(app.state.connector_ingestion.receive, raw)
        return JSONResponse(
            {
                "receipt_id": outcome.receipt_id,
                "disposition": outcome.disposition,
                "bundle_id": outcome.bundle_id,
                "canonical_sha256": outcome.canonical_sha256,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/metrics/turns",
        response_model=TurnMetricSummaryResponse,
        responses=_ERROR_RESPONSES,
    )
    def turn_metrics_endpoint(
        request: Request,
        metric: Literal[
            "stt_finalization_ms",
            "eou_ms",
            "first_token_ms",
            "generated_response_ms",
            "sent_response_ms",
            "received_response_ms",
            "render_start_response_ms",
            "response_ms",
            "turn_duration_ms",
        ] = "response_ms",
        group_by: Literal["framework", "provider", "model", "language", "status"] = "framework",
    ) -> JSONResponse:
        groups = repository.summarize_turn_metric(
            metric,
            project_id=request.state.project_id,
            group_by=group_by,
        )
        return JSONResponse(
            {
                "metric": metric,
                "group_by": group_by,
                "groups": [
                    {
                        "group": group.group,
                        "availability": group.availability,
                        "basis": group.basis,
                        "confidence": group.confidence,
                        "limitation": group.limitation,
                        "turn_count": group.turn_count,
                        "available_count": group.available_count,
                        "average_ms": group.average_ms,
                        "minimum_ms": group.minimum_ms,
                        "maximum_ms": group.maximum_ms,
                        "p50_ms": group.p50_ms,
                        "p95_ms": group.p95_ms,
                    }
                    for group in groups
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def decode_and_validate(
        request: Request,
    ) -> tuple[IncidentBundle, list[dict[str, object]], bytes]:
        content_encoding = request.headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity", "gzip"}:
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_CONTENT_ENCODING",
                "incident content encoding is not supported",
            )
        payload = await _read_body(request, settings.max_body_bytes)
        if content_encoding == "gzip":
            payload = await run_in_threadpool(
                _decompress_gzip,
                payload,
                settings.max_body_bytes,
            )
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
        result = await run_in_threadpool(
            repository.ingest,
            bundle,
            canonical,
            project_id=request.state.project_id,
        )
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
        request: Request,
        session_id: str | None = None,
        limit: int = Query(default=settings.default_page_size, ge=1, le=100),
        cursor: str | None = None,
    ) -> JSONResponse:
        try:
            page = repository.list_incidents(
                project_id=request.state.project_id,
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
            _, payload = repository.get_artifact(
                item.bundle_id, project_id=request.state.project_id
            )
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
        record, payload = repository.get_artifact(bundle_id, project_id=request.state.project_id)
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

    def resolve_analysis(
        bundle_id: str, *, project_id: str
    ) -> tuple[IncidentBundle, object, DerivedAnalysis]:
        record, payload = repository.get_artifact(bundle_id, project_id=project_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        assert_export_allowed(bundle, "local_api")
        stored = repository.get_analysis(
            bundle_id,
            settings.analyzer_version,
            project_id=project_id,
        )
        if stored is not None:
            try:
                bound_analysis = DerivedAnalysis.model_validate(stored.value)
            except ValidationError as error:
                raise ArtifactCorruptionError("stored analysis cannot be decoded") from error
            return bundle, stored, bound_analysis
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
            project_id=project_id,
        )
        return bundle, stored, bound_analysis

    @app.get(
        "/v1/incidents/{bundle_id}/analysis",
        response_model=StoredAnalysisResponse,
        responses=_ERROR_RESPONSES,
    )
    def analysis_endpoint(bundle_id: str, request: Request) -> JSONResponse:
        _, stored, _ = resolve_analysis(
            bundle_id,
            project_id=request.state.project_id,
        )
        return JSONResponse(
            stored.as_dict(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}/explanation",
        response_model=IncidentExplanation,
        responses=_ERROR_RESPONSES,
    )
    def explanation_endpoint(bundle_id: str, request: Request) -> JSONResponse:
        bundle, _, analysis = resolve_analysis(
            bundle_id,
            project_id=request.state.project_id,
        )
        explanation = explain_incident(bundle, analysis)
        return JSONResponse(
            explanation.model_dump(mode="json", exclude_none=True),
            headers={"Cache-Control": "no-store"},
        )

    @app.delete(
        "/v1/incidents/{bundle_id}",
        status_code=204,
        responses=_ERROR_RESPONSES,
    )
    def delete_endpoint(bundle_id: str, request: Request) -> Response:
        repository.purge(bundle_id, project_id=request.state.project_id)
        return Response(status_code=204)

    # The SPA catch-all is registered last so every API route takes precedence.
    web_root = _resolve_web_dir(web_dir)
    if web_root is not None:
        _mount_spa(app, web_root)

    return app
