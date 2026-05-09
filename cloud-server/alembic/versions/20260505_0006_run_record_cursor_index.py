"""add run record cursor index

Revision ID: 0006_run_record_cursor_index
Revises: 0005_user_profile_fields
Create Date: 2026-05-05 00:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_run_record_cursor_index"
down_revision: Union[str, None] = "0005_user_profile_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_rr_ws_task_id", "run_records", ["workspace_id", "task_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_rr_ws_task_id", table_name="run_records")
