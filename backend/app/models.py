"""Core SQLAlchemy 2.0 ORM models for Phase P0 + P1.

Prefixed IDs mirror Pecan Studio conventions (usr_, prj_, wsp_, can_, obj_,
inv_, tsk_, sto_, res_, rc_, usg_, chg_, sbx_, rdc_, gen_).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("usr"))
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    avatar_storage_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["user"], nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class OAuthBinding(Base):
    __tablename__ = "oauth_bindings"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_uid"),
        UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("oab"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    provider_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("rfs"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("evc"))
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("prj"))
    workspace_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, default=lambda: new_prefixed_id("wsp"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    cover_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cover_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cover_storage_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("mbr"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="editor")  # owner | editor | viewer
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectInvitation(Base):
    __tablename__ = "project_invitations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("inv"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(96), unique=True, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="editor")
    created_by: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Canvas(Base):
    __tablename__ = "canvases"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("can"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="默认画板")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class CanvasObject(Base):
    __tablename__ = "canvas_objects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("obj"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[str] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    x: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    y: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w: Mapped[float] = mapped_column(Float, default=320.0, nullable=False)
    h: Mapped[float] = mapped_column(Float, default=180.0, nullable=False)
    rotation: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    z_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    asset_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    style_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # P1 connector & media search extensions
    from_object_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    from_anchor: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    to_object_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    to_anchor: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("tsk"))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    agent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    element_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    skills_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    usage_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    estimated_cost_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auto_retry_stopped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    events_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ProjectApprovalPolicy(Base):
    __tablename__ = "project_approval_policies"
    __table_args__ = (
        UniqueConstraint("project_id", "tool", name="uq_project_tool_policy"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("pol"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(24), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class StorageObject(Base):
    __tablename__ = "storage_objects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("sto"))
    space: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # avatars | drafts | workspace | private_runtime
    owner_user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)  # pending | uploaded | deleted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Phase P1: Billing, Credits, RateCards & Redeem Codes ----------------
class CreditWallet(Base):
    __tablename__ = "credit_wallets"

    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    bonus_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("300.0000"), nullable=False)
    cash_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    reserved_bonus_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    reserved_cash_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class CreditReservation(Base):
    __tablename__ = "credit_reservations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("res"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # generation | llm | sandbox | service
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    estimated_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reserved_bonus: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    reserved_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    settled_bonus: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    settled_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="reserved", nullable=False, index=True)  # reserved | settled | released
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RateCard(Base):
    __tablename__ = "rate_cards"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("rc"))
    model_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    modality: Mapped[str] = mapped_column(String(24), nullable=False, index=True)  # text | image | video | tts | asr | music
    provider_family: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)  # per_million_tokens | per_image | per_second | per_call | per_operation
    unit_price_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    output_price_credits: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    pricing_meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BillingUsageRecord(Base):
    __tablename__ = "billing_usage_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("usg"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    modality: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_source: Mapped[str] = mapped_column(String(32), default="platform_credits", nullable=False, index=True)  # platform_credits | byok
    quantity_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    bonus_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    cash_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="settled", nullable=False)  # settled | mock_free | refunded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class BillingChargeRecord(Base):
    __tablename__ = "billing_charge_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("chg"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    charge_type: Mapped[str] = mapped_column(String(32), nullable=False)  # voice_first_use | storage | service
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    bonus_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    cash_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class BillingSandboxRecord(Base):
    __tablename__ = "billing_sandbox_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("sbx"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    spec_name: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    failure_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="settled", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("rdc"))
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    credit_pool: Mapped[str] = mapped_column(String(16), default="bonus", nullable=False)  # bonus | cash
    credits_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RedeemCodeRedemption(Base):
    __tablename__ = "redeem_code_redemptions"
    __table_args__ = (
        UniqueConstraint("redeem_code_id", "user_id", name="uq_redeem_code_user"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("rdr"))
    redeem_code_id: Mapped[str] = mapped_column(String(40), ForeignKey("redeem_codes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credits_credited: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    credit_pool: Mapped[str] = mapped_column(String(16), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserVoiceActivation(Base):
    __tablename__ = "user_voice_activations"
    __table_args__ = (
        UniqueConstraint("user_id", "voice_id", name="uq_user_voice_activation"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("vac"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    voice_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("gen"))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[str] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False, index=True)
    target_object_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("canvas_objects.id", ondelete="SET NULL"), nullable=True)
    parent_job_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    modality: Mapped[str] = mapped_column(String(24), nullable=False, index=True)  # image | video | audio | music
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    input_object_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)  # queued | running | succeeded | failed
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reservation_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    payment_source: Mapped[str] = mapped_column(String(32), default="platform_credits", nullable=False)
    mock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Phase P2: Multi-Agent Sessions, TaskRun Leases, Sandbox & Asset Tools
class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("ses"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="Pecan Studio 会话", nullable=False)
    context_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_storage_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    compressed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("msg"))
    session_id: Mapped[str] = mapped_column(String(40), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False)  # user | assistant | tool | system
    agent_name: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # pecan | orange | xiaoa | blueberry | yueyue
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    skills_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_object_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tool_calls_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    compressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("run"))
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    primary_agent: Mapped[str] = mapped_column(String(32), default="pecan", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    generation_fence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_tree_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pending_tool_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    recovery_bundle_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectSandboxConfig(Base):
    __tablename__ = "project_sandbox_configs"

    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    spec_name: Mapped[str] = mapped_column(String(32), default="2核4G", nullable=False)
    spend_cap_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("500.0000"), nullable=False)
    idle_keep_minutes: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    spent_credits: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SandboxSession(Base):
    __tablename__ = "sandbox_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("sbs"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    spec_name: Mapped[str] = mapped_column(String(32), default="2核4G", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="local-docker", nullable=False)
    container_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)  # active | idle | stopped | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    failure_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AssetToolJob(Base):
    __tablename__ = "asset_tool_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_prefixed_id("atj"))
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    canvas_id: Mapped[str] = mapped_column(String(40), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
