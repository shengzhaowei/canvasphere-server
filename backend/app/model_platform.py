"""Protocol-driven multimodal model platform & generation execution engine.

Provides:
- Standardized Protocol Profiles (mirroring ai.pecan.wang adapters such as
  zongheng_video, ark_images, openlux_gpt_images, midjourney_proxy, suno_music)
- Strict parameter validators for Image (15 models + MJ actions), Video (40 offerings
  + 6 guidance modes), Audio (TTS/voice_clone/voice_design/ASR), and Music (Suno 6 modes)
- Real upstream gateway HTTP invocation when MODEL_GATEWAY_BASE_URL + API key exist,
  with full-pipeline safe mock fallback that writes valid placeholder artifacts to
  project workspace storage, updates target canvas nodes, and broadcasts SSE events.
"""
from __future__ import annotations

import base64
import struct
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import billing, byok, storage
from app.config import get_settings
from app.models import (
    BillingUsageRecord,
    ByokChannel,
    ByokEntitlement,
    ByokOffering,
    Canvas,
    CanvasObject,
    GenerationJob,
    Project,
    RateCard,
    StorageObject,
    UserVoiceActivation,
    new_prefixed_id,
    utcnow,
)
from app.protocol_profiles import PROTOCOL_PROFILES
from app.providers import AdapterCallResult, get_adapter_for_profile, resolve_env_credentials
from app.rate_cards_seed import MJ_ACTIONS_MULTIPLIER, SUNO_OPERATIONS_PRICING, ensure_rate_cards_seeded
from app.realtime import event_bus
from app.redis_client import rate_limiter

# Minimal valid binary headers for mock artifacts
_MOCK_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_MOCK_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08free"
_MOCK_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00" + b"\x00" * 64


def _make_mock_wav() -> bytes:
    sample_rate = 16000
    num_samples = 160
    data = b"\x00\x00" * num_samples
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(data),
    )
    return header + data


_MOCK_WAV_BYTES = _make_mock_wav()


_PROFILE_MAP = {p["profile_key"]: p for p in PROTOCOL_PROFILES}

ALLOWED_IMAGE_FORMATS = frozenset({"png", "jpeg", "webp", "gif"})
ALLOWED_AUDIO_MODES = frozenset({"tts", "voice_clone", "voice_design", "asr"})
ALLOWED_AUDIO_EMOTIONS = frozenset({"auto", "happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"})
ALLOWED_AUDIO_FORMATS = frozenset({"mp3", "pcm", "flac", "wav", "pcmu_raw", "pcmu_wav", "opus"})
ALLOWED_AUDIO_EFFECTS = frozenset({"spacious_echo", "auditorium_echo", "lofi_telephone", "robotic"})
ALLOWED_MUSIC_MODES = frozenset({"song_bgm", "sfx", "reference", "mashup", "hum", "expand_style"})
ALLOWED_MUSIC_VERSIONS = frozenset({"suno-v6", "suno-v6-wild", "suno-v6-mini"})
ALLOWED_MUSIC_FORMATS = frozenset({"mp3", "wav", "aac", "flac", "opus"})


def get_rate_card(db: Session, model_key: str, expected_modality: Optional[str] = None) -> RateCard:
    ensure_rate_cards_seeded(db)
    card = db.scalar(select(RateCard).where(RateCard.model_key == model_key, RateCard.enabled.is_(True)))
    if card is None:
        raise HTTPException(status_code=404, detail=f"model not found or disabled: {model_key}")
    if expected_modality:
        allowed = {expected_modality} if isinstance(expected_modality, str) else set(expected_modality)
        if expected_modality == "audio":
            allowed = {"tts", "asr"}
        if card.modality not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"model {model_key} has modality {card.modality!r}, expected {sorted(allowed)}",
            )
    return card


