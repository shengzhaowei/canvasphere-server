"""Project-scoped cloud sandbox lifecycle manager, idle reaping, spend cap & billing.

Specs aligned with ai.pecan.wang:
- 2核4G : 53.20 credits/hour
- 4核8G : 96.40 credits/hour
- Default project spend cap : 500 credits
- Default idle retention    : 2 minutes
- Failure-free billing      : failed runs charge 0 credits (failure_free=True)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import billing, sandbox, storage
from app.config import get_settings
from app.models import (
    BillingSandboxRecord,
    Project,
    ProjectSandboxConfig,
    SandboxSession,
    StorageObject,
    new_prefixed_id,
    utcnow,
)

Q4 = Decimal("0.0001")
ZERO = Decimal("0.0000")

SANDBOX_SPECS: dict[str, dict[str, Any]] = {
    "2核4G": {"name": "2核4G", "cpus": 2, "memory_gb": 4, "price_per_hour": Decimal("53.2000")},
    "4核8G": {"name": "4核8G", "cpus": 4, "memory_gb": 8, "price_per_hour": Decimal("96.4000")},
}


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def get_or_create_sandbox_config(db: Session, project_id: str) -> ProjectSandboxConfig:
    cfg = db.get(ProjectSandboxConfig, project_id)
    if cfg is None:
        settings = get_settings()
        cfg = ProjectSandboxConfig(
            project_id=project_id,
            enabled=False,
            spec_name="2核4G",
            spend_cap_credits=Decimal(str(settings.sandbox_default_cap_credits)).quantize(Q4),
            idle_keep_minutes=settings.sandbox_default_idle_minutes,
            spent_credits=ZERO,
            updated_at=utcnow(),
        )
        db.add(cfg)
        db.flush()
    return cfg


def serialize_sandbox_config(db: Session, cfg: ProjectSandboxConfig) -> dict[str, Any]:
    active_sess = db.scalar(
        select(SandboxSession)
        .where(SandboxSession.project_id == cfg.project_id, SandboxSession.status == "active")
        .order_by(SandboxSession.last_active_at.desc())
        .limit(1)
    )
    cap = Decimal(str(cfg.spend_cap_credits)).quantize(Q4)
    spent = Decimal(str(cfg.spent_credits)).quantize(Q4)
    return {
        "project_id": cfg.project_id,
        "enabled": bool(cfg.enabled),
        "spec_name": cfg.spec_name,
        "specs": [
            {
                "name": s["name"],
                "cpus": s["cpus"],
                "memory_gb": s["memory_gb"],
                "price_per_hour": float(s["price_per_hour"]),
            }
            for s in SANDBOX_SPECS.values()
        ],
        "spend_cap_credits": float(cap),
        "spent_credits": float(spent),
        "remaining_cap_credits": float(max(ZERO, cap - spent)),
        "idle_keep_minutes": int(cfg.idle_keep_minutes),
        "failure_free": True,
        "active_session": (
            {
                "session_id": active_sess.id,
                "spec_name": active_sess.spec_name,
                "provider": active_sess.provider,
                "status": active_sess.status,
                "started_at": active_sess.started_at.isoformat(),
                "last_active_at": active_sess.last_active_at.isoformat(),
                "duration_seconds": float(active_sess.duration_seconds),
            }
            if active_sess
            else None
        ),
    }


def update_sandbox_config(
    db: Session,
    project_id: str,
    *,
    enabled: Optional[bool] = None,
    spec_name: Optional[str] = None,
    spend_cap_credits: Optional[float] = None,
    idle_keep_minutes: Optional[int] = None,
) -> dict[str, Any]:
    cfg = get_or_create_sandbox_config(db, project_id)
    if spec_name is not None:
        if spec_name not in SANDBOX_SPECS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported sandbox spec: {spec_name!r} (allowed: {list(SANDBOX_SPECS.keys())})",
            )
        cfg.spec_name = spec_name
    if enabled is not None:
        cfg.enabled = bool(enabled)
    if spend_cap_credits is not None:
        if spend_cap_credits < 0:
            raise HTTPException(status_code=400, detail="spend_cap_credits must be >= 0")
        cfg.spend_cap_credits = Decimal(str(spend_cap_credits)).quantize(Q4)
    if idle_keep_minutes is not None:
        if idle_keep_minutes < 1 or idle_keep_minutes > 60:
            raise HTTPException(status_code=400, detail="idle_keep_minutes must be 1..60")
        cfg.idle_keep_minutes = int(idle_keep_minutes)
    cfg.updated_at = utcnow()
    db.add(cfg)
    db.commit()
    return serialize_sandbox_config(db, cfg)


def reap_idle_sandboxes(db: Session, project_id: Optional[str] = None) -> list[str]:
    """Stop any active sandbox session idle longer than its project's idle_keep_minutes."""
    now = utcnow()
    stmt = select(SandboxSession).where(SandboxSession.status == "active")
    if project_id:
        stmt = stmt.where(SandboxSession.project_id == project_id)
    sessions = db.scalars(stmt).all()
    reaped: list[str] = []
    for sess in sessions:
        cfg = get_or_create_sandbox_config(db, sess.project_id)
        idle_limit = timedelta(minutes=int(cfg.idle_keep_minutes))
        if now - _as_utc(sess.last_active_at) >= idle_limit:
            sess.status = "stopped"
            sess.stopped_at = now
            db.add(sess)
            reaped.append(sess.id)
    if reaped:
        db.commit()
    return reaped


