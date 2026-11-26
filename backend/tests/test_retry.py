"""Failure without auto-retry + POST .../retry resume + stable request_id."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app import agents, store  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

GOOD = {"agent": "story", "result": "ok", "text": "ok", "mock": True,
        "approval_required": False,
        "element": {"type": "text", "x": 40, "y": 40, "w": 320, "h": 180,
                    "text": "ok"},
        "usage": None}


@pytest.fixture(autouse=True)
def _reset():
    store.reset_store()
    yield
    store.reset_store()


def _project():
    r = client.post("/api/projects", json={"name": "retry-proj"})
    assert r.status_code == 201
    return r.json()["id"]


def test_fail_then_retry_succeeds_request_id_stable(monkeypatch):
    pid = _project()
    calls = {"n": 0}

    def flaky(prompt, hint=None, skills=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom: llm down")
        return dict(GOOD)

    monkeypatch.setattr(agents, "run_task_sync", flaky)
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "boom: llm down"
    assert body["auto_retry_stopped"] is True
    assert body["message"] == "已停止自动重试"
    assert body["request_id"], "failure response must carry request_id"
    tid, rid = body["task_id"], body["request_id"]

    r = client.post(f"/api/projects/{pid}/tasks/{tid}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["request_id"] == rid, "request_id must not change across retry"
    assert body["failure_reason"] is None
    assert body["auto_retry_stopped"] is False
    # stored task agrees
    stored = store.get_task(tid)
    assert stored["status"] == "done"
    assert stored["request_id"] == rid


def test_retry_stays_failed_on_second_failure(monkeypatch):
    pid = _project()
    monkeypatch.setattr(
        agents, "run_task_sync",
        lambda prompt, hint=None, skills=None: (_ for _ in ()).throw(
            RuntimeError("still down")))
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    assert r.json()["status"] == "failed"
    tid, rid = r.json()["task_id"], r.json()["request_id"]
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["failure_reason"] == "still down"
    assert r.json()["request_id"] == rid


def test_retry_done_task_returns_409():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    assert r.status_code == 201
    assert r.json()["status"] == "done"
    tid = r.json()["task_id"]
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/retry")
    assert r.status_code == 409


def test_retry_unknown_task_404_and_viewer_403():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks/nope/retry")
    assert r.status_code == 404
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    tid = r.json()["task_id"]
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/retry",
                    headers={"X-Role": "viewer"})
    assert r.status_code == 403


def test_mock_task_reports_usage_null_and_mock_estimate():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    assert r.status_code == 201
    body = r.json()
    assert body["usage"] is None
    assert body["estimated_cost"]["mock_estimate"] is True
    assert body["estimated_cost"]["amount"] is None
