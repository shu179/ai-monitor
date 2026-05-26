"""add hot-path indexes for sync snapshot / incremental pulls

Revision ID: 0010_hot_path_indexes
Revises: 0009_viewer_all_tasks
Create Date: 2026-05-16 00:00:00

Covers the snapshot "query battery" (build_workspace_event_snapshot) plus the
article incremental cursor and the task_day_status JSON grouping, which
previously fell back to sequential scans that worsen as append-only tables grow.

Note: these use plain CREATE INDEX (brief lock). On a very large live table
prefer CREATE INDEX CONCURRENTLY out-of-band; kept simple here for typical
workspace sizes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_hot_path_indexes"
down_revision: Union[str, None] = "0009_viewer_all_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_articles_ws_updated_id",
        "articles",
        ["workspace_id", "updated_at", "id"],
    )
    op.create_index(
        "idx_sync_events_ws_type_id",
        "sync_events",
        ["workspace_id", "event_type", "id"],
    )
    op.create_index(
        "idx_sync_events_tds_task",
        "sync_events",
        ["workspace_id", sa.text("((payload_json ->> 'task_id')::integer)"), "id"],
        postgresql_where=sa.text("event_type = 'task_day_status'"),
    )
    op.create_index(
        "idx_tae_ws_id",
        "task_assignment_events",
        ["workspace_id", "id"],
    )
    op.create_index(
        "idx_cjobs_ws_updated",
        "classification_jobs",
        ["workspace_id", "updated_at"],
    )
    op.create_index(
        "idx_are_ws_id",
        "article_reference_events",
        ["workspace_id", "id"],
    )
    op.create_index(
        "idx_tasks_ws_updated",
        "brand_tasks",
        ["workspace_id", "updated_at"],
    )
    op.create_index(
        "idx_users_ws_updated",
        "users",
        ["workspace_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_users_ws_updated", table_name="users")
    op.drop_index("idx_tasks_ws_updated", table_name="brand_tasks")
    op.drop_index("idx_are_ws_id", table_name="article_reference_events")
    op.drop_index("idx_cjobs_ws_updated", table_name="classification_jobs")
    op.drop_index("idx_tae_ws_id", table_name="task_assignment_events")
    op.drop_index("idx_sync_events_tds_task", table_name="sync_events")
    op.drop_index("idx_sync_events_ws_type_id", table_name="sync_events")
    op.drop_index("idx_articles_ws_updated_id", table_name="articles")
