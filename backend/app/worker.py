"""Lightweight ARQ async worker tuned for 4 vCPU / 8 GiB RAM production servers.

Run in Docker/production:
  arq app.worker.WorkerSettings

Memory footprint: ~120–180 MiB idle (vs 1 GiB+ for prefork Celery), with bounded
concurrency (`max_jobs = 4`) so multimodal polling & FFmpeg never starve the host.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app import model_platform, sandbox_manager, storage
from app.config import get_settings
from app.db import init_db, session_scope
from app.models import GenerationJob, Project, StorageObject, utcnow

logger = logging.getLogger(__name__)


def cleanup_orphan_storage(max_age_hours: int | None = None) -> dict[str, int]:
    """Delete unconfirmed `pending` uploads older than max_age_hours & finalize deleting projects."""
    hours = max_age_hours if max_age_hours is not None else get_settings().orphan_upload_max_age_hours
    cutoff = utcnow() - timedelta(hours=hours)
    cleaned_pending = 0
    cleaned_projects = 0

    with session_scope() as db:
        pending = db.scalars(
            select(StorageObject).where(
                StorageObject.status == "pending",
                StorageObject.created_at <= cutoff,
            )
        ).all()
        for obj in pending:
            storage.invalidate_object(db, obj.id)
            cleaned_pending += 1

        deleting_projects = db.scalars(
            select(Project).where(
                Project.status == "deleting",
                Project.deleted_at.is_not(None),
                Project.deleted_at <= cutoff,
            )
        ).all()
        for proj in deleting_projects:
            objs = db.scalars(
                select(StorageObject).where(
                    StorageObject.project_id == proj.id,
                    StorageObject.status != "deleted",
                )
            ).all()
            for obj in objs:
                storage.invalidate_object(db, obj.id)
            proj.status = "deleted"
            proj.updated_at = utcnow()
            db.add(proj)
            cleaned_projects += 1

    return {"cleaned_pending_uploads": cleaned_pending, "finalized_deleting_projects": cleaned_projects}


async def execute_generation_job_task(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """ARQ background task executing a queued multimodal GenerationJob."""
    def _run() -> dict[str, Any]:
        with session_scope() as db:
            job = db.get(GenerationJob, job_id)
            if job is None:
                return {"job_id": job_id, "status": "not_found"}
            return model_platform.execute_existing_generation_job(db, job)

    return await asyncio.to_thread(_run)


async def cron_reap_idle_sandboxes(ctx: dict[str, Any]) -> dict[str, Any]:
    """Periodic reaper stopping idle sandboxes every minute."""
    def _run() -> list[str]:
        with session_scope() as db:
            return sandbox_manager.reap_idle_sandboxes(db)

    reaped = await asyncio.to_thread(_run)
    if reaped:
        logger.info("Reaped %d idle sandbox session(s)", len(reaped))
    return {"reaped": reaped}


async def cron_cleanup_orphan_uploads(ctx: dict[str, Any]) -> dict[str, int]:
    """Daily 70G ESSD protection job removing stale pending uploads & deleting projects."""
    result = await asyncio.to_thread(cleanup_orphan_storage)
    logger.info("Storage cleanup result: %s", result)
    return result


async def enqueue_generation_job_async(job_id: str) -> bool:
    """Enqueue a generation job to ARQ Redis queue when ASYNC_WORKER_ENABLED=true."""
    settings = get_settings()
    if not settings.async_worker_enabled or not settings.redis_url:
        return False
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            await pool.enqueue_job("execute_generation_job_task", job_id)
        finally:
            await pool.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enqueue ARQ job %s (%s); falling back to inline execution", job_id, exc)
        return False


def enqueue_generation_job(job_id: str) -> bool:
    """Sync wrapper for FastAPI sync routes to enqueue onto ARQ when enabled."""
    settings = get_settings()
    if not settings.async_worker_enabled or not settings.redis_url:
        return False
    try:
        asyncio.run(enqueue_generation_job_async(job_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sync ARQ enqueue failed for %s (%s); running inline", job_id, exc)
        return False


async def startup(ctx: dict[str, Any]) -> None:
    init_db()
    logger.info("Canvasphere ARQ worker started (4C8G profile: max_jobs=4)")


def _build_worker_settings():
    try:
        from arq.connections import RedisSettings
        from arq.cron import cron

        class WorkerSettings:
            functions = [execute_generation_job_task]
            cron_jobs = [
                cron(cron_reap_idle_sandboxes, minute=set(range(60))),
                cron(cron_cleanup_orphan_uploads, hour={3}, minute={30}),
            ]
            on_startup = startup
            max_jobs = 4
            job_timeout = 600
            keep_result = 300
            redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://redis:6379/0")

        return WorkerSettings
    except ImportError:
        class _FallbackWorkerSettings:
            functions = [execute_generation_job_task]
            max_jobs = 4
            job_timeout = 600
            keep_result = 300

        return _FallbackWorkerSettings


WorkerSettings = _build_worker_settings()
