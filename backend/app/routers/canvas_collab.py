"""Canvas version snapshots (#6), threaded comments (#14), and public read-only shares (#47)."""
from __future__ import annotations

from datetime import timedelta, timezone
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.config import get_settings
from app.db import get_db
from app.models import (
    Canvas,
    CanvasComment,
    CanvasObject,
    CanvasPublicShare,
    CanvasSnapshot,
    Project,
    new_prefixed_id,
    utcnow,
)
from app.permissions import can_write, normalize_role
from app.realtime import event_bus
from app.routers.canvas import serialize_canvas_object

router = APIRouter(tags=["canvas_collab"])


class SnapshotCreateRequest(BaseModel):
    name: str = ""


class CommentCreateRequest(BaseModel):
    content: str
    object_id: Optional[str] = None
    parent_id: Optional[str] = None
    anchor_x: Optional[float] = None
    anchor_y: Optional[float] = None


class CommentPatchRequest(BaseModel):
    content: Optional[str] = None
    resolved: Optional[bool] = None


class PublicShareCreateRequest(BaseModel):
    title: str = ""
    expires_in_hours: Optional[int] = None


def _ensure_canvas(db: Session, project_id: str, canvas_id: str) -> Canvas:
    c = db.get(Canvas, canvas_id)
    if c is None or c.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    return c


