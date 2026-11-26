"""Authentication, dual-token sessions, email verification, and OAuth helpers.

Supports:
- Email + bcrypt password credentials
- Short-lived HS256 JWT access tokens (default 7200s) with token_version check
- Opaque refresh tokens stored as SHA-256 hashes with rotation & logout-all
- Email verification codes (SMTP if configured, logged + test sink otherwise)
- OAuth providers: github, google, linuxdo (login + bind/unbind)
- Project-level real RBAC resolution with optional legacy X-Role fallback
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional
from urllib.parse import urlencode

import bcrypt
import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    EmailVerificationCode,
    OAuthBinding,
    Project,
    ProjectMember,
    RefreshSession,
    User,
    new_prefixed_id,
    utcnow,
)
from app.permissions import normalize_role

logger = logging.getLogger(__name__)

AVAILABLE_OAUTH_PROVIDERS = ("github", "google", "linuxdo")
EMAIL_CODE_PURPOSES = ("register", "bind_email", "change_email", "reset_password")

# In-process test sink storing the last plaintext code per (email, purpose).
# Never exposed over HTTP; used by automated tests when SMTP is unconfigured.
_last_sent_codes: dict[tuple[str, str], str] = {}


def clear_email_test_sink() -> None:
    _last_sent_codes.clear()


def get_last_sent_code(email: str, purpose: str) -> Optional[str]:
    return _last_sent_codes.get((email.strip().lower(), purpose))


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user: User) -> tuple[str, int]:
    settings = get_settings()
    ttl = settings.access_token_ttl_seconds
    now = utcnow()
    payload = {
        "sub": user.id,
        "tv": int(user.token_version),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, ttl


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid access token") from exc
    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="invalid access token payload")
    return payload


def issue_token_pair(
    db: Session,
    user: User,
    request: Optional[Request] = None,
    response: Optional[Response] = None,
) -> dict[str, Any]:
    settings = get_settings()
    access_token, expires_in = create_access_token(user)
    raw_refresh = secrets.token_urlsafe(48)
    now = utcnow()
    session_row = RefreshSession(
        id=new_prefixed_id("rfs"),
        user_id=user.id,
        token_hash=_sha256(raw_refresh),
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
        ip_address=(request.client.host if (request and request.client) else None),
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        created_at=now,
    )
    db.add(session_row)
    db.commit()

    if response is not None:
        response.set_cookie(
            key="access_token",
            value=access_token,
            max_age=expires_in,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            key="refresh_token",
            value=raw_refresh,
            max_age=settings.refresh_token_ttl_days * 86400,
            httponly=True,
            samesite="lax",
            path="/api/auth",
        )

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


def rotate_refresh_token(
    db: Session,
    raw_refresh_token: str,
    request: Optional[Request] = None,
    response: Optional[Response] = None,
) -> tuple[User, dict[str, Any]]:
    if not raw_refresh_token:
        raise HTTPException(status_code=401, detail="missing refresh token")
    token_hash = _sha256(raw_refresh_token)
    sess = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_hash))
    now = utcnow()
    if sess is None or sess.revoked_at is not None or _as_utc(sess.expires_at) <= now:
        raise HTTPException(status_code=401, detail="invalid or expired refresh token")
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user inactive or not found")

    sess.revoked_at = now
    db.add(sess)
    db.flush()
    tokens = issue_token_pair(db, user, request=request, response=response)
    return user, tokens


def revoke_refresh_token(db: Session, raw_refresh_token: Optional[str]) -> None:
    if not raw_refresh_token:
        return
    sess = db.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == _sha256(raw_refresh_token))
    )
    if sess is not None and sess.revoked_at is None:
        sess.revoked_at = utcnow()
        db.add(sess)
        db.commit()


def revoke_all_user_sessions(db: Session, user: User) -> None:
    now = utcnow()
    user.token_version = int(user.token_version) + 1
    user.updated_at = now
    db.add(user)
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()


# --- Email verification codes --------------------------------------------
def send_email_code(
    db: Session,
    email: str,
    purpose: str,
    user_id: Optional[str] = None,
) -> str:
    norm_email = (email or "").strip().lower()
    if not norm_email or "@" not in norm_email:
        raise HTTPException(status_code=400, detail="invalid email address")
    if purpose not in EMAIL_CODE_PURPOSES:
        raise HTTPException(status_code=400, detail=f"invalid purpose: {purpose}")

    settings = get_settings()
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = utcnow()
    row = EmailVerificationCode(
        id=new_prefixed_id("evc"),
        user_id=user_id,
        email=norm_email,
        purpose=purpose,
        code_hash=_sha256(code),
        expires_at=now + timedelta(seconds=settings.email_code_ttl_seconds),
        created_at=now,
    )
    db.add(row)
    db.commit()

    _last_sent_codes[(norm_email, purpose)] = code

    if settings.smtp_host:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[Canvasphere] 邮箱验证码 ({purpose})"
            msg["From"] = settings.smtp_from or settings.smtp_user or "noreply@canvasphere.local"
            msg["To"] = norm_email
            msg.set_content(f"您的验证码是：{code}，{settings.email_code_ttl_seconds // 60} 分钟内有效。")
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SMTP send failed for %s: %s", norm_email, exc)
    else:
        logger.info("Email verification code generated for %s (%s): [logged in dev mode]", norm_email, purpose)
    return code


def verify_and_consume_email_code(
    db: Session,
    email: str,
    purpose: str,
    code: str,
) -> None:
    norm_email = (email or "").strip().lower()
    if not code or not code.strip():
        raise HTTPException(status_code=400, detail="verification code is required")
    now = utcnow()
    row = db.scalar(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.email == norm_email,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.code_hash == _sha256(code.strip()),
            EmailVerificationCode.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCode.created_at.desc())
        .limit(1)
    )
    if row is None or _as_utc(row.expires_at) <= now:
        raise HTTPException(status_code=400, detail="invalid or expired verification code")
    row.consumed_at = now
    db.add(row)
    db.flush()


# --- OAuth provider helpers ----------------------------------------------
@dataclass
class OAuthProfile:
    provider: str
    provider_user_id: str
    username: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None


def _oauth_provider_config(provider: str) -> dict[str, str]:
    settings = get_settings()
    callback = f"{settings.oauth_redirect_base_url}/api/auth/oauth/{provider}/callback"
    configs = {
        "github": {
            "client_id": settings.oauth_github_client_id,
            "client_secret": settings.oauth_github_client_secret,
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "userinfo_url": "https://api.github.com/user",
            "scope": "read:user user:email",
            "redirect_uri": callback,
        },
        "google": {
            "client_id": settings.oauth_google_client_id,
            "client_secret": settings.oauth_google_client_secret,
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
            "scope": "openid email profile",
            "redirect_uri": callback,
        },
        "linuxdo": {
            "client_id": settings.oauth_linuxdo_client_id,
            "client_secret": settings.oauth_linuxdo_client_secret,
            "authorize_url": "https://connect.linux.do/oauth2/authorize",
            "token_url": "https://connect.linux.do/oauth2/token",
            "userinfo_url": "https://connect.linux.do/api/user",
            "scope": "user",
            "redirect_uri": callback,
        },
    }
    if provider not in configs:
        raise HTTPException(status_code=400, detail=f"unsupported oauth provider: {provider}")
    return configs[provider]


def create_oauth_state(provider: str, mode: str = "login", user_id: Optional[str] = None) -> str:
    settings = get_settings()
    now = utcnow()
    payload = {
        "type": "oauth_state",
        "provider": provider,
        "mode": mode,
        "uid": user_id,
        "nonce": secrets.token_hex(8),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_oauth_state(state: str, expected_provider: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail="invalid or expired oauth state") from exc
    if payload.get("type") != "oauth_state" or payload.get("provider") != expected_provider:
        raise HTTPException(status_code=400, detail="oauth state mismatch")
    return payload


def build_oauth_authorize_url(
    provider: str,
    mode: str = "login",
    user_id: Optional[str] = None,
) -> tuple[str, str]:
    cfg = _oauth_provider_config(provider)
    state = create_oauth_state(provider, mode=mode, user_id=user_id)
    params = {
        "client_id": cfg["client_id"] or f"unconfigured-{provider}",
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    return f"{cfg['authorize_url']}?{urlencode(params)}", state


async def exchange_oauth_code(provider: str, code: str) -> OAuthProfile:
    cfg = _oauth_provider_config(provider)
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise HTTPException(status_code=503, detail=f"oauth provider not configured: {provider}")
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            cfg["token_url"],
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "redirect_uri": cfg["redirect_uri"],
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"oauth token exchange failed for {provider}")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail=f"oauth provider returned no access_token: {provider}")

        user_resp = await client.get(
            cfg["userinfo_url"],
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if user_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"oauth userinfo fetch failed for {provider}")
        info = user_resp.json()

    if provider == "github":
        return OAuthProfile(
            provider="github",
            provider_user_id=str(info["id"]),
            username=info.get("login") or info.get("name"),
            email=info.get("email"),
            avatar_url=info.get("avatar_url"),
        )
    if provider == "google":
        return OAuthProfile(
            provider="google",
            provider_user_id=str(info.get("id") or info.get("sub")),
            username=info.get("name"),
            email=info.get("email"),
            avatar_url=info.get("picture"),
        )
    return OAuthProfile(
        provider="linuxdo",
        provider_user_id=str(info.get("id") or info.get("sub")),
        username=info.get("username") or info.get("name"),
        email=info.get("email"),
        avatar_url=info.get("avatar_url") or info.get("avatar_template"),
    )


# --- User serialization & FastAPI dependencies ---------------------------
def serialize_user_profile(user: User) -> dict[str, Any]:
    return {
        "user_id": user.id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "email": user.email,
        "email_verified": bool(user.email_verified),
        "has_local_credential": bool(user.password_hash),
        "roles": list(user.roles or ["user"]),
        "is_active": bool(user.is_active),
    }


def _extract_bearer_or_cookie(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token.strip()
    return None


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    token = _extract_bearer_or_cookie(request)
    if not token:
        return None
    payload = decode_access_token(token)
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or inactive")
    if int(payload.get("tv", -1)) != int(user.token_version):
        raise HTTPException(status_code=401, detail="session revoked")
    return user


def get_current_user(
    user: Optional[User] = Depends(get_optional_current_user),
) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def resolve_project_actor(
    project_id: str,
    request: Request,
    db: Session,
    x_role: Optional[str] = None,
) -> tuple[Optional[User], str]:
    """Resolve (user, role) for a project route.

    - Authenticated user: role comes strictly from project_members table.
    - Unauthenticated + AUTH_ALLOW_LEGACY_ROLE_HEADER=true: fallback to X-Role.
    - Unauthenticated + strict mode: 401.
    """
    user = get_optional_current_user(request, db)
    proj = db.get(Project, project_id)
    if proj is None or proj.status == "deleted":
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")

    if user is not None:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        )
        if member is None:
            raise HTTPException(status_code=403, detail="not a member of this project")
        return user, normalize_role(member.role)

    if get_settings().auth_allow_legacy_role_header:
        return None, normalize_role(x_role or "owner")

    raise HTTPException(status_code=401, detail="authentication required")


def get_optional_actor_or_legacy(
    request: Request,
    db: Session = Depends(get_db),
    x_role: Optional[str] = Header(default=None),
) -> tuple[Optional[User], bool]:
    """Returns (user, is_legacy_mode) for non-project endpoints like /api/projects."""
    user = get_optional_current_user(request, db)
    if user is not None:
        return user, False
    if get_settings().auth_allow_legacy_role_header:
        return None, True
    raise HTTPException(status_code=401, detail="authentication required")
