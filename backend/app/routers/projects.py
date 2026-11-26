"""Project management, RBAC members, shareable invitations, and multi-canvas routes."""
from __future__ import annotations

import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, storage, store
from app.config import get_settings
from app.db import get_db
from app.models import (
    Canvas,
    CanvasObject,
    Project,
    ProjectInvitation,
    ProjectMember,
    User,
    new_prefixed_id,
    utcnow,
)
from app.permissions import can_write, normalize_role, project_permission_flags

router = APIRouter(tags=["projects"])


# --- schemas --------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str


class CoverUploadUrlRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int
    duration_seconds: Optional[float] = None


class CoverConfirmRequest(BaseModel):
    storage_id: str


class MemberAddRequest(BaseModel):
    user_id: Optional[str] = None
    email: Optional[str] = None
    role: str = "editor"


class MemberRoleUpdateRequest(BaseModel):
    role: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: str


class InvitationCreateRequest(BaseModel):
    role: str = "editor"


class CanvasCreateRequest(BaseModel):
    name: str = "未命名画板"


class CanvasUpdateRequest(BaseModel):
    name: str


class CanvasObjectCreateRequest(BaseModel):
    type: str
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 320.0
    h: float = 180.0
    text: str = ""
    content: Optional[dict[str, Any]] = None
    asset: Optional[dict[str, Any]] = None
    style: Optional[dict[str, Any]] = None


