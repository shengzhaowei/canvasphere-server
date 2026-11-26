"""Shared in-memory store: projects, tasks, canvas elements.

Pure functions over module-level dicts. main.py and mcp_server.py both
use this module so there is exactly one copy of the storage logic.
No persistence: restart loses data unless a checkpoint restores it
(see checkpoint.py / session_host.restore_from_checkpoint).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

projects: dict[str, dict[str, Any]] = {}
tasks: dict[str, dict[str, Any]] = {}          # task_id -> task dict
project_tasks: dict[str, list[str]] = {}       # project_id -> [task_id]
canvas_elements: dict[str, list[dict[str, Any]]] = {}  # project_id -> elements


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def reset_store() -> None:
    """Clear everything. Used by tests; never called by the server."""
    projects.clear()
    tasks.clear()
    project_tasks.clear()
    canvas_elements.clear()


def _price_per_million(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name, default))
    except (TypeError, ValueError):
        return float(default)


def estimate_cost(usage: Any, mock: bool) -> dict[str, Any]:
    """Cost estimate for a finished task. Never invents a real fee.

    Mock runs have no token usage, so the estimate is explicitly a
    mock estimate with amount None. Live runs compute from the
    provider-reported token_usage and the placeholder prices
    (LLM_PRICE_INPUT / LLM_PRICE_OUTPUT, defaults 30/150 积分/百万Token).
    """
    if mock or not isinstance(usage, dict):
        return {"mock_estimate": True, "amount": None, "unit": "积分",
                "note": "mock 模式，无真实 token 用量，不产生费用"}
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion_tokens = usage.get("completion_tokens",
                                  usage.get("output_tokens", 0)) or 0
    try:
        amount = (float(prompt_tokens) * _price_per_million("LLM_PRICE_INPUT", 30.0)
                  + float(completion_tokens) * _price_per_million("LLM_PRICE_OUTPUT", 150.0)) / 1_000_000
    except (TypeError, ValueError):
        return {"mock_estimate": True, "amount": None, "unit": "积分",
                "note": "token 用量无法解析，未计费"}
    return {"mock_estimate": False, "amount": round(amount, 6),
            "unit": "积分", "unit_detail": "积分/百万Token",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prices_are_placeholder": True,
            "note": "占位价，需按所用中转实际价改"}


# --- projects -------------------------------------------------------------
def create_project(name: str) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("project name must not be empty")
    pid = _new_id()
    project = {"id": pid, "name": name.strip(), "created_at": _now()}
    projects[pid] = project
    project_tasks[pid] = []
    canvas_elements[pid] = []
    return dict(project)


def get_project(project_id: str) -> Optional[dict[str, Any]]:
    p = projects.get(project_id)
    return dict(p) if p else None


def list_projects() -> list[dict[str, Any]]:
    return [dict(p) for p in projects.values()]


# --- tasks ----------------------------------------------------------------
def create_task(project_id: str, prompt: str, agent: Optional[str] = None,
                skills: Optional[list[str]] = None) -> dict[str, Any]:
    if project_id not in projects:
        raise KeyError(f"project not found: {project_id}")
    if not prompt or not prompt.strip():
        raise ValueError("prompt must not be empty")
    tid = _new_id()
    task = {
        "task_id": tid,
        "id": tid,
        # Stable id for the whole task lifecycle (incl. retries);
        # returned in failure responses so the client can resume it.
        "request_id": uuid.uuid4().hex,
        "project_id": project_id,
        "prompt": prompt.strip(),
        # No default agent here: None means "route by keywords at run
        # time". Defaulting to "story" used to be passed back as an
        # explicit hint, which silently forced every unhinted task
        # (e.g. a picture task) to story and skipped approval.
        "agent": agent,
        "status": "pending",
        "result": None,
        "element": None,
        "skills": list(skills or []),
        "approval_required": False,
        "mock": True,
        "usage": None,
        "estimated_cost": None,
        "failure_reason": None,
        "auto_retry_stopped": False,
        # Kept for backwards compat; the single source of truth for
        # events is SessionHost.events -- main.py merges host events
        # into this field when listing tasks, deduped.
        "events": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    tasks[tid] = task
    project_tasks[project_id].append(tid)
    return dict(task)


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    t = tasks.get(task_id)
    return dict(t) if t else None


def list_tasks(project_id: str) -> list[dict[str, Any]]:
    if project_id not in projects:
        raise KeyError(f"project not found: {project_id}")
    return [dict(tasks[tid]) for tid in project_tasks.get(project_id, [])]


def update_task(task_id: str, **fields: Any) -> dict[str, Any]:
    if task_id not in tasks:
        raise KeyError(f"task not found: {task_id}")
    tasks[task_id].update(fields)
    tasks[task_id]["updated_at"] = _now()
    return dict(tasks[task_id])


def apply_agent_result(task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Record an agent run. Approval-gated results wait; others are done.

    The agent's complete canvas element (result["element"], a full
    dict with type/x/y/w/h/text) is stored verbatim in task["element"]
    so approve() can flow it onto the canvas unchanged. The canvas
    element itself is NOT added here: callers add it when the task
    reaches done (immediately, or on approve). See main.py / mcp_server.py.
    A successful run clears any previous failure state.
    """
    if task_id not in tasks:
        raise KeyError(f"task not found: {task_id}")
    approval = bool(result.get("approval_required"))
    status = "waiting_approval" if approval else "done"
    mock = bool(result.get("mock", True))
    element = result.get("element")
    return update_task(
        task_id,
        agent=result.get("agent", tasks[task_id]["agent"]),
        status=status,
        result=result.get("result") or result.get("text"),
        element=dict(element) if isinstance(element, dict) else None,
        approval_required=approval,
        mock=mock,
        usage=result.get("usage"),
        estimated_cost=estimate_cost(result.get("usage"), mock),
        failure_reason=None,
        auto_retry_stopped=False,
    )


# --- canvas ---------------------------------------------------------------
def add_element(project_id: str, type: str, x: float, y: float,
                w: float, h: float, text: str = "") -> dict[str, Any]:
    if project_id not in projects:
        raise KeyError(f"project not found: {project_id}")
    if not type or not type.strip():
        raise ValueError("element type must not be empty")
    el = {
        "id": _new_id(),
        "type": type.strip(),
        "x": float(x), "y": float(y),
        "w": float(w), "h": float(h),
        "text": text or "",
        "created_at": _now(),
    }
    canvas_elements[project_id].append(el)
    return dict(el)


def list_elements(project_id: str) -> list[dict[str, Any]]:
    if project_id not in projects:
        raise KeyError(f"project not found: {project_id}")
    return [dict(e) for e in canvas_elements.get(project_id, [])]


def get_element(project_id: str, node_id: str) -> Optional[dict[str, Any]]:
    for e in canvas_elements.get(project_id, []):
        if e["id"] == node_id:
            return dict(e)
    return None


def update_element(project_id: str, node_id: str, text: str) -> dict[str, Any]:
    for e in canvas_elements.get(project_id, []):
        if e["id"] == node_id:
            e["text"] = text
            return dict(e)
    raise KeyError(f"element not found: {node_id} in project {project_id}")
