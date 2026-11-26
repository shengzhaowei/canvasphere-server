"""Database-backed shared store: projects, canvases, canvas objects, tasks.

Both HTTP routes (main.py / routers) and MCP tools (mcp_server.py) call
this module so there is one single source of truth for storage logic.
Backed by SQLAlchemy (PostgreSQL in production, in-memory SQLite in unit
tests when DATABASE_URL is unset).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import Base, get_engine, session_scope
from app.models import (
    Canvas,
    CanvasObject,
    Project,
    ProjectMember,
    Task,
    new_prefixed_id,
    utcnow,
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _project_dict(p: Project, default_canvas_id: Optional[str] = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "workspace_id": p.workspace_id,
        "name": p.name,
        "status": p.status,
        "owner_id": p.owner_id,
        "cover_url": p.cover_url,
        "cover_type": p.cover_type,
        "last_activity_at": _iso(p.last_activity_at),
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }
    if default_canvas_id is not None:
        out["default_canvas_id"] = default_canvas_id
    return out


def _task_dict(t: Task) -> dict[str, Any]:
    return {
        "task_id": t.id,
        "id": t.id,
        "request_id": t.request_id,
        "project_id": t.project_id,
        "canvas_id": t.canvas_id,
        "prompt": t.prompt,
        "agent": t.agent,
        "status": t.status,
        "result": t.result,
        "element": dict(t.element_json) if isinstance(t.element_json, dict) else None,
        "skills": list(t.skills_json or []),
        "approval_required": bool(t.approval_required),
        "mock": bool(t.mock),
        "usage": t.usage_json,
        "estimated_cost": t.estimated_cost_json,
        "failure_reason": t.failure_reason,
        "auto_retry_stopped": bool(t.auto_retry_stopped),
        "events": list(t.events_json or []),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def _element_dict(obj: CanvasObject) -> dict[str, Any]:
    return {
        "id": obj.id,
        "project_id": obj.project_id,
        "canvas_id": obj.canvas_id,
        "type": obj.type,
        "name": obj.name,
        "x": float(obj.x),
        "y": float(obj.y),
        "w": float(obj.w),
        "h": float(obj.h),
        "rotation": float(obj.rotation),
        "z_index": int(obj.z_index),
        "text": obj.text or "",
        "content": obj.content_json,
        "asset": obj.asset_json,
        "style": obj.style_json,
        "version": int(obj.version),
        "created_at": _iso(obj.created_at),
        "updated_at": _iso(obj.updated_at),
    }


def reset_store() -> None:
    """Clear all rows in every table. Used by tests; never called by production server."""
    eng = get_engine()
    with eng.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(delete(table))


def estimate_cost(usage: Any, mock: bool) -> dict[str, Any]:
    """Cost estimate for a finished task. Never invents a real fee."""
    if mock or not isinstance(usage, dict):
        return {
            "mock_estimate": True,
            "amount": None,
            "unit": "积分",
            "note": "mock 模式，无真实 token 用量，不产生费用",
        }
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    settings = get_settings()
    try:
        amount = (
            float(prompt_tokens) * settings.llm_price_input
            + float(completion_tokens) * settings.llm_price_output
        ) / 1_000_000
    except (TypeError, ValueError):
        return {
            "mock_estimate": True,
            "amount": None,
            "unit": "积分",
            "note": "token 用量无法解析，未计费",
        }
    return {
        "mock_estimate": False,
        "amount": round(amount, 6),
        "unit": "积分",
        "unit_detail": "积分/百万Token",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prices_are_placeholder": True,
        "note": "占位价，需按所用中转实际价改",
    }


# --- projects -------------------------------------------------------------
def create_project(name: str, owner_id: Optional[str] = None) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("project name must not be empty")
    now = utcnow()
    with session_scope() as db:
        proj = Project(
            id=new_prefixed_id("prj"),
            workspace_id=new_prefixed_id("wsp"),
            name=name.strip(),
            status="active",
            owner_id=owner_id,
            last_activity_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(proj)
        db.flush()

        canvas = Canvas(
            id=new_prefixed_id("can"),
            project_id=proj.id,
            name="默认画板",
            is_default=True,
            sort_order=0,
            created_at=now,
            updated_at=now,
        )
        db.add(canvas)

        if owner_id:
            db.add(ProjectMember(
                project_id=proj.id,
                user_id=owner_id,
                role="owner",
                joined_at=now,
            ))

        db.flush()
        return _project_dict(proj, default_canvas_id=canvas.id)


def get_project(project_id: str) -> Optional[dict[str, Any]]:
    with session_scope() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status == "deleted":
            return None
        default_canvas = db.scalar(
            select(Canvas.id)
            .where(Canvas.project_id == project_id)
            .order_by(Canvas.is_default.desc(), Canvas.sort_order.asc(), Canvas.created_at.asc())
            .limit(1)
        )
        return _project_dict(proj, default_canvas_id=default_canvas)


def list_projects() -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = db.scalars(
            select(Project)
            .where(Project.status != "deleted")
            .order_by(Project.created_at.asc())
        ).all()
        return [_project_dict(p) for p in rows]


def _resolve_canvas_id(db, project_id: str, canvas_id: Optional[str] = None) -> str:
    if canvas_id:
        c = db.get(Canvas, canvas_id)
        if c is None or c.project_id != project_id:
            raise KeyError(f"canvas not found: {canvas_id} in project {project_id}")
        return c.id
    c = db.scalar(
        select(Canvas)
        .where(Canvas.project_id == project_id)
        .order_by(Canvas.is_default.desc(), Canvas.sort_order.asc(), Canvas.created_at.asc())
        .limit(1)
    )
    if c is None:
        now = utcnow()
        c = Canvas(
            id=new_prefixed_id("can"),
            project_id=project_id,
            name="默认画板",
            is_default=True,
            sort_order=0,
            created_at=now,
            updated_at=now,
        )
        db.add(c)
        db.flush()
    return c.id


# --- tasks ----------------------------------------------------------------
def create_task(
    project_id: str,
    prompt: str,
    agent: Optional[str] = None,
    skills: Optional[list[str]] = None,
    canvas_id: Optional[str] = None,
) -> dict[str, Any]:
    if not prompt or not prompt.strip():
        raise ValueError("prompt must not be empty")
    now = utcnow()
    with session_scope() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status == "deleted":
            raise KeyError(f"project not found: {project_id}")
        resolved_canvas_id = _resolve_canvas_id(db, project_id, canvas_id) if canvas_id else None
        task = Task(
            id=new_prefixed_id("tsk"),
            request_id=uuid.uuid4().hex,
            project_id=project_id,
            canvas_id=resolved_canvas_id,
            prompt=prompt.strip(),
            agent=agent,
            status="pending",
            result=None,
            element_json=None,
            skills_json=list(skills or []),
            approval_required=False,
            mock=True,
            usage_json=None,
            estimated_cost_json=None,
            failure_reason=None,
            auto_retry_stopped=False,
            events_json=[],
            created_at=now,
            updated_at=now,
        )
        proj.last_activity_at = now
        db.add(task)
        db.flush()
        return _task_dict(task)


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with session_scope() as db:
        task = db.get(Task, task_id)
        return _task_dict(task) if task else None


def list_tasks(project_id: str) -> list[dict[str, Any]]:
    with session_scope() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status == "deleted":
            raise KeyError(f"project not found: {project_id}")
        rows = db.scalars(
            select(Task)
            .where(Task.project_id == project_id)
            .order_by(Task.created_at.asc())
        ).all()
        return [_task_dict(t) for t in rows]


_TASK_FIELD_MAP = {
    "agent": "agent",
    "status": "status",
    "result": "result",
    "element": "element_json",
    "skills": "skills_json",
    "approval_required": "approval_required",
    "mock": "mock",
    "usage": "usage_json",
    "estimated_cost": "estimated_cost_json",
    "failure_reason": "failure_reason",
    "auto_retry_stopped": "auto_retry_stopped",
    "events": "events_json",
}


def update_task(task_id: str, **fields: Any) -> dict[str, Any]:
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        for key, value in fields.items():
            col = _TASK_FIELD_MAP.get(key)
            if col is not None:
                setattr(task, col, value)
        task.updated_at = utcnow()
        db.flush()
        return _task_dict(task)


def apply_agent_result(task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Record an agent run. Approval-gated results wait; others are done."""
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        approval = bool(result.get("approval_required"))
        status = "waiting_approval" if approval else "done"
        mock = bool(result.get("mock", True))
        element = result.get("element")
        task.agent = result.get("agent", task.agent)
        task.status = status
        task.result = result.get("result") or result.get("text")
        task.element_json = dict(element) if isinstance(element, dict) else None
        task.approval_required = approval
        task.mock = mock
        task.usage_json = result.get("usage")
        task.estimated_cost_json = estimate_cost(result.get("usage"), mock)
        task.failure_reason = None
        task.auto_retry_stopped = False
        task.updated_at = utcnow()
        db.flush()
        return _task_dict(task)


