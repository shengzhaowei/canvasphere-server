"""P3 tables: user skills, project installed skills, MCP OAuth DCR/grants & BYOK channels/offerings.

Revision ID: 0004_p3_skills_mcp_byok
Revises: 0003_p2_agents_sandbox_tools
Create Date: 2026-11-26 22:58:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_p3_skills_mcp_byok"
down_revision = "0003_p2_agents_sandbox_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_skills",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(32), nullable=False, server_default="未分类"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("frontmatter_json", sa.JSON(), nullable=False),
        sa.Column("files_json", sa.JSON(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_user_skill_owner_name"),
    )
    op.create_index("ix_user_skills_owner_user_id", "user_skills", ["owner_user_id"])
    op.create_index("ix_user_skills_name", "user_skills", ["name"])
    op.create_index("ix_user_skills_category", "user_skills", ["category"])

    op.create_table(
        "project_installed_skills",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.String(40), sa.ForeignKey("user_skills.id", ondelete="SET NULL"), nullable=True),
        sa.Column("skill_name", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("installed_by", sa.String(40), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "skill_name", name="uq_project_installed_skill"),
    )
    op.create_index("ix_project_installed_skills_project_id", "project_installed_skills", ["project_id"])

    op.create_table(
        "mcp_oauth_clients",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("client_id", sa.String(96), nullable=False, unique=True),
        sa.Column("client_secret_hash", sa.String(64), nullable=True),
        sa.Column("client_name", sa.String(128), nullable=False),
        sa.Column("redirect_uris_json", sa.JSON(), nullable=False),
        sa.Column("grant_types_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_oauth_clients_client_id", "mcp_oauth_clients", ["client_id"], unique=True)

    op.create_table(
        "mcp_oauth_codes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(96), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("server_type", sa.String(24), nullable=False, server_default="canvas"),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=True),
        sa.Column("code_challenge_method", sa.String(16), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_oauth_codes_code_hash", "mcp_oauth_codes", ["code_hash"], unique=True)
    op.create_index("ix_mcp_oauth_codes_client_id", "mcp_oauth_codes", ["client_id"])

    op.create_table(
        "mcp_grants",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("client_id", sa.String(96), nullable=False),
        sa.Column("client_name", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(40), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("server_type", sa.String(24), nullable=False, server_default="canvas"),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_grants_client_id", "mcp_grants", ["client_id"])
    op.create_index("ix_mcp_grants_user_id", "mcp_grants", ["user_id"])
    op.create_index("ix_mcp_grants_project_id", "mcp_grants", ["project_id"])
    op.create_index("ix_mcp_grants_token_hash", "mcp_grants", ["token_hash"], unique=True)

    op.create_table(
        "byok_entitlements",
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_channels", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_offerings_per_channel", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "byok_channels",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("protocol_profile", sa.String(64), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_masked", sa.String(32), nullable=False),
        sa.Column("rpm_limit", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_byok_channels_user_id", "byok_channels", ["user_id"])

    op.create_table(
        "byok_offerings",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(40), sa.ForeignKey("byok_channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_model_key", sa.String(128), nullable=False),
        sa.Column("upstream_model_id", sa.String(128), nullable=False),
        sa.Column("verified_status", sa.String(24), nullable=False, server_default="unverified"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_error", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "canonical_model_key", name="uq_byok_channel_model"),
    )
    op.create_index("ix_byok_offerings_user_id", "byok_offerings", ["user_id"])
    op.create_index("ix_byok_offerings_channel_id", "byok_offerings", ["channel_id"])


def downgrade() -> None:
    op.drop_table("byok_offerings")
    op.drop_table("byok_channels")
    op.drop_table("byok_entitlements")
    op.drop_table("mcp_grants")
    op.drop_table("mcp_oauth_codes")
    op.drop_table("mcp_oauth_clients")
    op.drop_table("project_installed_skills")
    op.drop_table("user_skills")
