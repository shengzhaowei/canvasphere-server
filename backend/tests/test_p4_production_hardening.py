"""P4 tests: rate limiting (429), ARQ async generation dispatch, 4C8G sandbox concurrency cap & orphan cleanup."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from app import auth, db, sandbox, worker
from app.models import Project, StorageObject, utcnow


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


def test_auth_rate_limiting_returns_429_with_retry_after(strict_client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EMAIL_CODE_PER_MIN", "2")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_MIN", "2")

    assert strict_client.post("/api/auth/email/send-code", json={"email": "rl@example.com"}).status_code == 200
    assert strict_client.post("/api/auth/email/send-code", json={"email": "rl@example.com"}).status_code == 200
    r3 = strict_client.post("/api/auth/email/send-code", json={"email": "rl@example.com"})
    assert r3.status_code == 429
    assert int(r3.headers.get("retry-after", "0")) >= 1

    # Login brute-force protection
    assert strict_client.post("/api/auth/login", json={"email": "nope@example.com", "password": "wrong"}).status_code == 401
    assert strict_client.post("/api/auth/login", json={"email": "nope@example.com", "password": "wrong"}).status_code == 401
    assert strict_client.post("/api/auth/login", json={"email": "nope@example.com", "password": "wrong"}).status_code == 429


def test_async_worker_generation_dispatch_and_background_execution(strict_client, monkeypatch):
    _uid, h = _make_user(strict_client, "arq@example.com", "AsyncUser")
    proj = strict_client.post("/api/projects", json={"name": "Async Gen"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    enqueued_ids: list[str] = []
    monkeypatch.setattr(worker, "enqueue_generation_job", lambda jid: (enqueued_ids.append(jid) or True))

    r = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "gpt-image-2.6-flare", "prompt": "Async sunset"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["dispatched_async"] is True
    assert enqueued_ids == [body["id"]]

    # Credits are pre-frozen while queued (18.0 credits reserved out of 300)
    w_queued = strict_client.get("/api/user/billing/wallet", headers=h).json()
    assert w_queued["reserved_bonus_credits"] == 18.0
    assert w_queued["available_credits"] == 282.0

    # ARQ worker runs the job asynchronously -> succeeds & releases mock reservation
    res = asyncio.run(worker.execute_generation_job_task({}, body["id"]))
    assert res["status"] == "succeeded"
    assert res["output"]["target_object_id"]

    w_done = strict_client.get("/api/user/billing/wallet", headers=h).json()
    assert w_done["reserved_bonus_credits"] == 0.0
    assert w_done["available_credits"] == 300.0


def test_4c8g_sandbox_global_concurrency_guardrail(strict_client, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_CONCURRENT", "1")
    monkeypatch.setattr(
        sandbox,
        "run_code",
        lambda code, timeout=30: {"sandbox": "local-docker", "stdout": "ok", "stderr": "", "exit_code": 0, "error": None},
    )
    _uid, h = _make_user(strict_client, "conc@example.com", "HostGuard")
    p1 = strict_client.post("/api/projects", json={"name": "Proj A"}, headers=h).json()["id"]
    p2 = strict_client.post("/api/projects", json={"name": "Proj B"}, headers=h).json()["id"]
    strict_client.patch(f"/api/projects/{p1}/sandbox", json={"enabled": True}, headers=h)
    strict_client.patch(f"/api/projects/{p2}/sandbox", json={"enabled": True}, headers=h)

    # Project A acquires the single host slot
    assert strict_client.post(f"/api/projects/{p1}/sandbox/execute", json={"code": "print(1)", "simulated_duration_seconds": 60}, headers=h).status_code == 200
    # Project A can reuse its own active keepalive session
    assert strict_client.post(f"/api/projects/{p1}/sandbox/execute", json={"code": "print(2)", "simulated_duration_seconds": 60}, headers=h).status_code == 200
    # Project B is blocked with 503 SANDBOX_CONCURRENCY_LIMIT while A is active
    r_b = strict_client.post(f"/api/projects/{p2}/sandbox/execute", json={"code": "print(3)", "simulated_duration_seconds": 60}, headers=h)
    assert r_b.status_code == 503

    # Once Project A stops its sandbox, Project B can run
    assert strict_client.post(f"/api/projects/{p1}/sandbox/stop", headers=h).status_code == 200
    assert strict_client.post(f"/api/projects/{p2}/sandbox/execute", json={"code": "print(3)", "simulated_duration_seconds": 60}, headers=h).status_code == 200


def test_orphan_upload_and_deleting_project_cleanup_and_configs(strict_client):
    _uid, h = _make_user(strict_client, "clean@example.com", "Cleaner")
    p1 = strict_client.post("/api/projects", json={"name": "Will Delete"}, headers=h).json()["id"]

    # Create a pending upload and backdate it > 24h
    up = strict_client.post(
        f"/api/projects/{p1}/storage/upload-url",
        json={"filename": "abandoned.png", "content_type": "image/png", "size_bytes": 100, "space": "workspace"},
        headers=h,
    ).json()
    strict_client.delete(f"/api/projects/{p1}", headers=h)

    old_ts = utcnow() - timedelta(hours=30)
    with db.session_scope() as s:
        sto = s.get(StorageObject, up["storage_id"])
        sto.created_at = old_ts
        s.add(sto)
        proj = s.get(Project, p1)
        proj.deleted_at = old_ts
        s.add(proj)

    summary = worker.cleanup_orphan_storage(max_age_hours=24)
    assert summary["cleaned_pending_uploads"] >= 1
    assert summary["finalized_deleting_projects"] == 1

    # Verify health reports redis & async_worker + verify 4C8G deploy configs exist
    health = strict_client.get("/health").json()
    assert health["redis"] == "memory"
    assert health["async_worker"] is False

    repo_root = Path(__file__).resolve().parents[2]
    nginx_conf = (repo_root / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    compose_yml = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "proxy_buffering off;" in nginx_conf
    assert "client_max_body_size 64m;" in nginx_conf
    assert "arq" in compose_yml and "max_connections=60" in compose_yml and "--maxmemory" in compose_yml
