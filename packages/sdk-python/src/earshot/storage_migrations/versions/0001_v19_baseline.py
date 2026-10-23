"""v19 baseline: the 0.1.x schema-v19 catalog, without its trigger, plus earshot_meta.

A new catalog is created by this revision. A 0.1.x catalog is converged to v19 by
the frozen ``storage_legacy`` routine and then stamped at this revision instead.

Revision ID: 0001_v19_baseline
Revises:
"""

from __future__ import annotations

from alembic import op

from earshot.storage_schema import V19_SEED_STATEMENTS, v19_baseline_metadata

revision = "0001_v19_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    v19_baseline_metadata().create_all(bind, checkfirst=False)
    for statement in V19_SEED_STATEMENTS:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
