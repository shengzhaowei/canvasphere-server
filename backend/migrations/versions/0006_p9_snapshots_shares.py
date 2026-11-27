"""P9: project description, canvas_objects locked/group_id, snapshots, comments & public read-only shares.

Revision ID: 0006_p9_snapshots_shares
Revises: 0005_p5_provider_external_tasks
Create Date: 2026-11-27 21:50:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_p9_snapshots_shares"
down_revision = "0005_p5_provider_external_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))

    with op.batch_alter_table("canvas_objects") as batch_op:
        batch_op.add_column(sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("group_id", sa.String(40), nullable=True))
        batch_op.create_index("ix_canvas_objects_group_id", ["group_id"])

    op.create_table(
        "canvas_snapshots",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("objects_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canvas_snapshots_project_id", "canvas_snapshots", ["project_id"])
    op.create_index("ix_canvas_snapshots_canvas_id", "canvas_snapshots", ["canvas_id"])
    op.create_index("ix_canvas_snapshots_created_at", "canvas_snapshots", ["created_at"])

    op.create_table(
        "canvas_comments",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("object_id", sa.String(40), sa.ForeignKey("canvas_objects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("parent_id", sa.String(40), nullable=True),
        sa.Column("author_user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("anchor_x", sa.Float(), nullable=True),
        sa.Column("anchor_y", sa.Float(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canvas_comments_project_id", "canvas_comments", ["project_id"])
    op.create_index("ix_canvas_comments_canvas_id", "canvas_comments", ["canvas_id"])
    op.create_index("ix_canvas_comments_object_id", "canvas_comments", ["object_id"])
    op.create_index("ix_canvas_comments_parent_id", "canvas_comments", ["parent_id"])
    op.create_index("ix_canvas_comments_resolved", "canvas_comments", ["resolved"])

    op.create_table(
        "canvas_public_shares",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(96), unique=True, nullable=False),
        sa.Column("title", sa.String(128), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canvas_public_shares_project_id", "canvas_public_shares", ["project_id"])
    op.create_index("ix_canvas_public_shares_canvas_id", "canvas_public_shares", ["canvas_id"])
    op.create_index("ix_canvas_public_shares_token", "canvas_public_shares", ["token"], unique=True)


def downgrade() -> None:
    op.drop_table("canvas_public_shares")
    op.drop_table("canvas_comments")
    op.drop_table("canvas_snapshots")

    with op.batch_alter_table("canvas_objects") as batch_op:
        batch_op.drop_index("ix_canvas_objects_group_id")
        batch_op.drop_column("group_id")
        batch_op.drop_column("locked")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("description")