def _serialize_snapshot(s: CanvasSnapshot, include_objects: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": s.id,
        "project_id": s.project_id,
        "canvas_id": s.canvas_id,
        "name": s.name,
        "created_by": s.created_by,
        "object_count": int(s.object_count),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
    if include_objects:
        out["objects"] = list(s.objects_json or [])
    return out


def _serialize_comment(c: CanvasComment) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "canvas_id": c.canvas_id,
        "object_id": c.object_id,
        "parent_id": c.parent_id,
        "author_user_id": c.author_user_id,
        "author_name": c.author_name,
        "content": c.content,
        "anchor_x": c.anchor_x,
        "anchor_y": c.anchor_y,
        "resolved": bool(c.resolved),
        "resolved_by": c.resolved_by,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _serialize_share(s: CanvasPublicShare) -> dict[str, Any]:
    base = get_settings().app_base_url
    return {
        "id": s.id,
        "project_id": s.project_id,
        "canvas_id": s.canvas_id,
        "token": s.token,
        "share_url": f"{base}/share/{s.token}",
        "title": s.title,
        "is_active": bool(s.is_active),
        "created_by": s.created_by,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


# --- #6 Canvas Version Snapshots & Restore --------------------------------
@router.get("/api/projects/{project_id}/canvases/{canvas_id}/snapshots")
def list_canvas_snapshots(
    project_id: str,
    canvas_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    rows = db.scalars(
        select(CanvasSnapshot)
        .where(CanvasSnapshot.project_id == project_id, CanvasSnapshot.canvas_id == canvas_id)
        .order_by(CanvasSnapshot.created_at.desc())
    ).all()
    return {"items": [_serialize_snapshot(r) for r in rows], "total": len(rows)}


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/snapshots", status_code=201)
def create_canvas_snapshot(
    project_id: str,
    canvas_id: str,
    body: SnapshotCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot create snapshots")
    _ensure_canvas(db, project_id, canvas_id)

    objs = db.scalars(
        select(CanvasObject)
        .where(CanvasObject.canvas_id == canvas_id, CanvasObject.deleted_at.is_(None))
        .order_by(CanvasObject.z_index.asc(), CanvasObject.created_at.asc())
    ).all()
    serialized = [serialize_canvas_object(o) for o in objs]
    now = utcnow()
    snap = CanvasSnapshot(
        id=new_prefixed_id("snp"),
        project_id=project_id,
        canvas_id=canvas_id,
        name=(body.name or "").strip()[:128] or f"快照 {now.strftime('%Y-%m-%d %H:%M')}",
        created_by=user.id if user else None,
        object_count=len(serialized),
        objects_json=serialized,
        created_at=now,
    )
    db.add(snap)
    db.commit()
    return _serialize_snapshot(snap, include_objects=True)


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/snapshots/{snapshot_id}/restore")
def restore_canvas_snapshot(
    project_id: str,
    canvas_id: str,
    snapshot_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot restore snapshots")
    _ensure_canvas(db, project_id, canvas_id)

    snap = db.get(CanvasSnapshot, snapshot_id)
    if snap is None or snap.project_id != project_id or snap.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"snapshot not found: {snapshot_id}")

    now = utcnow()
    # Soft-delete current active objects on this canvas
    current = db.scalars(
        select(CanvasObject).where(CanvasObject.canvas_id == canvas_id, CanvasObject.deleted_at.is_(None))
    ).all()
    for obj in current:
        obj.deleted_at = now
        obj.updated_at = now
        db.add(obj)
    db.flush()

    # Restore snapshot objects (revive existing row if id matches, else insert fresh row)
    restored: list[dict[str, Any]] = []
    for item in (snap.objects_json or []):
        oid = item.get("id") or new_prefixed_id("obj")
        existing = db.get(CanvasObject, oid)
        if existing is not None and existing.canvas_id == canvas_id:
            existing.type = str(item.get("type") or existing.type)
            existing.name = str(item.get("name") or "")[:128]
            existing.x = float(item.get("x", 0.0))
            existing.y = float(item.get("y", 0.0))
            existing.w = float(item.get("w", 320.0))
            existing.h = float(item.get("h", 180.0))
            existing.rotation = float(item.get("rotation", 0.0))
            existing.z_index = int(item.get("z_index", 0))
            existing.text = str(item.get("text") or "")
            existing.content_json = item.get("content")
            existing.asset_json = item.get("asset")
            existing.style_json = item.get("style")
            existing.from_object_id = item.get("from_object_id")
            existing.from_anchor = item.get("from_anchor")
            existing.to_object_id = item.get("to_object_id")
            existing.to_anchor = item.get("to_anchor")
            existing.media_type = item.get("media_type")
            existing.locked = bool(item.get("locked", False))
            existing.group_id = item.get("group_id")
            existing.deleted_at = None
            existing.version = int(existing.version) + 1
            existing.updated_at = now
            db.add(existing)
            restored.append(serialize_canvas_object(existing))
        else:
            new_obj = CanvasObject(
                id=new_prefixed_id("obj") if existing is not None else oid,
                project_id=project_id,
                canvas_id=canvas_id,
                type=str(item.get("type") or "text"),
                name=str(item.get("name") or "")[:128],
                x=float(item.get("x", 0.0)),
                y=float(item.get("y", 0.0)),
                w=float(item.get("w", 320.0)),
                h=float(item.get("h", 180.0)),
                rotation=float(item.get("rotation", 0.0)),
                z_index=int(item.get("z_index", 0)),
                text=str(item.get("text") or ""),
                content_json=item.get("content"),
                asset_json=item.get("asset"),
                style_json=item.get("style"),
                from_object_id=item.get("from_object_id"),
                from_anchor=item.get("from_anchor"),
                to_object_id=item.get("to_object_id"),
                to_anchor=item.get("to_anchor"),
                media_type=item.get("media_type"),
                locked=bool(item.get("locked", False)),
                group_id=item.get("group_id"),
                version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(new_obj)
            restored.append(serialize_canvas_object(new_obj))

    db.commit()
    event_bus.publish(
        project_id,
        canvas_id,
        "canvas.snapshot_restored",
        {"snapshot_id": snap.id, "restored_count": len(restored)},
        actor_id=user.id if user else None,
    )
    return {"snapshot_id": snap.id, "restored_count": len(restored), "objects": restored}


@router.delete("/api/projects/{project_id}/canvases/{canvas_id}/snapshots/{snapshot_id}")
def delete_canvas_snapshot(
    project_id: str,
    canvas_id: str,
    snapshot_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot delete snapshots")
    snap = db.get(CanvasSnapshot, snapshot_id)
    if snap is None or snap.project_id != project_id or snap.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"snapshot not found: {snapshot_id}")
    db.delete(snap)
    db.commit()
    return {"deleted": True, "snapshot_id": snapshot_id}


# --- #14 Canvas Threaded Comments / Annotations ---------------------------
@router.get("/api/projects/{project_id}/canvases/{canvas_id}/comments")
def list_canvas_comments(
    project_id: str,
    canvas_id: str,
    request: Request,
    object_id: Optional[str] = Query(default=None),
    resolved: Optional[bool] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    stmt = select(CanvasComment).where(
        CanvasComment.project_id == project_id,
        CanvasComment.canvas_id == canvas_id,
    )
    if object_id:
        stmt = stmt.where(CanvasComment.object_id == object_id)
    if resolved is not None:
        stmt = stmt.where(CanvasComment.resolved.is_(resolved))
    rows = db.scalars(stmt.order_by(CanvasComment.created_at.asc())).all()
    return {"items": [_serialize_comment(r) for r in rows], "total": len(rows)}


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/comments", status_code=201)
def create_canvas_comment(
    project_id: str,
    canvas_id: str,
    body: CommentCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, _role = auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="comment content must not be empty")
    if body.object_id:
        obj = db.get(CanvasObject, body.object_id)
        if obj is None or obj.canvas_id != canvas_id or obj.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"object not found: {body.object_id}")

    now = utcnow()
    cmt = CanvasComment(
        id=new_prefixed_id("cmt"),
        project_id=project_id,
        canvas_id=canvas_id,
        object_id=body.object_id,
        parent_id=body.parent_id,
        author_user_id=user.id if user else None,
        author_name=(user.display_name or user.email) if user else "Member",
        content=text,
        anchor_x=body.anchor_x,
        anchor_y=body.anchor_y,
        resolved=False,
        created_at=now,
        updated_at=now,
    )
    db.add(cmt)
    db.commit()
    out = _serialize_comment(cmt)
    event_bus.publish(project_id, canvas_id, "comment.created", {"comment": out}, actor_id=user.id if user else None)
    return out


@router.patch("/api/projects/{project_id}/canvases/{canvas_id}/comments/{comment_id}")
def update_canvas_comment(
    project_id: str,
    canvas_id: str,
    comment_id: str,
    body: CommentPatchRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    cmt = db.get(CanvasComment, comment_id)
    if cmt is None or cmt.project_id != project_id or cmt.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"comment not found: {comment_id}")
    if body.content is not None:
        if user and cmt.author_user_id and cmt.author_user_id != user.id and normalize_role(role) != "owner":
            raise HTTPException(status_code=403, detail="only author or project owner can edit comment content")
        clean = body.content.strip()
        if not clean:
            raise HTTPException(status_code=400, detail="comment content must not be empty")
        cmt.content = clean
    if body.resolved is not None:
        cmt.resolved = bool(body.resolved)
        cmt.resolved_by = (user.id if user else None) if cmt.resolved else None
        cmt.resolved_at = utcnow() if cmt.resolved else None
    cmt.updated_at = utcnow()
    db.add(cmt)
    db.commit()
    out = _serialize_comment(cmt)
    event_bus.publish(project_id, canvas_id, "comment.updated", {"comment": out}, actor_id=user.id if user else None)
    return out


@router.delete("/api/projects/{project_id}/canvases/{canvas_id}/comments/{comment_id}")
def delete_canvas_comment(
    project_id: str,
    canvas_id: str,
    comment_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    cmt = db.get(CanvasComment, comment_id)
    if cmt is None or cmt.project_id != project_id or cmt.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"comment not found: {comment_id}")
    if user and cmt.author_user_id and cmt.author_user_id != user.id and normalize_role(role) != "owner":
        raise HTTPException(status_code=403, detail="only author or project owner can delete comment")
    db.delete(cmt)
    db.commit()
    return {"deleted": True, "comment_id": comment_id}


# --- #47 Public Read-Only Share Links -------------------------------------
@router.get("/api/projects/{project_id}/canvases/{canvas_id}/public-shares")
def list_public_shares(
    project_id: str,
    canvas_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    rows = db.scalars(
        select(CanvasPublicShare)
        .where(CanvasPublicShare.project_id == project_id, CanvasPublicShare.canvas_id == canvas_id)
        .order_by(CanvasPublicShare.created_at.desc())
    ).all()
    return {"items": [_serialize_share(r) for r in rows], "total": len(rows)}


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/public-shares", status_code=201)
def create_public_share(
    project_id: str,
    canvas_id: str,
    body: PublicShareCreateRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot create public shares")
    canvas = _ensure_canvas(db, project_id, canvas_id)
    now = utcnow()
    expires_at = None
    if body.expires_in_hours is not None:
        if body.expires_in_hours < 1 or body.expires_in_hours > 24 * 365:
            raise HTTPException(status_code=400, detail="expires_in_hours must be 1..8760")
        expires_at = now + timedelta(hours=int(body.expires_in_hours))

    share = CanvasPublicShare(
        id=new_prefixed_id("shr"),
        project_id=project_id,
        canvas_id=canvas_id,
        token=secrets.token_urlsafe(32),
        title=(body.title or "").strip()[:128] or canvas.name,
        is_active=True,
        created_by=user.id if user else None,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(share)
    db.commit()
    return _serialize_share(share)


@router.delete("/api/projects/{project_id}/canvases/{canvas_id}/public-shares/{share_id}")
def revoke_public_share(
    project_id: str,
    canvas_id: str,
    share_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot revoke public shares")
    share = db.get(CanvasPublicShare, share_id)
    if share is None or share.project_id != project_id or share.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"public share not found: {share_id}")
    share.is_active = False
    share.revoked_at = utcnow()
    db.add(share)
    db.commit()
    return {"revoked": True, "share_id": share_id}


@router.get("/api/public/canvases/{token}")
def get_public_shared_canvas(
    token: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Unauthenticated read-only snapshot of a publicly shared canvas (no private project/user data)."""
    share = db.scalar(select(CanvasPublicShare).where(CanvasPublicShare.token == token))
    if share is None or not share.is_active:
        raise HTTPException(status_code=404, detail="shared canvas not found or revoked")
    if share.expires_at is not None:
        exp = share.expires_at.replace(tzinfo=timezone.utc) if share.expires_at.tzinfo is None else share.expires_at
        if exp <= utcnow():
            raise HTTPException(status_code=404, detail="shared canvas link has expired")

    proj = db.get(Project, share.project_id)
    canvas = db.get(Canvas, share.canvas_id)
    if proj is None or proj.status != "active" or canvas is None:
        raise HTTPException(status_code=404, detail="shared canvas is no longer available")

    objs = db.scalars(
        select(CanvasObject)
        .where(CanvasObject.canvas_id == canvas.id, CanvasObject.deleted_at.is_(None))
        .order_by(CanvasObject.z_index.asc(), CanvasObject.created_at.asc())
    ).all()
    return {
        "read_only": True,
        "share_id": share.id,
        "title": share.title or canvas.name,
        "project_name": proj.name,
        "canvas_name": canvas.name,
        "objects": [serialize_canvas_object(o) for o in objs],
        "total": len(objs),
    }
