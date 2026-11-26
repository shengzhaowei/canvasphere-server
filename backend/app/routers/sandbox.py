"""Project sandbox settings, execution, idle reaping & lifecycle routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth, sandbox_manager
from app.db import get_db
from app.models import Project
from app.permissions import can_write

router = APIRouter(tags=["sandbox"])


class SandboxConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    spec_name: Optional[str] = None
    spend_cap_credits: Optional[float] = None
    idle_keep_minutes: Optional[int] = None


class SandboxExecuteRequest(BaseModel):
    code: str
    timeout: int = 30
    output_files: Optional[list[dict[str, str]]] = None
    simulated_duration_seconds: Optional[float] = None


@router.get("/api/projects/{project_id}/sandbox")
def get_project_sandbox(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    sandbox_manager.reap_idle_sandboxes(db, project_id=project_id)
    cfg = sandbox_manager.get_or_create_sandbox_config(db, project_id)
    db.commit()
    return sandbox_manager.serialize_sandbox_config(db, cfg)


@router.patch("/api/projects/{project_id}/sandbox")
def patch_project_sandbox(
    project_id: str,
    body: SandboxConfigPatch,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot update sandbox settings")
    return sandbox_manager.update_sandbox_config(
        db,
        project_id,
        enabled=body.enabled,
        spec_name=body.spec_name,
        spend_cap_credits=body.spend_cap_credits,
        idle_keep_minutes=body.idle_keep_minutes,
    )


@router.post("/api/projects/{project_id}/sandbox/execute")
def execute_project_sandbox(
    project_id: str,
    body: SandboxExecuteRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot execute code in sandbox")
    proj = db.get(Project, project_id)
    assert proj is not None
    return sandbox_manager.execute_in_project_sandbox(
        db,
        project=proj,
        user_id=user.id if user else None,
        code=body.code,
        timeout=body.timeout,
        output_files=body.output_files,
        simulated_duration_seconds=body.simulated_duration_seconds,
    )


@router.post("/api/projects/{project_id}/sandbox/stop")
def stop_project_sandbox(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot stop sandbox")
    stopped = sandbox_manager.stop_project_sandbox(db, project_id)
    return {"project_id": project_id, "stopped_sessions": stopped}


@router.post("/api/projects/{project_id}/sandbox/reap-idle")
def reap_project_idle_sandboxes(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    reaped = sandbox_manager.reap_idle_sandboxes(db, project_id=project_id)
    return {"project_id": project_id, "reaped_sessions": reaped}
