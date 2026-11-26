"""FastAPI app (port 8000). Memory store + optional Postgres checkpoint.

Mode: "live" when LLM_API_KEY is set, else "mock".
Role enforcement is server-side: the X-Role header (default owner)
is checked with permissions.check_permission on every mutating route;
viewer (read-only) gets 403 on task creation / canvas writes / approve.

Events: SessionHost.events is the single source of truth. list_tasks
merges host events into each task's `events` field, deduped.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app import agents, store  # noqa: E402
from app.checkpoint import get_checkpointer  # noqa: E402
from app.permissions import (  # noqa: E402
    ALL_TOOLS, TIERS, assert_permission, check_permission,
    get_policy, grant_tool, has_grant, require_approval,
    set_tool_tier, tier_of,
)
from app.skills import is_skill_enabled, list_skills, load_skill, set_skill_enabled  # noqa: E402

app = FastAPI(title="canvasphere backend")
# CORS：前端在 5173、后端在 8000，是跨源调用。浏览器发请求前会先发
# OPTIONS 预检；没有这个中间件时 FastAPI 对 OPTIONS 返回 405，
# 前端 fetch 就会报 Failed to fetch（2026-11-26 线上实测）。
# 允许的源从 CORS_ORIGINS（逗号分隔）读，默认含本机 + 线上 IP。
_cors_origins = [
    o.strip() for o in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://8.220.194.80:5173",
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
session_host = None  # created lazily to avoid import-time side effects


def _host():
    global session_host
    if session_host is None:
        from app.session_host import SessionHost
        session_host = SessionHost()
        session_host.start()
    return session_host


def _mode() -> str:
    return "live" if os.environ.get("LLM_API_KEY") else "mock"


def _role(x_role: Optional[str]) -> str:
    return (x_role or "owner").strip().lower()


def _require_role(role: str, tool: str) -> None:
    try:
        assert_permission(role, tool)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


def _require_owner(role: str) -> None:
    if role != "owner":
        raise HTTPException(status_code=403, detail="only owner can do this")


def _task_view(task: dict[str, Any], host: Any = None) -> dict[str, Any]:
    """Task dict with events merged from the single source (host log).

    Same event appears once, in log order. Legacy task["events"] (if
    any were stored before the single-source change) are merged in
    too, deduped by (type, at, detail).
    """
    out = dict(task)
    merged: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def _add(ev: Any) -> None:
        if not isinstance(ev, dict):
            return
        key = (ev.get("type"), ev.get("at"), repr(ev.get("detail")))
        if key in seen:
            return
        seen.add(key)
        merged.append(ev)

    for ev in (task.get("events") or []):
        _add(ev)
    if host is not None:
        for ev in host.events_for(task["task_id"]):
            _add(ev)
    # Keep insertion (chronological) order; host log is already ordered.
    out["events"] = merged
    return out


def _add_element_verbatim(project_id: str, el: dict[str, Any]) -> dict[str, Any]:
    """Add the agent's complete element dict onto the canvas as-is."""
    return store.add_element(project_id, el["type"], el.get("x", 40),
                             el.get("y", 40), el.get("w", 320),
                             el.get("h", 180), el.get("text", ""))


def _maybe_add_element(project_id: str, result: dict[str, Any]) -> None:
    el = result.get("element")
    if isinstance(el, dict) and el.get("type"):
        try:
            _add_element_verbatim(project_id, el)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to add canvas element: %s", exc)


def _validate_skills(project_id: str, skills: Optional[list[str]]) -> list[dict[str, Any]]:
    """Validate @skill mentions: each must exist and be enabled here.

    Returns [{name, description}] for the agent prompt context.
    Raises ValueError with a clear message on the first bad skill.
    """
    out: list[dict[str, Any]] = []
    for name in (skills or []):
        try:
            skill = load_skill(name)
        except FileNotFoundError:
            raise ValueError(f"skill not found: {name}")
        except ValueError as exc:
            raise ValueError(str(exc))
        if not is_skill_enabled(project_id, name):
            raise ValueError(f"skill is disabled in this project: {name}")
        out.append({"name": skill["name"], "description": skill.get("description", "")})
    return out