def validate_image_params(card: RateCard, params: dict[str, Any]) -> None:
    caps = card.capabilities_json or {}
    if caps.get("is_midjourney"):
        prompt_mode = str(params.get("prompt_mode") or "visual").lower()
        if prompt_mode not in ("manual", "visual", "hybrid"):
            raise HTTPException(status_code=400, detail="midjourney prompt_mode must be manual, visual, or hybrid")
        action = str(params.get("mj_action") or "imagine").lower()
        if action not in MJ_ACTIONS_MULTIPLIER:
            raise HTTPException(status_code=400, detail=f"unsupported midjourney action: {action}")
        return

    count = int(params.get("count", 1))
    max_count = int(caps.get("max_count", 10))
    if count < 1 or count > max_count:
        raise HTTPException(status_code=400, detail=f"count must be between 1 and {max_count}")
    fmt = str(params.get("output_format") or "png").lower()
    if fmt not in ALLOWED_IMAGE_FORMATS:
        raise HTTPException(status_code=400, detail=f"unsupported output_format: {fmt}")


def validate_video_params(card: RateCard, mode: str, params: dict[str, Any]) -> None:
    caps = card.capabilities_json or {}
    allowed_modes = caps.get("guidance_modes") or []
    if mode not in allowed_modes:
        raise HTTPException(
            status_code=400,
            detail=f"model {card.model_key} does not support guidance mode {mode!r} (allowed: {allowed_modes})",
        )
    res = params.get("resolution")
    if res and caps.get("resolutions") and res not in caps["resolutions"]:
        raise HTTPException(
            status_code=400,
            detail=f"model {card.model_key} does not support resolution {res!r} (allowed: {caps['resolutions']})",
        )
    ratio = params.get("aspect_ratio")
    if ratio and caps.get("aspect_ratios") and ratio not in caps["aspect_ratios"]:
        raise HTTPException(
            status_code=400,
            detail=f"model {card.model_key} does not support aspect_ratio {ratio!r}",
        )
    dur = params.get("duration_seconds")
    if dur is not None:
        min_s = int(caps.get("min_duration_seconds", 4))
        max_s = int(caps.get("max_duration_seconds", 30))
        if int(dur) < min_s or int(dur) > max_s:
            raise HTTPException(status_code=400, detail=f"duration_seconds must be {min_s}..{max_s}")

    if mode == "first_frame" and not params.get("first_frame_object_id"):
        raise HTTPException(status_code=400, detail="first_frame mode requires first_frame_object_id")
    if mode == "last_frame" and not params.get("last_frame_object_id"):
        raise HTTPException(status_code=400, detail="last_frame mode requires last_frame_object_id")
    if mode == "first_last_frame" and (
        not params.get("first_frame_object_id") or not params.get("last_frame_object_id")
    ):
        raise HTTPException(status_code=400, detail="first_last_frame mode requires first_frame_object_id and last_frame_object_id")
    if mode == "draft_extend" and not params.get("draft_job_id"):
        raise HTTPException(status_code=400, detail="draft_extend mode requires draft_job_id")
    if mode == "multimodal_ref":
        imgs = params.get("ref_image_ids") or []
        vids = params.get("ref_video_ids") or []
        auds = params.get("ref_audio_ids") or []
        if len(imgs) > int(caps.get("max_ref_images", 9)):
            raise HTTPException(status_code=400, detail=f"too many ref images (max {caps.get('max_ref_images', 9)})")
        if len(vids) > int(caps.get("max_ref_videos", 0)):
            raise HTTPException(status_code=400, detail=f"too many ref videos (max {caps.get('max_ref_videos', 0)})")
        if len(auds) > int(caps.get("max_ref_audios", 0)):
            raise HTTPException(status_code=400, detail=f"too many ref audios (max {caps.get('max_ref_audios', 0)})")


def validate_audio_params(card: RateCard, mode: str, params: dict[str, Any]) -> None:
    if mode not in ALLOWED_AUDIO_MODES:
        raise HTTPException(status_code=400, detail=f"unsupported audio mode: {mode}")
    caps = card.capabilities_json or {}
    if caps.get("modes") and mode not in caps["modes"]:
        raise HTTPException(status_code=400, detail=f"model {card.model_key} does not support mode {mode!r}")
    if mode == "tts":
        emotion = str(params.get("emotion") or "auto").lower()
        if emotion not in ALLOWED_AUDIO_EMOTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported emotion: {emotion}")
        fmt = str(params.get("output_format") or "mp3").lower()
        if fmt not in ALLOWED_AUDIO_FORMATS:
            raise HTTPException(status_code=400, detail=f"unsupported audio output_format: {fmt}")
        effect = params.get("voice_effect")
        if effect and effect not in ALLOWED_AUDIO_EFFECTS:
            raise HTTPException(status_code=400, detail=f"unsupported voice_effect: {effect}")
    if mode == "voice_clone" and not params.get("source_audio_object_id"):
        raise HTTPException(status_code=400, detail="voice_clone requires source_audio_object_id")
    if mode == "asr" and not params.get("source_audio_object_id"):
        raise HTTPException(status_code=400, detail="asr requires source_audio_object_id")


