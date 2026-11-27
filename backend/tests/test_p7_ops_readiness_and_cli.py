"""Phase P7 tests: /health/ready deep probe, startup guardrails, ops CLI
(bootstrap-admin, create-redeem-code, grant-byok, check-env), and 4C8G deploy invariants.
"""
from __future__ import annotations

import json
from pathlib import Path

from app import cli
from app.config import get_settings, validate_production_readiness


def test_health_ready_probe_and_production_strict_guardrails(strict_client, monkeypatch):
    # Default non-strict test mode -> 200 ready, with DATABASE_URL warning because SQLite in-memory is used
    r = strict_client.get("/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["db"]["ok"] is True
    assert any("DATABASE_URL" in w for w in body["warnings"])

    # In PRODUCTION_STRICT=true with a weak JWT_SECRET -> 503 degraded
    monkeypatch.setenv("PRODUCTION_STRICT", "true")
    monkeypatch.setenv("JWT_SECRET", "short-weak")
    r2 = strict_client.get("/health/ready")
    assert r2.status_code == 503, r2.text
    b2 = r2.json()
    assert b2["ready"] is False
    assert b2["status"] == "degraded"
    assert any("JWT_SECRET" in w for w in b2["warnings"])

    # With strong secret, strict auth, real webhook secret, and DATABASE_URL -> zero warnings
    with monkeypatch.context() as mp:
        mp.setenv("JWT_SECRET", "a-very-strong-random-production-secret-key-at-least-32-chars")
        mp.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
        mp.setenv("PROVIDER_WEBHOOK_SECRET", "real-prod-webhook-secret-999")
        mp.setenv("DATABASE_URL", "postgresql://canvas:strongpass@db:5432/canvas")
        assert validate_production_readiness(get_settings()) == []


def test_ops_cli_bootstrap_admin_redeem_code_byok_and_check_env(strict_client, monkeypatch):
    # 1. bootstrap-admin creates admin and is idempotent on re-run
    res1 = cli.cmd_bootstrap_admin(email="ops-admin@example.com", password="AdminPassword123!")
    assert res1["action"] == "created"
    assert "admin" in res1["roles"]

    res2 = cli.cmd_bootstrap_admin(email="ops-admin@example.com", password="AdminPassword123!")
    assert res2["action"] == "promoted"
    assert res2["user_id"] == res1["user_id"]

    # Admin can log in and call /api/admin/users
    login = strict_client.post(
        "/api/auth/login",
        json={"email": "ops-admin@example.com", "password": "AdminPassword123!"},
    )
    assert login.status_code == 200, login.text
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    users = strict_client.get("/api/admin/users", headers=admin_headers)
    assert users.status_code == 200, users.text
    assert any(u["email"] == "ops-admin@example.com" for u in users.json()["items"])

    # 2. create-redeem-code mints code usable via /api/user/billing/redeem
    rdm = cli.cmd_create_redeem_code(code="launch777", credits=500, max_uses=5)
    assert rdm["action"] == "created"
    assert rdm["code"] == "LAUNCH777"

    w_before = strict_client.get("/api/user/billing/wallet", headers=admin_headers)
    assert w_before.status_code == 200, w_before.text
    before = w_before.json()["available_credits"]
    redeem_resp = strict_client.post("/api/user/billing/redeem", headers=admin_headers, json={"code": "LAUNCH777"})
    assert redeem_resp.status_code == 200, redeem_resp.text
    after = strict_client.get("/api/user/billing/wallet", headers=admin_headers).json()["available_credits"]
    assert after == before + 500

    # 3. grant-byok enables BYOK entitlement for user
    ent = cli.cmd_grant_byok(email="ops-admin@example.com", max_channels=8)
    assert ent["enabled"] is True
    assert ent["max_channels"] == 8
    ent_api = strict_client.get("/api/user/model-platform/byok-entitlement", headers=admin_headers)
    assert ent_api.status_code == 200, ent_api.text
    assert ent_api.json()["enabled"] is True
    assert ent_api.json()["max_channels"] == 8

    # 4. check-env --strict returns non-zero when weak secret is set
    monkeypatch.setenv("JWT_SECRET", "changeme")
    assert cli.main(["check-env", "--strict"]) == 1


def test_4c8g_deploy_configs_backup_script_and_mcp_json():
    repo_root = Path(__file__).resolve().parents[2]

    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("restart: unless-stopped") == 6
    assert "LOG_FORMAT=${LOG_FORMAT:-json}" in compose
    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 2
    assert "api:\n        condition: service_healthy" in compose

    cicd = (repo_root / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8")
    assert "/health/ready" in cicd
    assert "/metrics" in cicd
    assert "python:3.12-slim" in cicd

    backup = (repo_root / "scripts" / "backup-pg.sh").read_text(encoding="utf-8")
    assert "pg_dump" in backup
    assert "RETENTION_DAYS" in backup

    mcp_cfg = json.loads((repo_root / "mcp.json").read_text(encoding="utf-8"))
    servers = mcp_cfg["mcpServers"]
    assert servers["pecan-canvas"]["url"].endswith("/mcp/canvas/")
    assert servers["pecan-orchestrator"]["url"].endswith("/mcp/pecan/")

    env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "PRODUCTION_STRICT=" in env_example
    assert "YJS_REQUIRE_AUTH=" in env_example
    assert "LOG_FORMAT=" in env_example
