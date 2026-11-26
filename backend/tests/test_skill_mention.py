"""@skill mention validation on task creation + single-source events."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app import skills, store  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset():
    store.reset_store()
    yield
    store.reset_store()


def _project():
    r = client.post("/api/projects", json={"name": "skill-proj"})
    assert r.status_code == 201
    return r.json()["id"]


def test_unknown_skill_400():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"prompt": "写个故事", "skills": ["does-not-exist"]})
    assert r.status_code == 400
    assert "does-not-exist" in r.json()["detail"]


def test_disabled_skill_400():
    pid = _project()
    assert skills.set_skill_enabled(pid, "storyboard", False) is False
    try:
        r = client.post(f"/api/projects/{pid}/tasks",
                        json={"prompt": "写个故事", "skills": ["storyboard"]})
        assert r.status_code == 400
        assert "storyboard" in r.json()["detail"]
    finally:
        skills.set_skill_enabled(pid, "storyboard", True)


def test_enabled_skill_stored_and_named_in_prompt():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"prompt": "写个故事", "skills": ["storyboard"]})
    assert r.status_code == 201
    body = r.json()
    assert body["skills"] == ["storyboard"]
    # only the skill name + one-line description went into the prompt:
    # the mock product text carries the skill name, not the full body
    assert "storyboard" in (body["result"] or "")
    stored = store.get_task(body["task_id"])
    assert stored["skills"] == ["storyboard"]


def test_task_list_events_not_duplicated_after_approve():
    """Event single source via the API: create -> approve, then the
    task list shows each event once, in order."""
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "画一张图"})
    assert r.status_code == 201
    assert r.json()["status"] == "waiting_approval"
    tid = r.json()["task_id"]
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/approve")
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}/tasks")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    events = tasks[0]["events"]
    keys = [(e["type"], e["at"], repr(e["detail"])) for e in events]
    assert len(keys) == len(set(keys)), f"duplicate events: {events}"
    types = [e["type"] for e in events]
    assert types[0] == "created"
    assert "agent_finished" in types
    assert types[-1] == "transition"  # approve transition is last
    assert all(e["task_id"] == tid for e in events)
    # second read returns the same events, still no duplicates
    r2 = client.get(f"/api/projects/{pid}/tasks")
    assert r2.json()[0]["events"] == events
