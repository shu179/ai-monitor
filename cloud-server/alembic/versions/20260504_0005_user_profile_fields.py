"""add cloud user profile fields

Revision ID: 0005_user_profile_fields
Revises: 0004_user_soft_delete
Create Date: 2026-05-04 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_user_profile_fields"
down_revision: Union[str, None] = "0004_user_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("birthday", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("hire_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "hire_date")
    op.drop_column("users", "birthday")
    op.drop_column("users", "avatar")
