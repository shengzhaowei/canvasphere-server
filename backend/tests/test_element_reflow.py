"""Complete element dict flows back onto the canvas verbatim on approve."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app import agents, store  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

VERBATIM = {"type": "image", "x": 123, "y": 456, "w": 111, "h": 222,
            "text": "verbatim-text"}


@pytest.fixture(autouse=True)
def _reset():
    store.reset_store()
    yield
    store.reset_store()


def _project():
    r = client.post("/api/projects", json={"name": "reflow-proj"})
    assert r.status_code == 201
    return r.json()["id"]


def test_complete_element_stored_and_approved_verbatim(monkeypatch):
    pid = _project()

    def gated(prompt, hint=None, skills=None):
        return {"agent": "visual", "result": "verbatim-text",
                "text": "verbatim-text", "mock": True,
                "approval_required": True, "element": dict(VERBATIM),
                "usage": None}

    monkeypatch.setattr(agents, "run_task_sync", gated)
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "画一张图"})
    assert r.status_code == 201
    assert r.json()["status"] == "waiting_approval"
    # the complete element dict is stored in the task, verbatim
    assert r.json()["element"] == VERBATIM
    tid = r.json()["task_id"]
    assert store.get_task(tid)["element"] == VERBATIM
    # nothing on the canvas yet (approval-gated)
    assert client.get(f"/api/projects/{pid}/canvas").json()["elements"] == []

    r = client.post(f"/api/projects/{pid}/tasks/{tid}/approve")
    assert r.status_code == 200
    els = client.get(f"/api/projects/{pid}/canvas").json()["elements"]
    assert len(els) == 1
    el = els[0]
    assert el["type"] == "image"
    assert (el["x"], el["y"], el["w"], el["h"]) == (123, 456, 111, 222)
    assert el["text"] == "verbatim-text"


def test_missing_element_falls_back_to_rebuild(monkeypatch):
    pid = _project()

    def gated_no_element(prompt, hint=None, skills=None):
        return {"agent": "visual", "result": "fallback text",
                "text": "fallback text", "mock": True,
                "approval_required": True, "element": None,
                "usage": None}

    monkeypatch.setattr(agents, "run_task_sync", gated_no_element)
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "画一张图"})
    assert r.json()["status"] == "waiting_approval"
    assert r.json()["element"] is None
    tid = r.json()["task_id"]
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/approve")
    assert r.status_code == 200
    els = client.get(f"/api/projects/{pid}/canvas").json()["elements"]
    assert len(els) == 1
    # fallback: agent type -> image, default geometry, result text
    assert els[0]["type"] == "image"
    assert (els[0]["x"], els[0]["y"], els[0]["w"], els[0]["h"]) == (40, 40, 320, 180)
    assert els[0]["text"] == "fallback text"


def test_non_gated_task_element_flows_immediately():
    pid = _project()
    r = client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写个故事"})
    assert r.status_code == 201
    assert r.json()["status"] == "done"
    el = r.json()["element"]
    assert el and el["type"] == "text"
    els = client.get(f"/api/projects/{pid}/canvas").json()["elements"]
    assert len(els) == 1
    assert els[0]["text"] == el["text"]
