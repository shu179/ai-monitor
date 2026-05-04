"""soft delete brand tasks

Revision ID: 0003_task_soft_delete
Revises: 0002_email_verification_codes
Create Date: 2026-05-04 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_task_soft_delete"
down_revision: Union[str, None] = "0002_email_verification_codes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("brand_tasks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("brand_tasks", sa.Column("delete_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("brand_tasks", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_brand_tasks_deleted_by_users",
        "brand_tasks",
        "users",
        ["deleted_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_tasks_workspace_deleted", "brand_tasks", ["workspace_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("idx_tasks_workspace_deleted", table_name="brand_tasks")
    op.drop_constraint("fk_brand_tasks_deleted_by_users", "brand_tasks", type_="foreignkey")
    op.drop_column("brand_tasks", "deleted_by")
    op.drop_column("brand_tasks", "delete_expires_at")
    op.drop_column("brand_tasks", "deleted_at")