def _run_agent_for_task(project_id: str, task: dict[str, Any],
                        host: Any) -> dict[str, Any]:
    """Run agents.run_task_sync for a task, recording success/failure.

    On success returns the updated task (done / waiting_approval).
    On exception the task stays failed with failure_reason +
    auto_retry_stopped=True and the message '已停止自动重试'; the
    exception is NOT retried automatically.
    """
    skill_ctx = _validate_skills(project_id, task.get("skills") or [])
    try:
        result = agents.run_task_sync(task["prompt"], task.get("agent"),
                                      skills=skill_ctx)
    except Exception as exc:  # noqa: BLE001 - failure is a task outcome
        logger.warning("agent run failed for task %s: %s", task["task_id"], exc)
        updated = store.update_task(
            task["task_id"], status="failed",
            failure_reason=str(exc), auto_retry_stopped=True)
        if task["task_id"] in host.machines:
            try:
                host.transition(task["task_id"], "failed", {"error": str(exc)})
            except Exception:  # noqa: BLE001 - machine may already be failed
                pass
        host.append_event(task["task_id"], "failed", {"error": str(exc)})
        return updated
    updated = store.apply_agent_result(task["task_id"], result)
    if result.get("approval_required"):
        host.transition(task["task_id"], "waiting_approval")
    else:
        host.transition(task["task_id"], "done")
        _maybe_add_element(project_id, result)
    host.append_event(task["task_id"], "agent_finished",
                      {"agent": result["agent"], "mock": result["mock"]})
    return updated


def _public_task(task: dict[str, Any], host: Any = None) -> dict[str, Any]:
    t = _task_view(task, host)
    return {
        "task_id": t["task_id"], "request_id": t.get("request_id"),
        "project_id": t["project_id"], "prompt": t["prompt"],
        "agent": t["agent"], "status": t["status"],
        "result": t.get("result"), "element": t.get("element"),
        "skills": t.get("skills") or [],
        "approval_required": t["approval_required"], "mock": t["mock"],
        "usage": t.get("usage"), "estimated_cost": t.get("estimated_cost"),
        "failure_reason": t.get("failure_reason"),
        "auto_retry_stopped": bool(t.get("auto_retry_stopped")),
        "events": t.get("events") or [],
        "created_at": t.get("created_at"), "updated_at": t.get("updated_at"),
    }


# --- schemas --------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str


class TaskCreate(BaseModel):
    prompt: str
    agent: Optional[str] = None
    skills: Optional[list[str]] = None


class ElementCreate(BaseModel):
    type: str
    x: float
    y: float
    w: float
    h: float
    text: str = ""


class SkillToggle(BaseModel):
    project_id: str
    enabled: bool


class ApprovalPolicyUpdate(BaseModel):
    tool: Optional[str] = None
    tier: Optional[str] = None
    updates: Optional[dict[str, str]] = None


class ApprovalGrantCreate(BaseModel):
    tool: str


# --- routes ---------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    _kind = get_checkpointer()[1]
    return {"status": "ok", "mode": _mode(), "checkpointer": _kind}


