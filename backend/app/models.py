"""Core SQLAlchemy 2.0 ORM models for Phase P0.

Prefixed IDs mirror Pecan Studio conventions (usr_, prj_, wsp_, can_, obj_,
inv_, tsk_, sto_) while preserving compatibility with legacy Canvasphere IDs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
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
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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
