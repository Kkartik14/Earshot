from __future__ import annotations

from earshot.storage import IncidentPage, IncidentRecord
from earshot.summary import summarize_project


class _LegacySummaryStore:
    def __init__(self, framework: str) -> None:
        self._framework = framework

    def list_incidents(self, **_kwargs: object) -> IncidentPage:
        return IncidentPage(
            items=(
                IncidentRecord(
                    project_id="project-a",
                    bundle_id="bundle-a",
                    session_id="session-a",
                    schema_version="1",
                    digest="a" * 64,
                    size_bytes=1,
                    status="completed",
                    finality="final",
                    completeness="complete",
                    framework=self._framework,
                    created_at_unix_nano="1",
                    ingested_at_unix_nano="1",
                ),
            ),
            next_cursor=None,
        )


def test_summary_caps_legacy_framework_metadata_and_reports_truncation() -> None:
    summary = summarize_project(
        _LegacySummaryStore("framework-name" * 2_000),  # type: ignore[arg-type]
        project_id="project-a",
    )

    item = summary.items[0]
    assert item.framework == ("framework-name" * 10)[:128]
    assert item.framework_truncated is True
