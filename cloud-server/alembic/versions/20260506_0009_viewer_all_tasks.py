"""add viewer all-tasks visibility flag

Revision ID: 0009_viewer_all_tasks
Revises: 0008_global_active_usernames
Create Date: 2026-05-06 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_viewer_all_tasks"
down_revision: Union[str, None] = "0008_global_active_usernames"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("view_all_tasks", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "view_all_tasks")
