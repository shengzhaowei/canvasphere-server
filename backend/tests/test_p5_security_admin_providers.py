"""P5 tests: MCP OAuth tool scope isolation, password reset, Admin APIs, BYOK routing, 6 adapters & Webhooks."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from app import auth, db, model_platform, providers
from app.main import app
from app.models import User


def _make_user(client, email: str, name: str, is_admin: bool = False):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "code": code,
        "display_name": name,
    })
    assert r.status_code == 201
    uid = r.json()["user"]["user_id"]
    if is_admin:
        with db.session_scope() as s:
            u = s.get(User, uid)
            u.roles = ["user", "admin"]
            s.add(u)
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mcp_call_tool(client: TestClient, mount_path: str, token: str, tool_name: str, arguments: dict) -> dict:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
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
    headers["mcp-session-id"] = r.headers["mcp-session-id"]
    client.post(mount_path, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r2 = client.post(
        mount_path,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}},
    )
    assert r2.status_code == 200
    body = r2.text
    if body.startswith("event:"):
        data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        payload = json.loads(data_lines[-1])
    else:
        payload = r2.json()
    text_out = payload["result"]["content"][0]["text"]
    return json.loads(text_out)


def test_mcp_http_oauth_middleware_enforces_project_and_scope_isolation():
    with TestClient(app) as client:
        _uid, h = _make_user(client, "mcp_iso@example.com", "IsoUser")
        p1 = client.post("/api/projects", json={"name": "Granted Project"}, headers=h).json()["id"]
        p2 = client.post("/api/projects", json={"name": "Secret Project"}, headers=h).json()["id"]

        cli_id = client.post(
            "/api/mcp/oauth/register",
            json={"client_name": "ScopeTest", "redirect_uris": ["http://127.0.0.1:7777/cb"]},
        ).json()["client_id"]
        # Grant ONLY canvas:read on p1 (not canvas:write, not p2)
        code = client.post(
            "/api/mcp/oauth/authorize",
            json={
                "client_id": cli_id,
                "project_id": p1,
                "scopes": ["canvas:read"],
                "redirect_uri": "http://127.0.0.1:7777/cb",
                "server_type": "canvas",
            },
            headers=h,
        ).json()["code"]
        tok_data = client.post(
            "/api/mcp/oauth/token",
            json={"client_id": cli_id, "code": code, "redirect_uri": "http://127.0.0.1:7777/cb"},
        ).json()
        tok, grant_id = tok_data["access_token"], tok_data["grant_id"]

        # pecan_list_projects sees ONLY p1, never p2
        listed = _mcp_call_tool(client, "/mcp/canvas/", tok, "pecan_list_projects", {})
        assert [p["id"] for p in listed["projects"]] == [p1]

        # Reading p1 succeeds; reading p2 denied; writing p1 denied (missing canvas:write)
        assert "objects" in _mcp_call_tool(client, "/mcp/canvas/", tok, "pecan_canvas_read", {"project_id": p1})
        assert _mcp_call_tool(client, "/mcp/canvas/", tok, "pecan_canvas_read", {"project_id": p2})["code"] == "MCP_PROJECT_DENIED"
        assert _mcp_call_tool(client, "/mcp/canvas/", tok, "pecan_canvas_add_node", {"project_id": p1, "type": "text", "x": 0, "y": 0})["code"] == "MCP_SCOPE_DENIED"

        # Calling /mcp/pecan/ with a canvas-only token returns 403 at middleware
        r_wrong_srv = client.post("/mcp/pecan/", headers={"Authorization": f"Bearer {tok}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r_wrong_srv.status_code == 403

        # Revoking grant immediately returns 401 at middleware
        assert client.delete(f"/api/mcp/grants/{grant_id}", headers=h).status_code == 200
        assert client.post("/mcp/canvas/", headers={"Authorization": f"Bearer {tok}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).status_code == 401


def test_forgot_password_reset_revokes_all_old_sessions(strict_client):
    _uid, h = _make_user(strict_client, "forgot@example.com", "Forgetful")
    assert strict_client.get("/api/user/profile", headers=h).status_code == 200

    assert strict_client.post("/api/auth/email/send-code", json={"email": "forgot@example.com", "purpose": "reset_password"}).status_code == 200
    code = auth.get_last_sent_code("forgot@example.com", "reset_password")
    r = strict_client.post(
        "/api/auth/password/reset",
        json={"email": "forgot@example.com", "code": code, "new_password": "NewStrongPassword99!"},
    )
    assert r.status_code == 200 and r.json()["reset"] is True

    # Old access token is immediately revoked (401); old password rejected; new password succeeds
    assert strict_client.get("/api/user/profile", headers=h).status_code == 401
    assert strict_client.post("/api/auth/login", json={"email": "forgot@example.com", "password": "Password123!"}).status_code == 401
    assert strict_client.post("/api/auth/login", json={"email": "forgot@example.com", "password": "NewStrongPassword99!"}).status_code == 200


def test_admin_users_rate_cards_and_billing_overview(strict_client):
    uid, h_user = _make_user(strict_client, "normal@example.com", "Normal")
    _adm, h_admin = _make_user(strict_client, "boss@example.com", "Boss", is_admin=True)

    # Non-admin blocked on all admin endpoints
    assert strict_client.get("/api/admin/users", headers=h_user).status_code == 403
    assert strict_client.get("/api/admin/rate-cards", headers=h_user).status_code == 403
    assert strict_client.get("/api/admin/billing/overview", headers=h_user).status_code == 403

    # Admin updates gpt-image-2.6-flare-ox price -> estimate reflects new price immediately
    r_patch = strict_client.patch(
        "/api/admin/rate-cards/gpt-image-2.6-flare-ox",
        json={"unit_price_credits": 50.0},
        headers=h_admin,
    )
    assert r_patch.status_code == 200 and r_patch.json()["unit_price_credits"] == 50.0
    est = strict_client.post("/api/model-platform/estimate", json={"model_key": "gpt-image-2.6-flare-ox", "params": {"count": 2}}).json()
    assert est["estimated_credits"] == 100.0

    # Admin disables normal user -> their active access token immediately returns 401
    assert strict_client.patch(f"/api/admin/users/{uid}", json={"is_active": False}, headers=h_admin).status_code == 200
    assert strict_client.get("/api/user/profile", headers=h_user).status_code == 401

    # Admin overview returns aggregated platform metrics
    ov = strict_client.get("/api/admin/billing/overview", headers=h_admin).json()
    assert ov["total_users"] == 2
    assert "generation_jobs_by_status" in ov


def test_byok_runtime_routing_rpm_limit_and_zero_platform_deduction(strict_client, monkeypatch):
    uid, h = _make_user(strict_client, "byok_rt@example.com", "ByokUser")
    _adm, h_admin = _make_user(strict_client, "byok_adm@example.com", "Admin", is_admin=True)
    proj = strict_client.post("/api/projects", json={"name": "BYOK Studio"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Enable BYOK, add Ark channel with rpm_limit=1 and offering for seedream-5.0-pro-ox
    strict_client.put(f"/api/admin/model-platform/byok-entitlement/{uid}", json={"enabled": True, "max_channels": 2, "max_offerings_per_channel": 5}, headers=h_admin)
    ch = strict_client.post(
        "/api/user/model-platform/channels",
        json={"name": "Custom Ark", "protocol_profile": "ark_images", "base_url": "https://ark.custom.test", "api_key": "sk-byok-live-key-8888", "rpm_limit": 1},
        headers=h,
    ).json()
    strict_client.post(
        "/api/user/model-platform/offerings",
        json={"channel_id": ch["id"], "canonical_model_key": "seedream-5.0-pro-ox", "upstream_model_id": "ep-custom-seedream"},
        headers=h,
    )

    captured: dict = {}

    def fake_ark_invoke(self, *, base_url, api_key, model_id, mode, prompt, params, callback_url=None):
        captured.update({"base_url": base_url, "api_key": api_key, "model_id": model_id})
        return providers.AdapterCallResult(
            mock=False,
            artifacts=[{"filename": "byok.png", "content_type": "image/png", "bytes": model_platform._MOCK_PNG_BYTES, "meta": {}}],
            metadata={"via": "byok_ark"},
        )

    monkeypatch.setattr(providers.ArkImageAdapter, "invoke", fake_ark_invoke)

    # First BYOK generation routes to user's custom Ark channel, charges 0 platform credits
    r1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "seedream-5.0-pro-ox", "prompt": "BYOK art", "payment_source": "byok"},
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["byok_channel_id"] == ch["id"]
    assert captured == {"base_url": "https://ark.custom.test", "api_key": "sk-byok-live-key-8888", "model_id": "ep-custom-seedream"}
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 300.0
    byok_usg = strict_client.get("/api/user/billing/usage-records?payment_source=byok", headers=h).json()
    assert byok_usg["total"] == 1 and byok_usg["items"][0]["credits_charged"] == 0.0

    # Second immediate call exceeds channel rpm_limit=1 -> 429
    r2 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "seedream-5.0-pro-ox", "prompt": "Too fast", "payment_source": "byok"},
        headers=h,
    )
    assert r2.status_code == 429


def test_six_vendor_adapters_and_async_webhook_idempotency_and_refund(strict_client, monkeypatch):
    # Verify all 6 independent adapters construct vendor-accurate URLs & payloads
    u1, p1 = providers.OpenAIImageAdapter().build_request(base_url="https://oai.test", model_id="gpt-image-2", mode="imagine", prompt="cat", params={"count": 2, "size": "1024x1024"})
    assert u1 == "https://oai.test/v1/images/generations" and p1["n"] == 2

    u2, p2 = providers.ArkImageAdapter().build_request(base_url="https://ark.test", model_id="seedream-5", mode="imagine", prompt="dog", params={})
    assert u2 == "https://ark.test/api/v3/images/generations" and p2["response_format"] == "b64_json"

    u3, p3 = providers.MidjourneyAdapter().build_request(base_url="https://mj.test", model_id="midjourney-v8.2", mode="imagine", prompt="city", params={"stylize": 250, "aspect_ratio": "16:9"}, callback_url="https://cb.test")
    assert u3 == "https://mj.test/mj/submit/imagine" and "--stylize 250" in p3["prompt"] and p3["notifyHook"] == "https://cb.test"
    u3a, p3a = providers.MidjourneyAdapter().build_request(base_url="https://mj.test", model_id="midjourney-v8.2", mode="u1", prompt="city", params={"mj_action": "u1", "parent_external_task_id": "mj_123"})
    assert u3a == "https://mj.test/mj/submit/action" and p3a["taskId"] == "mj_123" and p3a["action"] == "U1"

    u4, p4 = providers.VideoAsyncAdapter().build_request(base_url="https://vid.test", model_id="seedance-2.5", mode="first_frame", prompt="pan", params={"first_frame_object_id": "obj_1", "duration_seconds": 5})
    assert u4 == "https://vid.test/v1/video/generations" and p4["first_frame_object_id"] == "obj_1"

    u5, p5 = providers.MiniMaxMediaAdapter().build_request(base_url="https://mm.test", model_id="speech-2.8-hd", mode="tts", prompt="hello", params={"voice_id": "v1", "emotion": "happy", "generate_subtitles": True})
    assert u5 == "https://mm.test/v1/t2a_v2" and p5["voice_setting"]["emotion"] == "happy" and p5["subtitle_enable"] is True

    u6, p6 = providers.SunoMusicAdapter().build_request(base_url="https://suno.test", model_id="suno-music", mode="song_bgm", prompt="synthwave", params={"version": "suno-v6", "operation": "song_bgm"})
    assert u6 == "https://suno.test/v1/suno/generate" and p6["mv"] == "suno-v6"

    # End-to-end async Webhook completion + idempotency + failure refund
    monkeypatch.setenv("PROVIDER_WEBHOOK_SECRET", "whsec_test_999")
    monkeypatch.setenv("SEEDANCE_BASE_URL", "https://seedance.test")
    monkeypatch.setenv("SEEDANCE_API_KEY", "sk-seedance-test")

    _uid, h = _make_user(strict_client, "wh@example.com", "AsyncVid")
    proj = strict_client.post("/api/projects", json={"name": "Webhook Studio"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    counter = {"n": 0}

    def fake_async_video_submit(self, **kwargs):
        counter["n"] += 1
        return providers.AdapterCallResult(mock=False, artifacts=[], metadata={"queued": True}, pending_external_task_id=f"ext_vid_{counter['n']}")

    monkeypatch.setattr(providers.VideoAsyncAdapter, "invoke", fake_async_video_submit)

    # Submit async video job (5s * 28 = 140 credits reserved)
    r_sub = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/video",
        json={"model_key": "seedance-2.5-pro-zh-tt-720", "mode": "t2v", "prompt": "Ocean", "resolution": "720P", "duration_seconds": 5},
        headers=h,
    )
    assert r_sub.status_code == 201
    assert r_sub.json()["status"] == "running"
    assert r_sub.json()["external_task_id"] == "ext_vid_1"
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["reserved_bonus_credits"] == 140.0

    # Wrong webhook secret rejected with 401
    assert strict_client.post(
        "/api/webhooks/providers/seedance_video",
        headers={"X-Webhook-Secret": "wrong"},
        json={"external_task_id": "ext_vid_1", "status": "succeeded"},
    ).status_code == 401

    # Valid succeeded webhook -> settles 140 credits (300 -> 160), attaches artifact
    b64_mp4 = base64.b64encode(model_platform._MOCK_MP4_BYTES).decode("ascii")
    wh_ok = strict_client.post(
        "/api/webhooks/providers/seedance_video",
        headers={"X-Webhook-Secret": "whsec_test_999"},
        json={"external_task_id": "ext_vid_1", "status": "succeeded", "b64_data": b64_mp4},
    )
    assert wh_ok.status_code == 200 and wh_ok.json()["idempotent"] is False and wh_ok.json()["status"] == "succeeded"
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 160.0

    # Duplicate webhook is idempotent -> wallet stays 160.0
    wh_dup = strict_client.post(
        "/api/webhooks/providers/seedance_video",
        headers={"X-Webhook-Secret": "whsec_test_999"},
        json={"external_task_id": "ext_vid_1", "status": "succeeded", "b64_data": b64_mp4},
    )
    assert wh_dup.status_code == 200 and wh_dup.json()["idempotent"] is True
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 160.0

    # Second async job fails via webhook -> 100% refund of frozen 140 credits (wallet remains 160.0)
    r_sub2 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/video",
        json={"model_key": "seedance-2.5-pro-zh-tt-720", "mode": "t2v", "prompt": "Fail me", "resolution": "720P", "duration_seconds": 5},
        headers=h,
    )
    assert r_sub2.json()["external_task_id"] == "ext_vid_2"
    wh_fail = strict_client.post(
        "/api/webhooks/providers/seedance_video",
        headers={"X-Webhook-Secret": "whsec_test_999"},
        json={"external_task_id": "ext_vid_2", "status": "failed", "error_message": "gpu oom"},
    )
    assert wh_fail.status_code == 200 and wh_fail.json()["status"] == "failed"
    w_final = strict_client.get("/api/user/billing/wallet", headers=h).json()
    assert w_final["available_credits"] == 160.0 and w_final["reserved_bonus_credits"] == 0.0


def test_alembic_0005_upgrade_and_downgrade(monkeypatch, tmp_path):
    db_file = tmp_path / "alembic_p5.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))

    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "0004_p3_skills_mcp_byok")
    alembic_command.upgrade(cfg, "head")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
