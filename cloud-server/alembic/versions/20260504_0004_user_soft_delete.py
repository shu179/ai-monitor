"""soft delete workspace users

Revision ID: 0004_user_soft_delete
Revises: 0003_task_soft_delete
Create Date: 2026-05-04 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_user_soft_delete"
down_revision: Union[str, None] = "0003_task_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("idx_users_workspace_deleted", "users", ["workspace_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("idx_users_workspace_deleted", table_name="users")
    op.drop_column("users", "deleted_at")
