"""End-to-end wire contract test replaying exact request/response shapes from canvasphere/src/services/api.ts."""
from __future__ import annotations

from app import auth


def test_frontend_api_ts_exact_wire_contract(strict_client, monkeypatch):
    # 1. Register & login -> verify AuthUser shape (id, has_password, email_verified, roles)
    strict_client.post("/api/auth/email/send-code", json={"email": "fe-wire@example.com", "purpose": "register"})
    code = auth.get_last_sent_code("fe-wire@example.com", "register")
    reg = strict_client.post(
        "/api/auth/register",
        json={"email": "fe-wire@example.com", "password": "Password123!", "code": code, "display_name": "FE Tester"},
    )
    assert reg.status_code == 201, reg.text
    user = reg.json()["user"]
    assert user["id"] == user["user_id"]
    assert user["has_password"] is True
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    prof = strict_client.get("/api/user/profile", headers=headers)
    assert prof.status_code == 200
    assert prof.json()["id"] == user["id"]
    assert prof.json()["has_password"] is True

    # 2. GET /api/projects?ensure_default=true&sort=activity&search= -> returns { projects, total } with role & member_count
    p_list = strict_client.get("/api/projects?ensure_default=true&sort=activity", headers=headers)
    assert p_list.status_code == 200, p_list.text
    p_body = p_list.json()
    assert len(p_body["projects"]) == 1
    proj = p_body["projects"][0]
    assert proj["role"] == "owner"
    assert proj["member_count"] == 1
    pid = proj["id"]
    cid = proj["default_canvas_id"]

    # 3. Studio stream with frontend's `agent_hint` field + GET .../studio/runs returning `runs` + messages returning `session_id`
    stream = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat/stream",
        headers=headers,
        json={
            "prompt": "帮我画一张赛博夜雨街道",
            "agent_hint": "xiaoa",
            "request_id": "fe-wire-req-1",
            "enable_tool_loop": False,
        },
    )
    assert stream.status_code == 200, stream.text
    assert "event: approval_required\n" in stream.text

    sessions = strict_client.get(f"/api/projects/{pid}/studio/sessions", headers=headers).json()["sessions"]
    assert len(sessions) == 1
    sid = sessions[0]["id"]

    msgs = strict_client.get(f"/api/projects/{pid}/studio/sessions/{sid}/messages", headers=headers).json()["messages"]
    assert len(msgs) >= 1
    assert msgs[0]["session_id"] == sid

    runs = strict_client.get(f"/api/projects/{pid}/studio/runs?session_id={sid}&status=waiting_approval", headers=headers).json()
    assert len(runs["runs"]) == 1
    assert runs["runs"][0]["primary_agent"] == "xiaoa"

    # 4. Image generation with frontend's nested `params: { count, output_format }` + `mode`
    gen = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        headers=headers,
        json={
            "model_key": "gpt-image-2.6-flare-ox",
            "prompt": "neon cyberpunk street",
            "mode": "imagine",
            "params": {"count": 2, "output_format": "webp"},
            "payment_source": "platform_credits",
        },
    )
    assert gen.status_code == 201, gen.text
    job = gen.json()
    assert job["status"] == "succeeded"
    assert len(job["output"]["artifacts"]) == 2
    assert job["output"]["artifacts"][0]["content_type"] == "image/webp"

    # 5. Price explanation returning { models, total } and supporting ?search=
    pe = strict_client.get("/api/user/billing/price-explanation?modality=image&search=flare", headers=headers)
    assert pe.status_code == 200, pe.text
    assert pe.json()["total"] >= 1
    assert any(m["model_key"] == "gpt-image-2.6-flare-ox" for m in pe.json()["models"])

    # 6. CORS preflight for Vite dev port 5175 succeeds
    cors = strict_client.options(
        "/api/projects",
        headers={
            "Origin": "http://localhost:5175",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert cors.status_code == 200
    assert cors.headers.get("access-control-allow-origin") == "http://localhost:5175"
