"""P2 tests: 5-agent Studio orchestration, canvas DAG context, token compression & TaskRun fences."""
from __future__ import annotations

from datetime import timedelta

from app import auth, db
from app.models import TaskRun, utcnow


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


def test_5_agents_dag_graph_context_and_token_compression_to_private_storage(strict_client, monkeypatch):
    monkeypatch.setenv("CONTEXT_COMPRESS_THRESHOLD_TOKENS", "30")
    _uid, h = _make_user(strict_client, "studio@example.com", "Director")
    proj = strict_client.post("/api/projects", json={"name": "SciFi Film"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # 5 Pecan Studio agents present
    ags = strict_client.get(f"/api/projects/{pid}/studio/agents", headers=h).json()["agents"]
    assert {a["id"] for a in ags} == {"pecan", "orange", "xiaoa", "blueberry", "yueyue"}

    # Allow add_node auto so story runs finish immediately for multi-turn compression testing
    strict_client.post(f"/api/projects/{pid}/approval-policy", json={"tool": "add_node", "tier": "auto"}, headers=h)

    # Build a 3-node DAG: Style Bible -> Character Sheet -> Scene Shot
    n1 = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "document", "name": "Style Bible", "text": "Neon noir wet asphalt palette"}, headers=h).json()
    n2 = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "text", "name": "Character Sheet", "text": "Detective K2026 in trenchcoat"}, headers=h).json()
    n3 = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "image", "name": "Scene 1"}, headers=h).json()
    strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "connector", "from_object_id": n1["id"], "to_object_id": n2["id"]}, headers=h)
    strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "connector", "from_object_id": n2["id"], "to_object_id": n3["id"]}, headers=h)

    # First prompt lazily creates session + traverses upstream DAG n1 -> n2 -> n3
    r1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        json={"prompt": "根据参考写一段开场故事 @storyboard", "agent": "orange", "context_object_ids": [n3["id"]]},
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    sid = b1["session"]["id"]
    assert b1["run"]["primary_agent"] == "orange"
    assert b1["run"]["status"] == "done"
    assert {n["id"] for n in b1["graph_context"]["nodes"]} == {n1["id"], n2["id"], n3["id"]}

    # Subsequent long prompts trigger automatic token compression into private_runtime storage
    for idx in range(3):
        r = strict_client.post(
            f"/api/projects/{pid}/canvases/{cid}/studio/chat",
            json={"session_id": sid, "agent": "orange", "prompt": f"续写第 {idx + 2} 场详细对白与环境描写：" + "雨夜霓虹街道" * 15},
            headers=h,
        )
        assert r.status_code == 201

    msgs = strict_client.get(f"/api/projects/{pid}/studio/sessions/{sid}/messages", headers=h).json()
    assert msgs["session"]["compressed_count"] >= 1
    assert msgs["session"]["summary_storage_id"]
    assert msgs["session"]["context_summary"]

    # Private runtime storage recorded under my_private_used_bytes
    st = strict_client.get(f"/api/projects/{pid}/storage", headers=h).json()
    assert st["my_private_used_bytes"] > 0


def test_task_run_idempotency_approval_fence_and_stale_recovery(strict_client):
    _uid, h = _make_user(strict_client, "ops@example.com", "Ops")
    proj = strict_client.post("/api/projects", json={"name": "Reliable"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Visual task (xiaoa -> generate_image ALWAYS tier) stops at waiting_approval
    req_id = "idemp-req-001"
    r1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        json={"prompt": "画一张赛博朋克海报", "agent": "xiaoa", "request_id": req_id},
        headers=h,
    )
    assert r1.status_code == 201
    run = r1.json()["run"]
    assert run["status"] == "waiting_approval"
    assert run["pending_tool"]["tool"] == "generate_image"
    assert run["generation_fence"] == 1

    # Replaying same request_id returns exact same TaskRun without creating a duplicate
    r_dup = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        json={"prompt": "画一张赛博朋克海报", "agent": "xiaoa", "request_id": req_id},
        headers=h,
    )
    assert r_dup.status_code == 201
    assert r_dup.json()["idempotent_replay"] is True
    assert r_dup.json()["run"]["id"] == run["id"]

    # Stale generation fence rejected with 409
    assert strict_client.post(
        f"/api/projects/{pid}/studio/runs/{run['id']}/approve",
        json={"expected_fence": 999},
        headers=h,
    ).status_code == 409

    # Valid approve completes run and materializes canvas node
    approved = strict_client.post(
        f"/api/projects/{pid}/studio/runs/{run['id']}/approve",
        json={"expected_fence": 1},
        headers=h,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "done"
    assert approved.json()["result"]["created_object_id"]

    # Recovery bundle accessible
    bundle = strict_client.get(f"/api/projects/{pid}/studio/runs/{run['id']}/recovery-bundle", headers=h).json()
    assert bundle["run_id"] == run["id"] and bundle["generation_fence"] == 1

    # Simulate an in-flight run whose lease expired -> recover bumps fence & marks failed
    r_stale = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        json={"prompt": "画分镜", "agent": "blueberry"},
        headers=h,
    ).json()["run"]
    with db.session_scope() as s:
        row = s.get(TaskRun, r_stale["id"])
        row.status = "running"
        row.lease_expires_at = utcnow() - timedelta(minutes=5)
        s.add(row)

    rec = strict_client.post(f"/api/projects/{pid}/studio/runs/recover", headers=h).json()
    assert rec["count"] == 1
    assert rec["recovered"][0]["run_id"] == r_stale["id"]
    assert rec["recovered"][0]["generation_fence"] == 2
