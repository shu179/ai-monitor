"""initial cloud schema

Revision ID: 0001_initial_cloud_schema
Revises:
Create Date: 2026-05-02 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_cloud_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    user_role = postgresql.ENUM("admin", "operator", "viewer", name="user_role", create_type=False)
    task_access_level = postgresql.ENUM("operate", "view", name="task_access_level", create_type=False)
    classification_status = postgresql.ENUM("unresolved", "resolved", "ignored", name="classification_status", create_type=False)
    postgresql.ENUM("admin", "operator", "viewer", name="user_role").create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("operate", "view", name="task_access_level").create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("unresolved", "resolved", "ignored", name="classification_status").create(op.get_bind(), checkfirst=True)

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("display_name", sa.String(length=128)),
        sa.Column("email", sa.String(length=256)),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("username"),
    )
    op.create_index("idx_users_workspace_role", "users", ["workspace_id", "role"])
    op.create_index("idx_admin_email_unique", "users", ["email"], unique=True, postgresql_where=sa.text("role = 'admin'"))

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("jti", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("jti"),
    )
    op.create_index("idx_refresh_tokens_user", "refresh_tokens", ["user_id"])

    op.create_table(
        "device_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(length=256), nullable=False),
        sa.Column("app_version", sa.String(length=32)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "device_id", name="uq_device_sessions_user_device"),
    )

    op.create_table(
        "brand_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("workspace_id", "task_key", name="uq_brand_tasks_workspace_task_key"),
    )
    op.create_index("idx_tasks_workspace", "brand_tasks", ["workspace_id"])

    op.create_table(
        "task_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_level", task_access_level, nullable=False),
        sa.Column("assigned_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("task_id", "user_id", name="uq_task_members_task_user"),
    )
    op.create_index("idx_task_members_workspace_user", "task_members", ["workspace_id", "user_id"])

    op.create_table(
        "task_assignment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("access_level", task_access_level, nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("assigned_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_task_assignment_events_task", "task_assignment_events", ["task_id", "created_at"])

    op.create_table(
        "run_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("executed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("keyword", sa.String(length=256), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("idx_rr_ws_task_time", "run_records", ["workspace_id", "task_id", "executed_at"])

    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512)),
        sa.Column("source", sa.String(length=128)),
        sa.Column("media_type", sa.String(length=32)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("workspace_id", "url_hash", name="uq_articles_workspace_url_hash"),
    )
    op.create_index("idx_articles_workspace_time", "articles", ["workspace_id", "created_at"])

    op.create_table(
        "article_task_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("article_id", sa.BigInteger(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Integer(), server_default="100", nullable=False),
        sa.Column("reason_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_article_task_links_confidence"),
        sa.UniqueConstraint("article_id", "task_id", name="uq_article_task_links_article_task"),
    )
    op.create_index("idx_article_task_links_task", "article_task_links", ["workspace_id", "task_id"])

    op.create_table(
        "article_reference_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("article_id", sa.BigInteger(), sa.ForeignKey("articles.id", ondelete="SET NULL")),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("record_day", sa.String(length=10), nullable=False),
        sa.Column("source_record_key", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("event_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("idx_article_ref_events_rank", "article_reference_events", ["workspace_id", "task_id", "url_hash", "record_day"])

    op.create_table(
        "classification_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("article_id", sa.BigInteger(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", classification_status, server_default="unresolved", nullable=False),
        sa.Column("reason_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("resolved_task_id", sa.Integer(), sa.ForeignKey("brand_tasks.id", ondelete="SET NULL")),
        sa.Column("resolved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_classification_jobs_workspace_status", "classification_jobs", ["workspace_id", "status"])

    op.create_table(
        "weather_data",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("data_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("workspace_id", "date", name="uq_weather_data_workspace_date"),
    )

    op.create_table(
        "update_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), server_default="stable", nullable=False),
        sa.Column("platform_key", sa.String(length=32), nullable=False),
        sa.Column("download_url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("version", "channel", "platform_key", name="uq_updates_version_channel_platform"),
    )

    op.create_table(
        "sync_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("idx_sync_events_workspace_id", "sync_events", ["workspace_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_sync_events_workspace_id", table_name="sync_events")
    op.drop_table("sync_events")
    op.drop_table("update_packages")
    op.drop_table("weather_data")
    op.drop_index("idx_classification_jobs_workspace_status", table_name="classification_jobs")
    op.drop_table("classification_jobs")
    op.drop_index("idx_article_ref_events_rank", table_name="article_reference_events")
    op.drop_table("article_reference_events")
    op.drop_index("idx_article_task_links_task", table_name="article_task_links")
    op.drop_table("article_task_links")
    op.drop_index("idx_articles_workspace_time", table_name="articles")
    op.drop_table("articles")
    op.drop_index("idx_rr_ws_task_time", table_name="run_records")
    op.drop_table("run_records")
    op.drop_index("idx_task_assignment_events_task", table_name="task_assignment_events")
    op.drop_table("task_assignment_events")
    op.drop_index("idx_task_members_workspace_user", table_name="task_members")
    op.drop_table("task_members")
    op.drop_index("idx_tasks_workspace", table_name="brand_tasks")
    op.drop_table("brand_tasks")
    op.drop_table("device_sessions")
    op.drop_index("idx_refresh_tokens_user", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("idx_admin_email_unique", table_name="users")
    op.drop_index("idx_users_workspace_role", table_name="users")
    op.drop_table("users")
    op.drop_table("workspaces")
    postgresql.ENUM(name="classification_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="task_access_level").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)