# --- helpers --------------------------------------------------------------
def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _canvas_dict(c: Canvas, object_count: int = 0) -> dict[str, Any]:
    return {
        "id": c.id,
        "canvas_id": c.id,
        "project_id": c.project_id,
        "name": c.name,
        "is_default": bool(c.is_default),
        "sort_order": int(c.sort_order),
        "object_count": object_count,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def _project_detail(
    db: Session,
    proj: Project,
    role: str,
) -> dict[str, Any]:
    canvases = db.scalars(
        select(Canvas)
        .where(Canvas.project_id == proj.id)
        .order_by(Canvas.is_default.desc(), Canvas.sort_order.asc(), Canvas.created_at.asc())
    ).all()
    default_cid = canvases[0].id if canvases else None
    flags = project_permission_flags(role)
    return {
        "id": proj.id,
        "project_id": proj.id,
        "workspace_id": proj.workspace_id,
        "name": proj.name,
        "status": proj.status,
        "cover_url": proj.cover_url,
        "cover_type": proj.cover_type,
        "owner_id": proj.owner_id,
        "canvas_count": len(canvases),
        "default_canvas_id": default_cid,
        "my_role": normalize_role(role),
        **flags,
        "permissions": flags,
        "last_activity_at": _iso(proj.last_activity_at),
        "created_at": _iso(proj.created_at),
        "updated_at": _iso(proj.updated_at),
    }


def _require_owner_role(role: str) -> None:
    if normalize_role(role) != "owner":
        raise HTTPException(status_code=403, detail="only project owner can perform this action")


def _invitation_dict(inv: ProjectInvitation) -> dict[str, Any]:
    base = get_settings().app_base_url
    return {
        "id": inv.id,
        "project_id": inv.project_id,
        "token": inv.token,
        "invite_url": f"{base}/invite/{inv.token}",
        "role": inv.role,
        "is_active": bool(inv.is_active),
        "created_by": inv.created_by,
        "created_at": _iso(inv.created_at),
        "revoked_at": _iso(inv.revoked_at),
    }


# --- project CRUD & list --------------------------------------------------
@router.post("/api/projects", status_code=201)
def create_project(
    body: ProjectCreate,
    actor: tuple[Optional[User], bool] = Depends(auth.get_optional_actor_or_legacy),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, _is_legacy = actor
    try:
        created = store.create_project(body.name, owner_id=user.id if user else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    proj = db.get(Project, created["id"])
    assert proj is not None
    return _project_detail(db, proj, role="owner")


@router.get("/api/projects")
def list_projects(
    status: str = Query(default="active"),
    scope: str = Query(default="all"),
    keyword: Optional[str] = Query(default=None),
    order_by: str = Query(default="recent"),
    page: Optional[int] = Query(default=None, ge=1),
    size: Optional[int] = Query(default=None, ge=1, le=100),
    actor: tuple[Optional[User], bool] = Depends(auth.get_optional_actor_or_legacy),
    db: Session = Depends(get_db),
) -> Any:
    user, is_legacy = actor
    if status not in ("active", "deleting", "all"):
        raise HTTPException(status_code=400, detail="status must be active, deleting, or all")
    if scope not in ("all", "owned", "joined"):
        raise HTTPException(status_code=400, detail="scope must be all, owned, or joined")
    if order_by not in ("recent", "created_at", "role"):
        raise HTTPException(status_code=400, detail="order_by must be recent, created_at, or role")

    # Legacy unauthenticated tests expect a plain list when no pagination params are passed
    if is_legacy and page is None and size is None and not keyword and status == "active" and scope == "all":
        return store.list_projects()

    role_by_project: dict[str, str] = {}
    if user is not None:
        memberships = db.scalars(
            select(ProjectMember).where(ProjectMember.user_id == user.id)
        ).all()
        role_by_project = {m.project_id: normalize_role(m.role) for m in memberships}
        if not role_by_project:
            return {"items": [], "total": 0, "page": page or 1, "size": size or 20}
        stmt = select(Project).where(Project.id.in_(list(role_by_project.keys())))
        if scope == "owned":
            owned_ids = [pid for pid, r in role_by_project.items() if r == "owner"]
            if not owned_ids:
                return {"items": [], "total": 0, "page": page or 1, "size": size or 20}
            stmt = stmt.where(Project.id.in_(owned_ids))
        elif scope == "joined":
            joined_ids = [pid for pid, r in role_by_project.items() if r != "owner"]
            if not joined_ids:
                return {"items": [], "total": 0, "page": page or 1, "size": size or 20}
            stmt = stmt.where(Project.id.in_(joined_ids))
    else:
        stmt = select(Project)

    if status == "all":
        stmt = stmt.where(Project.status != "deleted")
    else:
        stmt = stmt.where(Project.status == status)

    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(Project.name.ilike(kw))

    rows = db.scalars(stmt).all()

    role_rank = {"owner": 0, "editor": 1, "viewer": 2}
    if order_by == "recent":
        rows.sort(key=lambda p: p.last_activity_at, reverse=True)
    elif order_by == "created_at":
        rows.sort(key=lambda p: p.created_at, reverse=True)
    elif order_by == "role":
        rows.sort(key=lambda p: (role_rank.get(role_by_project.get(p.id, "owner"), 9), -p.last_activity_at.timestamp()))

    total = len(rows)
    cur_page = page or 1
    cur_size = size or 20
    start = (cur_page - 1) * cur_size
    sliced = rows[start: start + cur_size]
    items = [_project_detail(db, p, role=role_by_project.get(p.id, "owner")) for p in sliced]
    return {"items": items, "total": total, "page": cur_page, "size": cur_size}


@router.get("/api/projects/{project_id}")
def get_project(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    proj = db.get(Project, project_id)
    assert proj is not None
    return _project_detail(db, proj, role=role)


@router.patch("/api/projects/{project_id}")
def rename_project(
    project_id: str,
    body: ProjectUpdate,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot rename project")
    name = (body.name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="project name must be 1..128 characters")
    proj = db.get(Project, project_id)
    assert proj is not None
    now = utcnow()
    proj.name = name
    proj.updated_at = now
    proj.last_activity_at = now
    db.add(proj)
    db.commit()
    return _project_detail(db, proj, role=role)


@router.delete("/api/projects/{project_id}")
def delete_project(
    project_id: str,
    request: Request,
    finalize: bool = Query(default=False),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    proj = db.get(Project, project_id)
    assert proj is not None
    now = utcnow()
    proj.status = "deleted" if finalize else "deleting"
    proj.deleted_at = now
    proj.updated_at = now
    # Revoke active invitations immediately
    invs = db.scalars(
        select(ProjectInvitation).where(
            ProjectInvitation.project_id == project_id,
            ProjectInvitation.is_active.is_(True),
        )
    ).all()
    for inv in invs:
        inv.is_active = False
        inv.revoked_at = now
        db.add(inv)
    db.add(proj)
    db.commit()
    return {"project_id": proj.id, "status": proj.status, "deleted": True}


# --- cover upload ---------------------------------------------------------
@router.post("/api/projects/{project_id}/cover/upload-url")
def create_cover_upload_url(
    project_id: str,
    body: CoverUploadUrlRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot update project cover")
    mime = body.content_type.strip().lower()
    settings = get_settings()
    if mime in storage.ALLOWED_COVER_IMAGE_MIMES:
        cover_type = "image"
    elif mime in storage.ALLOWED_COVER_VIDEO_MIMES:
        cover_type = "video"
        if body.duration_seconds is None or body.duration_seconds <= 0:
            raise HTTPException(status_code=400, detail="duration_seconds is required for video cover")
        if body.duration_seconds > settings.cover_video_max_seconds:
            raise HTTPException(status_code=400, detail="video cover must be <= 30 seconds")
    else:
        raise HTTPException(status_code=400, detail="cover must be a supported image or video format")

    if body.size_bytes <= 0 or body.size_bytes > settings.cover_max_bytes:
        raise HTTPException(status_code=413, detail="cover file exceeds size limit")

    proj = db.get(Project, project_id)
    assert proj is not None
    out = storage.create_presigned_upload(
        db,
        space="workspace",
        filename=body.filename,
        content_type=mime,
        size_bytes=body.size_bytes,
        owner_user_id=proj.owner_id or (user.id if user else None),
        project_id=proj.id,
        workspace_id=proj.workspace_id,
        duration_seconds=body.duration_seconds,
    )
    out["cover_type"] = cover_type
    return out


@router.post("/api/projects/{project_id}/cover/confirm")
def confirm_cover_upload(
    project_id: str,
    body: CoverConfirmRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot update project cover")
    obj, new_url = storage.confirm_upload(
        db,
        body.storage_id,
        expected_space="workspace",
        expected_project_id=project_id,
    )
    proj = db.get(Project, project_id)
    assert proj is not None
    old_storage_id = proj.cover_storage_id
    proj.cover_url = new_url
    proj.cover_type = "video" if obj.content_type.startswith("video/") else "image"
    proj.cover_storage_id = obj.id
    proj.updated_at = utcnow()
    proj.last_activity_at = proj.updated_at
    db.add(proj)
    if old_storage_id and old_storage_id != obj.id:
        storage.invalidate_object(db, old_storage_id)
    db.commit()
    return _project_detail(db, proj, role=role)


# --- members & RBAC -------------------------------------------------------
@router.get("/api/projects/{project_id}/members")
def list_members(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    rows = db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.joined_at.asc())
    ).all()
    return [
        {
            "user_id": u.id,
            "display_name": u.display_name,
            "email": u.email,
            "avatar_url": u.avatar_url,
            "role": normalize_role(m.role),
            "joined_at": _iso(m.joined_at),
        }
        for m, u in rows
    ]


@router.post("/api/projects/{project_id}/members", status_code=201)
def add_member(
    project_id: str,
    body: MemberAddRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    target_role = normalize_role(body.role)
    if target_role not in ("editor", "viewer"):
        raise HTTPException(status_code=400, detail="member role must be editor or viewer (read-only)")

    target_user: Optional[User] = None
    if body.user_id:
        target_user = db.get(User, body.user_id)
    elif body.email:
        target_user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    else:
        raise HTTPException(status_code=400, detail="provide user_id or email")
    if target_user is None or not target_user.is_active:
        raise HTTPException(status_code=404, detail="target user not found")

    existing = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == target_user.id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="user is already a member of this project")

    m = ProjectMember(
        id=new_prefixed_id("mbr"),
        project_id=project_id,
        user_id=target_user.id,
        role=target_role,
        joined_at=utcnow(),
    )
    db.add(m)
    db.commit()
    return {
        "user_id": target_user.id,
        "display_name": target_user.display_name,
        "email": target_user.email,
        "role": target_role,
        "joined_at": _iso(m.joined_at),
    }


@router.patch("/api/projects/{project_id}/members/{member_user_id}")
def update_member_role(
    project_id: str,
    member_user_id: str,
    body: MemberRoleUpdateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    target_role = normalize_role(body.role)
    if target_role not in ("editor", "viewer"):
        raise HTTPException(status_code=400, detail="role must be editor or viewer (use transfer-ownership for owner)")

    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    if normalize_role(member.role) == "owner":
        raise HTTPException(status_code=400, detail="cannot demote project owner directly; transfer ownership first")
    member.role = target_role
    db.add(member)
    db.commit()
    return {"project_id": project_id, "user_id": member_user_id, "role": target_role}


@router.delete("/api/projects/{project_id}/members/{member_user_id}")
def remove_member(
    project_id: str,
    member_user_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    if normalize_role(member.role) == "owner":
        raise HTTPException(status_code=400, detail="cannot remove project owner")
    db.delete(member)
    db.commit()
    return {"removed": True, "user_id": member_user_id}


@router.post("/api/projects/{project_id}/transfer-ownership")
def transfer_ownership(
    project_id: str,
    body: TransferOwnershipRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    if actor is None:
        raise HTTPException(status_code=401, detail="authenticated owner required")
    if body.new_owner_user_id == actor.id:
        raise HTTPException(status_code=400, detail="already the owner")

    new_owner_member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == body.new_owner_user_id,
        )
    )
    if new_owner_member is None:
        raise HTTPException(status_code=404, detail="new owner must be an existing project member")

    old_owner_member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == actor.id,
        )
    )
    proj = db.get(Project, project_id)
    assert proj is not None

    new_owner_member.role = "owner"
    if old_owner_member is not None:
        old_owner_member.role = "editor"
        db.add(old_owner_member)
    proj.owner_id = body.new_owner_user_id
    proj.updated_at = utcnow()
    db.add(new_owner_member)
    db.add(proj)
    db.commit()
    return {"project_id": project_id, "owner_id": body.new_owner_user_id, "transferred": True}


@router.post("/api/projects/{project_id}/leave")
def leave_project(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if actor is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if normalize_role(role) == "owner":
        raise HTTPException(status_code=400, detail="owner must transfer ownership before leaving project")
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == actor.id,
        )
    )
    if member is not None:
        db.delete(member)
        db.commit()
    return {"project_id": project_id, "left": True}


# --- shareable invitations ------------------------------------------------
@router.get("/api/projects/{project_id}/invitations")
def list_invitations(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    rows = db.scalars(
        select(ProjectInvitation)
        .where(ProjectInvitation.project_id == project_id)
        .order_by(ProjectInvitation.created_at.desc())
    ).all()
    return [_invitation_dict(r) for r in rows]


@router.post("/api/projects/{project_id}/invitations", status_code=201)
def create_invitation(
    project_id: str,
    body: InvitationCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    inv_role = normalize_role(body.role)
    if inv_role not in ("editor", "viewer"):
        raise HTTPException(status_code=400, detail="invitation role must be editor or viewer")
    inv = ProjectInvitation(
        id=new_prefixed_id("inv"),
        project_id=project_id,
        token=secrets.token_urlsafe(32),
        role=inv_role,
        created_by=actor.id if actor else None,
        is_active=True,
        created_at=utcnow(),
    )
    db.add(inv)
    db.commit()
    return _invitation_dict(inv)


@router.post("/api/projects/{project_id}/invitations/{invitation_id}/refresh")
def refresh_invitation(
    project_id: str,
    invitation_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    inv = db.get(ProjectInvitation, invitation_id)
    if inv is None or inv.project_id != project_id:
        raise HTTPException(status_code=404, detail="invitation not found")
    if not inv.is_active:
        raise HTTPException(status_code=400, detail="cannot refresh a revoked invitation")
    inv.token = secrets.token_urlsafe(32)
    db.add(inv)
    db.commit()
    return _invitation_dict(inv)


@router.delete("/api/projects/{project_id}/invitations/{invitation_id}")
def revoke_invitation(
    project_id: str,
    invitation_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _actor, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _require_owner_role(role)
    inv = db.get(ProjectInvitation, invitation_id)
    if inv is None or inv.project_id != project_id:
        raise HTTPException(status_code=404, detail="invitation not found")
    inv.is_active = False
    inv.revoked_at = utcnow()
    db.add(inv)
    db.commit()
    return {"id": inv.id, "revoked": True}


@router.post("/api/invitations/{token}/accept")
def accept_invitation(
    token: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    inv = db.scalar(select(ProjectInvitation).where(ProjectInvitation.token == token))
    if inv is None or not inv.is_active:
        raise HTTPException(status_code=404, detail="invitation link is invalid or revoked")
    proj = db.get(Project, inv.project_id)
    if proj is None or proj.status != "active":
        raise HTTPException(status_code=404, detail="project is not available")

    existing = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == inv.project_id,
            ProjectMember.user_id == user.id,
        )
    )
    if existing is None:
        existing = ProjectMember(
            id=new_prefixed_id("mbr"),
            project_id=inv.project_id,
            user_id=user.id,
            role=normalize_role(inv.role),
            joined_at=utcnow(),
        )
        db.add(existing)
        db.commit()
    return {
        "project_id": inv.project_id,
        "role": normalize_role(existing.role),
        "joined": True,
    }


# --- multi-canvas directory -----------------------------------------------
@router.get("/api/projects/{project_id}/canvases")
def list_canvases(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    canvases = db.scalars(
        select(Canvas)
        .where(Canvas.project_id == project_id)
        .order_by(Canvas.sort_order.asc(), Canvas.created_at.asc())
    ).all()
    counts = dict(
        db.execute(
            select(CanvasObject.canvas_id, func.count(CanvasObject.id))
            .where(CanvasObject.project_id == project_id)
            .group_by(CanvasObject.canvas_id)
        ).all()
    )
    return [_canvas_dict(c, object_count=int(counts.get(c.id, 0))) for c in canvases]


@router.post("/api/projects/{project_id}/canvases", status_code=201)
def create_canvas(
    project_id: str,
    body: CanvasCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot create canvas")
    name = (body.name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="canvas name must be 1..128 characters")
    max_order = db.scalar(
        select(func.coalesce(func.max(Canvas.sort_order), -1))
        .where(Canvas.project_id == project_id)
    )
    now = utcnow()
    c = Canvas(
        id=new_prefixed_id("can"),
        project_id=project_id,
        name=name,
        is_default=False,
        sort_order=int(max_order or 0) + 1,
        created_at=now,
        updated_at=now,
    )
    proj = db.get(Project, project_id)
    if proj is not None:
        proj.last_activity_at = now
        db.add(proj)
    db.add(c)
    db.commit()
    return _canvas_dict(c, object_count=0)


@router.patch("/api/projects/{project_id}/canvases/{canvas_id}")
def rename_canvas(
    project_id: str,
    canvas_id: str,
    body: CanvasUpdateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot rename canvas")
    name = (body.name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="canvas name must be 1..128 characters")
    c = db.get(Canvas, canvas_id)
    if c is None or c.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    c.name = name
    c.updated_at = utcnow()
    db.add(c)
    db.commit()
    obj_count = int(
        db.scalar(select(func.count(CanvasObject.id)).where(CanvasObject.canvas_id == canvas_id)) or 0
    )
    return _canvas_dict(c, object_count=obj_count)


@router.delete("/api/projects/{project_id}/canvases/{canvas_id}")
def delete_canvas(
    project_id: str,
    canvas_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot delete canvas")
    canvases = db.scalars(
        select(Canvas)
        .where(Canvas.project_id == project_id)
        .order_by(Canvas.sort_order.asc(), Canvas.created_at.asc())
    ).all()
    target = next((c for c in canvases if c.id == canvas_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    if len(canvases) <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last remaining canvas in a project")
    was_default = bool(target.is_default)
    db.delete(target)
    if was_default:
        replacement = next(c for c in canvases if c.id != canvas_id)
        replacement.is_default = True
        db.add(replacement)
    db.commit()
    return {"canvas_id": canvas_id, "deleted": True}

