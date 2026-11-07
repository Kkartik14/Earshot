"""Read the stored inputs shared by durable and live Call projections.

An open Call's spans and stored Recordings have no ``first_revision`` yet, except
while the closer is materializing its first Revision. A durable projection reads
only evidence whose ``first_revision`` is at most its Revision, leaving late
evidence for the next Revision.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .call_capture import stored_span
from .call_projection import CallSource, RecordingSource
from .storage_calls import call_spans_sql
from .storage_closer import SNAPSHOT_RESOURCES_SQL

if TYPE_CHECKING:
    from .storage_database import DatabaseConnection


def _source_spans(
    connection: DatabaseConnection,
    project_id: str,
    call_id: str,
    revision: int | None,
    open_revision: int | None,
) -> tuple:
    if revision is not None:
        condition = "s.first_revision <= ?"
        parameters = (project_id, call_id, revision)
    else:
        assert open_revision is not None
        condition = "(s.first_revision IS NULL OR s.first_revision = ?)"
        parameters = (project_id, call_id, open_revision)
    resources = {
        bytes(row["resource_digest"]): bytes(row["payload"])
        for row in connection.execute(
            SNAPSHOT_RESOURCES_SQL, (project_id, project_id, call_id)
        ).fetchall()
    }
    query = call_spans_sql(
        "s.trace_id, s.span_id, s.resource_digest, s.payload", condition=condition
    )
    rows = connection.execute(query, parameters)
    # Decode one stored payload at a time instead of retaining the full raw result
    # beside the decoded spans for large Calls.
    return tuple(
        stored_span(bytes(row["payload"]), resources[bytes(row["resource_digest"])]) for row in rows
    )


def _source_recordings(
    connection: DatabaseConnection,
    project_id: str,
    call_id: str,
    revision: int | None,
    open_revision: int | None,
) -> tuple[RecordingSource, ...]:
    if revision is not None:
        revision_filter = "r.first_revision <= ?"
        parameters = (project_id, call_id, revision)
    else:
        assert open_revision is not None
        revision_filter = "(r.first_revision IS NULL OR r.first_revision = ?)"
        parameters = (project_id, call_id, open_revision)
    rows = connection.execute(
        "SELECT r.recording_id, r.speaker, r.channel_speakers_json, r.content_type, "
        "r.started_at_unix_nano, r.duration_ms, a.state, a.error_code, "
        "a.peaks_blob_key, a.rendition_blob_key, "
        "CASE WHEN a.state = 'stored' THEN a.result_json END AS result_json "
        "FROM recordings r LEFT JOIN audio_analysis a ON a.project_id = r.project_id "
        "AND a.recording_id = r.recording_id "
        f"WHERE r.project_id = ? AND r.call_id = ? AND r.state = 'stored' AND {revision_filter} "
        "ORDER BY r.recording_id",
        parameters,
    ).fetchall()
    return tuple(
        RecordingSource(
            recording_id=row["recording_id"],
            speaker=row["speaker"],
            channel_speakers=(
                None
                if row["channel_speakers_json"] is None
                else tuple(json.loads(row["channel_speakers_json"]))
            ),
            content_type=row["content_type"],
            started_at_unix_nano=int(row["started_at_unix_nano"]),
            duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
            analysis_state=row["state"],
            analysis_error_code=row["error_code"],
            result=None if row["result_json"] is None else json.loads(row["result_json"]),
            has_peaks=row["peaks_blob_key"] is not None,
            holds_rendition=row["rendition_blob_key"] is not None,
        )
        for row in rows
    )


def read_call_source(
    connection: DatabaseConnection,
    *,
    project_id: str,
    call_id: str,
    revision: int | None,
    open_revision: int | None = None,
    first_evidence_unix_nano: int,
    ended: bool,
) -> CallSource:
    """Read one Call's spans and stored recordings under the specified revision cutoff.

    ``revision=None`` reads unassigned evidence and, for an open Call, evidence already
    marked for ``open_revision``. This includes the same snapshot during a claim and
    between failed attempts. An integer reads only evidence held by that durable
    Revision.
    """

    return CallSource(
        project_id=project_id,
        call_id=call_id,
        revision=0 if revision is None else revision,
        first_evidence_unix_nano=first_evidence_unix_nano,
        ended=ended,
        spans=_source_spans(connection, project_id, call_id, revision, open_revision),
        recordings=_source_recordings(connection, project_id, call_id, revision, open_revision),
    )