def stop_project_sandbox(db: Session, project_id: str) -> list[str]:
    now = utcnow()
    sessions = db.scalars(
        select(SandboxSession).where(
            SandboxSession.project_id == project_id,
            SandboxSession.status == "active",
        )
    ).all()
    stopped: list[str] = []
    for sess in sessions:
        sess.status = "stopped"
        sess.stopped_at = now
        db.add(sess)
        stopped.append(sess.id)
    db.commit()
    return stopped


def _persist_sandbox_output_file(
    db: Session,
    project: Project,
    user_id: Optional[str],
    filename: str,
    content_type: str,
    content: str,
) -> dict[str, Any]:
    raw = content.encode("utf-8")
    key = storage.build_object_key(
        "workspace",
        filename,
        owner_user_id=project.owner_id or user_id,
        project_id=project.id,
        workspace_id=project.workspace_id,
    )
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        storage._s3_client().put_object(Bucket=settings.s3_bucket, Key=key, Body=raw, ContentType=content_type)
    else:
        path = storage._local_path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    obj = StorageObject(
        id=new_prefixed_id("sto"),
        space="workspace",
        owner_user_id=project.owner_id or user_id,
        project_id=project.id,
        object_key=key,
        filename=filename,
        content_type=content_type,
        size_bytes=len(raw),
        status="uploaded",
        created_at=utcnow(),
    )
    db.add(obj)
    db.flush()
    return {
        "storage_id": obj.id,
        "filename": obj.filename,
        "content_type": obj.content_type,
        "size_bytes": obj.size_bytes,
        "url": storage.build_object_url(obj),
    }