# --- canvas ---------------------------------------------------------------
def add_element(
    project_id: str,
    type: str,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str = "",
    canvas_id: Optional[str] = None,
    name: str = "",
    content: Optional[dict[str, Any]] = None,
    asset: Optional[dict[str, Any]] = None,
    style: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not type or not type.strip():
        raise ValueError("element type must not be empty")
    now = utcnow()
    with session_scope() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status == "deleted":
            raise KeyError(f"project not found: {project_id}")
        cid = _resolve_canvas_id(db, project_id, canvas_id)
        obj = CanvasObject(
            id=new_prefixed_id("obj"),
            project_id=project_id,
            canvas_id=cid,
            type=type.strip(),
            name=name or "",
            x=float(x),
            y=float(y),
            w=float(w),
            h=float(h),
            text=text or "",
            content_json=content,
            asset_json=asset,
            style_json=style,
            version=1,
            created_at=now,
            updated_at=now,
        )
        proj.last_activity_at = now
        db.add(obj)
        db.flush()
        return _element_dict(obj)


def list_elements(project_id: str, canvas_id: Optional[str] = None) -> list[dict[str, Any]]:
    with session_scope() as db:
        proj = db.get(Project, project_id)
        if proj is None or proj.status == "deleted":
            raise KeyError(f"project not found: {project_id}")
        stmt = select(CanvasObject).where(
            CanvasObject.project_id == project_id,
            CanvasObject.deleted_at.is_(None),
        )
        if canvas_id:
            _resolve_canvas_id(db, project_id, canvas_id)
            stmt = stmt.where(CanvasObject.canvas_id == canvas_id)
        rows = db.scalars(stmt.order_by(CanvasObject.created_at.asc())).all()
        return [_element_dict(o) for o in rows]


def get_element(project_id: str, node_id: str) -> Optional[dict[str, Any]]:
    with session_scope() as db:
        obj = db.get(CanvasObject, node_id)
        if obj is None or obj.project_id != project_id or obj.deleted_at is not None:
            return None
        return _element_dict(obj)


def update_element(project_id: str, node_id: str, text: str) -> dict[str, Any]:
    with session_scope() as db:
        obj = db.get(CanvasObject, node_id)
        if obj is None or obj.project_id != project_id:
            raise KeyError(f"element not found: {node_id} in project {project_id}")
        obj.text = text
        obj.version = int(obj.version) + 1
        obj.updated_at = utcnow()
        proj = db.get(Project, project_id)
        if proj is not None:
            proj.last_activity_at = obj.updated_at
        db.flush()
        return _element_dict(obj)
