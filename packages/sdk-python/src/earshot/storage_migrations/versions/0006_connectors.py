"""Connectors: deliveries become call evidence, and provider recordings are fetched.

- Three columns are added with ``ALTER TABLE … ADD COLUMN``, never by rebuilding a
  table (a SQLite batch rebuild would drop its unnamed CHECKs):
  ``connectors.config_json`` (NULL: the defaults), ``delivery_receipts.call_id``
  (the call a delivery's evidence went to) and ``delivery_receipts.digest_kind``.
- Every receipt already stored keeps its digest, now marked ``body_v1``: the sha256
  of the raw body, as 0.3.x computed it. A retry of a delivery 0.3.x applied is
  still recognised as the same delivery after the upgrade. New receipts digest the
  delivery's evidence instead (``evidence_v1``).
- ``recording_fetches`` is created (``storage_schema.add_connector_tables``): one
  fetch job per provider recording, deleted with its recording or its connector.

Revision ID: 0006_connectors
Revises: 0005_projections
"""

from __future__ import annotations

from alembic import op

from earshot.storage_schema import (
    add_calls_tables,
    add_connector_tables,
    build_v19_baseline_metadata,
    connector_columns,
)

revision = "0006_connectors"
down_revision = "0005_projections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in connector_columns():
        op.add_column(table, column)
    metadata = build_v19_baseline_metadata()
    add_calls_tables(metadata)  # the tables the new one references
    metadata.create_all(bind, tables=add_connector_tables(metadata), checkfirst=False)


def downgrade() -> None:
    raise NotImplementedError("catalog migrations are forward-only")