def execute_in_project_sandbox(
    db: Session,
    *,
    project: Project,
    user_id: Optional[str],
    code: str,
    timeout: int = 30,
    output_files: Optional[list[dict[str, str]]] = None,
    simulated_duration_seconds: Optional[float] = None,
) -> dict[str, Any]:
    if not code or not code.strip():
        raise HTTPException(status_code=400, detail="code must not be empty")
    reap_idle_sandboxes(db, project_id=project.id)

    cfg = get_or_create_sandbox_config(db, project.id)
    if not cfg.enabled:
        raise HTTPException(status_code=403, detail="sandbox is disabled in this project; enable it in sandbox settings")

    cap = Decimal(str(cfg.spend_cap_credits)).quantize(Q4)
    spent = Decimal(str(cfg.spent_credits)).quantize(Q4)
    if spent >= cap:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "SANDBOX_CAP_EXCEEDED",
                "message": "project sandbox spend cap reached",
                "spent_credits": float(spent),
                "spend_cap_credits": float(cap),
            },
        )

    # Reuse active keepalive session for matching spec, or start a new one
    now = utcnow()
    sess = db.scalar(
        select(SandboxSession)
        .where(
            SandboxSession.project_id == project.id,
            SandboxSession.status == "active",
            SandboxSession.spec_name == cfg.spec_name,
        )
        .order_by(SandboxSession.last_active_at.desc())
        .limit(1)
    )
    if sess is None:
        sess = SandboxSession(
            id=new_prefixed_id("sbs"),
            project_id=project.id,
            user_id=user_id,
            spec_name=cfg.spec_name,
            provider="local-docker",
            status="active",
            started_at=now,
            last_active_at=now,
            duration_seconds=0.0,
            credits_charged=ZERO,
            failure_free=False,
        )
        db.add(sess)
        db.flush()

    t0 = time.monotonic()
    exec_result = sandbox.run_code(code, timeout=timeout)
    measured = max(0.1, time.monotonic() - t0)
    run_duration = float(simulated_duration_seconds) if simulated_duration_seconds is not None else round(measured, 3)

    failed = exec_result.get("error") is not None or exec_result.get("exit_code") not in (0, None) or exec_result.get("sandbox") == "unavailable"
    spec_info = SANDBOX_SPECS[cfg.spec_name]
    hourly = spec_info["price_per_hour"]

    credits_charged = ZERO
    failure_free = False
    record_status = "settled"

    if failed:
        failure_free = True
        record_status = "failure_free"
    else:
        raw_cost = (Decimal(str(run_duration)) / Decimal("3600")) * hourly
        credits_charged = max(Decimal("0.0100"), raw_cost.quantize(Q4, rounding=ROUND_HALF_UP))
        # Cap check before charging
        if spent + credits_charged > cap:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "SANDBOX_CAP_EXCEEDED",
                    "message": "execution cost exceeds project sandbox spend cap",
                    "required_credits": float(credits_charged),
                    "remaining_cap_credits": float(max(ZERO, cap - spent)),
                },
            )
        if user_id:
            res = billing.reserve_credits(
                db,
                user_id=user_id,
                estimated_credits=credits_charged,
                source_type="sandbox",
                source_id=sess.id,
            )
            billing.settle_reservation(
                db,
                res.id,
                actual_credits=credits_charged,
                project_id=project.id,
                job_id=sess.id,
                modality="sandbox",
                model_key=cfg.spec_name,
                quantity={"duration_seconds": run_duration},
            )
        cfg.spent_credits = (spent + credits_charged).quantize(Q4)
        cfg.updated_at = utcnow()
        db.add(cfg)

    sess.provider = str(exec_result.get("sandbox") or sess.provider)
    sess.last_active_at = utcnow()
    sess.duration_seconds = float(sess.duration_seconds) + run_duration
    sess.credits_charged = (Decimal(str(sess.credits_charged)) + credits_charged).quantize(Q4)
    sess.failure_free = failure_free
    db.add(sess)

    if user_id:
        db.add(BillingSandboxRecord(
            id=new_prefixed_id("sbx"),
            user_id=user_id,
            project_id=project.id,
            spec_name=cfg.spec_name,
            duration_seconds=run_duration,
            credits_charged=credits_charged,
            failure_free=failure_free,
            status=record_status,
            created_at=utcnow(),
        ))

    saved_files: list[dict[str, Any]] = []
    if not failed and output_files:
        for f in output_files:
            fname = f.get("filename") or "sandbox_output.html"
            ctype = f.get("content_type") or "text/html"
            body = f.get("content") or ""
            saved_files.append(_persist_sandbox_output_file(db, project, user_id, fname, ctype, body))

    db.commit()
    return {
        "session_id": sess.id,
        "spec_name": cfg.spec_name,
        "duration_seconds": run_duration,
        "credits_charged": float(credits_charged),
        "failure_free": failure_free,
        "execution": exec_result,
        "output_files": saved_files,
        "config": serialize_sandbox_config(db, cfg),
    }
