"""Tiered approval (Pecan-style three tiers) + session grants + policy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app import permissions, store  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset():
    store.reset_store()
    permissions.reset_approval_state()
    yield
    store.reset_store()
    permissions.reset_approval_state()


def _project():
    r = client.post("/api/projects", json={"name": "tier-proj"})
    assert r.status_code == 201
    return r.json()["id"]


def test_default_tiers():
    assert permissions.tier_of("read_canvas") == "auto"
    assert permissions.tier_of("list_project_files") == "auto"
    for tool in ("add_node", "update_node", "delegate_task",
                 "toggle_skill", "run_code"):
        assert permissions.tier_of(tool) == "session", tool
    for tool in ("generate_image", "generate_video", "delete_node",
                 "open_sandbox", "manage_subagent", "update_plan"):
        assert permissions.tier_of(tool) == "always", tool


def test_require_approval_by_tier_and_grant():
    pid = "proj-grant-1"
    assert permissions.require_approval("read_canvas", pid) is False
    assert permissions.require_approval("generate_image", pid) is True
    # session tier: needs approval until granted, then passes
    assert permissions.require_approval("add_node", pid) is True
    permissions.grant_tool(pid, "add_node")
    assert permissions.require_approval("add_node", pid) is False
    # grant is per-project
    assert permissions.require_approval("add_node", "other-proj") is True
    # unknown tool fails closed
    assert permissions.require_approval("exec_shell", pid) is True


def test_policy_endpoint_returns_tiers_and_grants():
    pid = _project()
    r = client.get(f"/api/projects/{pid}/approval-policy")
    assert r.status_code == 200
    body = r.json()
    assert body["tiers"]["auto"] == ["read_canvas", "list_project_files"]
    assert set(body["tiers"]["session"]) == {
        "add_node", "update_node", "delegate_task", "toggle_skill", "run_code"}
    assert set(body["tiers"]["always"]) == {
        "generate_image", "generate_video", "delete_node", "open_sandbox",
        "manage_subagent", "update_plan"}
    assert body["grants"] == []
    assert body["require_approval"]["read_canvas"] is False
    assert body["require_approval"]["generate_image"] is True
    assert body["require_approval"]["add_node"] is True


def test_grant_endpoint_confirms_tool():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/approval-grants",
                    json={"tool": "delegate_task"})
    assert r.status_code == 201
    assert r.json() == {"project_id": pid, "tool": "delegate_task",
                        "granted": True, "tier": "session",
                        "require_approval": False}
    r = client.get(f"/api/projects/{pid}/approval-policy")
    assert "delegate_task" in r.json()["grants"]
    assert r.json()["require_approval"]["delegate_task"] is False
    # viewer cannot grant
    r = client.post(f"/api/projects/{pid}/approval-grants",
                    json={"tool": "add_node"}, headers={"X-Role": "viewer"})
    assert r.status_code == 403
    # unknown tool -> 400
    r = client.post(f"/api/projects/{pid}/approval-grants",
                    json={"tool": "exec_shell"})
    assert r.status_code == 400


def test_policy_update_owner_only_and_validates():
    pid = _project()
    # viewer / editor cannot change policy
    for role in ("viewer", "editor"):
        r = client.post(f"/api/projects/{pid}/approval-policy",
                        json={"tool": "add_node", "tier": "auto"},
                        headers={"X-Role": role})
        assert r.status_code == 403, role
    # owner moves add_node to auto
    r = client.post(f"/api/projects/{pid}/approval-policy",
                    json={"tool": "add_node", "tier": "auto"})
    assert r.status_code == 200
    assert r.json()["tiers"]["auto"] == ["read_canvas", "list_project_files", "add_node"] \
        or "add_node" in r.json()["tiers"]["auto"]
    assert r.json()["require_approval"]["add_node"] is False
    assert permissions.tier_of("add_node", pid) == "auto"
    # unknown tool / tier -> 400
    r = client.post(f"/api/projects/{pid}/approval-policy",
                    json={"tool": "exec_shell", "tier": "auto"})
    assert r.status_code == 400
    r = client.post(f"/api/projects/{pid}/approval-policy",
                    json={"tool": "add_node", "tier": "sometimes"})
    assert r.status_code == 400
    # missing project -> 404
    r = client.post("/api/projects/nope/approval-policy",
                    json={"tool": "add_node", "tier": "auto"})
    assert r.status_code == 404


def test_config_placeholder_prices():
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["prices_are_placeholder"] is True
    assert "占位价，需按所用中转实际价改" in body["placeholder_note"]
    assert body["models"][0]["unit"] == "积分/百万Token"
    assert body["models"][0]["input_price"] == 30.0
    assert body["models"][0]["output_price"] == 150.0
    assert "LLM_API_KEY" not in str(body) and "key" not in body.get("base_url", "")
    assert body["sandbox"]["specs"] == [
        {"name": "2核4G", "price_per_hour": 53.20},
        {"name": "4核8G", "price_per_hour": 96.40}]
    assert body["sandbox"]["cap"] == 500
    assert body["sandbox"]["idle_keep_minutes"] == 2
    assert body["sandbox"]["failure_free"] is True
