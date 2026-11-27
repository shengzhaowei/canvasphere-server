"""Infinite canvas 10-type object engine, optimistic leases, SSE stream, presence & workspace files."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import auth, storage
from app.db import get_db
from app.models import (
    Canvas,
    CanvasObject,
    Project,
    StorageObject,
    User,
    new_prefixed_id,
    utcnow,
)
from app.permissions import can_write
from app.realtime import event_bus, lease_manager, presence_manager

router = APIRouter(tags=["canvas"])

BASIC_OBJECT_TYPES = frozenset({"text", "rect", "line", "connector"})
CONTENT_NODE_TYPES = frozenset({"image", "video", "audio", "music", "document", "file"})
ALL_OBJECT_TYPES = BASIC_OBJECT_TYPES | CONTENT_NODE_TYPES

RECT_FILL_PRESETS = frozenset({
    "theme", "transparent", "slate", "graphite", "steel", "terracotta", "moss", "deep_sea",
})
LINE_THICKNESSES = frozenset({1, 2, 4, 6})
LINE_STYLES = frozenset({"solid", "dashed", "dotted"})
ANCHOR_SIDES = frozenset({"left", "right"})


# --- schemas --------------------------------------------------------------
class CanvasObjectCreate(BaseModel):
    type: str
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 320.0
    h: float = 180.0
    rotation: float = 0.0
    z_index: int = 0
    text: str = ""
    content: Optional[dict[str, Any]] = None
    asset: Optional[dict[str, Any]] = None
    style: Optional[dict[str, Any]] = None
    from_object_id: Optional[str] = None
    from_anchor: Optional[str] = None
    to_object_id: Optional[str] = None
    to_anchor: Optional[str] = None
    media_type: Optional[str] = None


class CanvasObjectPatch(BaseModel):
    expected_version: Optional[int] = None
    name: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    w: Optional[float] = None
    h: Optional[float] = None
    rotation: Optional[float] = None
    z_index: Optional[int] = None
    text: Optional[str] = None
    content: Optional[dict[str, Any]] = None
    asset: Optional[dict[str, Any]] = None
    style: Optional[dict[str, Any]] = None
    from_object_id: Optional[str] = None
    from_anchor: Optional[str] = None
    to_object_id: Optional[str] = None
    to_anchor: Optional[str] = None
    media_type: Optional[str] = None


class ObjectDeletionRequest(BaseModel):
    object_ids: list[str]
    expected_versions: Optional[dict[str, int]] = None


class LeaseAcquireRequest(BaseModel):
    object_ids: list[str]
    ttl_seconds: Optional[int] = None


class LeaseReleaseRequest(BaseModel):
    object_ids: Optional[list[str]] = None


class CameraPresenceRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0
    cursor_x: Optional[float] = None
    cursor_y: Optional[float] = None


# --- helpers --------------------------------------------------------------
def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _actor_identity(user: Optional[User], role: str) -> tuple[str, str, Optional[str]]:
    if user is not None:
        return user.id, user.display_name or user.email or user.id, user.avatar_url
    return f"legacy:{role}", f"legacy-{role}", None


def _ensure_canvas(db: Session, project_id: str, canvas_id: str) -> Canvas:
    c = db.get(Canvas, canvas_id)
    if c is None or c.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    return c


def _validate_object_spec(
    db: Session,
    project_id: str,
    canvas_id: str,
    obj_type: str,
    style: Optional[dict[str, Any]],
    from_object_id: Optional[str],
    from_anchor: Optional[str],
    to_object_id: Optional[str],
    to_anchor: Optional[str],
    self_id: Optional[str] = None,
) -> None:
    if obj_type not in ALL_OBJECT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported object type: {obj_type!r} (allowed: {sorted(ALL_OBJECT_TYPES)})",
        )
    st = style or {}
    if obj_type == "rect" and "fill_preset" in st and st["fill_preset"] not in RECT_FILL_PRESETS:
        raise HTTPException(status_code=400, detail=f"invalid rect fill_preset: {st['fill_preset']!r}")
    if obj_type == "line":
        if "thickness" in st and st["thickness"] not in LINE_THICKNESSES:
            raise HTTPException(status_code=400, detail=f"line thickness must be one of {sorted(LINE_THICKNESSES)}")
        if "line_style" in st and st["line_style"] not in LINE_STYLES:
            raise HTTPException(status_code=400, detail=f"line_style must be one of {sorted(LINE_STYLES)}")
    if obj_type == "connector":
        if not from_object_id or not to_object_id:
            raise HTTPException(status_code=400, detail="connector requires from_object_id and to_object_id")
        if from_object_id == to_object_id or (self_id and self_id in (from_object_id, to_object_id)):
            raise HTTPException(status_code=400, detail="connector cannot connect a node to itself")
        if from_anchor and from_anchor not in ANCHOR_SIDES:
            raise HTTPException(status_code=400, detail="from_anchor must be left or right")
        if to_anchor and to_anchor not in ANCHOR_SIDES:
            raise HTTPException(status_code=400, detail="to_anchor must be left or right")
        src = db.get(CanvasObject, from_object_id)
        dst = db.get(CanvasObject, to_object_id)
        if src is None or src.canvas_id != canvas_id or src.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"source node not found: {from_object_id}")
        if dst is None or dst.canvas_id != canvas_id or dst.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"target node not found: {to_object_id}")


def serialize_canvas_object(obj: CanvasObject, lease: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "id": obj.id,
        "object_id": obj.id,
        "project_id": obj.project_id,
        "canvas_id": obj.canvas_id,
        "type": obj.type,
        "name": obj.name,
        "media_type": obj.media_type or (obj.type if obj.type in CONTENT_NODE_TYPES else None),
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
        "from_object_id": obj.from_object_id,
        "from_anchor": obj.from_anchor,
        "to_object_id": obj.to_object_id,
        "to_anchor": obj.to_anchor,
        "version": int(obj.version),
        "lease": lease,
        "properties": {
            "basic": {"id": obj.id, "name": obj.name, "type": obj.type, "media_type": obj.media_type},
            "content": obj.content_json or {"text": obj.text or ""},
            "asset": obj.asset_json,
            "geometry": {
                "x": float(obj.x),
                "y": float(obj.y),
                "w": float(obj.w),
                "h": float(obj.h),
                "rotation": float(obj.rotation),
                "z_index": int(obj.z_index),
            },
            "style": obj.style_json or {},
            "collaboration": {"version": int(obj.version), "lease": lease},
        },
        "created_at": _iso(obj.created_at),
        "updated_at": _iso(obj.updated_at),
    }


# --- object CRUD, duplicate & batch delete --------------------------------
@router.get("/api/projects/{project_id}/canvases/{canvas_id}/objects")
def list_canvas_objects(
    project_id: str,
    canvas_id: str,
    request: Request,
    keyword: Optional[str] = Query(default=None),
    object_type: Optional[str] = Query(default=None, alias="type"),
    media_type: Optional[str] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    stmt = (
        select(CanvasObject)
        .where(
            CanvasObject.project_id == project_id,
            CanvasObject.canvas_id == canvas_id,
            CanvasObject.deleted_at.is_(None),
        )
        .order_by(CanvasObject.z_index.asc(), CanvasObject.created_at.asc())
    )
    if object_type:
        stmt = stmt.where(CanvasObject.type == object_type.strip().lower())
    if media_type:
        stmt = stmt.where(CanvasObject.media_type == media_type.strip().lower())
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(CanvasObject.name.ilike(kw), CanvasObject.text.ilike(kw)))

    rows = db.scalars(stmt).all()
    active_leases = lease_manager.list_canvas_leases(project_id, canvas_id)
    items = [serialize_canvas_object(o, lease=active_leases.get(o.id)) for o in rows]
    return {"project_id": project_id, "canvas_id": canvas_id, "objects": items, "total": len(items)}


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/objects", status_code=201)
def create_canvas_object(
    project_id: str,
    canvas_id: str,
    body: CanvasObjectCreate,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot create canvas objects")
    _ensure_canvas(db, project_id, canvas_id)

    obj_type = body.type.strip().lower()
    _validate_object_spec(
        db, project_id, canvas_id, obj_type, body.style,
        body.from_object_id, body.from_anchor, body.to_object_id, body.to_anchor,
    )

    now = utcnow()
    media_type = body.media_type or (obj_type if obj_type in CONTENT_NODE_TYPES else None)
    obj = CanvasObject(
        id=new_prefixed_id("obj"),
        project_id=project_id,
        canvas_id=canvas_id,
        type=obj_type,
        name=(body.name or "").strip()[:128],
        x=float(body.x),
        y=float(body.y),
        w=float(body.w),
        h=float(body.h),
        rotation=float(body.rotation),
        z_index=int(body.z_index),
        text=body.text or "",
        content_json=body.content,
        asset_json=body.asset,
        style_json=body.style,
        from_object_id=body.from_object_id,
        from_anchor=body.from_anchor or ("right" if obj_type == "connector" else None),
        to_object_id=body.to_object_id,
        to_anchor=body.to_anchor or ("left" if obj_type == "connector" else None),
        media_type=media_type,
        version=1,
        created_at=now,
        updated_at=now,
    )
    proj = db.get(Project, project_id)
    if proj is not None:
        proj.last_activity_at = now
        db.add(proj)
    db.add(obj)
    db.commit()

    actor_id, _, _ = _actor_identity(user, role)
    serialized = serialize_canvas_object(obj)
    event_bus.publish(project_id, canvas_id, "object.created", {"object": serialized}, actor_id=actor_id)
    return serialized


@router.patch("/api/projects/{project_id}/canvases/{canvas_id}/objects/{object_id}")
def update_canvas_object(
    project_id: str,
    canvas_id: str,
    object_id: str,
    body: CanvasObjectPatch,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot update canvas objects")
    _ensure_canvas(db, project_id, canvas_id)

    obj = db.get(CanvasObject, object_id)
    if obj is None or obj.project_id != project_id or obj.canvas_id != canvas_id or obj.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"object not found: {object_id}")

    actor_id, _, _ = _actor_identity(user, role)
    lease_manager.check_writable(project_id, canvas_id, object_id, actor_id)

    if body.expected_version is not None and int(body.expected_version) != int(obj.version):
        cur_lease = lease_manager.get_active_lease(project_id, canvas_id, object_id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERSION_CONFLICT",
                "message": f"object {object_id} version mismatch",
                "expected_version": body.expected_version,
                "current_version": int(obj.version),
                "current_object": serialize_canvas_object(obj, lease=cur_lease.to_dict() if cur_lease else None),
            },
        )

    new_style = body.style if body.style is not None else obj.style_json
    new_from = body.from_object_id if body.from_object_id is not None else obj.from_object_id
    new_from_a = body.from_anchor if body.from_anchor is not None else obj.from_anchor
    new_to = body.to_object_id if body.to_object_id is not None else obj.to_object_id
    new_to_a = body.to_anchor if body.to_anchor is not None else obj.to_anchor
    _validate_object_spec(
        db, project_id, canvas_id, obj.type, new_style,
        new_from, new_from_a, new_to, new_to_a, self_id=obj.id,
    )

    if body.name is not None:
        obj.name = body.name.strip()[:128]
    if body.x is not None:
        obj.x = float(body.x)
    if body.y is not None:
        obj.y = float(body.y)
    if body.w is not None:
        obj.w = float(body.w)
    if body.h is not None:
        obj.h = float(body.h)
    if body.rotation is not None:
        obj.rotation = float(body.rotation)
    if body.z_index is not None:
        obj.z_index = int(body.z_index)
    if body.text is not None:
        obj.text = body.text
    if body.content is not None:
        obj.content_json = body.content
    if body.asset is not None:
        obj.asset_json = body.asset
    if body.style is not None:
        obj.style_json = body.style
    if body.from_object_id is not None:
        obj.from_object_id = body.from_object_id
    if body.from_anchor is not None:
        obj.from_anchor = body.from_anchor
    if body.to_object_id is not None:
        obj.to_object_id = body.to_object_id
    if body.to_anchor is not None:
        obj.to_anchor = body.to_anchor
    if body.media_type is not None:
        obj.media_type = body.media_type

    now = utcnow()
    obj.version = int(obj.version) + 1
    obj.updated_at = now
    proj = db.get(Project, project_id)
    if proj is not None:
        proj.last_activity_at = now
        db.add(proj)
    db.add(obj)
    db.commit()

    lease = lease_manager.get_active_lease(project_id, canvas_id, object_id)
    serialized = serialize_canvas_object(obj, lease=lease.to_dict() if lease else None)
    event_bus.publish(project_id, canvas_id, "object.updated", {"object": serialized}, actor_id=actor_id)
    return serialized


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/objects/{object_id}/duplicate", status_code=201)
def duplicate_canvas_object(
    project_id: str,
    canvas_id: str,
    object_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot duplicate objects")
    _ensure_canvas(db, project_id, canvas_id)

    src = db.get(CanvasObject, object_id)
    if src is None or src.project_id != project_id or src.canvas_id != canvas_id or src.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"object not found: {object_id}")

    now = utcnow()
    dup = CanvasObject(
        id=new_prefixed_id("obj"),
        project_id=project_id,
        canvas_id=canvas_id,
        type=src.type,
        name=f"{src.name} (副本)"[:128] if src.name else "",
        x=float(src.x) + 24.0,
        y=float(src.y) + 24.0,
        w=float(src.w),
        h=float(src.h),
        rotation=float(src.rotation),
        z_index=int(src.z_index) + 1,
        text=src.text,
        content_json=dict(src.content_json) if isinstance(src.content_json, dict) else None,
        asset_json=dict(src.asset_json) if isinstance(src.asset_json, dict) else None,
        style_json=dict(src.style_json) if isinstance(src.style_json, dict) else None,
        from_object_id=src.from_object_id,
        from_anchor=src.from_anchor,
        to_object_id=src.to_object_id,
        to_anchor=src.to_anchor,
        media_type=src.media_type,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(dup)
    db.commit()

    actor_id, _, _ = _actor_identity(user, role)
    serialized = serialize_canvas_object(dup)
    event_bus.publish(project_id, canvas_id, "object.created", {"object": serialized}, actor_id=actor_id)
    return serialized


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/objects/deletions")
def delete_canvas_objects(
    project_id: str,
    canvas_id: str,
    body: ObjectDeletionRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot delete canvas objects")
    _ensure_canvas(db, project_id, canvas_id)
    if not body.object_ids:
        raise HTTPException(status_code=400, detail="object_ids must not be empty")

    actor_id, _, _ = _actor_identity(user, role)
    expected_versions = body.expected_versions or {}
    targets: list[CanvasObject] = []

    # Atomic pre-validation: check existence, lease conflicts, and expected_versions
    for oid in body.object_ids:
        obj = db.get(CanvasObject, oid)
        if obj is None or obj.project_id != project_id or obj.canvas_id != canvas_id or obj.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"object not found: {oid}")
        lease_manager.check_writable(project_id, canvas_id, oid, actor_id)
        if oid in expected_versions and int(expected_versions[oid]) != int(obj.version):
            cur_lease = lease_manager.get_active_lease(project_id, canvas_id, oid)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VERSION_CONFLICT",
                    "message": f"object {oid} version mismatch on delete",
                    "expected_version": expected_versions[oid],
                    "current_version": int(obj.version),
                    "current_object": serialize_canvas_object(obj, lease=cur_lease.to_dict() if cur_lease else None),
                },
            )
        targets.append(obj)

    now = utcnow()
    deleted_ids = {o.id for o in targets}
    for obj in targets:
        obj.deleted_at = now
        obj.updated_at = now
        db.add(obj)

    # Cascade soft-delete connectors attached to any deleted node
    dangling = db.scalars(
        select(CanvasObject).where(
            CanvasObject.canvas_id == canvas_id,
            CanvasObject.type == "connector",
            CanvasObject.deleted_at.is_(None),
            or_(
                CanvasObject.from_object_id.in_(list(deleted_ids)),
                CanvasObject.to_object_id.in_(list(deleted_ids)),
            ),
        )
    ).all()
    cascaded_ids: list[str] = []
    for conn in dangling:
        if conn.id not in deleted_ids:
            conn.deleted_at = now
            conn.updated_at = now
            db.add(conn)
            cascaded_ids.append(conn.id)

    db.commit()
    all_deleted = list(deleted_ids) + cascaded_ids
    lease_manager.release(project_id, canvas_id, all_deleted, actor_id)
    event_bus.publish(project_id, canvas_id, "object.deleted", {"object_ids": all_deleted}, actor_id=actor_id)
    return {"deleted_ids": list(deleted_ids), "cascaded_connector_ids": cascaded_ids}


# --- object leases --------------------------------------------------------
@router.post("/api/projects/{project_id}/canvases/{canvas_id}/objects/leases")
def acquire_object_leases(
    project_id: str,
    canvas_id: str,
    body: LeaseAcquireRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot acquire edit leases")
    _ensure_canvas(db, project_id, canvas_id)
    if not body.object_ids:
        raise HTTPException(status_code=400, detail="object_ids must not be empty")
    for oid in body.object_ids:
        obj = db.get(CanvasObject, oid)
        if obj is None or obj.canvas_id != canvas_id or obj.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"object not found: {oid}")
    actor_id, display_name, _ = _actor_identity(user, role)
    leases = lease_manager.acquire(
        project_id, canvas_id, body.object_ids, actor_id, display_name, ttl_seconds=body.ttl_seconds
    )
    return {"leases": leases}


@router.delete("/api/projects/{project_id}/canvases/{canvas_id}/objects/leases")
def release_object_leases(
    project_id: str,
    canvas_id: str,
    request: Request,
    body: Optional[LeaseReleaseRequest] = None,
    object_ids: Optional[str] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    actor_id, _, _ = _actor_identity(user, role)
    ids: Optional[list[str]] = None
    if body and body.object_ids is not None:
        ids = body.object_ids
    elif object_ids:
        ids = [i.strip() for i in object_ids.split(",") if i.strip()]
    released = lease_manager.release(project_id, canvas_id, ids, actor_id)
    return {"released_object_ids": released}


# --- SSE events, camera presence & online members -------------------------
@router.get("/api/projects/{project_id}/canvases/{canvas_id}/events")
async def canvas_events_stream(
    project_id: str,
    canvas_id: str,
    request: Request,
    once: bool = Query(default=False),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    return StreamingResponse(
        event_bus.subscribe(project_id, canvas_id, once=once),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/camera/presence", status_code=204)
def update_camera_presence(
    project_id: str,
    canvas_id: str,
    body: CameraPresenceRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    actor_id, display_name, avatar_url = _actor_identity(user, role)
    cursor = (
        {"x": body.cursor_x, "y": body.cursor_y}
        if (body.cursor_x is not None and body.cursor_y is not None)
        else None
    )
    presence_manager.heartbeat(
        project_id=project_id,
        canvas_id=canvas_id,
        user_id=actor_id,
        display_name=display_name,
        avatar_url=avatar_url,
        camera={"x": body.x, "y": body.y, "zoom": body.zoom},
        cursor=cursor,
    )
    return Response(status_code=204)


@router.get("/api/projects/{project_id}/canvases/{canvas_id}/cameras")
def list_canvas_cameras(
    project_id: str,
    canvas_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    _ensure_canvas(db, project_id, canvas_id)
    cams = presence_manager.list_canvas_cameras(project_id, canvas_id)
    return {"project_id": project_id, "canvas_id": canvas_id, "cameras": cams}


@router.get("/api/projects/{project_id}/online-members")
def list_online_members(
    project_id: str,
    request: Request,
    canvas_id: Optional[str] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    return presence_manager.list_online_members(db, project_id, current_canvas_id=canvas_id)


# --- shared workspace files drawer ----------------------------------------
@router.get("/api/projects/{project_id}/files")
def list_project_workspace_files(
    project_id: str,
    request: Request,
    keyword: Optional[str] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    stmt = (
        select(StorageObject)
        .where(
            StorageObject.project_id == project_id,
            StorageObject.space == "workspace",
            StorageObject.status == "uploaded",
        )
        .order_by(StorageObject.created_at.desc())
    )
    if keyword and keyword.strip():
        stmt = stmt.where(StorageObject.filename.ilike(f"%{keyword.strip()}%"))
    rows = db.scalars(stmt).all()
    items = [
        {
            "storage_id": r.id,
            "filename": r.filename,
            "object_key": r.object_key,
            "content_type": r.content_type,
            "size_bytes": int(r.size_bytes),
            "duration_seconds": r.duration_seconds,
            "url": storage.build_object_url(r),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]
    return {"project_id": project_id, "files": items, "total": len(items)}
