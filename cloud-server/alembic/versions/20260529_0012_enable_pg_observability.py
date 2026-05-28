"""Enable PostgreSQL query observability helpers.

Revision ID: 0012_pg_observability
Revises: 0011_cloud_sync_v2_foundations
Create Date: 2026-05-29 02:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0012_pg_observability"
down_revision = "0011_cloud_sync_v2_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    # Keep query history tooling installed. Dropping the extension during a
    # rollback would erase useful production diagnostics.
    pass
