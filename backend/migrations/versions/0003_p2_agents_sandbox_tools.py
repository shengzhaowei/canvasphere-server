"""P2 tables: agent sessions/messages, TaskRun leases/fences, sandbox config/sessions & asset tool jobs.

Revision ID: 0003_p2_agents_sandbox_tools
Revises: 0002_p1_canvas_billing_models
Create Date: 2026-11-26 22:36:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_p2_agents_sandbox_tools"
down_revision = "0002_p1_canvas_billing_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(128), nullable=False, server_default="Pecan Studio 会话"),
        sa.Column("context_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_storage_id", sa.String(40), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_sessions_project_id", "agent_sessions", ["project_id"])
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("session_id", sa.String(40), sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("skills_json", sa.JSON(), nullable=False),
        sa.Column("context_object_ids_json", sa.JSON(), nullable=False),
        sa.Column("tool_calls_json", sa.JSON(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_messages_session_id", "agent_messages", ["session_id"])
    op.create_index("ix_agent_messages_compressed", "agent_messages", ["compressed"])

    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False, unique=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", sa.String(40), sa.ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("primary_agent", sa.String(32), nullable=False, server_default="pecan"),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("generation_fence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_tree_json", sa.JSON(), nullable=False),
        sa.Column("pending_tool_json", sa.JSON(), nullable=True),
        sa.Column("recovery_bundle_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_runs_request_id", "task_runs", ["request_id"], unique=True)
    op.create_index("ix_task_runs_project_id", "task_runs", ["project_id"])
    op.create_index("ix_task_runs_session_id", "task_runs", ["session_id"])
    op.create_index("ix_task_runs_status", "task_runs", ["status"])

    op.create_table(
        "project_sandbox_configs",
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("spec_name", sa.String(32), nullable=False, server_default="2核4G"),
        sa.Column("spend_cap_credits", sa.Numeric(18, 4), nullable=False, server_default="500.0000"),
        sa.Column("idle_keep_minutes", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("spent_credits", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sandbox_sessions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("spec_name", sa.String(32), nullable=False, server_default="2核4G"),
        sa.Column("provider", sa.String(32), nullable=False, server_default="local-docker"),
        sa.Column("container_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("credits_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("failure_free", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_sandbox_sessions_project_id", "sandbox_sessions", ["project_id"])
    op.create_index("ix_sandbox_sessions_user_id", "sandbox_sessions", ["user_id"])
    op.create_index("ix_sandbox_sessions_status", "sandbox_sessions", ["status"])

    op.create_table(
        "asset_tool_jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_asset_tool_jobs_project_id", "asset_tool_jobs", ["project_id"])
    op.create_index("ix_asset_tool_jobs_canvas_id", "asset_tool_jobs", ["canvas_id"])
    op.create_index("ix_asset_tool_jobs_tool_name", "asset_tool_jobs", ["tool_name"])


def downgrade() -> None:
    op.drop_table("asset_tool_jobs")
    op.drop_table("sandbox_sessions")
    op.drop_table("project_sandbox_configs")
    op.drop_table("task_runs")
    op.drop_table("agent_messages")
    op.drop_table("agent_sessions")
