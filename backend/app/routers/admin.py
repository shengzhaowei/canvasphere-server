"""Admin management routes: users, rate cards CRUD & platform billing overview."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import auth, billing
from app.db import get_db
from app.models import (
    BillingUsageRecord,
    CreditWallet,
    GenerationJob,
    Project,
    RateCard,
    SandboxSession,
    User,
    new_prefixed_id,
    utcnow,
)
from app.rate_cards_seed import ensure_rate_cards_seeded

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(user: User = Depends(auth.get_current_user)) -> User:
    if "admin" not in (user.roles or []):
        raise HTTPException(status_code=403, detail="admin role required")
    return user


class AdminUserPatchRequest(BaseModel):
    is_active: Optional[bool] = None
    roles: Optional[list[str]] = None
    display_name: Optional[str] = None


class AdminRateCardCreateRequest(BaseModel):
    model_key: str
    display_name: str
    modality: str
    provider_family: str
    protocol_profile: str
    billing_mode: str
    unit_price_credits: float
    output_price_credits: Optional[float] = None
    pricing_meta: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sort_order: int = 999


class AdminRateCardPatchRequest(BaseModel):
    display_name: Optional[str] = None
    unit_price_credits: Optional[float] = None
    output_price_credits: Optional[float] = None
    pricing_meta: Optional[dict[str, Any]] = None
    capabilities: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


def _serialize_card(c: RateCard) -> dict[str, Any]:
    return {
        "id": c.id,
        "model_key": c.model_key,
        "display_name": c.display_name,
        "modality": c.modality,
        "provider_family": c.provider_family,
        "protocol_profile": c.protocol_profile,
        "billing_mode": c.billing_mode,
        "unit_price_credits": float(c.unit_price_credits),
        "output_price_credits": float(c.output_price_credits) if c.output_price_credits is not None else None,
        "pricing_meta": c.pricing_meta_json,
        "capabilities": c.capabilities_json,
        "enabled": bool(c.enabled),
        "sort_order": int(c.sort_order),
    }


@router.get("/users")
def admin_list_users(
    keyword: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(User)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(kw), User.display_name.ilike(kw), User.id.ilike(kw)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(User.created_at.desc()).offset((page - 1) * size).limit(size)
    ).all()
    items: list[dict[str, Any]] = []
    for u in rows:
        w = db.get(CreditWallet, u.id)
        items.append({
            **auth.serialize_user_profile(u),
            "wallet": billing.serialize_wallet(w) if w else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return {"items": items, "total": total, "page": page, "size": size}


@router.patch("/users/{user_id}")
def admin_patch_user(
    user_id: str,
    body: AdminUserPatchRequest,
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    revoked = False
    if body.is_active is not None:
        target.is_active = bool(body.is_active)
        if not target.is_active:
            revoked = True
    if body.roles is not None:
        clean_roles = [r.strip().lower() for r in body.roles if r.strip()]
        if not clean_roles:
            raise HTTPException(status_code=400, detail="roles must not be empty")
        target.roles = clean_roles
    if body.display_name is not None:
        name = body.display_name.strip()
        if not name or len(name) > 64:
            raise HTTPException(status_code=400, detail="display_name must be 1..64 characters")
        target.display_name = name
    target.updated_at = utcnow()
    db.add(target)
    if revoked:
        auth.revoke_all_user_sessions(db, target)
    else:
        db.commit()
    return auth.serialize_user_profile(target)


@router.get("/rate-cards")
def admin_list_rate_cards(
    modality: Optional[str] = Query(default=None),
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    stmt = select(RateCard).order_by(RateCard.sort_order.asc())
    if modality:
        stmt = stmt.where(RateCard.modality == modality.strip().lower())
    cards = db.scalars(stmt).all()
    return {"items": [_serialize_card(c) for c in cards], "total": len(cards)}


@router.post("/rate-cards", status_code=201)
def admin_create_rate_card(
    body: AdminRateCardCreateRequest,
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    if db.scalar(select(RateCard).where(RateCard.model_key == body.model_key)) is not None:
        raise HTTPException(status_code=409, detail=f"rate card already exists: {body.model_key}")
    card = RateCard(
        id=new_prefixed_id("rc"),
        model_key=body.model_key.strip(),
        display_name=body.display_name.strip(),
        modality=body.modality.strip().lower(),
        provider_family=body.provider_family.strip(),
        protocol_profile=body.protocol_profile.strip(),
        billing_mode=body.billing_mode.strip(),
        unit_price_credits=Decimal(str(body.unit_price_credits)),
        output_price_credits=Decimal(str(body.output_price_credits)) if body.output_price_credits is not None else None,
        pricing_meta_json=dict(body.pricing_meta),
        capabilities_json=dict(body.capabilities),
        enabled=bool(body.enabled),
        sort_order=int(body.sort_order),
    )
    db.add(card)
    db.commit()
    return _serialize_card(card)


@router.patch("/rate-cards/{model_key}")
def admin_patch_rate_card(
    model_key: str,
    body: AdminRateCardPatchRequest,
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    card = db.scalar(select(RateCard).where(RateCard.model_key == model_key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"rate card not found: {model_key}")
    if body.display_name is not None:
        card.display_name = body.display_name.strip()
    if body.unit_price_credits is not None:
        if body.unit_price_credits < 0:
            raise HTTPException(status_code=400, detail="unit_price_credits must be >= 0")
        card.unit_price_credits = Decimal(str(body.unit_price_credits))
    if body.output_price_credits is not None:
        card.output_price_credits = Decimal(str(body.output_price_credits))
    if body.pricing_meta is not None:
        card.pricing_meta_json = dict(body.pricing_meta)
    if body.capabilities is not None:
        card.capabilities_json = dict(body.capabilities)
    if body.enabled is not None:
        card.enabled = bool(body.enabled)
    if body.sort_order is not None:
        card.sort_order = int(body.sort_order)
    db.add(card)
    db.commit()
    return _serialize_card(card)


@router.get("/billing/overview")
def admin_billing_overview(
    _admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    total_users = int(db.scalar(select(func.count(User.id))) or 0)
    total_projects = int(db.scalar(select(func.count(Project.id)).where(Project.status != "deleted")) or 0)
    active_sandboxes = int(db.scalar(select(func.count(SandboxSession.id)).where(SandboxSession.status == "active")) or 0)

    job_rows = db.execute(
        select(GenerationJob.status, func.count(GenerationJob.id)).group_by(GenerationJob.status)
    ).all()
    jobs_by_status = {status: int(cnt) for status, cnt in job_rows}

    usage_rows = db.execute(
        select(
            BillingUsageRecord.modality,
            func.coalesce(func.sum(BillingUsageRecord.credits_charged), 0),
        ).group_by(BillingUsageRecord.modality)
    ).all()
    credits_by_modality = {mod: float(total) for mod, total in usage_rows}

    return {
        "total_users": total_users,
        "total_projects": total_projects,
        "active_sandboxes": active_sandboxes,
        "generation_jobs_by_status": jobs_by_status,
        "credits_charged_by_modality": credits_by_modality,
        "total_credits_charged": round(sum(credits_by_modality.values()), 4),
    }