def validate_music_params(mode: str, operation: str, params: dict[str, Any]) -> None:
    if mode not in ALLOWED_MUSIC_MODES:
        raise HTTPException(status_code=400, detail=f"unsupported music mode: {mode}")
    if operation not in SUNO_OPERATIONS_PRICING:
        raise HTTPException(status_code=400, detail=f"unsupported suno operation: {operation}")
    version = str(params.get("version") or "suno-v6").lower()
    if version not in ALLOWED_MUSIC_VERSIONS:
        raise HTTPException(status_code=400, detail=f"unsupported suno version: {version}")
    fmt = str(params.get("output_format") or "mp3").lower()
    if fmt not in ALLOWED_MUSIC_FORMATS:
        raise HTTPException(status_code=400, detail=f"unsupported music format: {fmt}")


@dataclass
class ProviderInvocationResult:
    mock: bool
    artifacts: list[dict[str, Any]]  # [{"filename", "content_type", "bytes", "meta"}]
    metadata: dict[str, Any]
    pending_external_task_id: Optional[str] = None


def resolve_byok_route(
    db: Session,
    user_id: Optional[str],
    canonical_model_key: str,
) -> dict[str, Any]:
    """Resolve user's enabled BYOK offering + encrypted channel and enforce per-channel RPM limit."""
    if not user_id:
        raise HTTPException(status_code=401, detail="authenticated user required for BYOK generation")
    ent = db.get(ByokEntitlement, user_id)
    if ent is None or not ent.enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "BYOK_ENTITLEMENT_NOT_CONFIGURED", "message": "BYOK is not enabled for this account"},
        )
    row = db.execute(
        select(ByokOffering, ByokChannel)
        .join(ByokChannel, ByokChannel.id == ByokOffering.channel_id)
        .where(
            ByokOffering.user_id == user_id,
            ByokOffering.canonical_model_key == canonical_model_key,
            ByokOffering.enabled.is_(True),
            ByokChannel.enabled.is_(True),
        )
        .order_by(ByokOffering.updated_at.desc())
        .limit(1)
    ).first()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=f"no enabled BYOK offering configured for model: {canonical_model_key}",
        )
    offering, channel = row
    rate_limiter.check("byok_channel", channel.id, int(channel.rpm_limit), 60)
    return {
        "channel_id": channel.id,
        "offering_id": offering.id,
        "base_url": channel.base_url,
        "api_key": byok.decrypt_api_key(channel.api_key_encrypted),
        "upstream_model_id": offering.upstream_model_id,
        "protocol_profile": channel.protocol_profile,
    }


