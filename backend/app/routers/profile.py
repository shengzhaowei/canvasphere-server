"""User profile, email/password binding, OAuth bindings, and avatar upload routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, storage
from app.config import get_settings
from app.db import get_db
from app.models import OAuthBinding, User, utcnow

router = APIRouter(prefix="/api/user/profile", tags=["profile"])


class ProfileUpdateRequest(BaseModel):
    display_name: str


class BindEmailRequest(BaseModel):
    email: str
    code: str
    purpose: str = "bind_email"


class SetPasswordRequest(BaseModel):
    new_password: str
    current_password: Optional[str] = None


class AvatarUploadUrlRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class AvatarConfirmRequest(BaseModel):
    storage_id: str


@router.get("")
def get_profile(user: User = Depends(auth.get_current_user)) -> dict[str, Any]:
    return auth.serialize_user_profile(user)


@router.patch("")
def update_profile(
    body: ProfileUpdateRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    name = (body.display_name or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="display_name must be 1..64 characters")
    user.display_name = name
    user.updated_at = utcnow()
    db.add(user)
    db.commit()
    return auth.serialize_user_profile(user)


@router.post("/email/bind")
def bind_or_change_email(
    body: BindEmailRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.purpose not in ("bind_email", "change_email"):
        raise HTTPException(status_code=400, detail="purpose must be bind_email or change_email")
    norm_email = body.email.strip().lower()
    if not norm_email or "@" not in norm_email:
        raise HTTPException(status_code=400, detail="invalid email address")
    conflict = db.scalar(select(User).where(User.email == norm_email, User.id != user.id))
    if conflict is not None:
        raise HTTPException(status_code=409, detail="email already in use")

    auth.verify_and_consume_email_code(db, norm_email, body.purpose, body.code)
    user.email = norm_email
    user.email_verified = True
    user.updated_at = utcnow()
    db.add(user)
    db.commit()
    return auth.serialize_user_profile(user)


@router.post("/password")
def set_local_password(
    body: SetPasswordRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not user.email or not user.email_verified:
        raise HTTPException(status_code=400, detail="verified email is required before setting a local password")
    if user.password_hash:
        if not body.current_password or not auth.verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="current password is incorrect")
    try:
        user.password_hash = auth.hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user.updated_at = utcnow()
    db.add(user)
    db.commit()
    return auth.serialize_user_profile(user)


@router.get("/oauth/bindings")
def list_oauth_bindings(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(OAuthBinding).where(OAuthBinding.user_id == user.id).order_by(OAuthBinding.created_at.asc())
    ).all()
    return {
        "available_providers": list(auth.AVAILABLE_OAUTH_PROVIDERS),
        "bindings": [
            {
                "provider": r.provider,
                "provider_user_id": r.provider_user_id,
                "provider_username": r.provider_username,
                "provider_email": r.provider_email,
                "avatar_url": r.avatar_url,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.delete("/oauth/bindings/{provider}")
def unbind_oauth(
    provider: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if provider not in auth.AVAILABLE_OAUTH_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unsupported oauth provider: {provider}")
    rows = db.scalars(select(OAuthBinding).where(OAuthBinding.user_id == user.id)).all()
    target = next((r for r in rows if r.provider == provider), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"oauth binding not found: {provider}")
    # Prevent locking the user out if no local password and this is the only OAuth binding
    if not user.password_hash and len(rows) <= 1:
        raise HTTPException(
            status_code=400,
            detail="cannot unbind the last login method without a local password",
        )
    db.delete(target)
    db.commit()
    return {"unbound": True, "provider": provider}


@router.post("/avatar/upload-url")
def create_avatar_upload_url(
    body: AvatarUploadUrlRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.content_type.strip().lower() not in storage.ALLOWED_AVATAR_MIMES:
        raise HTTPException(status_code=400, detail="avatar must be PNG, JPEG, GIF, or WebP")
    max_bytes = get_settings().avatar_max_bytes
    if body.size_bytes <= 0 or body.size_bytes > max_bytes:
        raise HTTPException(status_code=413, detail=f"avatar size must be <= {max_bytes} bytes (2 MiB)")
    return storage.create_presigned_upload(
        db,
        space="avatars",
        filename=body.filename,
        content_type=body.content_type.strip().lower(),
        size_bytes=body.size_bytes,
        owner_user_id=user.id,
    )


@router.post("/avatar/confirm")
def confirm_avatar_upload(
    body: AvatarConfirmRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    obj, new_url = storage.confirm_upload(
        db,
        body.storage_id,
        expected_space="avatars",
        expected_user_id=user.id,
    )
    old_storage_id = user.avatar_storage_id
    user.avatar_url = new_url
    user.avatar_storage_id = obj.id
    user.updated_at = utcnow()
    db.add(user)
    if old_storage_id and old_storage_id != obj.id:
        storage.invalidate_object(db, old_storage_id)
    db.commit()
    return auth.serialize_user_profile(user)
