"""Host-neutral, metadata-only projections over one Earshot project."""

from __future__ import annotations

from dataclasses import dataclass

from .storage import IncidentStore

FRAMEWORK_NAME_MAX_LENGTH = 128
_ITEM_LIMIT = 50
_PAGE_SIZE = 100
_SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ProjectSummaryItem:
    """One retained session's safe catalog metadata, with no host presentation."""

    session_id: str
    status: str
    framework: str | None
    framework_truncated: bool
    created_at_unix_nano: str


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    items: tuple[ProjectSummaryItem, ...]


def summarize_project(
    store: IncidentStore,
    *,
    project_id: str,
    session_id: str | None = None,
) -> ProjectSummary:
    """Read bounded catalog metadata; never load artifact bytes or derived content."""

    items: list[ProjectSummaryItem] = []
    seen_session_ids: set[str] = set()
    cursor: str | None = None
    scanned = 0
    item_limit = 1 if session_id is not None else _ITEM_LIMIT
    while scanned < _SCAN_LIMIT and len(items) < item_limit:
        page = store.list_incidents(
            project_id=project_id,
            session_id=session_id,
            limit=_PAGE_SIZE,
            cursor=cursor,
            destination="local_api",
        )
        scanned += len(page.items)
        for item in page.items:
            if item.session_id in seen_session_ids:
                continue
            if len(item.session_id) > 200 or len(item.status) > 40:
                continue
            framework_truncated = (
                item.framework is not None and len(item.framework) > FRAMEWORK_NAME_MAX_LENGTH
            )
            framework = (
                item.framework[:FRAMEWORK_NAME_MAX_LENGTH]
                if framework_truncated
                else item.framework
            )
            items.append(
                ProjectSummaryItem(
                    session_id=item.session_id,
                    status=item.status,
                    framework=framework,
                    framework_truncated=framework_truncated,
                    created_at_unix_nano=item.created_at_unix_nano,
                )
            )
            seen_session_ids.add(item.session_id)
            if len(items) >= item_limit:
                break
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return ProjectSummary(project_id=project_id, items=tuple(items))


__all__ = ["ProjectSummary", "ProjectSummaryItem", "summarize_project"]
