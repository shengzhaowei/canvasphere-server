"""Billing center routes: wallet, usage/charge/sandbox records, 64+ price explanation & redeem codes."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, billing
from app.db import get_db
from app.models import (
    BillingChargeRecord,
    BillingSandboxRecord,
    BillingUsageRecord,
    RateCard,
    User,
)
from app.rate_cards_seed import ensure_rate_cards_seeded

router = APIRouter(tags=["billing"])


class RedeemCodeApplyRequest(BaseModel):
    code: str


class AdminRedeemCodeCreateRequest(BaseModel):
    credits_amount: float
    count: int = 1
    credit_pool: str = "bonus"  # bonus | cash
    max_uses: int = 1
    prefix: str = "PCN"


@router.get("/api/user/billing/wallet")
def get_billing_wallet(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    wallet = billing.get_or_create_wallet(db, user.id)
    db.commit()
    return billing.serialize_wallet(wallet)


@router.get("/api/user/billing/usage-records")
def list_usage_records(
    modality: Optional[str] = Query(default=None),
    payment_source: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(BillingUsageRecord).where(BillingUsageRecord.user_id == user.id)
    if modality:
        stmt = stmt.where(BillingUsageRecord.modality == modality.strip().lower())
    if payment_source:
        stmt = stmt.where(BillingUsageRecord.payment_source == payment_source.strip().lower())

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(BillingUsageRecord.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [
            {
                "id": r.id,
                "project_id": r.project_id,
                "job_id": r.job_id,
                "modality": r.modality,
                "model_key": r.model_key,
                "payment_source": r.payment_source,
                "quantity": r.quantity_json,
                "credits_charged": float(r.credits_charged),
                "bonus_charged": float(r.bonus_charged),
                "cash_charged": float(r.cash_charged),
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/api/user/billing/charge-records")
def list_charge_records(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(BillingChargeRecord).where(BillingChargeRecord.user_id == user.id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(BillingChargeRecord.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [
            {
                "id": r.id,
                "project_id": r.project_id,
                "charge_type": r.charge_type,
                "description": r.description,
                "credits_charged": float(r.credits_charged),
                "bonus_charged": float(r.bonus_charged),
                "cash_charged": float(r.cash_charged),
                "meta": r.meta_json,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/api/user/billing/sandbox-records")
def list_sandbox_records(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(BillingSandboxRecord).where(BillingSandboxRecord.user_id == user.id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(BillingSandboxRecord.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [
            {
                "id": r.id,
                "project_id": r.project_id,
                "spec_name": r.spec_name,
                "duration_seconds": float(r.duration_seconds),
                "credits_charged": float(r.credits_charged),
                "failure_free": bool(r.failure_free),
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/api/user/billing/price-explanation")
def get_price_explanation(
    modality: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    stmt = select(RateCard).where(RateCard.enabled.is_(True)).order_by(RateCard.sort_order.asc())
    if modality:
        stmt = stmt.where(RateCard.modality == modality.strip().lower())
    if search and search.strip():
        kw = f"%{search.strip()}%"
        stmt = stmt.where(RateCard.display_name.ilike(kw) | RateCard.model_key.ilike(kw))
    cards = db.scalars(stmt).all()
    items = [
        {
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
        }
        for c in cards
    ]
    by_modality: dict[str, int] = {}
    for it in items:
        by_modality[it["modality"]] = by_modality.get(it["modality"], 0) + 1
    return {
        "exchange_rate": "100 credits = 1 CNY",
        "total_enabled_rate_cards": len(items),
        "total": len(items),
        "counts_by_modality": by_modality,
        "rate_cards": items,
        "models": items,
    }


@router.post("/api/user/billing/redeem")
def apply_redeem_code(
    body: RedeemCodeApplyRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return billing.redeem_code(db, user.id, body.code)


@router.post("/api/admin/billing/redeem-codes", status_code=201)
def admin_create_redeem_codes(
    body: AdminRedeemCodeCreateRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if "admin" not in (user.roles or []):
        raise HTTPException(status_code=403, detail="admin role required")
    rows = billing.create_redeem_codes(
        db,
        credits_amount=Decimal(str(body.credits_amount)),
        count=body.count,
        credit_pool=body.credit_pool,
        max_uses=body.max_uses,
        prefix=body.prefix,
        created_by=user.id,
    )
    return {
        "codes": [
            {
                "id": r.id,
                "code": r.code,
                "credit_pool": r.credit_pool,
                "credits_amount": float(r.credits_amount),
                "max_uses": r.max_uses,
            }
            for r in rows
        ]
    }
