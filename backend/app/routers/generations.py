"""Model platform catalog, price estimation & 4-modality generation routes."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, billing, model_platform
from app.db import get_db
from app.models import Canvas, GenerationJob, Project, RateCard
from app.permissions import can_write
from app.rate_cards_seed import ensure_rate_cards_seeded

router = APIRouter(tags=["generations"])


class EstimateRequest(BaseModel):
    model_key: str
    params: dict[str, Any] = Field(default_factory=dict)


class ImageGenerationRequest(BaseModel):
    model_key: str
    prompt: str
    target_object_id: Optional[str] = None
    input_object_ids: list[str] = Field(default_factory=list)
    count: int = 1
    size: str = "auto"
    aspect_ratio: str = "1:1"
    quality: str = "auto"
    background: str = "auto"
    output_format: str = "png"
    composition_complexity: str = "medium"
    moderation: str = "auto"
    # Midjourney specific
    prompt_mode: str = "visual"
    stylize: Optional[int] = None
    seed: Optional[int] = None
    payment_source: str = "platform_credits"


class MidjourneyActionRequest(BaseModel):
    action: str  # u1..u4, v1..v4, vary_subtle, vary_strong, reroll, upscale_subtle, upscale_creative, zoom_1.5x, zoom_2x, pan_left/right/up/down
    target_object_id: Optional[str] = None
    payment_source: str = "platform_credits"


class VideoGenerationRequest(BaseModel):
    model_key: str
    prompt: str = ""
    mode: str = "t2v"  # t2v | first_frame | last_frame | first_last_frame | multimodal_ref | draft_extend
    target_object_id: Optional[str] = None
    resolution: Optional[str] = None
    aspect_ratio: Optional[str] = "16:9"
    duration_seconds: int = 5
    first_frame_object_id: Optional[str] = None
    last_frame_object_id: Optional[str] = None
    ref_image_ids: list[str] = Field(default_factory=list)
    ref_video_ids: list[str] = Field(default_factory=list)
    ref_audio_ids: list[str] = Field(default_factory=list)
    draft_job_id: Optional[str] = None
    payment_source: str = "platform_credits"


class AudioGenerationRequest(BaseModel):
    model_key: str = "speech-2.8-hd"
    mode: str = "tts"  # tts | voice_clone | voice_design | asr
    prompt: str = ""
    target_object_id: Optional[str] = None
    voice_id: Optional[str] = None
    source_audio_object_id: Optional[str] = None
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    emotion: str = "auto"
    language_boost: str = "auto"
    watermark: bool = False
    output_format: str = "mp3"
    sample_rate: int = 32000
    channels: int = 1
    mp3_bitrate: int = 128000
    normalize_numbers: bool = False
    read_latex: bool = False
    generate_subtitles: bool = False
    pronunciation_dict: Optional[dict[str, str]] = None
    voice_effect: Optional[str] = None
    voice_mix: Optional[list[dict[str, Any]]] = None
    duration_seconds: int = 10
    payment_source: str = "platform_credits"


class MusicGenerationRequest(BaseModel):
    model_key: str = "suno-music"
    mode: str = "song_bgm"  # song_bgm | sfx | reference | mashup | hum | expand_style
    operation: Optional[str] = None  # defaults to mode; supports all 20 Suno ops
    prompt: str = ""
    lyrics: Optional[str] = None
    instrumental: bool = False
    style: Optional[str] = None
    title: Optional[str] = None
    version: str = "suno-v6"
    output_format: str = "mp3"
    loop_sfx: bool = False
    reference_object_ids: list[str] = Field(default_factory=list)
    target_object_id: Optional[str] = None
    payment_source: str = "platform_credits"


def _ensure_project_and_canvas(db: Session, project_id: str, canvas_id: str) -> tuple[Project, Canvas]:
    proj = db.get(Project, project_id)
    if proj is None or proj.status == "deleted":
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    canvas = db.get(Canvas, canvas_id)
    if canvas is None or canvas.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"canvas not found: {canvas_id}")
    return proj, canvas


# --- Model Platform Catalog & Estimation ---------------------------------
@router.get("/api/model-platform/protocol-profiles")
def list_protocol_profiles() -> dict[str, Any]:
    return {"profiles": model_platform.PROTOCOL_PROFILES, "total": len(model_platform.PROTOCOL_PROFILES)}


@router.get("/api/model-platform/models")
def list_platform_models(
    modality: Optional[str] = Query(default=None),
    billing_mode: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    stmt = select(RateCard).where(RateCard.enabled.is_(True)).order_by(RateCard.sort_order.asc())
    if modality:
        m = modality.strip().lower()
        if m == "audio":
            stmt = stmt.where(RateCard.modality.in_(["tts", "asr"]))
        else:
            stmt = stmt.where(RateCard.modality == m)
    if billing_mode and billing_mode.strip().lower() != "all":
        stmt = stmt.where(RateCard.billing_mode == billing_mode.strip().lower())
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(RateCard.display_name.ilike(kw) | RateCard.model_key.ilike(kw))

    cards = db.scalars(stmt).all()
    items = [
        {
            "model_key": c.model_key,
            "display_name": c.display_name,
            "modality": c.modality,
            "provider_family": c.provider_family,
            "protocol_profile": c.protocol_profile,
            "billing_mode": c.billing_mode,
            "unit_price_credits": float(c.unit_price_credits),
            "output_price_credits": float(c.output_price_credits) if c.output_price_credits is not None else None,
            "pricing_meta": c.pricing_meta_json,
            "capabilities": c.capabilities_json,
        }
        for c in cards
    ]
    return {"models": items, "total": len(items)}


@router.post("/api/model-platform/estimate")
def estimate_model_cost(
    body: EstimateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = auth.get_optional_current_user(request, db)
    return billing.estimate_generation_cost(db, body.model_key, body.params, user_id=user.id if user else None)


# --- 4-Modality Generation Routes ----------------------------------------
@router.post("/api/projects/{project_id}/canvases/{canvas_id}/generations/image", status_code=201)
def generate_image(
    project_id: str,
    canvas_id: str,
    body: ImageGenerationRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger image generation")
    proj, canvas = _ensure_project_and_canvas(db, project_id, canvas_id)
    params = body.model_dump(exclude={"model_key", "prompt", "target_object_id", "input_object_ids", "payment_source"})
    return model_platform.run_generation_pipeline(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        modality="image",
        model_key=body.model_key,
        mode="imagine",
        prompt=body.prompt,
        params=params,
        target_object_id=body.target_object_id,
        input_object_ids=body.input_object_ids,
        payment_source=body.payment_source,
    )


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/generations/{job_id}/mj-action", status_code=201)
def run_midjourney_action(
    project_id: str,
    canvas_id: str,
    job_id: str,
    body: MidjourneyActionRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger Midjourney action")
    proj, canvas = _ensure_project_and_canvas(db, project_id, canvas_id)
    parent = db.get(GenerationJob, job_id)
    if parent is None or parent.project_id != project_id or parent.canvas_id != canvas_id:
        raise HTTPException(status_code=404, detail=f"parent generation job not found: {job_id}")
    if parent.protocol_profile != "midjourney_proxy":
        raise HTTPException(status_code=400, detail="mj-action is only valid for Midjourney jobs")
    action = body.action.strip().lower()
    params = {**dict(parent.params_json or {}), "mj_action": action, "parent_job_id": parent.id}
    return model_platform.run_generation_pipeline(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        modality="image",
        model_key=parent.model_key,
        mode=action,
        prompt=parent.prompt,
        params=params,
        target_object_id=body.target_object_id,
        input_object_ids=[parent.target_object_id] if parent.target_object_id else [],
        parent_job_id=parent.id,
        payment_source=body.payment_source,
    )


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/generations/video", status_code=201)
def generate_video(
    project_id: str,
    canvas_id: str,
    body: VideoGenerationRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger video generation")
    proj, canvas = _ensure_project_and_canvas(db, project_id, canvas_id)
    params = body.model_dump(exclude={"model_key", "prompt", "mode", "target_object_id", "payment_source"})
    refs = [x for x in [body.first_frame_object_id, body.last_frame_object_id] if x] + body.ref_image_ids + body.ref_video_ids + body.ref_audio_ids
    return model_platform.run_generation_pipeline(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        modality="video",
        model_key=body.model_key,
        mode=body.mode.strip().lower(),
        prompt=body.prompt,
        params=params,
        target_object_id=body.target_object_id,
        input_object_ids=refs,
        parent_job_id=body.draft_job_id,
        payment_source=body.payment_source,
    )


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/generations/audio", status_code=201)
def generate_audio(
    project_id: str,
    canvas_id: str,
    body: AudioGenerationRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger audio generation")
    proj, canvas = _ensure_project_and_canvas(db, project_id, canvas_id)
    params = body.model_dump(exclude={"model_key", "prompt", "mode", "target_object_id", "payment_source"})
    refs = [body.source_audio_object_id] if body.source_audio_object_id else []
    return model_platform.run_generation_pipeline(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        modality="audio",
        model_key=body.model_key,
        mode=body.mode.strip().lower(),
        prompt=body.prompt,
        params=params,
        target_object_id=body.target_object_id,
        input_object_ids=refs,
        payment_source=body.payment_source,
    )


@router.post("/api/projects/{project_id}/canvases/{canvas_id}/generations/music", status_code=201)
def generate_music(
    project_id: str,
    canvas_id: str,
    body: MusicGenerationRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot trigger music generation")
    proj, canvas = _ensure_project_and_canvas(db, project_id, canvas_id)
    mode = body.mode.strip().lower()
    operation = (body.operation or mode).strip().lower()
    params = body.model_dump(exclude={"model_key", "prompt", "mode", "target_object_id", "payment_source"})
    params["operation"] = operation
    return model_platform.run_generation_pipeline(
        db,
        project=proj,
        canvas=canvas,
        user_id=user.id if user else None,
        modality="music",
        model_key=body.model_key,
        mode=mode,
        prompt=body.prompt,
        params=params,
        target_object_id=body.target_object_id,
        input_object_ids=body.reference_object_ids,
        payment_source=body.payment_source,
    )


@router.get("/api/projects/{project_id}/generations")
def list_project_generations(
    project_id: str,
    request: Request,
    canvas_id: Optional[str] = Query(default=None),
    modality: Optional[str] = Query(default=None),
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    stmt = (
        select(GenerationJob)
        .where(GenerationJob.project_id == project_id)
        .order_by(GenerationJob.created_at.desc())
    )
    if canvas_id:
        stmt = stmt.where(GenerationJob.canvas_id == canvas_id)
    if modality:
        stmt = stmt.where(GenerationJob.modality == modality.strip().lower())
    rows = db.scalars(stmt).all()
    return {"items": [model_platform.serialize_job(r) for r in rows], "total": len(rows)}


@router.get("/api/projects/{project_id}/generations/{job_id}")
def get_project_generation(
    project_id: str,
    job_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    job = db.get(GenerationJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"generation job not found: {job_id}")
    return model_platform.serialize_job(job)
