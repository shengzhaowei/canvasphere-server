"""Pecan Studio multi-agent chat, lazy sessions, TaskRun approvals & recovery routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, studio_agents
from app.db import get_db
from app.models import AgentMessage, AgentSession, Canvas, Project, TaskRun, new_prefixed_id, utcnow
from app.permissions import can_write

router = APIRouter(tags=["studio"])


class StudioSessionCreateRequest(BaseModel):
    canvas_id: Optional[str] = None
    title: str = "Pecan Studio 会话"


class StudioChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    agent: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    context_object_ids: list[str] = Field(default_factory=list)
    request_id: Optional[str] = None


class RunApproveRequest(BaseModel):
    expected_fence: Optional[int] = None
    grant_session_tool: bool = False


class RunRejectRequest(BaseModel):
    expected_fence: Optional[int] = None
    reason: str = "已驳回：人工拒绝"


@router.get("/api/projects/{project_id}/studio/agents")
def list_studio_agents(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    return {"agents": list(studio_agents.STUDIO_AGENTS.values())}


@router.get("/api/projects/{project_id}/studio/sessions")
def list_studio_sessions(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    rows = db.scalars(
        select(AgentSession)
        .where(AgentSession.project_id == project_id)
        .order_by(AgentSession.updated_at.desc())
    ).all()
    return {"sessions": [studio_agents.serialize_session(s) for s in rows], "total": len(rows)}


@router.post("/api/projects/{project_id}/studio/sessions", status_code=201)
def create_studio_session(
    project_id: str,
    body: StudioSessionCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot create studio sessions")
    now = utcnow()
    s = AgentSession(
        id=new_prefixed_id("ses"),
        project_id=project_id,
        canvas_id=body.canvas_id,
        user_id=user.id if user else None,
        title=(body.title or "Pecan Studio 会话").strip()[:128],
        created_at=now,
        updated_at=now,
    )
    db.add(s)
    db.commit()
    return studio_agents.serialize_session(s)


@router.get("/api/projects/{project_id}/studio/sessions/{session_id}/messages")
def list_session_messages(
    project_id: str,
    session_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    s = db.get(AgentSession, session_id)
    if s is None or s.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"studio session not found: {session_id}")
    msgs = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.created_at.asc())
    ).all()
    return {
        "session": studio_agents.serialize_session(s),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "agent_name": m.agent_name,
                "content": m.content,
                "skills": m.skills_json,
                "context_object_ids": m.context_object_ids_json,
                "tool_calls": m.tool_calls_json,
                "token_estimate": int(m.token_estimate),
                "compressed": bool(m.compressed),
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/studio/chat", status_code=201)
def studio_chat(
    project_id: str,
    canvas_id: str,
    body: StudioChatRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot send studio chat prompts")
    proj = db.get(Project, project_id)
    assert proj is not None
    canvas = db.get(Canvas, canvas_id)
    if canvas is None or canvas.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")

    return studio_agents.run_studio_chat(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        prompt=body.prompt,
        session_id=body.session_id,
        agent_hint=body.agent,
        explicit_skills=body.skills,
        context_object_ids=body.context_object_ids,
        request_id=body.request_id,
    )


@router.get("/api/projects/{project_id}/studio/runs/{run_id}")
def get_studio_run(
    project_id: str,
    run_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    run = db.get(TaskRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"task run not found: {run_id}")
    return studio_agents.serialize_run(run)


@router.post("/api/projects/{project_id}/studio/runs/{run_id}/approve")
def approve_studio_run(
    project_id: str,
    run_id: str,
    request: Request,
    body: Optional[RunApproveRequest] = None,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot approve studio runs")
    run = db.get(TaskRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"task run not found: {run_id}")
    req = body or RunApproveRequest()
    return studio_agents.approve_task_run(
        db,
        run,
        expected_fence=req.expected_fence,
        grant_session_tool=req.grant_session_tool,
    )


@router.post("/api/projects/{project_id}/studio/runs/{run_id}/reject")
def reject_studio_run(
    project_id: str,
    run_id: str,
    request: Request,
    body: Optional[RunRejectRequest] = None,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot reject studio runs")
    run = db.get(TaskRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"task run not found: {run_id}")
    req = body or RunRejectRequest()
    return studio_agents.reject_task_run(
        db,
        run,
        reason=req.reason,
        expected_fence=req.expected_fence,
    )


@router.get("/api/projects/{project_id}/studio/runs/{run_id}/recovery-bundle")
def get_run_recovery_bundle(
    project_id: str,
    run_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    run = db.get(TaskRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"task run not found: {run_id}")
    return studio_agents.build_recovery_bundle(db, run)


@router.post("/api/projects/{project_id}/studio/runs/recover")
def recover_project_runs(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger run recovery")
    recovered = studio_agents.recover_stale_runs(db, project_id)
    return {"project_id": project_id, "recovered": recovered, "count": len(recovered)}
