"""Projections: stereo channel maps, recording analyses, call projections and prices.

- Five columns are added with ``ALTER TABLE … ADD COLUMN``, never by rebuilding a
  table (a SQLite batch rebuild would drop its unnamed CHECKs):
  ``recordings.channel_speakers_json`` (a stereo recording's speaker per channel),
  ``calls.projection_seq`` and ``projects.price_version`` (both start at 0), and
  ``spans.first_revision`` and ``recordings.first_revision`` (the first revision built
  from each).
- Evidence already stored is marked with its call's latest revision, unless it was
  stored after that revision was built (a span received, or a recording stored, after
  the revision's ``created_unix_nano``): that evidence stays unmarked, so the call's
  next revision marks it. Evidence stored while the latest revision was being built
  (after its claim, before its commit) cannot be told from evidence it holds, and is
  marked with it. Pending recordings and the evidence of a call with no revision stay
  unmarked.
- The tables ``storage_schema.add_projection_tables`` defines are created:
  ``builtin_price_version`` (seeded with version 0), ``prices`` (empty: built-in
  prices are seeded by the code that owns them), ``audio_analysis``,
  ``call_projections``, ``call_transcripts`` and ``call_tags``.
- Every recording already stored gets a pending ``audio_analysis`` row, as storing
  one does from now on, so recordings stored before this revision are analysed too.
  A pending recording gets its row when it is stored.

Revision ID: 0005_projections
Revises: 0004_calls
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op

from earshot.storage_schema import (
    add_calls_tables,
    add_projection_tables,
    build_v19_baseline_metadata,
    projection_columns,
)

revision = "0005_projections"
down_revision = "0004_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in projection_columns():
        op.add_column(table, column)
    metadata = build_v19_baseline_metadata()
    add_calls_tables(metadata)  # the tables the new ones reference
    metadata.create_all(bind, tables=add_projection_tables(metadata), checkfirst=False)
    bind.execute(sa.text("INSERT INTO builtin_price_version(singleton, version) VALUES (1, 0)"))
    bind.execute(
        sa.text(
            "UPDATE spans SET first_revision = (SELECT c.latest_revision FROM call_traces t "
            "JOIN calls c ON c.project_id = t.project_id AND c.call_id = t.call_id "
            "JOIN call_revisions r ON r.project_id = c.project_id AND r.call_id = c.call_id "
            "AND r.revision = c.latest_revision "
            "WHERE t.project_id = spans.project_id AND t.trace_id = spans.trace_id "
            "AND spans.received_unix_nano <= r.created_unix_nano)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE recordings SET first_revision = (SELECT c.latest_revision FROM calls c "
            "JOIN call_revisions r ON r.project_id = c.project_id AND r.call_id = c.call_id "
            "AND r.revision = c.latest_revision "
            "WHERE c.project_id = recordings.project_id AND c.call_id = recordings.call_id "
            "AND COALESCE(recordings.stored_unix_nano, recordings.created_unix_nano) "
            "<= r.created_unix_nano) "
            "WHERE state = 'stored'"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO audio_analysis(project_id, recording_id, state, attempts, "
            "lease_token, algorithm_version, updated_unix_nano) "
            "SELECT project_id, recording_id, 'pending', 0, 0, 0, :now FROM recordings "
            "WHERE state = 'stored'"
        ),
        {"now": time.time_ns()},
    )


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
