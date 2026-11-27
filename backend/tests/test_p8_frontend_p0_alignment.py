"""Phase P8 tests: OpenLux /v1 URL normalization, live_configured model catalog gating,
sync-live-models CLI, GET .../studio/runs approval recovery, ensure_default starter project,
409 VERSION_CONFLICT current_object payload, and legacy OpenAPI deprecation flags.
"""
from __future__ import annotations

from pathlib import Path

from app import auth, cli
from app.providers.adapters import OpenAIImageAdapter, VideoAsyncAdapter


def _register(client, email: str, name: str = "P8 Creator"):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "Password123!", "code": code, "display_name": name},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_adapter_v1_url_normalization_and_live_models_filter(strict_client, monkeypatch):
    # Both with and without trailing /v1 produce exactly one /v1 segment
    img_adapter = OpenAIImageAdapter()
    u1, _ = img_adapter.build_request(
        base_url="https://openlux.example.com",
        model_id="gpt-image-2.6-flare",
        mode="imagine",
        prompt="cat",
        params={"count": 1},
    )
    u2, _ = img_adapter.build_request(
        base_url="https://openlux.example.com/v1/",
        model_id="gpt-image-2.6-flare",
        mode="imagine",
        prompt="cat",
        params={"count": 1},
    )
    assert u1 == "https://openlux.example.com/v1/images/generations"
    assert u2 == "https://openlux.example.com/v1/images/generations"

    vid_adapter = VideoAsyncAdapter()
    v1, _ = vid_adapter.build_request(
        base_url="https://video.example.com/v1",
        model_id="seedance",
        mode="t2v",
        prompt="ocean",
        params={},
    )
    assert v1 == "https://video.example.com/v1/video/generations"

    # Without vendor keys, live_only=true returns 0 image models
    monkeypatch.delenv("OPENAI_IMAGE_API_KEY", raising=False)
    r_empty = strict_client.get("/api/model-platform/models?modality=image&live_only=true")
    assert r_empty.status_code == 200
    assert r_empty.json()["total"] == 0

    # With OPENAI_IMAGE_API_KEY set, OpenLux/Qwen/Gemini image models report live_configured=True
    monkeypatch.setenv("OPENAI_IMAGE_BASE_URL", "https://openlux.example.com/v1")
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", "sk-openlux-test")
    r_live = strict_client.get("/api/model-platform/models?modality=image&live_only=true")
    assert r_live.status_code == 200
    live_models = r_live.json()["models"]
    assert len(live_models) > 0
    assert all(m["live_configured"] is True for m in live_models)
    assert any(m["model_key"] == "gpt-image-2.6-flare" for m in live_models)

    # sync-live-models CLI restricts enabled rate cards to only the verified key(s)
    synced = cli.cmd_sync_live_models(enable_keys=["gpt-image-2.6-flare"])
    assert synced["enabled_keys"] == ["gpt-image-2.6-flare"]
    assert synced["disabled_count"] >= 60

    r_after = strict_client.get("/api/model-platform/models")
    assert r_after.status_code == 200
    assert [m["model_key"] for m in r_after.json()["models"]] == ["gpt-image-2.6-flare"]


