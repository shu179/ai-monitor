"""scope usernames to workspace

Revision ID: 0007_workspace_scoped_usernames
Revises: 0006_run_record_cursor_index
Create Date: 2026-05-05 00:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_workspace_scoped_usernames"
down_revision: Union[str, None] = "0006_run_record_cursor_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("users_username_key", "users", type_="unique")
    op.create_index(
        "idx_users_workspace_username_unique",
        "users",
        ["workspace_id", sa.text("lower(username)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_users_workspace_username_unique", table_name="users")
    op.create_unique_constraint("users_username_key", "users", ["username"])