def invoke_provider(
    *,
    card: RateCard,
    mode: str,
    prompt: str,
    params: dict[str, Any],
    byok_route: Optional[dict[str, Any]] = None,
) -> ProviderInvocationResult:
    """Invoke BYOK channel or configured vendor adapter, else return valid mock artifacts."""
    settings = get_settings()
    adapter = get_adapter_for_profile(
        byok_route["protocol_profile"] if byok_route else card.protocol_profile
    )
    callback_url = f"{settings.app_base_url}/api/webhooks/providers/{adapter.provider_family}"

    if byok_route is not None:
        res: AdapterCallResult = adapter.invoke(
            base_url=byok_route["base_url"],
            api_key=byok_route["api_key"],
            model_id=byok_route["upstream_model_id"],
            mode=mode,
            prompt=prompt,
            params=params,
            callback_url=callback_url,
        )
        return ProviderInvocationResult(
            mock=res.mock,
            artifacts=res.artifacts,
            metadata={**res.metadata, "byok_channel_id": byok_route["channel_id"]},
            pending_external_task_id=res.pending_external_task_id,
        )

    env_creds = resolve_env_credentials(card.protocol_profile)
    if env_creds is not None:
        base_url, api_key = env_creds
        res = adapter.invoke(
            base_url=base_url,
            api_key=api_key,
            model_id=card.model_key,
            mode=mode,
            prompt=prompt,
            params=params,
            callback_url=callback_url,
        )
        return ProviderInvocationResult(
            mock=res.mock,
            artifacts=res.artifacts,
            metadata=res.metadata,
            pending_external_task_id=res.pending_external_task_id,
        )

    # Safe Mock fallback: generate valid minimal binary artifacts per modality
    if card.modality == "image":
        count = 1 if (card.capabilities_json or {}).get("is_midjourney") else max(1, int(params.get("count", 1)))
        fmt = str(params.get("output_format") or "png").lower()
        mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
        artifacts = [
            {
                "filename": f"gen_{idx + 1}.{fmt}",
                "content_type": mime,
                "bytes": _MOCK_PNG_BYTES,
                "meta": {
                    "width": 1024,
                    "height": 1024,
                    "grid_count": 4 if (card.capabilities_json or {}).get("is_midjourney") and mode == "imagine" else 1,
                },
            }
            for idx in range(count)
        ]
        return ProviderInvocationResult(mock=True, artifacts=artifacts, metadata={"prompt": prompt, "mode": mode})

    if card.modality == "video":
        dur = int(params.get("duration_seconds", 5))
        return ProviderInvocationResult(
            mock=True,
            artifacts=[{
                "filename": "gen_video.mp4",
                "content_type": "video/mp4",
                "bytes": _MOCK_MP4_BYTES,
                "meta": {"duration_seconds": dur, "resolution": params.get("resolution", "720P")},
            }],
            metadata={"mode": mode, "duration_seconds": dur},
        )

    if card.modality in ("tts", "asr"):
        if mode == "asr":
            return ProviderInvocationResult(
                mock=True,
                artifacts=[],
                metadata={"transcript": f"【mock 转写】{prompt or '音频内容转写文本'}", "duration_seconds": int(params.get("duration_seconds", 10))},
            )
        fmt = str(params.get("output_format") or "mp3").lower()
        raw = _MOCK_WAV_BYTES if fmt == "wav" else _MOCK_MP3_BYTES
        mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
        meta: dict[str, Any] = {"voice_id": params.get("voice_id") or "default_voice"}
        if params.get("generate_subtitles"):
            meta["subtitles"] = [{"start": 0.0, "end": 2.0, "text": prompt[:80]}]
        return ProviderInvocationResult(
            mock=True,
            artifacts=[{"filename": f"gen_audio.{fmt}", "content_type": mime, "bytes": raw, "meta": meta}],
            metadata=meta,
        )

    # Suno Music
    op = str(params.get("operation") or mode).lower()
    if op in ("lyrics_gen", "expand_style", "save_persona"):
        return ProviderInvocationResult(
            mock=True,
            artifacts=[],
            metadata={"operation": op, "text": f"【mock Suno {op}】{prompt}"},
        )
    fmt = str(params.get("output_format") or "mp3").lower()
    raw = _MOCK_WAV_BYTES if fmt == "wav" else _MOCK_MP3_BYTES
    mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
    return ProviderInvocationResult(
        mock=True,
        artifacts=[{
            "filename": f"suno_{op}.{fmt}",
            "content_type": mime,
            "bytes": raw,
            "meta": {"version": params.get("version", "suno-v6"), "operation": op},
        }],
        metadata={"operation": op, "version": params.get("version", "suno-v6")},
    )


