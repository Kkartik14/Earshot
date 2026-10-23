"""Calls: capture presets, OTLP spans grouped into calls, and their bookkeeping.

- ``projects.capture_preset`` (``full`` or ``metadata_only``, default ``full``) is
  added with ``ALTER TABLE … ADD COLUMN``, never by rebuilding ``projects``: a
  SQLite batch rebuild would drop the table's unnamed CHECKs.
- The call tables (``storage_schema.add_calls_tables``) are created. ``spans``
  keeps each span's transcript line (``text_json``) beside its payload, and
  stores the payload last.
- A catalog converged from 0.1.x (``earshot_meta('legacy_bootstrap')``, written in
  this same transaction by the legacy bootstrap) keeps its privacy posture: every
  project it already has captures ``metadata_only``, and one log line gives the
  count. A catalog 0.2.0 created has no marker, so its projects stay ``full``.

Revision ID: 0004_calls
Revises: 0003_projection_version
"""

from __future__ import annotations

import logging

from alembic import op

from earshot.storage_schema import (
    add_calls_tables,
    build_v19_baseline_metadata,
    capture_preset_column,
)

revision = "0004_calls"
down_revision = "0003_projection_version"
branch_labels = None
depends_on = None

_LOGGER = logging.getLogger("earshot.storage_migrations")


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("projects", capture_preset_column())
    metadata = build_v19_baseline_metadata()  # the tables the new ones reference
    metadata.create_all(bind, tables=add_calls_tables(metadata), checkfirst=False)

    legacy = bind.exec_driver_sql(
        "SELECT 1 FROM earshot_meta WHERE key = 'legacy_bootstrap'"
    ).fetchone()
    if legacy is None:
        return
    downgraded = bind.exec_driver_sql(
        "UPDATE projects SET capture_preset = 'metadata_only'"
    ).rowcount
    _LOGGER.warning(
        "EARSHOT_LEGACY_CAPTURE_PRESET: %d existing project(s) of an upgraded 0.1.x "
        "catalog now capture metadata_only; run `earshot project set-capture` to change it",
        downgraded,
    )


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