def test_first_login_ensure_default_project_idempotent(strict_client):
    headers = _register(strict_client, "p8-starter@example.com")

    # Without ensure_default, brand-new user has 0 projects (preserves backward compatibility)
    r0 = strict_client.get("/api/projects", headers=headers)
    assert r0.status_code == 200
    assert r0.json()["total"] == 0

    # With ensure_default=true, auto-provisions 1 starter project with default canvas
    r1 = strict_client.get("/api/projects?ensure_default=true", headers=headers)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["total"] == 1
    assert b1["items"][0]["name"] == "我的第一个项目"
    assert b1["items"][0]["default_canvas_id"] is not None
    first_pid = b1["items"][0]["id"]

    # Calling again is idempotent (no duplicate starter project)
    r2 = strict_client.get("/api/projects?ensure_default=true", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["total"] == 1
    assert r2.json()["items"][0]["id"] == first_pid


def test_studio_runs_list_recovers_waiting_approval_after_reload(strict_client):
    headers = _register(strict_client, "p8-runs@example.com")
    proj = strict_client.get("/api/projects?ensure_default=true", headers=headers).json()["items"][0]
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Default tier for xiaoa's generate_image is always -> suspends in waiting_approval
    chat = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        headers=headers,
        json={"prompt": "生成一张霓虹夜景图", "agent": "xiaoa"},
    )
    assert chat.status_code == 201, chat.text
    sid = chat.json()["session"]["id"]
    run_id = chat.json()["run"]["id"]
    fence = chat.json()["run"]["generation_fence"]
    assert chat.json()["run"]["status"] == "waiting_approval"

    # Simulate page reload: list waiting_approval runs for this session
    pending = strict_client.get(
        f"/api/projects/{pid}/studio/runs?session_id={sid}&status=waiting_approval",
        headers=headers,
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["total"] == 1
    assert pending.json()["items"][0]["id"] == run_id
    assert pending.json()["items"][0]["generation_fence"] == fence
    assert pending.json()["items"][0]["pending_tool"]["tool"] == "generate_image"

    # Approve -> waiting_approval list becomes empty
    appr = strict_client.post(
        f"/api/projects/{pid}/studio/runs/{run_id}/approve",
        headers=headers,
        json={"expected_fence": fence},
    )
    assert appr.status_code == 200, appr.text
    after = strict_client.get(
        f"/api/projects/{pid}/studio/runs?session_id={sid}&status=waiting_approval",
        headers=headers,
    )
    assert after.status_code == 200
    assert after.json()["total"] == 0


def test_canvas_409_version_conflict_includes_current_object_and_legacy_deprecated(strict_client):
    headers = _register(strict_client, "p8-409@example.com")
    proj = strict_client.get("/api/projects?ensure_default=true", headers=headers).json()["items"][0]
    pid, cid = proj["id"], proj["default_canvas_id"]

    created = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=headers,
        json={"type": "text", "name": "Note 1", "text": "v1"},
    )
    assert created.status_code == 201
    oid = created.json()["id"]

    # Advance version to 2
    p1 = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{oid}",
        headers=headers,
        json={"expected_version": 1, "text": "v2"},
    )
    assert p1.status_code == 200
    assert p1.json()["version"] == 2

    # Stale PATCH with expected_version=1 returns 409 including full current_object
    p_stale = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{oid}",
        headers=headers,
        json={"expected_version": 1, "text": "stale"},
    )
    assert p_stale.status_code == 409
    detail = p_stale.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["current_version"] == 2
    assert detail["current_object"]["id"] == oid
    assert detail["current_object"]["text"] == "v2"
    assert detail["current_object"]["version"] == 2

    # OpenAPI marks legacy /tasks, /canvas, /skills routes deprecated=True
    schema = strict_client.get("/openapi.json").json()
    assert schema["paths"]["/api/projects/{project_id}/tasks"]["post"].get("deprecated") is True
    assert schema["paths"]["/api/projects/{project_id}/canvas"]["get"].get("deprecated") is True
    assert schema["paths"]["/api/skills/{name}/toggle"]["post"].get("deprecated") is True
    assert not schema["paths"]["/api/projects/{project_id}/canvases/{canvas_id}/studio/chat/stream"]["post"].get("deprecated")

    # Verify frontend P0 contract doc exists and covers all key endpoints
    repo_root = Path(__file__).resolve().parents[2]
    doc = (repo_root / "docs" / "frontend-p0-contract.md").read_text(encoding="utf-8")
    assert "ensure_default=true" in doc
    assert "studio/chat/stream" in doc
    assert "live_only=true" in doc
    assert "current_object" in doc
