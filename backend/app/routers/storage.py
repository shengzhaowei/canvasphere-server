"""Storage quota, project storage accounting, and local signed upload/object routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth, storage
from app.db import get_db
from app.models import Project, User
from app.permissions import can_write

router = APIRouter(tags=["storage"])


class ProjectStorageUploadRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int
    space: str = "workspace"  # workspace | private_runtime


class ProjectStorageConfirmRequest(BaseModel):
    storage_id: str


@router.get("/api/user/storage")
def get_user_storage(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return storage.get_user_storage_summary(db, user.id)


@router.get("/api/projects/{project_id}/storage")
def get_project_storage(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, _role = auth.resolve_project_actor(project_id, request, db, x_role)
    proj = db.get(Project, project_id)
    assert proj is not None
    return storage.get_project_storage_summary(db, proj, user.id if user else None)


@router.post("/api/projects/{project_id}/storage/upload-url")
def create_project_storage_upload(
    project_id: str,
    body: ProjectStorageUploadRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot upload project files")
    if body.space not in ("workspace", "private_runtime"):
        raise HTTPException(status_code=400, detail="space must be workspace or private_runtime")
    proj = db.get(Project, project_id)
    assert proj is not None
    billed_user = proj.owner_id if body.space == "workspace" else (user.id if user else proj.owner_id)
    return storage.create_presigned_upload(
        db,
        space=body.space,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        owner_user_id=billed_user,
        project_id=proj.id,
        workspace_id=proj.workspace_id,
    )


@router.post("/api/projects/{project_id}/storage/confirm")
def confirm_project_storage_upload(
    project_id: str,
    body: ProjectStorageConfirmRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot confirm project files")
    obj, url = storage.confirm_upload(
        db,
        body.storage_id,
        expected_project_id=project_id,
    )
    return {
        "storage_id": obj.id,
        "space": obj.space,
        "object_key": obj.object_key,
        "filename": obj.filename,
        "content_type": obj.content_type,
        "size_bytes": obj.size_bytes,
        "url": url,
    }


@router.put("/api/storage/local/upload/{storage_id}")
async def local_signed_upload(
    storage_id: str,
    request: Request,
    expires: int = Query(...),
    sig: str = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = await request.body()
    obj = storage.write_local_object(db, storage_id, data, expires, sig)
    return {
        "storage_id": obj.id,
        "size_bytes": obj.size_bytes,
        "status": obj.status,
    }


@router.get("/api/storage/local/object/{storage_id}")
def local_signed_object(
    storage_id: str,
    expires: int = Query(...),
    sig: str = Query(...),
    db: Session = Depends(get_db),
) -> Response:
    obj, data = storage.read_local_object(db, storage_id, expires, sig)
    return Response(content=data, media_type=obj.content_type)
