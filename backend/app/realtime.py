"""Realtime collaboration engine: Canvas EventBus (SSE), Object Leases, and Camera Presence.

In-process thread-safe implementation designed with clean boundaries so it can
be backed by Redis Pub/Sub & hash keys when scaling to multiple API workers.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ProjectMember, User


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Canvas EventBus (SSE) ------------------------------------------------
@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]


class CanvasEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[tuple[str, str], list[_Subscriber]] = {}
        self._history: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def reset(self) -> None:
        with self._lock:
            self._subs.clear()
            self._history.clear()

    def publish(
        self,
        project_id: str,
        canvas_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ev = {
            "event_id": uuid.uuid4().hex[:16],
            "type": event_type,
            "project_id": project_id,
            "canvas_id": canvas_id,
            "actor_id": actor_id,
            "payload": payload,
            "at": _now_iso(),
        }
        key = (project_id, canvas_id)
        with self._lock:
            hist = self._history.setdefault(key, [])
            hist.append(ev)
            if len(hist) > 100:
                del hist[:-100]
            subs = list(self._subs.get(key, []))

        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(self._safe_put, sub.queue, ev)
            except RuntimeError:
                pass
        return ev

    @staticmethod
    def _safe_put(queue: asyncio.Queue[dict[str, Any]], ev: dict[str, Any]) -> None:
        try:
            queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    def recent_events(self, project_id: str, canvas_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history.get((project_id, canvas_id), [])[-limit:])

    async def subscribe(
        self,
        project_id: str,
        canvas_id: str,
        *,
        once: bool = False,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        sub = _Subscriber(loop=loop, queue=q)
        key = (project_id, canvas_id)
        with self._lock:
            self._subs.setdefault(key, []).append(sub)
        try:
            hello = {
                "event_id": uuid.uuid4().hex[:16],
                "type": "connected",
                "project_id": project_id,
                "canvas_id": canvas_id,
                "at": _now_iso(),
            }
            yield f"event: connected\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
            if once:
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=heartbeat_seconds)
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with self._lock:
                lst = self._subs.get(key)
                if lst and sub in lst:
                    lst.remove(sub)


# --- Object Leases (Optimistic / Exclusive Edit Lock) ---------------------
@dataclass
class ObjectLease:
    lease_id: str
    project_id: str
    canvas_id: str
    object_id: str
    user_id: str
    display_name: str
    acquired_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "object_id": self.object_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "acquired_at": datetime.fromtimestamp(self.acquired_at, tz=timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat(),
        }


class LeaseManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str, str], ObjectLease] = {}

    def reset(self) -> None:
        with self._lock:
            self._leases.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._leases.items() if v.expires_at <= now]
        for k in expired:
            del self._leases[k]

    def get_active_lease(self, project_id: str, canvas_id: str, object_id: str) -> Optional[ObjectLease]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            return self._leases.get((project_id, canvas_id, object_id))

    def list_canvas_leases(self, project_id: str, canvas_id: str) -> dict[str, dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            return {
                oid: lease.to_dict()
                for (pid, cid, oid), lease in self._leases.items()
                if pid == project_id and cid == canvas_id
            }

    def check_writable(self, project_id: str, canvas_id: str, object_id: str, actor_id: str) -> None:
        lease = self.get_active_lease(project_id, canvas_id, object_id)
        if lease is not None and lease.user_id != actor_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "OBJECT_LEASE_CONFLICT",
                    "message": f"object {object_id} is currently locked by {lease.display_name}",
                    "holder": lease.to_dict(),
                },
            )

    def acquire(
        self,
        project_id: str,
        canvas_id: str,
        object_ids: list[str],
        actor_id: str,
        display_name: str,
        ttl_seconds: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        ttl = ttl_seconds or get_settings().object_lease_ttl_seconds
        now = time.time()
        acquired: list[ObjectLease] = []
        with self._lock:
            self._prune_locked(now)
            # Atomic pre-check: all or nothing
            for oid in object_ids:
                existing = self._leases.get((project_id, canvas_id, oid))
                if existing is not None and existing.user_id != actor_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "OBJECT_LEASE_CONFLICT",
                            "message": f"object {oid} is locked by {existing.display_name}",
                            "holder": existing.to_dict(),
                        },
                    )
            for oid in object_ids:
                key = (project_id, canvas_id, oid)
                existing = self._leases.get(key)
                if existing is not None:
                    existing.expires_at = now + ttl
                    lease = existing
                else:
                    lease = ObjectLease(
                        lease_id=f"lse_{uuid.uuid4().hex[:12]}",
                        project_id=project_id,
                        canvas_id=canvas_id,
                        object_id=oid,
                        user_id=actor_id,
                        display_name=display_name,
                        acquired_at=now,
                        expires_at=now + ttl,
                    )
                    self._leases[key] = lease
                acquired.append(lease)

        result = [l.to_dict() for l in acquired]
        event_bus.publish(project_id, canvas_id, "lease.acquired", {"leases": result}, actor_id=actor_id)
        return result

    def release(
        self,
        project_id: str,
        canvas_id: str,
        object_ids: Optional[list[str]],
        actor_id: str,
    ) -> list[str]:
        now = time.time()
        released: list[str] = []
        with self._lock:
            self._prune_locked(now)
            targets = list(self._leases.keys())
            for key in targets:
                pid, cid, oid = key
                if pid != project_id or cid != canvas_id:
                    continue
                if object_ids is not None and oid not in object_ids:
                    continue
                lease = self._leases[key]
                if lease.user_id == actor_id:
                    del self._leases[key]
                    released.append(oid)
        if released:
            event_bus.publish(project_id, canvas_id, "lease.released", {"object_ids": released}, actor_id=actor_id)
        return released


# --- Camera Presence & Online Members -------------------------------------
@dataclass
class CameraPresence:
    project_id: str
    canvas_id: str
    user_id: str
    display_name: str
    avatar_url: Optional[str]
    camera: dict[str, Any] = field(default_factory=dict)
    cursor: Optional[dict[str, Any]] = None
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "canvas_id": self.canvas_id,
            "camera": dict(self.camera),
            "cursor": dict(self.cursor) if isinstance(self.cursor, dict) else None,
            "updated_at": datetime.fromtimestamp(self.updated_at, tz=timezone.utc).isoformat(),
        }


class PresenceManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._presence: dict[tuple[str, str], CameraPresence] = {}  # (project_id, user_id)

    def reset(self) -> None:
        with self._lock:
            self._presence.clear()

    def _prune_locked(self, now: float) -> None:
        ttl = get_settings().presence_ttl_seconds
        expired = [k for k, v in self._presence.items() if now - v.updated_at > ttl]
        for k in expired:
            del self._presence[k]

    def heartbeat(
        self,
        *,
        project_id: str,
        canvas_id: str,
        user_id: str,
        display_name: str,
        avatar_url: Optional[str],
        camera: dict[str, Any],
        cursor: Optional[dict[str, Any]] = None,
    ) -> CameraPresence:
        now = time.time()
        entry = CameraPresence(
            project_id=project_id,
            canvas_id=canvas_id,
            user_id=user_id,
            display_name=display_name,
            avatar_url=avatar_url,
            camera=dict(camera or {}),
            cursor=dict(cursor) if isinstance(cursor, dict) else None,
            updated_at=now,
        )
        with self._lock:
            self._prune_locked(now)
            self._presence[(project_id, user_id)] = entry
        event_bus.publish(project_id, canvas_id, "camera.updated", entry.to_dict(), actor_id=user_id)
        return entry

    def list_canvas_cameras(self, project_id: str, canvas_id: str) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            return [
                p.to_dict()
                for (pid, _uid), p in self._presence.items()
                if pid == project_id and p.canvas_id == canvas_id
            ]

    def list_online_members(
        self,
        db: Session,
        project_id: str,
        current_canvas_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            active_map = {
                uid: p
                for (pid, uid), p in self._presence.items()
                if pid == project_id
            }

        rows = db.execute(
            select(ProjectMember, User)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.joined_at.asc())
        ).all()

        in_current: list[dict[str, Any]] = []
        in_other: list[dict[str, Any]] = []
        offline: list[dict[str, Any]] = []

        for m, u in rows:
            p = active_map.get(u.id)
            base = {
                "user_id": u.id,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "role": m.role,
            }
            if p is None:
                offline.append({**base, "online": False, "canvas_id": None})
            elif current_canvas_id and p.canvas_id == current_canvas_id:
                in_current.append({**base, "online": True, "canvas_id": p.canvas_id})
            else:
                in_other.append({**base, "online": True, "canvas_id": p.canvas_id})

        return {
            "project_id": project_id,
            "current_canvas_id": current_canvas_id,
            "in_current_canvas": in_current,
            "in_other_canvases": in_other,
            "offline": offline,
            "online_count": len(in_current) + len(in_other),
        }


event_bus = CanvasEventBus()
lease_manager = LeaseManager()
presence_manager = PresenceManager()


def reset_realtime_state() -> None:
    event_bus.reset()
    lease_manager.reset()
    presence_manager.reset()
