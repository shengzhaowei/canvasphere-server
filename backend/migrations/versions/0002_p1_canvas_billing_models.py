"""P1 tables: canvas connector/media columns, billing wallets, rate cards, redeem codes & generation jobs.

Revision ID: 0002_p1_canvas_billing_models
Revises: 0001_p0_core_tables
Create Date: 2026-11-26 21:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_p1_canvas_billing_models"
down_revision = "0001_p0_core_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("canvas_objects") as batch_op:
        batch_op.add_column(sa.Column("from_object_id", sa.String(40), nullable=True))
        batch_op.add_column(sa.Column("from_anchor", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("to_object_id", sa.String(40), nullable=True))
        batch_op.add_column(sa.Column("to_anchor", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("media_type", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_canvas_objects_from_object_id", ["from_object_id"])
        batch_op.create_index("ix_canvas_objects_to_object_id", ["to_object_id"])
        batch_op.create_index("ix_canvas_objects_media_type", ["media_type"])

    op.create_table(
        "credit_wallets",
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("bonus_credits", sa.Numeric(18, 4), nullable=False, server_default="300.0000"),
        sa.Column("cash_credits", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("reserved_bonus_credits", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("reserved_cash_credits", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "credit_reservations",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("estimated_credits", sa.Numeric(18, 4), nullable=False),
        sa.Column("reserved_bonus", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("reserved_cash", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("settled_bonus", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("settled_cash", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("status", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credit_reservations_user_id", "credit_reservations", ["user_id"])
    op.create_index("ix_credit_reservations_source_id", "credit_reservations", ["source_id"])
    op.create_index("ix_credit_reservations_status", "credit_reservations", ["status"])

    op.create_table(
        "rate_cards",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("model_key", sa.String(128), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("provider_family", sa.String(64), nullable=False),
        sa.Column("protocol_profile", sa.String(64), nullable=False),
        sa.Column("billing_mode", sa.String(32), nullable=False),
        sa.Column("unit_price_credits", sa.Numeric(18, 4), nullable=False),
        sa.Column("output_price_credits", sa.Numeric(18, 4), nullable=True),
        sa.Column("pricing_meta_json", sa.JSON(), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_rate_cards_model_key", "rate_cards", ["model_key"], unique=True)
    op.create_index("ix_rate_cards_modality", "rate_cards", ["modality"])
    op.create_index("ix_rate_cards_enabled", "rate_cards", ["enabled"])

    op.create_table(
        "billing_usage_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_id", sa.String(40), nullable=True),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("model_key", sa.String(128), nullable=False),
        sa.Column("payment_source", sa.String(32), nullable=False, server_default="platform_credits"),
        sa.Column("quantity_json", sa.JSON(), nullable=False),
        sa.Column("credits_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("bonus_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("cash_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("status", sa.String(24), nullable=False, server_default="settled"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_usage_records_user_id", "billing_usage_records", ["user_id"])
    op.create_index("ix_billing_usage_records_project_id", "billing_usage_records", ["project_id"])
    op.create_index("ix_billing_usage_records_job_id", "billing_usage_records", ["job_id"])
    op.create_index("ix_billing_usage_records_modality", "billing_usage_records", ["modality"])
    op.create_index("ix_billing_usage_records_payment_source", "billing_usage_records", ["payment_source"])
    op.create_index("ix_billing_usage_records_created_at", "billing_usage_records", ["created_at"])

    op.create_table(
        "billing_charge_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("charge_type", sa.String(32), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("credits_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("bonus_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("cash_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_charge_records_user_id", "billing_charge_records", ["user_id"])
    op.create_index("ix_billing_charge_records_project_id", "billing_charge_records", ["project_id"])
    op.create_index("ix_billing_charge_records_created_at", "billing_charge_records", ["created_at"])

    op.create_table(
        "billing_sandbox_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("spec_name", sa.String(32), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("credits_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("failure_free", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(24), nullable=False, server_default="settled"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_sandbox_records_user_id", "billing_sandbox_records", ["user_id"])
    op.create_index("ix_billing_sandbox_records_project_id", "billing_sandbox_records", ["project_id"])
    op.create_index("ix_billing_sandbox_records_created_at", "billing_sandbox_records", ["created_at"])

    op.create_table(
        "redeem_codes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("credit_pool", sa.String(16), nullable=False, server_default="bonus"),
        sa.Column("credits_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_redeem_codes_code", "redeem_codes", ["code"], unique=True)

    op.create_table(
        "redeem_code_redemptions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("redeem_code_id", sa.String(40), sa.ForeignKey("redeem_codes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credits_credited", sa.Numeric(18, 4), nullable=False),
        sa.Column("credit_pool", sa.String(16), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("redeem_code_id", "user_id", name="uq_redeem_code_user"),
    )
    op.create_index("ix_redeem_code_redemptions_redeem_code_id", "redeem_code_redemptions", ["redeem_code_id"])
    op.create_index("ix_redeem_code_redemptions_user_id", "redeem_code_redemptions", ["user_id"])

    op.create_table(
        "user_voice_activations",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("voice_id", sa.String(128), nullable=False),
        sa.Column("credits_charged", sa.Numeric(18, 4), nullable=False, server_default="0.0000"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "voice_id", name="uq_user_voice_activation"),
    )
    op.create_index("ix_user_voice_activations_user_id", "user_voice_activations", ["user_id"])

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canvas_id", sa.String(40), sa.ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_object_id", sa.String(40), sa.ForeignKey("canvas_objects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("parent_job_id", sa.String(40), nullable=True),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("model_key", sa.String(128), nullable=False),
        sa.Column("protocol_profile", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("input_object_ids_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reservation_id", sa.String(40), nullable=True),
        sa.Column("payment_source", sa.String(32), nullable=False, server_default="platform_credits"),
        sa.Column("mock", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_generation_jobs_request_id", "generation_jobs", ["request_id"])
    op.create_index("ix_generation_jobs_user_id", "generation_jobs", ["user_id"])
    op.create_index("ix_generation_jobs_project_id", "generation_jobs", ["project_id"])
    op.create_index("ix_generation_jobs_canvas_id", "generation_jobs", ["canvas_id"])
    op.create_index("ix_generation_jobs_parent_job_id", "generation_jobs", ["parent_job_id"])
    op.create_index("ix_generation_jobs_modality", "generation_jobs", ["modality"])
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("generation_jobs")
    op.drop_table("user_voice_activations")
    op.drop_table("redeem_code_redemptions")
    op.drop_table("redeem_codes")
    op.drop_table("billing_sandbox_records")
    op.drop_table("billing_charge_records")
    op.drop_table("billing_usage_records")
    op.drop_table("rate_cards")
    op.drop_table("credit_reservations")
    op.drop_table("credit_wallets")

    with op.batch_alter_table("canvas_objects") as batch_op:
        batch_op.drop_index("ix_canvas_objects_media_type")
        batch_op.drop_index("ix_canvas_objects_to_object_id")
        batch_op.drop_index("ix_canvas_objects_from_object_id")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("media_type")
        batch_op.drop_column("to_anchor")
        batch_op.drop_column("to_object_id")
        batch_op.drop_column("from_anchor")
        batch_op.drop_column("from_object_id")
