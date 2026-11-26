"""P3 tests: dual MCP servers (/mcp/canvas, /mcp/pecan), OAuth 2.0 DCR PKCE & granular grants."""
from __future__ import annotations

import base64
import hashlib
import json

from fastapi.testclient import TestClient

from app import auth
from app.main import app


def _make_user(client, email: str, name: str):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "code": code,
        "display_name": name,
    })
    assert r.status_code == 201
    return r.json()["user"]["user_id"], {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mcp_init_and_list_tools(client: TestClient, mount_path: str) -> set[str]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = client.post(
        mount_path,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
    )
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid
    headers["mcp-session-id"] = sid
    client.post(mount_path, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r2 = client.post(mount_path, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r2.status_code == 200
    body = r2.text
    if body.startswith("event:"):
        data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        payload = json.loads(data_lines[-1])
    else:
        payload = r2.json()
    return {t["name"] for t in payload["result"]["tools"]}


def test_mcp_oauth_dcr_pkce_flow_scope_enforcement_and_revocation(strict_client):
    _uid, h = _make_user(strict_client, "mcp@example.com", "McpUser")
    pid = strict_client.post("/api/projects", json={"name": "MCP Proj"}, headers=h).json()["id"]

    # Well-known discovery + /api/mcp/configuration
    wk = strict_client.get("/.well-known/oauth-authorization-server").json()
    assert wk["registration_endpoint"].endswith("/api/mcp/oauth/register")
    assert "S256" in wk["code_challenge_methods_supported"]

    pr = strict_client.get("/.well-known/oauth-protected-resource").json()
    assert pr["mcp_servers"]["canvas"].endswith("/mcp/canvas/")
    assert pr["mcp_servers"]["pecan"].endswith("/mcp/pecan/")

    cfg = strict_client.get("/api/mcp/configuration", headers=h).json()
    assert cfg["oauth_dcr_enabled"] is True
    assert {s["id"] for s in cfg["servers"]} == {"pecan-canvas", "pecan-orchestrator"}

    # Step 1: Dynamic Client Registration (DCR)
    reg = strict_client.post(
        "/api/mcp/oauth/register",
        json={"client_name": "Claude Code CLI", "redirect_uris": ["http://127.0.0.1:9999/callback"]},
    )
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    # Step 2: PKCE authorize for canvas:read + assets:read
    verifier = "super-secret-pkce-code-verifier-string-1234567890"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    auth_r = strict_client.post(
        "/api/mcp/oauth/authorize",
        json={
            "client_id": client_id,
            "project_id": pid,
            "scopes": ["canvas:read", "assets:read"],
            "redirect_uri": "http://127.0.0.1:9999/callback",
            "server_type": "canvas",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers=h,
    )
    assert auth_r.status_code == 200
    code = auth_r.json()["code"]

    # Wrong PKCE verifier rejected
    assert strict_client.post(
        "/api/mcp/oauth/token",
        json={"client_id": client_id, "code": code, "redirect_uri": "http://127.0.0.1:9999/callback", "code_verifier": "wrong"},
    ).status_code == 400

    # Correct PKCE verifier returns MCP bearer token & grant
    tok_r = strict_client.post(
        "/api/mcp/oauth/token",
        json={"client_id": client_id, "code": code, "redirect_uri": "http://127.0.0.1:9999/callback", "code_verifier": verifier},
    )
    assert tok_r.status_code == 200
    mcp_token = tok_r.json()["access_token"]
    grant_id = tok_r.json()["grant_id"]

    # Verify scope + project + server boundaries
    assert strict_client.post(
        "/api/mcp/grants/verify",
        json={"token": mcp_token, "required_scope": "canvas:read", "project_id": pid, "server_type": "canvas"},
    ).status_code == 200
    assert strict_client.post(
        "/api/mcp/grants/verify",
        json={"token": mcp_token, "required_scope": "canvas:delete", "project_id": pid},
    ).status_code == 403
    assert strict_client.post(
        "/api/mcp/grants/verify",
        json={"token": mcp_token, "project_id": "prj_other"},
    ).status_code == 403

    # List & revoke grant -> token immediately returns 401
    assert strict_client.get("/api/mcp/grants", headers=h).json()["total"] == 1
    assert strict_client.delete(f"/api/mcp/grants/{grant_id}", headers=h).status_code == 200
    assert strict_client.post("/api/mcp/grants/verify", json={"token": mcp_token}).status_code == 401


def test_dual_mcp_streamable_http_endpoints_list_distinct_tools():
    with TestClient(app) as client:
        canvas_tools = _mcp_init_and_list_tools(client, "/mcp/canvas/")
        pecan_tools = _mcp_init_and_list_tools(client, "/mcp/pecan/")
        assert {"pecan_list_projects", "pecan_canvas_read", "directory_list", "pecan_canvas_add_node"}.issubset(canvas_tools)
        assert {"pecan_delegate_task", "pecan_get_run_status"}.issubset(pecan_tools)
