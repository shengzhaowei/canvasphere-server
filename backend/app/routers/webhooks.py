"""Unified async vendor Webhook callback endpoint (/api/webhooks/providers/{provider_family}).

Validates X-Webhook-Secret, locates GenerationJob by external_task_id, guarantees
idempotency on terminal states, persists artifacts, updates canvas nodes, settles
or refunds reserved credits, and broadcasts SSE completion/failure events.
"""
from __future__ import annotations

import base64
import hmac
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import model_platform
from app.config import get_settings
from app.db import get_db
from app.models import GenerationJob

router = APIRouter(tags=["webhooks"])


class ProviderWebhookPayload(BaseModel):
    external_task_id: str
    status: str  # succeeded | failed
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    b64_data: Optional[str] = None
    artifact_url: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


def _verify_webhook_secret(x_webhook_secret: Optional[str]) -> None:
    expected = get_settings().provider_webhook_secret
    if not expected:
        raise HTTPException(status_code=503, detail="PROVIDER_WEBHOOK_SECRET is not configured")
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret.strip(), expected):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


def _extract_artifacts(body: ProviderWebhookPayload, modality: str) -> list[dict[str, Any]]:
    default_mime = {
        "image": "image/png",
        "video": "video/mp4",
        "audio": "audio/mpeg",
        "music": "audio/mpeg",
    }.get(modality, "application/octet-stream")
    default_ext = {"image": "png", "video": "mp4", "audio": "mp3", "music": "mp3"}.get(modality, "bin")

    items = list(body.artifacts)
    if not items and (body.b64_data or body.artifact_url):
        items = [{
            "b64_data": body.b64_data,
            "url": body.artifact_url,
            "filename": body.filename or f"webhook_output.{default_ext}",
            "content_type": body.content_type or default_mime,
        }]

    out: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        if it.get("b64_data"):
            raw = base64.b64decode(it["b64_data"])
        elif it.get("url"):
            with httpx.Client(timeout=60) as client:
                r = client.get(it["url"])
                r.raise_for_status()
                raw = r.content
        else:
            raw = b""
        out.append({
            "filename": it.get("filename") or f"webhook_{idx + 1}.{default_ext}",
            "content_type": it.get("content_type") or default_mime,
            "bytes": raw,
            "meta": it.get("meta") or {},
        })
    return out


@router.post("/api/webhooks/providers/{provider_family}")
def handle_provider_webhook(
    provider_family: str,
    body: ProviderWebhookPayload,
    x_webhook_secret: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _verify_webhook_secret(x_webhook_secret)
    job = db.scalar(
        select(GenerationJob).where(GenerationJob.external_task_id == body.external_task_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail=f"generation job not found for external_task_id: {body.external_task_id}")

    # Idempotency guard: terminal jobs never re-settle or duplicate nodes
    if job.status in ("succeeded", "failed"):
        return {
            "received": True,
            "idempotent": True,
            "provider_family": provider_family,
            "job_id": job.id,
            "status": job.status,
        }

    norm_status = body.status.strip().lower()
    if norm_status not in ("succeeded", "failed"):
        raise HTTPException(status_code=400, detail="webhook status must be succeeded or failed")

    if norm_status == "failed":
        result = model_platform.finalize_generation_job(
            db,
            job,
            succeeded=False,
            error_message=body.error_message or f"{provider_family} reported failure",
        )
    else:
        artifacts = _extract_artifacts(body, job.modality)
        result = model_platform.finalize_generation_job(
            db,
            job,
            succeeded=True,
            artifacts=artifacts,
            metadata={**body.metadata, "webhook_provider": provider_family},
            mock=False,
        )

    return {
        "received": True,
        "idempotent": False,
        "provider_family": provider_family,
        "job_id": job.id,
        "status": result["status"],
        "result": result,
    }
