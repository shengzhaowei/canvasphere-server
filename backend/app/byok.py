"""BYOK (Bring Your Own Key) model platform with Fernet write-only API key encryption.

Features aligned with ai.pecan.wang:
- Admin-gated entitlement (defaults to BYOK_ENTITLEMENT_NOT_CONFIGURED, max_channels=0)
- Write-only encrypted API keys (Fernet AES-128-CBC + HMAC-SHA256) — never returned in plaintext
- Channels bound to standard Protocol Profiles + per-user RPM limits
- Offerings mapping upstream models to CanonicalModels (RateCard.model_key) with verification
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.model_platform import PROTOCOL_PROFILES
from app.models import ByokChannel, ByokEntitlement, ByokOffering, RateCard, new_prefixed_id, utcnow
from app.rate_cards_seed import ensure_rate_cards_seeded

_VALID_PROFILES = {p["profile_key"] for p in PROTOCOL_PROFILES}


def _fernet() -> Fernet:
    settings = get_settings()
    if settings.byok_encryption_key:
        try:
            return Fernet(settings.byok_encryption_key.encode("utf-8"))
        except Exception:
            pass
    # Deterministically derive a valid 32-byte Fernet key from JWT_SECRET
    digest = hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(plaintext: str) -> str:
    if not plaintext or not plaintext.strip():
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    return _fernet().encrypt(plaintext.strip().encode("utf-8")).decode("utf-8")


def decrypt_api_key(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def mask_api_key(plaintext: str) -> str:
    s = (plaintext or "").strip()
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}****{s[-4:]}"


def get_entitlement_view(db: Session, user_id: str) -> dict[str, Any]:
    ent = db.get(ByokEntitlement, user_id)
    if ent is None or not ent.enabled:
        return {
            "user_id": user_id,
            "enabled": False,
            "code": "BYOK_ENTITLEMENT_NOT_CONFIGURED",
            "max_channels": 0,
            "max_offerings_per_channel": 0,
        }
    return {
        "user_id": user_id,
        "enabled": True,
        "code": "OK",
        "max_channels": int(ent.max_channels),
        "max_offerings_per_channel": int(ent.max_offerings_per_channel),
        "updated_at": ent.updated_at.isoformat() if ent.updated_at else None,
    }


def set_entitlement(
    db: Session,
    user_id: str,
    *,
    enabled: bool,
    max_channels: int,
    max_offerings_per_channel: int,
) -> dict[str, Any]:
    if max_channels < 0 or max_offerings_per_channel < 0:
        raise HTTPException(status_code=400, detail="quota limits must be >= 0")
    ent = db.get(ByokEntitlement, user_id)
    now = utcnow()
    if ent is None:
        ent = ByokEntitlement(
            user_id=user_id,
            enabled=bool(enabled),
            max_channels=int(max_channels),
            max_offerings_per_channel=int(max_offerings_per_channel),
            updated_at=now,
        )
    else:
        ent.enabled = bool(enabled)
        ent.max_channels = int(max_channels)
        ent.max_offerings_per_channel = int(max_offerings_per_channel)
        ent.updated_at = now
    db.add(ent)
    db.commit()
    return get_entitlement_view(db, user_id)


def _require_entitlement(db: Session, user_id: str) -> ByokEntitlement:
    ent = db.get(ByokEntitlement, user_id)
    if ent is None or not ent.enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "BYOK_ENTITLEMENT_NOT_CONFIGURED",
                "message": "BYOK entitlement is not enabled for this account",
            },
        )
    return ent


def _validate_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must be a valid http(s) URL")
    return url


def serialize_channel(c: ByokChannel) -> dict[str, Any]:
    return {
        "id": c.id,
        "channel_id": c.id,
        "name": c.name,
        "protocol_profile": c.protocol_profile,
        "base_url": c.base_url,
        "api_key_masked": c.api_key_masked,
        "rpm_limit": int(c.rpm_limit),
        "enabled": bool(c.enabled),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def serialize_offering(o: ByokOffering) -> dict[str, Any]:
    return {
        "id": o.id,
        "offering_id": o.id,
        "channel_id": o.channel_id,
        "canonical_model_key": o.canonical_model_key,
        "upstream_model_id": o.upstream_model_id,
        "verified_status": o.verified_status,
        "last_verified_at": o.last_verified_at.isoformat() if o.last_verified_at else None,
        "verification_error": o.verification_error,
        "enabled": bool(o.enabled),
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def create_channel(
    db: Session,
    user_id: str,
    *,
    name: str,
    protocol_profile: str,
    base_url: str,
    api_key: str,
    rpm_limit: int = 60,
) -> dict[str, Any]:
    ent = _require_entitlement(db, user_id)
    current = int(db.scalar(select(func.count(ByokChannel.id)).where(ByokChannel.user_id == user_id)) or 0)
    if current >= int(ent.max_channels):
        raise HTTPException(status_code=403, detail=f"channel quota reached (max {ent.max_channels})")
    if protocol_profile not in _VALID_PROFILES:
        raise HTTPException(status_code=400, detail=f"unsupported protocol_profile: {protocol_profile}")
    clean_url = _validate_base_url(base_url)
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 128:
        raise HTTPException(status_code=400, detail="channel name must be 1..128 characters")
    if rpm_limit < 1 or rpm_limit > 10000:
        raise HTTPException(status_code=400, detail="rpm_limit must be 1..10000")

    now = utcnow()
    row = ByokChannel(
        id=new_prefixed_id("chn"),
        user_id=user_id,
        name=clean_name,
        protocol_profile=protocol_profile,
        base_url=clean_url,
        api_key_encrypted=encrypt_api_key(api_key),
        api_key_masked=mask_api_key(api_key),
        rpm_limit=int(rpm_limit),
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    return serialize_channel(row)


def create_offering(
    db: Session,
    user_id: str,
    *,
    channel_id: str,
    canonical_model_key: str,
    upstream_model_id: str,
) -> dict[str, Any]:
    ent = _require_entitlement(db, user_id)
    channel = db.get(ByokChannel, channel_id)
    if channel is None or channel.user_id != user_id:
        raise HTTPException(status_code=404, detail="BYOK channel not found")

    cur_offerings = int(
        db.scalar(select(func.count(ByokOffering.id)).where(ByokOffering.channel_id == channel_id)) or 0
    )
    if cur_offerings >= int(ent.max_offerings_per_channel):
        raise HTTPException(
            status_code=403,
            detail=f"offerings quota reached for channel (max {ent.max_offerings_per_channel})",
        )

    ensure_rate_cards_seeded(db)
    card = db.scalar(select(RateCard).where(RateCard.model_key == canonical_model_key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"unknown canonical_model_key: {canonical_model_key}")
    if not upstream_model_id or not upstream_model_id.strip():
        raise HTTPException(status_code=400, detail="upstream_model_id must not be empty")

    dup = db.scalar(
        select(ByokOffering).where(
            ByokOffering.channel_id == channel_id,
            ByokOffering.canonical_model_key == canonical_model_key,
        )
    )
    if dup is not None:
        raise HTTPException(status_code=409, detail="canonical_model_key already mapped on this channel")

    now = utcnow()
    row = ByokOffering(
        id=new_prefixed_id("off"),
        user_id=user_id,
        channel_id=channel_id,
        canonical_model_key=canonical_model_key,
        upstream_model_id=upstream_model_id.strip(),
        verified_status="unverified",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    return serialize_offering(row)


def probe_upstream_channel(base_url: str, api_key: str, upstream_model_id: str) -> tuple[bool, Optional[str]]:
    """Lightweight connectivity probe (monkeypatchable in tests)."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code < 400:
                return True, None
            return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def verify_offering(db: Session, user_id: str, offering_id: str) -> dict[str, Any]:
    off = db.get(ByokOffering, offering_id)
    if off is None or off.user_id != user_id:
        raise HTTPException(status_code=404, detail="BYOK offering not found")
    ch = db.get(ByokChannel, off.channel_id)
    assert ch is not None
    raw_key = decrypt_api_key(ch.api_key_encrypted)
    ok, err = probe_upstream_channel(ch.base_url, raw_key, off.upstream_model_id)
    now = utcnow()
    off.verified_status = "verified" if ok else "failed"
    off.verification_error = err
    off.last_verified_at = now
    off.updated_at = now
    db.add(off)
    db.commit()
    return serialize_offering(off)
