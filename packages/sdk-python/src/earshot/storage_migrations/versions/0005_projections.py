"""Projections: stereo channel maps, recording analyses, call projections and prices.

- Three columns are added with ``ALTER TABLE … ADD COLUMN``, never by rebuilding a
  table (a SQLite batch rebuild would drop its unnamed CHECKs):
  ``recordings.channel_speakers_json`` (a stereo recording's speaker per channel),
  ``calls.projection_seq`` and ``projects.price_version`` (both start at 0).
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
            "INSERT INTO audio_analysis(project_id, recording_id, state, attempts, "
            "lease_token, algorithm_version, updated_unix_nano) "
            "SELECT project_id, recording_id, 'pending', 0, 0, 0, :now FROM recordings "
            "WHERE state = 'stored'"
        ),
        {"now": time.time_ns()},
    )


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
