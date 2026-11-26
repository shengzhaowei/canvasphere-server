"""BYOK entitlement, encrypted API channels & canonical model offering routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, byok
from app.db import get_db
from app.models import ByokChannel, ByokOffering, User, utcnow

router = APIRouter(tags=["byok"])


class AdminByokEntitlementRequest(BaseModel):
    enabled: bool = True
    max_channels: int = 5
    max_offerings_per_channel: int = 20


class ChannelCreateRequest(BaseModel):
    name: str
    protocol_profile: str
    base_url: str
    api_key: str
    rpm_limit: int = 60


class ChannelPatchRequest(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    rpm_limit: Optional[int] = None
    enabled: Optional[bool] = None


class OfferingCreateRequest(BaseModel):
    channel_id: str
    canonical_model_key: str
    upstream_model_id: str


@router.get("/api/user/model-platform/byok-entitlement")
def get_my_byok_entitlement(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return byok.get_entitlement_view(db, user.id)


@router.put("/api/admin/model-platform/byok-entitlement/{target_user_id}")
def admin_set_byok_entitlement(
    target_user_id: str,
    body: AdminByokEntitlementRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if "admin" not in (user.roles or []):
        raise HTTPException(status_code=403, detail="admin role required")
    if db.get(User, target_user_id) is None:
        raise HTTPException(status_code=404, detail="target user not found")
    return byok.set_entitlement(
        db,
        target_user_id,
        enabled=body.enabled,
        max_channels=body.max_channels,
        max_offerings_per_channel=body.max_offerings_per_channel,
    )


@router.get("/api/user/model-platform/channels")
def list_my_channels(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(ByokChannel).where(ByokChannel.user_id == user.id).order_by(ByokChannel.created_at.desc())
    ).all()
    return {"channels": [byok.serialize_channel(r) for r in rows], "total": len(rows)}


@router.post("/api/user/model-platform/channels", status_code=201)
def create_my_channel(
    body: ChannelCreateRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return byok.create_channel(
        db,
        user.id,
        name=body.name,
        protocol_profile=body.protocol_profile,
        base_url=body.base_url,
        api_key=body.api_key,
        rpm_limit=body.rpm_limit,
    )


@router.patch("/api/user/model-platform/channels/{channel_id}")
def update_my_channel(
    channel_id: str,
    body: ChannelPatchRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ch = db.get(ByokChannel, channel_id)
    if ch is None or ch.user_id != user.id:
        raise HTTPException(status_code=404, detail="BYOK channel not found")
    if body.name is not None:
        name = body.name.strip()
        if not name or len(name) > 128:
            raise HTTPException(status_code=400, detail="channel name must be 1..128 characters")
        ch.name = name
    if body.base_url is not None:
        ch.base_url = byok._validate_base_url(body.base_url)
    if body.api_key is not None:
        ch.api_key_encrypted = byok.encrypt_api_key(body.api_key)
        ch.api_key_masked = byok.mask_api_key(body.api_key)
    if body.rpm_limit is not None:
        if body.rpm_limit < 1 or body.rpm_limit > 10000:
            raise HTTPException(status_code=400, detail="rpm_limit must be 1..10000")
        ch.rpm_limit = int(body.rpm_limit)
    if body.enabled is not None:
        ch.enabled = bool(body.enabled)
    ch.updated_at = utcnow()
    db.add(ch)
    db.commit()
    return byok.serialize_channel(ch)


@router.delete("/api/user/model-platform/channels/{channel_id}")
def delete_my_channel(
    channel_id: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ch = db.get(ByokChannel, channel_id)
    if ch is None or ch.user_id != user.id:
        raise HTTPException(status_code=404, detail="BYOK channel not found")
    db.delete(ch)
    db.commit()
    return {"deleted": True, "channel_id": channel_id}


@router.get("/api/user/model-platform/offerings")
def list_my_offerings(
    channel_id: Optional[str] = None,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(ByokOffering).where(ByokOffering.user_id == user.id)
    if channel_id:
        stmt = stmt.where(ByokOffering.channel_id == channel_id)
    rows = db.scalars(stmt.order_by(ByokOffering.created_at.desc())).all()
    return {"offerings": [byok.serialize_offering(r) for r in rows], "total": len(rows)}


@router.post("/api/user/model-platform/offerings", status_code=201)
def create_my_offering(
    body: OfferingCreateRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return byok.create_offering(
        db,
        user.id,
        channel_id=body.channel_id,
        canonical_model_key=body.canonical_model_key,
        upstream_model_id=body.upstream_model_id,
    )


@router.post("/api/user/model-platform/offerings/{offering_id}/verify")
def verify_my_offering(
    offering_id: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return byok.verify_offering(db, user.id, offering_id)


@router.delete("/api/user/model-platform/offerings/{offering_id}")
def delete_my_offering(
    offering_id: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    off = db.get(ByokOffering, offering_id)
    if off is None or off.user_id != user.id:
        raise HTTPException(status_code=404, detail="BYOK offering not found")
    db.delete(off)
    db.commit()
    return {"deleted": True, "offering_id": offering_id}
