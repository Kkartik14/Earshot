"""Artifacts: canonical incident bytes stored in the catalog, keyed by digest.

A Postgres catalog keeps each canonical artifact as a ``bytea`` row here, written
in the same transaction as its incident, instead of a content-addressed file.
SQLite keeps the file CAS and leaves this table empty; it exists on both dialects
so the two catalogs have one schema.

A row is live while some ``incidents.object_digest`` references it. There is no
reference count: the purge transaction deletes the rows nothing references.

Revision ID: 0002_artifacts
Revises: 0001_v19_baseline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_artifacts"
down_revision = "0001_v19_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("digest"),
        sa.CheckConstraint("length(digest) = 64"),
    )


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
