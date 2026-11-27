"""ASGI OAuth Bearer middleware and ContextVar grant propagation for /mcp/canvas & /mcp/pecan.

Enforces project-scoped OAuth 2.0 DCR bearer tokens on Streamable-HTTP MCP
servers and emits RFC 9728 WWW-Authenticate discovery headers on 401 so external
clients (Claude Code, Codex) automatically trigger DCR authorization.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException

from app import mcp_oauth
from app.config import get_settings
from app.db import session_scope


@dataclass(frozen=True)
class McpGrantContext:
    grant_id: str
    user_id: str
    project_id: str
    server_type: str
    scopes: frozenset[str]


_current_mcp_grant: ContextVar[Optional[McpGrantContext]] = ContextVar("current_mcp_grant", default=None)


def get_current_mcp_grant() -> Optional[McpGrantContext]:
    return _current_mcp_grant.get()


async def _send_json_error(send, status_code: int, payload: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class McpOAuthMiddleware:
    """Wrap a FastMCP Starlette ASGI app with mandatory Bearer Grant enforcement."""

    def __init__(self, app: Any, server_type: str) -> None:
        self.app = app
        self.server_type = server_type

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        headers_map = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }
        auth_header = headers_map.get("authorization", "")
        raw_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""

        if not raw_token:
            if not settings.mcp_require_oauth_token:
                await self.app(scope, receive, send)
                return
            meta_url = f"{settings.app_base_url}/.well-known/oauth-protected-resource"
            await _send_json_error(
                send,
                401,
                {"detail": "MCP OAuth Bearer token required"},
                extra_headers=[
                    (b"www-authenticate", f'Bearer resource_metadata="{meta_url}"'.encode("latin-1")),
                ],
            )
            return

        try:
            with session_scope() as db:
                grant = mcp_oauth.verify_mcp_token(db, raw_token, server_type=self.server_type)
                ctx = McpGrantContext(
                    grant_id=grant.id,
                    user_id=grant.user_id,
                    project_id=grant.project_id,
                    server_type=grant.server_type,
                    scopes=frozenset(grant.scopes_json or []),
                )
        except HTTPException as exc:
            meta_url = f"{settings.app_base_url}/.well-known/oauth-protected-resource"
            extra = (
                [(b"www-authenticate", f'Bearer error="invalid_token", resource_metadata="{meta_url}"'.encode("latin-1"))]
                if exc.status_code == 401
                else None
            )
            await _send_json_error(send, exc.status_code, {"detail": exc.detail}, extra_headers=extra)
            return

        token_handle = _current_mcp_grant.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_mcp_grant.reset(token_handle)
