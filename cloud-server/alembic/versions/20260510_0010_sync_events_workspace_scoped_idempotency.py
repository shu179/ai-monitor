"""scope idempotency_key to workspace on sync_events, run_records, article_reference_events

Revision ID: 0010_workspace_idempotency
Revises: 0009_viewer_all_tasks
Create Date: 2026-05-10 00:00:00

Upgrades three tables to workspace-scoped uniqueness on (workspace_id, idempotency_key).
Old constraints (single-column unique on idempotency_key) are detected and dropped
dynamically by column set — no hard-coded names.
New constraints are created only if they don't already exist.
Downgrade checks for cross-workspace idempotency_key duplicates before restoring
the global unique constraint.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0010_workspace_idempotency"
down_revision: Union[str, None] = "0009_viewer_all_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table_name, new_constraint_name, idempotency_key_column, workspace_column)
_TABLE_META = [
    (
        "sync_events",
        "uq_sync_events_workspace_idempotency",
        "idempotency_key",
        "workspace_id",
    ),
    (
        "run_records",
        "uq_run_records_workspace_idempotency",
        "idempotency_key",
        "workspace_id",
    ),
    (
        "article_reference_events",
        "uq_article_ref_events_workspace_idempotency",
        "idempotency_key",
        "workspace_id",
    ),
]

# Default names Alembic assigns when no explicit name is given.
# Used as a fallback for restore during downgrade.
_DEFAULT_GLOBAL_CONSTRAINT_NAMES = {
    "sync_events": "sync_events_idempotency_key_key",
    "run_records": "run_records_idempotency_key_key",
    "article_reference_events": "article_reference_events_idempotency_key_key",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_constraints(table_name: str) -> list[dict]:
    """Return raw Inspector unique-constraint records for *table_name*."""
    inspector = inspect(op.get_bind())
    return inspector.get_unique_constraints(table_name)


def _constraint_columns(constraint: dict) -> tuple[str, ...]:
    """Extract the sorted column tuple from an Inspector constraint record."""
    return tuple(sorted(c for c in (constraint.get("column_names") or [])))


def _find_unique_constraints_by_columns(
    table_name: str, columns: Sequence[str]
) -> list[str]:
    """Return names of unique constraints whose column set matches *columns*."""
    target = tuple(sorted(columns))
    return [
        c["name"]
        for c in _unique_constraints(table_name)
        if _constraint_columns(c) == target and c.get("name")
    ]


def _has_unique_constraint(table_name: str, columns: Sequence[str]) -> bool:
    """True if any unique constraint covers exactly *columns* on *table_name*."""
    return bool(_find_unique_constraints_by_columns(table_name, columns))


def _drop_unique_constraints_by_columns(table_name: str, columns: Sequence[str]) -> None:
    """Drop every unique constraint whose column set matches *columns*."""
    names = _find_unique_constraints_by_columns(table_name, columns)
    for name in names:
        op.drop_constraint(name, table_name, type_="unique")


def _ensure_unique_constraint(
    table_name: str, name: str, columns: Sequence[str]
) -> None:
    """Create a unique constraint on *columns* if one doesn't already exist."""
    if not _has_unique_constraint(table_name, columns):
        op.create_unique_constraint(name, table_name, list(columns))


def _assert_no_cross_workspace_duplicates(table_name: str, key_column: str) -> None:
    """Raise RuntimeError if *key_column* has duplicate values across workspaces."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            f"""
            SELECT {key_column}, COUNT(*) AS cnt
            FROM {table_name}
            GROUP BY {key_column}
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    )
    row = result.fetchone()
    if row is not None:
        dup_key = row[0]
        raise RuntimeError(
            f"Downgrade blocked: table '{table_name}' has cross-workspace "
            f"duplicate {key_column}='{dup_key}'. "
            f"Resolve duplicates manually before downgrading this migration."
        )


# ---------------------------------------------------------------------------
# Upgrade / Downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    for table, new_name, key_col, ws_col in _TABLE_META:
        # 1. Drop every single-column unique constraint on idempotency_key.
        #    This handles both the named version and any auto-named variant.
        _drop_unique_constraints_by_columns(table, [key_col])

        # 2. Create the composite unique constraint only if absent.
        _ensure_unique_constraint(table, new_name, [ws_col, key_col])


def downgrade() -> None:
    for table, new_name, key_col, ws_col in reversed(_TABLE_META):
        # 1. Guard: ensure no cross-workspace duplicate idempotency_key exists,
        #          otherwise restoring the global unique will fail.
        _assert_no_cross_workspace_duplicates(table, key_col)

        # 2. Drop the composite constraint (found by column set, not name).
        _drop_unique_constraints_by_columns(table, [ws_col, key_col])

        # 3. Restore the global single-column unique constraint.
        #    Use the default name that PostgreSQL/SQLAlchemy assigns automatically
        #    so this matches what existed before the upgrade.
        default_name = _DEFAULT_GLOBAL_CONSTRAINT_NAMES.get(table)
        if default_name and not _has_unique_constraint(table, [key_col]):
            op.create_unique_constraint(default_name, table, [key_col])