def _persist_generated_artifact(
    db: Session,
    *,
    project: Project,
    owner_user_id: Optional[str],
    filename: str,
    content_type: str,
    data: bytes,
    duration_seconds: Optional[float] = None,
) -> dict[str, Any]:
    object_key = storage.build_object_key(
        "workspace",
        filename,
        owner_user_id=owner_user_id,
        project_id=project.id,
        workspace_id=project.workspace_id,
    )
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        client = storage._s3_client()
        client.put_object(Bucket=settings.s3_bucket, Key=object_key, Body=data, ContentType=content_type)
    else:
        path = storage._local_path_for(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    obj = StorageObject(
        id=new_prefixed_id("sto"),
        space="workspace",
        owner_user_id=project.owner_id or owner_user_id,
        project_id=project.id,
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        duration_seconds=duration_seconds,
        status="uploaded",
        created_at=utcnow(),
    )
    db.add(obj)
    db.flush()
    return {
        "storage_id": obj.id,
        "object_key": obj.object_key,
        "filename": obj.filename,
        "content_type": obj.content_type,
        "size_bytes": obj.size_bytes,
        "url": storage.build_object_url(obj),
    }


def serialize_job(job: GenerationJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_id": job.id,
        "request_id": job.request_id,
        "user_id": job.user_id,
        "project_id": job.project_id,
        "canvas_id": job.canvas_id,
        "target_object_id": job.target_object_id,
        "parent_job_id": job.parent_job_id,
        "modality": job.modality,
        "model_key": job.model_key,
        "protocol_profile": job.protocol_profile,
        "mode": job.mode,
        "prompt": job.prompt,
        "params": job.params_json,
        "input_object_ids": job.input_object_ids_json,
        "status": job.status,
        "progress": int(job.progress),
        "reservation_id": job.reservation_id,
        "external_task_id": job.external_task_id,
        "byok_channel_id": job.byok_channel_id,
        "payment_source": job.payment_source,
        "mock": bool(job.mock),
        "output": job.output_json,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def finalize_generation_job(
    db: Session,
    job: GenerationJob,
    *,
    succeeded: bool,
    artifacts: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
    mock: bool = False,
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    """Idempotently finalize a GenerationJob (shared by inline, ARQ worker & provider Webhooks)."""
    project = db.get(Project, job.project_id)
    canvas = db.get(Canvas, job.canvas_id)
    card = get_rate_card(db, job.model_key)
    params = dict(job.params_json or {})
    estimate = billing.estimate_generation_cost(
        db,
        card.model_key,
        {**params, "mode": job.mode, "prompt": job.prompt},
        user_id=job.user_id,
    )
    est_credits = Decimal(str(estimate["estimated_credits"]))
    surcharge_credits = Decimal(str(estimate["surcharge_credits"]))
    now = utcnow()

    if not succeeded:
        job.status = "failed"
        job.error_message = error_message or "generation failed"
        job.completed_at = now
        job.updated_at = now
        db.add(job)
        if job.reservation_id:
            billing.release_reservation(
                db,
                job.reservation_id,
                record_refund_usage=True,
                project_id=job.project_id,
                job_id=job.id,
                modality=card.modality,
                model_key=card.model_key,
                status_label="refunded",
            )
        db.commit()
        if project and canvas:
            event_bus.publish(
                project.id,
                canvas.id,
                "generation.failed",
                {"job_id": job.id, "error": job.error_message},
                actor_id=job.user_id,
            )
        return {**serialize_job(job), "estimate": estimate}

    stored_artifacts: list[dict[str, Any]] = []
    for art in (artifacts or []):
        saved = _persist_generated_artifact(
            db,
            project=project,
            owner_user_id=job.user_id,
            filename=art["filename"],
            content_type=art["content_type"],
            data=art["bytes"],
            duration_seconds=(art.get("meta") or {}).get("duration_seconds"),
        )
        saved["meta"] = art.get("meta") or {}
        stored_artifacts.append(saved)

    target_obj: Optional[CanvasObject] = (
        db.get(CanvasObject, job.target_object_id) if job.target_object_id else None
    )
    node_type = {"image": "image", "video": "video", "audio": "audio", "music": "music"}.get(job.modality, "file")
    if target_obj is None and project and canvas:
        target_obj = CanvasObject(
            id=new_prefixed_id("obj"),
            project_id=project.id,
            canvas_id=canvas.id,
            type=node_type,
            name=f"{card.display_name} 产物"[:128],
            x=80.0,
            y=80.0,
            w=360.0,
            h=240.0,
            text=job.prompt[:500],
            media_type=node_type,
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(target_obj)
        db.flush()
        job.target_object_id = target_obj.id

    if target_obj is not None:
        primary = stored_artifacts[0] if stored_artifacts else None
        target_obj.asset_json = {
            "primary": primary,
            "artifacts": stored_artifacts,
            "job_id": job.id,
            "model_key": card.model_key,
            "mock": mock,
        }
        target_obj.content_json = {
            **(target_obj.content_json or {}),
            "prompt": job.prompt,
            "mode": job.mode,
            "metadata": metadata or {},
        }
        target_obj.version = int(target_obj.version) + 1
        target_obj.updated_at = now
        db.add(target_obj)

    job.status = "succeeded"
    job.progress = 100
    job.mock = mock
    job.output_json = {
        "artifacts": stored_artifacts,
        "metadata": metadata or {},
        "target_object_id": target_obj.id if target_obj else None,
    }
    job.completed_at = now
    job.updated_at = now
    db.add(job)

    if job.reservation_id:
        if mock:
            billing.release_reservation(
                db,
                job.reservation_id,
                record_refund_usage=True,
                project_id=job.project_id,
                job_id=job.id,
                modality=card.modality,
                model_key=card.model_key,
                status_label="mock_free",
            )
        else:
            surcharge_info = None
            voice_id = str(params.get("voice_id") or "").strip()
            if card.modality == "tts" and job.mode == "tts" and voice_id and surcharge_credits > Decimal("0") and job.user_id:
                surcharge_info = {
                    "charge_type": "voice_first_use",
                    "description": f"首次使用音色附加费 ({voice_id})",
                    "credits": surcharge_credits,
                    "meta": {"voice_id": voice_id, "job_id": job.id},
                }
                db.add(UserVoiceActivation(
                    id=new_prefixed_id("vac"),
                    user_id=job.user_id,
                    voice_id=voice_id,
                    credits_charged=surcharge_credits,
                    activated_at=now,
                ))
            billing.settle_reservation(
                db,
                job.reservation_id,
                actual_credits=est_credits,
                project_id=job.project_id,
                job_id=job.id,
                modality=card.modality,
                model_key=card.model_key,
                payment_source=job.payment_source,
                quantity={"mode": job.mode, "artifacts": len(stored_artifacts)},
                status_label="settled",
                surcharge_charge=surcharge_info,
            )
    elif job.payment_source == "byok" and job.user_id:
        # Record 0-credit platform usage record tagged as BYOK
        db.add(BillingUsageRecord(
            id=new_prefixed_id("usg"),
            user_id=job.user_id,
            project_id=job.project_id,
            job_id=job.id,
            modality=card.modality,
            model_key=card.model_key,
            payment_source="byok",
            quantity_json={"mode": job.mode, "artifacts": len(stored_artifacts), "byok_channel_id": job.byok_channel_id},
            credits_charged=Decimal("0.0000"),
            bonus_charged=Decimal("0.0000"),
            cash_charged=Decimal("0.0000"),
            status="settled",
            created_at=now,
        ))

    db.commit()
    if project and canvas:
        event_bus.publish(
            project.id,
            canvas.id,
            "generation.completed",
            {
                "job_id": job.id,
                "target_object_id": target_obj.id if target_obj else None,
                "modality": job.modality,
                "mock": mock,
                "artifacts": stored_artifacts,
            },
            actor_id=job.user_id,
        )
    return {**serialize_job(job), "estimate": estimate}


def execute_existing_generation_job(
    db: Session,
    job: GenerationJob,
    *,
    byok_route: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Execute a queued GenerationJob (inline or via ARQ background worker)."""
    project = db.get(Project, job.project_id)
    canvas = db.get(Canvas, job.canvas_id)
    if project is None or canvas is None:
        job.status = "failed"
        job.error_message = "project or canvas no longer exists"
        job.completed_at = utcnow()
        db.add(job)
        db.commit()
        return serialize_job(job)

    card = get_rate_card(db, job.model_key)
    params = dict(job.params_json or {})
    if byok_route is None and job.payment_source == "byok":
        byok_route = resolve_byok_route(db, job.user_id, card.model_key)

    job.status = "running"
    job.progress = 10
    job.updated_at = utcnow()
    db.add(job)
    db.commit()

    event_bus.publish(
        project.id,
        canvas.id,
        "generation.progress",
        {"job_id": job.id, "status": "running", "progress": 10, "modality": job.modality},
        actor_id=job.user_id,
    )

    try:
        provider_res = (
            invoke_provider(card=card, mode=job.mode, prompt=job.prompt, params=params, byok_route=byok_route)
            if byok_route is not None
            else invoke_provider(card=card, mode=job.mode, prompt=job.prompt, params=params)
        )
        if provider_res.pending_external_task_id:
            job.external_task_id = provider_res.pending_external_task_id
            job.status = "running"
            job.progress = 30
            job.mock = False
            job.output_json = {"metadata": provider_res.metadata, "awaiting_callback": True}
            job.updated_at = utcnow()
            db.add(job)
            db.commit()
            event_bus.publish(
                project.id,
                canvas.id,
                "generation.progress",
                {"job_id": job.id, "status": "running", "progress": 30, "external_task_id": job.external_task_id},
                actor_id=job.user_id,
            )
            return serialize_job(job)

        return finalize_generation_job(
            db,
            job,
            succeeded=True,
            artifacts=provider_res.artifacts,
            metadata=provider_res.metadata,
            mock=provider_res.mock,
        )
    except Exception as exc:  # noqa: BLE001
        return finalize_generation_job(db, job, succeeded=False, error_message=str(exc))


def run_generation_pipeline(
    db: Session,
    *,
    project: Project,
    canvas: Canvas,
    user_id: Optional[str],
    modality: str,
    model_key: str,
    mode: str,
    prompt: str,
    params: dict[str, Any],
    target_object_id: Optional[str] = None,
    input_object_ids: Optional[list[str]] = None,
    parent_job_id: Optional[str] = None,
    payment_source: str = "platform_credits",
) -> dict[str, Any]:
    """Validate -> estimate -> reserve credits -> dispatch to ARQ worker (if enabled) or run inline."""
    card = get_rate_card(db, model_key, expected_modality=modality)
    if modality == "image":
        validate_image_params(card, params)
    elif modality == "video":
        validate_video_params(card, mode, params)
    elif modality == "audio":
        validate_audio_params(card, mode, params)
    elif modality == "music":
        op = str(params.get("operation") or mode).lower()
        validate_music_params(mode, op, params)

    target_obj: Optional[CanvasObject] = None
    if target_object_id:
        target_obj = db.get(CanvasObject, target_object_id)
        if target_obj is None or target_obj.canvas_id != canvas.id or target_obj.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"target object not found: {target_object_id}")

    byok_route: Optional[dict[str, Any]] = None
    if payment_source == "byok":
        byok_route = resolve_byok_route(db, user_id, card.model_key)

    estimate = billing.estimate_generation_cost(
        db,
        card.model_key,
        {**params, "mode": mode, "prompt": prompt},
        user_id=user_id,
    )
    est_credits = Decimal(str(estimate["estimated_credits"]))

    job = GenerationJob(
        id=new_prefixed_id("gen"),
        request_id=uuid.uuid4().hex,
        user_id=user_id,
        project_id=project.id,
        canvas_id=canvas.id,
        target_object_id=target_obj.id if target_obj else None,
        parent_job_id=parent_job_id,
        byok_channel_id=byok_route["channel_id"] if byok_route else None,
        modality=modality,
        model_key=card.model_key,
        protocol_profile=card.protocol_profile,
        mode=mode,
        prompt=prompt,
        params_json=dict(params),
        input_object_ids_json=list(input_object_ids or []),
        status="queued",
        progress=0,
        payment_source=payment_source,
        mock=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(job)
    db.flush()

    if user_id and payment_source == "platform_credits" and est_credits > Decimal("0"):
        res = billing.reserve_credits(
            db,
            user_id=user_id,
            estimated_credits=est_credits,
            source_type="generation",
            source_id=job.id,
        )
        job.reservation_id = res.id
    db.commit()

    # In 4C8G production with ARQ worker enabled, enqueue & return immediately
    from app import worker
    if worker.enqueue_generation_job(job.id):
        event_bus.publish(
            project.id,
            canvas.id,
            "generation.queued",
            {"job_id": job.id, "modality": modality, "model_key": card.model_key},
            actor_id=user_id,
        )
        return {**serialize_job(job), "estimate": estimate, "dispatched_async": True}

    return execute_existing_generation_job(db, job, byok_route=byok_route)
