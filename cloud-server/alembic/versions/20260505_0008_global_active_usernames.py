"""enforce globally unique active usernames

Revision ID: 0008_global_active_usernames
Revises: 0007_workspace_scoped_usernames
Create Date: 2026-05-05 23:20:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_global_active_usernames"
down_revision: Union[str, None] = "0007_workspace_scoped_usernames"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_workspace_username_unique")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique "
        "ON users (lower(username)) "
        "WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_username_unique")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_workspace_username_unique "
        "ON users (workspace_id, lower(username)) "
        "WHERE deleted_at IS NULL"
    )
