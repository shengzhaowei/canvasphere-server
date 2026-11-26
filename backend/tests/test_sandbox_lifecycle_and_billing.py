"""P2 tests: sandbox config, keepalive reuse, failure-free billing, spend cap 402 & idle reaping."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from app import auth, db, sandbox
from app.models import SandboxSession, utcnow


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


def test_sandbox_config_billing_failure_free_cap_and_idle_reap(strict_client, monkeypatch):
    _uid, h = _make_user(strict_client, "sbx@example.com", "Builder")
    pid = strict_client.post("/api/projects", json={"name": "3D Previs"}, headers=h).json()["id"]

    # Default config: disabled, 2核4G, cap 500, idle 2 min
    cfg = strict_client.get(f"/api/projects/{pid}/sandbox", headers=h).json()
    assert cfg["enabled"] is False
    assert cfg["spec_name"] == "2核4G"
    assert cfg["spend_cap_credits"] == 500.0
    assert cfg["idle_keep_minutes"] == 2

    # Executing while disabled -> 403
    assert strict_client.post(f"/api/projects/{pid}/sandbox/execute", json={"code": "print(1)"}, headers=h).status_code == 403

    # Enable with 4核8G (96.40 credits/hr) and cap 100
    cfg2 = strict_client.patch(
        f"/api/projects/{pid}/sandbox",
        json={"enabled": True, "spec_name": "4核8G", "spend_cap_credits": 100.0},
        headers=h,
    ).json()
    assert cfg2["enabled"] is True and cfg2["spec_name"] == "4核8G"

    # Mock successful sandbox execution (1800s = 0.5h -> 48.20 credits) + HTML artifact output
    monkeypatch.setattr(
        sandbox,
        "run_code",
        lambda code, timeout=30: {"sandbox": "local-docker", "stdout": "ok\n", "stderr": "", "exit_code": 0, "error": None},
    )
    r1 = strict_client.post(
        f"/api/projects/{pid}/sandbox/execute",
        json={
            "code": "print('neon vector')",
            "simulated_duration_seconds": 1800,
            "output_files": [{"filename": "neon_vector.html", "content_type": "text/html", "content": "<h1>3D Previs</h1>"}],
        },
        headers=h,
    )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["failure_free"] is False
    assert b1["credits_charged"] == 48.2
    assert len(b1["output_files"]) == 1
    p_html = urlparse(b1["output_files"][0]["url"])
    assert strict_client.get(p_html.path + "?" + p_html.query).status_code == 200

    # Wallet deducted 48.2 from 300 -> 251.8
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 251.8

    # Failed execution is failure-free (0 credits) and reuses same keepalive session
    monkeypatch.setattr(
        sandbox,
        "run_code",
        lambda code, timeout=30: {"sandbox": "local-docker", "stdout": "", "stderr": "ZeroDivisionError", "exit_code": 1, "error": None},
    )
    r_fail = strict_client.post(
        f"/api/projects/{pid}/sandbox/execute",
        json={"code": "1/0", "simulated_duration_seconds": 600},
        headers=h,
    )
    assert r_fail.status_code == 200
    assert r_fail.json()["session_id"] == b1["session_id"]
    assert r_fail.json()["failure_free"] is True
    assert r_fail.json()["credits_charged"] == 0.0
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 251.8

    # Sandbox billing records contain both runs
    recs = strict_client.get("/api/user/billing/sandbox-records", headers=h).json()
    assert recs["total"] == 2

    # Next run costing 96.40 exceeds remaining cap (100 - 48.2 = 51.8) -> 402
    monkeypatch.setattr(
        sandbox,
        "run_code",
        lambda code, timeout=30: {"sandbox": "local-docker", "stdout": "ok", "stderr": "", "exit_code": 0, "error": None},
    )
    r_cap = strict_client.post(
        f"/api/projects/{pid}/sandbox/execute",
        json={"code": "print('heavy')", "simulated_duration_seconds": 3600},
        headers=h,
    )
    assert r_cap.status_code == 402

    # Simulate idle > 2 min -> reap-idle stops session
    with db.session_scope() as s:
        sess = s.get(SandboxSession, b1["session_id"])
        sess.last_active_at = utcnow() - timedelta(minutes=5)
        s.add(sess)

    reaped = strict_client.post(f"/api/projects/{pid}/sandbox/reap-idle", headers=h).json()
    assert b1["session_id"] in reaped["reaped_sessions"]
    assert strict_client.get(f"/api/projects/{pid}/sandbox", headers=h).json()["active_session"] is None
