"""Platform-specific links over Earshot's host-neutral project summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from ...storage import IncidentStore, InvalidCursorError
from ...summary import summarize_project


class PlatformObserveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(max_length=160)
    status: str = Field(max_length=40)
    created_at: str | None = None
    href: str = Field(max_length=4096)


class PlatformObserveSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=240)
    items: list[PlatformObserveItem] = Field(max_length=50)


def _created_at(unix_nanos: str) -> str | None:
    try:
        nanoseconds = int(unix_nanos)
        seconds, remainder = divmod(nanoseconds, 1_000_000_000)
        timestamp = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=remainder // 1_000)
    except (ValueError, OverflowError, OSError):
        return None
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _problem(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
    )


def create_platform_router(store: IncidentStore) -> APIRouter:
    """Build an opt-in Platform adapter; caller must install hosted JWT auth."""

    router = APIRouter()

    @router.get(
        "/v1/platform/projects/{project_id}/observe/summary",
        response_model=PlatformObserveSummary,
        response_model_exclude_none=True,
        summary="Adapt the Earshot project summary to Platform Observe links",
        description=(
            "Platform authenticates the user and mints a short-lived Earshot JWT. "
            "The path project UUID must match its signed project claim. The adapter "
            "returns metadata only and adds Platform-specific labels and links."
        ),
    )
    def observe_summary(
        project_id: str,
        request: Request,
        record_id: str | None = Query(default=None, max_length=200),
    ) -> PlatformObserveSummary | JSONResponse:
        if request.state.auth_method != "hosted_jwt":
            return _problem(401, "EARSHOT_UNAUTHORIZED", "hosted project token required")
        if project_id != request.state.project_id:
            return _problem(
                403,
                "EARSHOT_PROJECT_MISMATCH",
                "requested project does not match the signed project",
            )
        try:
            canonical_id = str(uuid.UUID(project_id))
        except (ValueError, AttributeError):
            return _problem(400, "EARSHOT_INVALID_PLATFORM_PROJECT", "project ID must be a UUID")
        if canonical_id != project_id:
            return _problem(
                400,
                "EARSHOT_INVALID_PLATFORM_PROJECT",
                "project ID must use canonical lowercase UUID notation",
            )
        assertions = request.headers.getlist("x-platform-project-id")
        if assertions and (len(assertions) != 1 or assertions[0] != project_id):
            return _problem(
                403,
                "EARSHOT_PROJECT_MISMATCH",
                "Platform project assertion must match the signed project",
            )
        try:
            summary = summarize_project(store, project_id=project_id, session_id=record_id)
        except InvalidCursorError:
            return _problem(400, "EARSHOT_INVALID_CURSOR", "invalid incident pagination cursor")

        items = [
            PlatformObserveItem(
                id=item.session_id,
                title=f"{(item.framework or 'Voice')[:150]} session",
                status=item.status,
                created_at=_created_at(item.created_at_unix_nano),
                href="/observe?" + urlencode({"sessionId": item.session_id}),
            )
            for item in summary.items
        ]
        return PlatformObserveSummary(summary="Recent retained voice evidence", items=items)

    return router


__all__ = [
    "PlatformObserveItem",
    "PlatformObserveSummary",
    "create_platform_router",
]