def _price(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name, default))
    except (TypeError, ValueError):
        return float(default)


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Model + placeholder pricing + sandbox config.

    Prices are placeholders (LLM_PRICE_INPUT / LLM_PRICE_OUTPUT,
    defaults 30/150 积分/百万Token); the response always says so.
    base_url returns only the host -- never a key.
    """
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    raw_base = os.environ.get("LLM_BASE_URL", "")
    host = urlparse(raw_base).netloc if raw_base else ""
    if os.environ.get("E2B_API_KEY"):
        provider = "e2b"
    elif shutil.which("docker"):
        provider = "local-docker"
    else:
        provider = "unavailable"
    return {
        "model": model,
        "base_url": host,
        "models": [{
            "name": model,
            "input_price": _price("LLM_PRICE_INPUT", 30.0),
            "output_price": _price("LLM_PRICE_OUTPUT", 150.0),
            "unit": "积分/百万Token",
        }],
        "sandbox": {
            "provider": provider,
            "specs": [
                {"name": "2核4G", "price_per_hour": 53.20},
                {"name": "4核8G", "price_per_hour": 96.40},
            ],
            "cap": 500,
            "idle_keep_minutes": 2,
            "failure_free": True,
        },
        "prices_are_placeholder": True,
        "placeholder_note": "占位价，需按所用中转实际价改",
    }


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate) -> dict[str, Any]:
    try:
        return store.create_project(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    return store.list_projects()


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    p = store.get_project(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return p


@app.get("/api/projects/{project_id}/approval-policy")
def get_approval_policy(project_id: str) -> dict[str, Any]:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    policy = get_policy(project_id)
    policy["require_approval"] = {
        tool: require_approval(tool, project_id) for tool in ALL_TOOLS}
    return policy


@app.post("/api/projects/{project_id}/approval-policy")
def update_approval_policy(project_id: str, body: ApprovalPolicyUpdate,
                           x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_owner(role)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    updates: dict[str, str] = {}
    if body.updates:
        updates.update(body.updates)
    if body.tool is not None:
        if body.tier is None:
            raise HTTPException(status_code=400, detail="tier is required when tool is given")
        updates[body.tool] = body.tier
    if not updates:
        raise HTTPException(status_code=400, detail="provide tool+tier or updates")
    try:
        for tool, tier in updates.items():
            set_tool_tier(project_id, tool, tier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    policy = get_policy(project_id)
    policy["require_approval"] = {
        tool: require_approval(tool, project_id) for tool in ALL_TOOLS}
    return policy


@app.post("/api/projects/{project_id}/approval-grants", status_code=201)
def create_approval_grant(project_id: str, body: ApprovalGrantCreate,
                          x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_role(role, "delegate_task")
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    try:
        grant_tool(project_id, body.tool)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"project_id": project_id, "tool": body.tool, "granted": True,
            "tier": tier_of(body.tool, project_id),
            "require_approval": require_approval(body.tool, project_id)}


@app.post("/api/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: str, body: TaskCreate,
                x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_role(role, "delegate_task")
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    try:
        _validate_skills(project_id, body.skills)
        task = store.create_task(project_id, body.prompt, body.agent,
                                 skills=body.skills)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    host = _host()
    host.create(task["task_id"]) if task["task_id"] not in host.machines else None
    host.transition(task["task_id"], "running")
    updated = _run_agent_for_task(project_id, task, host)
    out = _public_task(updated, host)
    if updated["status"] == "failed":
        out["message"] = "已停止自动重试"
    return out


@app.get("/api/projects/{project_id}/tasks")
def list_tasks(project_id: str) -> list[dict[str, Any]]:
    try:
        tasks = store.list_tasks(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    host = _host()
    return [_public_task(t, host) for t in tasks]


@app.post("/api/projects/{project_id}/tasks/{task_id}/retry")
def retry_task(project_id: str, task_id: str,
               x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Resume a failed task in the same session / same task.

    Re-runs agents.run_task_sync. Success -> done/waiting_approval;
    failure -> stays failed with an updated failure_reason. Retrying
    a done task returns 409. request_id never changes.
    """
    role = _role(x_role)
    _require_role(role, "delegate_task")
    task = store.get_task(task_id)
    if task is None or task["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    if task["status"] == "done":
        raise HTTPException(status_code=409, detail="task is already done")
    if task["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"task is {task['status']}, only failed tasks can be retried")
    host = _host()
    if task_id in host.machines:
        try:
            host.reset_for_retry(task_id)
        except ValueError:
            # Machine not in failed state (e.g. fresh host after restart):
            # re-create it so the retry can run.
            del host.machines[task_id]
            host.create(task_id)
    else:
        host.create(task_id)
    host.transition(task_id, "running")
    updated = _run_agent_for_task(project_id, task, host)
    out = _public_task(updated, host)
    if updated["status"] == "failed":
        out["message"] = "已停止自动重试"
    return out


@app.post("/api/projects/{project_id}/tasks/{task_id}/approve")
def approve_task(project_id: str, task_id: str,
                 x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_role(role, "delegate_task")
    task = store.get_task(task_id)
    if task is None or task["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    if task["status"] != "waiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"task is {task['status']}, not waiting_approval")
    # Prefer the complete element stored at run time, verbatim.
    el = task.get("element")
    if isinstance(el, dict) and el.get("type"):
        try:
            _add_element_verbatim(project_id, el)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to add approved canvas element: %s", exc)
    else:
        # Fallback only when no element was stored: rebuild from the
        # agent type + result text (positions/sizes are defaults).
        logger.warning(
            "approve task %s: no stored element, rebuilding from "
            "agent %r + result text", task_id, task.get("agent"))
        result = task.get("result")
        el_type = {"story": "text", "visual": "image",
                   "video": "video", "audio": "audio"}.get(task["agent"], "text")
        store.add_element(project_id, el_type, 40, 40, 320, 180, str(result or ""))
    updated = store.update_task(task_id, status="done")
    host = _host()
    if task_id in host.machines and host.get(task_id).status == "waiting_approval":
        host.transition(task_id, "done", {"approved_by": role})
    return {"task_id": task_id, "status": updated["status"], "approved": True}


@app.post("/api/projects/{project_id}/tasks/{task_id}/reject")
def reject_task(project_id: str, task_id: str,
                x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_role(role, "delegate_task")
    task = store.get_task(task_id)
    if task is None or task["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    if task["status"] not in ("waiting_approval", "running", "pending"):
        raise HTTPException(
            status_code=409, detail=f"task is {task['status']}, cannot reject")
    # Mark rejected distinctly from a run failure, so the frontend can
    # tell "rejected by a human" apart from "failed, auto-retry stopped".
    updated = store.update_task(
        task_id, status="failed",
        failure_reason="已驳回：人工拒绝",
        auto_retry_stopped=False)
    host = _host()
    if task_id in host.machines and host.get(task_id).status in (
            "waiting_approval", "running", "pending"):
        host.transition(task_id, "failed", {"rejected_by": role})
    return {"task_id": task_id, "status": updated["status"], "approved": False}


@app.get("/api/projects/{project_id}/canvas")
def get_canvas(project_id: str) -> dict[str, Any]:
    try:
        return {"project_id": project_id, "elements": store.list_elements(project_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/projects/{project_id}/canvas/elements", status_code=201)
def add_canvas_element(project_id: str, body: ElementCreate,
                       x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    _require_role(role, "add_node")
    try:
        return store.add_element(project_id, body.type, body.x, body.y,
                                 body.w, body.h, body.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/skills")
def get_skills(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    out = []
    for s in list_skills():
        entry = dict(s)
        entry.pop("content", None)
        entry["enabled"] = (is_skill_enabled(project_id, s["name"])
                            if project_id else True)
        out.append(entry)
    return out


@app.post("/api/skills/{name}/toggle")
def toggle_skill(name: str, body: SkillToggle,
                 x_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    role = _role(x_role)
    if not check_permission(role, "delegate_task"):
        raise HTTPException(status_code=403, detail="viewer cannot toggle skills")
    try:
        load_skill(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    enabled = set_skill_enabled(body.project_id, name, body.enabled)
    return {"name": name, "project_id": body.project_id, "enabled": enabled}


# --- MCP over HTTP/SSE (streamable-http), same tools as stdio ------------
# FastMCP requires its http app's lifespan to run in the parent app:
# without it the StreamableHTTPSessionManager task group is never
# initialised and every /mcp request 500s. Wire it onto the router
# (FastAPI reads router.lifespan_context at lifespan startup).
try:
    from app.mcp_server import mcp as _mcp  # noqa: E402
    _mcp_app = _mcp.http_app(path="/", transport="streamable-http")
    app.router.lifespan_context = _mcp_app.lifespan
    app.mount("/mcp", _mcp_app)
    logger.info("MCP streamable-http mounted at /mcp")
except Exception as exc:  # noqa: BLE001 - stdio MCP still works
    logger.warning("MCP http transport unavailable (%s); stdio only", exc)
