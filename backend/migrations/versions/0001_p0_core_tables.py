"""P0 core tables: auth, users, projects, rbac, canvases, tasks, storage.

Revision ID: 0001_p0_core_tables
Revises:
Create Date: 2026-11-26 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_p0_core_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("avatar_storage_id", sa.String(40), nullable=True),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "oauth_bindings",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_user_id", sa.String(128), nullable=False),
        sa.Column("provider_username", sa.String(128), nullable=True),
        sa.Column("provider_email", sa.String(255), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_uid"),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
    )
    op.create_index("ix_oauth_bindings_user_id", "oauth_bindings", ["user_id"])

    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index("ix_refresh_sessions_token_hash", "refresh_sessions", ["token_hash"], unique=True)

    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_email_verification_codes_user_id", "email_verification_codes", ["user_id"])
    op.create_index("ix_email_verification_codes_email", "email_verification_codes", ["email"])

    op.create_table(
        "projects",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("cover_type", sa.String(16), nullable=True),
        sa.Column("cover_storage_id", sa.String(40), nullable=True),
        sa.Column("owner_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_projects_status", "projects", ["status"])
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_index("ix_projects_last_activity_at", "projects", ["last_activity_at"])

    op.create_table(
        "project_members",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="editor"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    op.create_table(
        "project_invitations",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(96), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="editor"),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_invitations_project_id", "project_invitations", ["project_id"])
    op.create_index("ix_project_invitations_token", "project_invitations", ["token"], unique=True)

    op.create_table(
        "canvases",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default="默认画板"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canvases_project_id", "canvases", ["project_id"])

    op.create_table(
        "canvas_objects",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("w", sa.Float(), nullable=False, server_default="320"),
        sa.Column("h", sa.Float(), nullable=False, server_default="180"),
        sa.Column("rotation", sa.Float(), nullable=False, server_default="0"),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("asset_json", sa.JSON(), nullable=True),
        sa.Column("style_json", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canvas_objects_project_id", "canvas_objects", ["project_id"])
    op.create_index("ix_canvas_objects_canvas_id", "canvas_objects", ["canvas_id"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("agent", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("element_json", sa.JSON(), nullable=True),
        sa.Column("skills_json", sa.JSON(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mock", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("estimated_cost_json", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("auto_retry_stopped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("events_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_request_id", "tasks", ["request_id"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "project_approval_policies",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "tool", name="uq_project_tool_policy"),
    )
    op.create_index("ix_project_approval_policies_project_id", "project_approval_policies", ["project_id"])

    op.create_table(
        "storage_objects",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("space", sa.String(32), nullable=False),
        sa.Column("owner_user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_storage_objects_space", "storage_objects", ["space"])
    op.create_index("ix_storage_objects_owner_user_id", "storage_objects", ["owner_user_id"])
    op.create_index("ix_storage_objects_project_id", "storage_objects", ["project_id"])
    op.create_index("ix_storage_objects_status", "storage_objects", ["status"])


def downgrade() -> None:
    op.drop_table("storage_objects")
    op.drop_table("project_approval_policies")
    op.drop_table("tasks")
    op.drop_table("canvas_objects")
    op.drop_table("canvases")
    op.drop_table("project_invitations")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("email_verification_codes")
    op.drop_table("refresh_sessions")
    op.drop_table("oauth_bindings")
    op.drop_table("users")
