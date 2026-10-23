"""Projection version: which release projected each incident's read models.

``incidents.projection_version`` records the ``INCIDENT_PROJECTION_VERSION`` an
incident's graph rows, turn facts, and expiry and export columns were derived
with. Rows that predate this revision get NULL, so after an upgrade every
existing incident is stale: the server re-projects stale rows in the background
once it is serving, instead of before it binds. The index keeps counting and
finding them cheap.

Revision ID: 0003_projection_version
Revises: 0002_artifacts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_projection_version"
down_revision = "0002_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("projection_version", sa.Integer(), nullable=True))
    op.create_index("incidents_projection_version_idx", "incidents", ["projection_version"])


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
