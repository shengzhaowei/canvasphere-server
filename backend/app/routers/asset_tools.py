"""Asset Tools catalog & execution routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import asset_tools, auth
from app.db import get_db
from app.models import AssetToolJob, Canvas, Project
from app.permissions import can_write

router = APIRouter(tags=["asset_tools"])


class AssetToolRunRequest(BaseModel):
    tool_name: str
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/projects/{project_id}/asset-tools/catalog")
def get_asset_tools_catalog(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    return {
        "project_id": project_id,
        "tools": asset_tools.ASSET_TOOLS_CATALOG,
        "total": len(asset_tools.ASSET_TOOLS_CATALOG),
    }


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/asset-tools/run", status_code=201)
def run_project_asset_tool(
    project_id: str,
    canvas_id: str,
    body: AssetToolRunRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot run asset tools")
    proj = db.get(Project, project_id)
    assert proj is not None
    canvas = db.get(Canvas, canvas_id)
    if canvas is None or canvas.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    return asset_tools.run_asset_tool(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        tool_name=body.tool_name,
        payload=body.payload,
    )


@router.get("/api/projects/{project_id}/asset-tools/jobs")
def list_asset_tool_jobs(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    rows = db.scalars(
        select(AssetToolJob)
        .where(AssetToolJob.project_id == project_id)
        .order_by(AssetToolJob.created_at.desc())
    ).all()
    return {"items": [asset_tools.serialize_tool_job(r) for r in rows], "total": len(rows)}
