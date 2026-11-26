"""OAuth 2.0 Dynamic Client Registration (DCR) and project-scoped MCP grant enforcement."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    McpGrant,
    McpOAuthClient,
    McpOAuthCode,
    ProjectMember,
    new_prefixed_id,
    utcnow,
)

MCP_SCOPES = (
    "canvas:read",
    "canvas:write",
    "canvas:delete",
    "canvas:generate",
    "assets:read",
    "pecan:delegate",
)

MCP_SERVER_TYPES = ("canvas", "pecan", "both")


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def register_oauth_client(
    db: Session,
    *,
    client_name: str,
    redirect_uris: list[str],
    grant_types: Optional[list[str]] = None,
    token_endpoint_auth_method: str = "none",
) -> dict[str, Any]:
    name = (client_name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="client_name must be 1..128 characters")
    if not redirect_uris:
        raise HTTPException(status_code=400, detail="at least one redirect_uri is required")

    client_id = f"mcp_cli_{secrets.token_hex(12)}"
    raw_secret: Optional[str] = None
    secret_hash: Optional[str] = None
    if token_endpoint_auth_method != "none":
        raw_secret = f"mcp_sec_{secrets.token_urlsafe(24)}"
        secret_hash = _sha256(raw_secret)

    row = McpOAuthClient(
        id=new_prefixed_id("mcl"),
        client_id=client_id,
        client_secret_hash=secret_hash,
        client_name=name,
        redirect_uris_json=list(redirect_uris),
        grant_types_json=list(grant_types or ["authorization_code"]),
        created_at=utcnow(),
    )
    db.add(row)
    db.commit()

    resp: dict[str, Any] = {
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": row.redirect_uris_json,
        "grant_types": row.grant_types_json,
        "token_endpoint_auth_method": token_endpoint_auth_method,
    }
    if raw_secret:
        resp["client_secret"] = raw_secret
    return resp


def create_authorization_code(
    db: Session,
    *,
    client_id: str,
    user_id: str,
    project_id: str,
    scopes: list[str],
    redirect_uri: str,
    server_type: str = "canvas",
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
) -> dict[str, Any]:
    cli = db.scalar(select(McpOAuthClient).where(McpOAuthClient.client_id == client_id))
    if cli is None:
        raise HTTPException(status_code=404, detail="unknown oauth client_id")
    if redirect_uri not in (cli.redirect_uris_json or []):
        raise HTTPException(status_code=400, detail="redirect_uri not registered for client")
    if server_type not in MCP_SERVER_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid server_type: {server_type}")
    clean_scopes = [s.strip() for s in scopes if s.strip()]
    if not clean_scopes or any(s not in MCP_SCOPES for s in clean_scopes):
        raise HTTPException(status_code=400, detail=f"invalid scopes (allowed: {list(MCP_SCOPES)})")

    # User must be a member of the target project
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=403, detail="you are not a member of the target project")
    if member.role == "viewer" and any(
        s in ("canvas:write", "canvas:delete", "canvas:generate", "pecan:delegate") for s in clean_scopes
    ):
        raise HTTPException(status_code=403, detail="viewer cannot grant write/delegate MCP scopes")

    raw_code = secrets.token_urlsafe(32)
    now = utcnow()
    row = McpOAuthCode(
        id=new_prefixed_id("mcd"),
        code_hash=_sha256(raw_code),
        client_id=client_id,
        user_id=user_id,
        project_id=project_id,
        server_type=server_type,
        scopes_json=clean_scopes,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method or ("S256" if code_challenge else None),
        redirect_uri=redirect_uri,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(row)
    db.commit()
    return {
        "code": raw_code,
        "redirect_uri": redirect_uri,
        "project_id": project_id,
        "scopes": clean_scopes,
        "expires_in": 600,
    }


def exchange_code_for_grant(
    db: Session,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
    client_secret: Optional[str] = None,
    ttl_days: Optional[int] = None,
) -> dict[str, Any]:
    cli = db.scalar(select(McpOAuthClient).where(McpOAuthClient.client_id == client_id))
    if cli is None:
        raise HTTPException(status_code=401, detail="unknown oauth client")
    if cli.client_secret_hash:
        if not client_secret or _sha256(client_secret) != cli.client_secret_hash:
            raise HTTPException(status_code=401, detail="invalid client_secret")

    code_row = db.scalar(select(McpOAuthCode).where(McpOAuthCode.code_hash == _sha256(code)))
    now = utcnow()
    if (
        code_row is None
        or code_row.client_id != client_id
        or code_row.redirect_uri != redirect_uri
        or code_row.consumed_at is not None
        or _as_utc(code_row.expires_at) <= now
    ):
        raise HTTPException(status_code=400, detail="invalid or expired authorization code")

    if code_row.code_challenge:
        if not code_verifier:
            raise HTTPException(status_code=400, detail="code_verifier is required for PKCE")
        if _pkce_s256(code_verifier) != code_row.code_challenge:
            raise HTTPException(status_code=400, detail="PKCE code_verifier mismatch")

    code_row.consumed_at = now
    db.add(code_row)

    days = ttl_days or get_settings().mcp_grant_default_ttl_days
    raw_token = f"mcp_tok_{secrets.token_urlsafe(36)}"
    grant = McpGrant(
        id=new_prefixed_id("mgr"),
        client_id=cli.client_id,
        client_name=cli.client_name,
        user_id=code_row.user_id,
        project_id=code_row.project_id,
        server_type=code_row.server_type,
        scopes_json=list(code_row.scopes_json or []),
        token_hash=_sha256(raw_token),
        expires_at=now + timedelta(days=days),
        created_at=now,
    )
    db.add(grant)
    db.commit()
    return {
        "access_token": raw_token,
        "token_type": "Bearer",
        "expires_in": days * 86400,
        "scope": " ".join(grant.scopes_json),
        "grant_id": grant.id,
        "project_id": grant.project_id,
        "server_type": grant.server_type,
    }


def serialize_grant(g: McpGrant) -> dict[str, Any]:
    now = utcnow()
    active = g.revoked_at is None and _as_utc(g.expires_at) > now
    return {
        "id": g.id,
        "grant_id": g.id,
        "client_id": g.client_id,
        "client_name": g.client_name,
        "user_id": g.user_id,
        "project_id": g.project_id,
        "server_type": g.server_type,
        "scopes": list(g.scopes_json or []),
        "is_active": active,
        "expires_at": g.expires_at.isoformat(),
        "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
        "created_at": g.created_at.isoformat(),
    }


def verify_mcp_token(
    db: Session,
    raw_token: str,
    *,
    required_scope: Optional[str] = None,
    project_id: Optional[str] = None,
    server_type: Optional[str] = None,
) -> McpGrant:
    if not raw_token:
        raise HTTPException(status_code=401, detail="missing MCP bearer token")
    grant = db.scalar(select(McpGrant).where(McpGrant.token_hash == _sha256(raw_token)))
    now = utcnow()
    if grant is None or grant.revoked_at is not None or _as_utc(grant.expires_at) <= now:
        raise HTTPException(status_code=401, detail="MCP token is invalid, expired, or revoked")
    if project_id and grant.project_id != project_id:
        raise HTTPException(status_code=403, detail="MCP grant is scoped to a different project")
    if server_type and grant.server_type not in (server_type, "both"):
        raise HTTPException(status_code=403, detail=f"MCP grant does not permit {server_type} server")
    if required_scope and required_scope not in (grant.scopes_json or []):
        raise HTTPException(status_code=403, detail=f"MCP grant missing required scope: {required_scope}")
    return grant
