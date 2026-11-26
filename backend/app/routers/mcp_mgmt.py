"""MCP configuration, OAuth 2.0 DCR discovery/registration/token & granular grant routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, mcp_oauth
from app.config import get_settings
from app.db import get_db
from app.models import McpGrant, User, utcnow

router = APIRouter(tags=["mcp"])


class DcrRegisterRequest(BaseModel):
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code"])
    token_endpoint_auth_method: str = "none"


class McpAuthorizeRequest(BaseModel):
    client_id: str
    project_id: str
    scopes: list[str]
    redirect_uri: str
    server_type: str = "canvas"
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = None


class McpTokenRequest(BaseModel):
    grant_type: str = "authorization_code"
    client_id: str
    code: str
    redirect_uri: str
    code_verifier: Optional[str] = None
    client_secret: Optional[str] = None


class McpVerifyRequest(BaseModel):
    token: str
    required_scope: Optional[str] = None
    project_id: Optional[str] = None
    server_type: Optional[str] = None


@router.get("/.well-known/oauth-authorization-server")
def oauth_server_metadata() -> dict[str, Any]:
    base = get_settings().app_base_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/api/mcp/oauth/authorize",
        "token_endpoint": f"{base}/api/mcp/oauth/token",
        "registration_endpoint": f"{base}/api/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(mcp_oauth.MCP_SCOPES),
    }


@router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource_metadata() -> dict[str, Any]:
    base = get_settings().app_base_url
    return {
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": list(mcp_oauth.MCP_SCOPES),
        "mcp_servers": {
            "canvas": f"{base}/mcp/canvas/",
            "pecan": f"{base}/mcp/pecan/",
        },
    }


@router.post("/api/mcp/oauth/register", status_code=201)
def mcp_dcr_register(
    body: DcrRegisterRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return mcp_oauth.register_oauth_client(
        db,
        client_name=body.client_name,
        redirect_uris=body.redirect_uris,
        grant_types=body.grant_types,
        token_endpoint_auth_method=body.token_endpoint_auth_method,
    )


@router.post("/api/mcp/oauth/authorize")
def mcp_oauth_authorize(
    body: McpAuthorizeRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return mcp_oauth.create_authorization_code(
        db,
        client_id=body.client_id,
        user_id=user.id,
        project_id=body.project_id,
        scopes=body.scopes,
        redirect_uri=body.redirect_uri,
        server_type=body.server_type,
        code_challenge=body.code_challenge,
        code_challenge_method=body.code_challenge_method,
    )


@router.post("/api/mcp/oauth/token")
def mcp_oauth_token(
    body: McpTokenRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported grant_type")
    return mcp_oauth.exchange_code_for_grant(
        db,
        client_id=body.client_id,
        code=body.code,
        redirect_uri=body.redirect_uri,
        code_verifier=body.code_verifier,
        client_secret=body.client_secret,
    )


@router.get("/api/mcp/configuration")
def get_mcp_configuration(user: User = Depends(auth.get_current_user)) -> dict[str, Any]:
    base = get_settings().app_base_url
    return {
        "oauth_dcr_enabled": True,
        "discovery_url": f"{base}/.well-known/oauth-authorization-server",
        "available_scopes": list(mcp_oauth.MCP_SCOPES),
        "servers": [
            {
                "id": "pecan-canvas",
                "mode": "direct_canvas",
                "url": f"{base}/mcp/canvas/",
                "transport": "streamable-http",
                "description": "直接读写画布与项目目录（pecan_list_projects / pecan_canvas_read / directory_list 等）",
            },
            {
                "id": "pecan-orchestrator",
                "mode": "delegate_pecan",
                "url": f"{base}/mcp/pecan/",
                "transport": "streamable-http",
                "description": "向 Pecan 主控智能体委派多步骤创作与规划任务",
            },
        ],
        "cli_examples": {
            "claude_code_canvas": f"claude mcp add --transport http --oauth-client-registration dcr pecan-canvas {base}/mcp/canvas/",
            "claude_code_pecan": f"claude mcp add --transport http --oauth-client-registration dcr pecan-studio {base}/mcp/pecan/",
        },
    }


@router.get("/api/mcp/grants")
def list_mcp_grants(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(McpGrant)
        .where(McpGrant.user_id == user.id)
        .order_by(McpGrant.created_at.desc())
    ).all()
    items = [mcp_oauth.serialize_grant(g) for g in rows]
    return {"grants": items, "total": len(items)}


@router.delete("/api/mcp/grants/{grant_id}")
def revoke_mcp_grant(
    grant_id: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    g = db.get(McpGrant, grant_id)
    if g is None or g.user_id != user.id:
        raise HTTPException(status_code=404, detail="MCP grant not found")
    if g.revoked_at is None:
        g.revoked_at = utcnow()
        db.add(g)
        db.commit()
    return {"grant_id": g.id, "revoked": True}


@router.post("/api/mcp/grants/verify")
def verify_mcp_grant_token(
    body: McpVerifyRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    grant = mcp_oauth.verify_mcp_token(
        db,
        body.token,
        required_scope=body.required_scope,
        project_id=body.project_id,
        server_type=body.server_type,
    )
    return {"valid": True, "grant": mcp_oauth.serialize_grant(grant)}
