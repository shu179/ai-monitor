"""email verification codes

Revision ID: 0002_email_verification_codes
Revises: 0001_initial_cloud_schema
Create Date: 2026-05-03 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_email_verification_codes"
down_revision: Union[str, None] = "0001_initial_cloud_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column("code_hash", sa.String(length=256), nullable=False),
        sa.Column("purpose", sa.String(length=32), server_default="admin_register", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "idx_email_codes_user_purpose",
        "email_verification_codes",
        ["user_id", "purpose", "created_at"],
    )
    op.create_index(
        "idx_email_codes_email_purpose",
        "email_verification_codes",
        ["email", "purpose", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_email_codes_email_purpose", table_name="email_verification_codes")
    op.drop_index("idx_email_codes_user_purpose", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")
