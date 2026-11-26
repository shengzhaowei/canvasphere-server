"""Regression tests for two coordinator-found bugs (2026-11-25).

1. Skill descriptions must never re-route a task: run_task_sync routes
   on the ORIGINAL prompt + explicit hint; the supervisor respects the
   preset agent. A skill named "storyboard" whose description contains
   "分镜" used to flip an explicit story task to video.
2. Rejecting a task must mark it distinctly from a run failure:
   failure_reason "已驳回：人工拒绝" and auto_retry_stopped False.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.pop("LLM_API_KEY", None)
os.environ.pop("DATABASE_URL", None)

from app import agents, store  # noqa: E402
from app.checkpoint import reset_checkpointer_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    store.reset_store()
    reset_checkpointer_cache()
    yield
    store.reset_store()
    reset_checkpointer_cache()


def test_explicit_hint_survives_skill_description():
    skills = [{"name": "storyboard",
               "description": "电影级分镜规范：按镜头逐项核对分镜"}]
    out = agents.run_task_sync("写一段关于月亮的故事", agent_hint="story",
                               skills=skills)
    assert out["agent"] == "story"


def test_routing_without_hint_still_uses_original_prompt():
    # No hint: keyword routing on the prompt itself still works.
    assert agents.route_agent("画一张月亮海报图片") == "visual"
    assert agents.route_agent("写一段故事") == "story"


def test_reject_marks_failure_reason_distinctly():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        proj = client.post("/api/projects", json={"name": "p"}).json()
        pid = proj["id"]
        task = client.post(f"/api/projects/{pid}/tasks",
                           json={"prompt": "画一张月亮海报图片"}).json()
        assert task["status"] == "waiting_approval"
        r = client.post(
            f"/api/projects/{pid}/tasks/{task['task_id']}/reject")
        assert r.status_code == 200
        tasks = client.get(f"/api/projects/{pid}/tasks").json()
        rejected = next(t for t in tasks
                        if t["task_id"] == task["task_id"])
        assert rejected["status"] == "failed"
        assert rejected["failure_reason"] == "已驳回：人工拒绝"
        assert rejected["auto_retry_stopped"] is False
