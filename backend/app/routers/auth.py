"""Authentication & OAuth routes (/api/auth/*)."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, billing
from app.config import get_settings
from app.db import get_db
from app.models import OAuthBinding, User, new_prefixed_id, utcnow
from app.redis_client import rate_limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class EmailSendCodeRequest(BaseModel):
    email: str
    purpose: str = "register"


class RegisterRequest(BaseModel):
    email: str
    password: str
    code: str
    display_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: Optional[str] = None


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class PasswordResetRequest(BaseModel):
    email: str
    code: str
    new_password: str


@router.post("/email/send-code")
def send_code(
    body: EmailSendCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    norm_email = (body.email or "").strip().lower()
    rate_limiter.check("email_code", norm_email or "unknown", settings.rate_limit_email_code_per_min, 60)
    user = auth.get_optional_current_user(request, db)
    auth.send_email_code(db, body.email, body.purpose, user_id=user.id if user else None)
    return {"sent": True, "email": norm_email, "purpose": body.purpose}


@router.post("/register", status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    norm_email = body.email.strip().lower()
    if not norm_email or "@" not in norm_email:
        raise HTTPException(status_code=400, detail="invalid email address")
    if db.scalar(select(User).where(User.email == norm_email)) is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    auth.verify_and_consume_email_code(db, norm_email, "register", body.code)
    try:
        pw_hash = auth.hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    display = (body.display_name or norm_email.split("@")[0]).strip()[:64]
    now = utcnow()
    user = User(
        id=new_prefixed_id("usr"),
        email=norm_email,
        email_verified=True,
        password_hash=pw_hash,
        display_name=display,
        roles=["user"],
        is_active=True,
        token_version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    billing.get_or_create_wallet(db, user.id)
    tokens = auth.issue_token_pair(db, user, request=request, response=response)
    return {**tokens, "user": auth.serialize_user_profile(user)}


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    norm_email = body.email.strip().lower()
    rate_limiter.check("login", norm_email or "unknown", settings.rate_limit_login_per_min, 60)
    user = db.scalar(select(User).where(User.email == norm_email))
    if user is None or not user.is_active or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    tokens = auth.issue_token_pair(db, user, request=request, response=response)
    return {**tokens, "user": auth.serialize_user_profile(user)}


@router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw_refresh = (body.refresh_token if body and body.refresh_token else None) or request.cookies.get("refresh_token")
    user, tokens = auth.rotate_refresh_token(db, raw_refresh or "", request=request, response=response)
    return {**tokens, "user": auth.serialize_user_profile(user)}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    body: Optional[LogoutRequest] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw_refresh = (body.refresh_token if body and body.refresh_token else None) or request.cookies.get("refresh_token")
    auth.revoke_refresh_token(db, raw_refresh)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"logged_out": True}


@router.post("/logout-all")
def logout_all(
    response: Response,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.revoke_all_user_sessions(db, user)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"logged_out_all": True, "user_id": user.id}


@router.post("/password/reset")
def reset_password(
    body: PasswordResetRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    norm_email = (body.email or "").strip().lower()
    rate_limiter.check("password_reset", norm_email or "unknown", settings.rate_limit_email_code_per_min, 60)
    user = db.scalar(select(User).where(User.email == norm_email))
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="account not found")
    auth.verify_and_consume_email_code(db, norm_email, "reset_password", body.code)
    try:
        user.password_hash = auth.hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user.email_verified = True
    db.add(user)
    # Revoke all existing sessions across every device immediately
    auth.revoke_all_user_sessions(db, user)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"reset": True, "user_id": user.id}


@router.get("/oauth/{provider}/authorize")
def oauth_authorize(
    provider: str,
    request: Request,
    mode: str = Query(default="login"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if mode not in ("login", "bind"):
        raise HTTPException(status_code=400, detail="mode must be login or bind")
    uid: Optional[str] = None
    if mode == "bind":
        user = auth.get_current_user(auth.get_optional_current_user(request, db))
        uid = user.id
    url, state = auth.build_oauth_authorize_url(provider, mode=mode, user_id=uid)
    return {"provider": provider, "mode": mode, "authorize_url": url, "state": state}


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    settings = get_settings()
    state_payload = auth.decode_oauth_state(state, expected_provider=provider)
    profile = await auth.exchange_oauth_code(provider, code)
    mode = state_payload.get("mode", "login")

    existing_binding = db.scalar(
        select(OAuthBinding).where(
            OAuthBinding.provider == provider,
            OAuthBinding.provider_user_id == profile.provider_user_id,
        )
    )

    if mode == "bind":
        target_uid = state_payload.get("uid")
        user = db.get(User, target_uid) if target_uid else None
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="user not found for oauth bind")
        if existing_binding is not None and existing_binding.user_id != user.id:
            raise HTTPException(status_code=409, detail="oauth account already bound to another user")
        if existing_binding is None:
            same_provider = db.scalar(
                select(OAuthBinding).where(
                    OAuthBinding.user_id == user.id,
                    OAuthBinding.provider == provider,
                )
            )
            if same_provider is not None:
                raise HTTPException(status_code=409, detail=f"{provider} already bound on this account")
            db.add(OAuthBinding(
                id=new_prefixed_id("oab"),
                user_id=user.id,
                provider=provider,
                provider_user_id=profile.provider_user_id,
                provider_username=profile.username,
                provider_email=profile.email,
                avatar_url=profile.avatar_url,
                created_at=utcnow(),
            ))
            db.commit()
        # Bind 完成：302 回前端回调页（fragment 带绑定结果，不再返回 raw JSON）。
        fragment = urlencode({"bound": "true", "provider": provider})
        return RedirectResponse(
            url=f"{settings.frontend_url}/auth/callback#{fragment}",
            status_code=302,
        )

    # Login mode
    if existing_binding is not None:
        user = db.get(User, existing_binding.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=403, detail="account is disabled")
    else:
        user = None
        if profile.email:
            user = db.scalar(select(User).where(User.email == profile.email.strip().lower()))
        if user is None:
            now = utcnow()
            user = User(
                id=new_prefixed_id("usr"),
                email=profile.email.strip().lower() if profile.email else None,
                email_verified=bool(profile.email),
                password_hash=None,
                display_name=(profile.username or f"{provider}_user")[:64],
                avatar_url=profile.avatar_url,
                roles=["user"],
                is_active=True,
                token_version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            db.flush()
            billing.get_or_create_wallet(db, user.id)
        db.add(OAuthBinding(
            id=new_prefixed_id("oab"),
            user_id=user.id,
            provider=provider,
            provider_user_id=profile.provider_user_id,
            provider_username=profile.username,
            provider_email=profile.email,
            avatar_url=profile.avatar_url,
            created_at=utcnow(),
        ))
        db.flush()

    tokens = auth.issue_token_pair(db, user, request=request, response=response)
    # 登录成功：302 回前端回调页，token 放 URL fragment 里（不经过后端日志/查询串，
    # 前端弹窗读取后存入 localStorage；注入的 response 上设置的 cookie 不随重定向携带，
    # 跨站场景下本来也发不出去，故以 fragment 为准）。
    fragment = urlencode(
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": "Bearer",
            "expires_in": tokens["expires_in"],
            "provider": provider,
        }
    )
    return RedirectResponse(
        url=f"{settings.frontend_url}/auth/callback#{fragment}",
        status_code=302,
    )
