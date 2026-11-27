"""P5: add external_task_id & byok_channel_id to generation_jobs for async provider callbacks & BYOK routing.

Revision ID: 0005_p5_provider_external_tasks
Revises: 0004_p3_skills_mcp_byok
Create Date: 2026-11-27 09:07:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_p5_provider_external_tasks"
down_revision = "0004_p3_skills_mcp_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(sa.Column("external_task_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("byok_channel_id", sa.String(40), nullable=True))
        batch_op.create_index("ix_generation_jobs_external_task_id", ["external_task_id"])


def downgrade() -> None:
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_index("ix_generation_jobs_external_task_id")
        batch_op.drop_column("byok_channel_id")
        batch_op.drop_column("external_task_id")
