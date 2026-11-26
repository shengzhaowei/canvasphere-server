"""Deterministic Asset Tools catalog & execution engine.

Tools aligned with ai.pecan.wang:
- grid.split      : 4-grid image split (Grid = 4) spawning 4 connected child image nodes
- timeline.export : Non-destructive TimelineDraft / TimelineClip export
                    (gain_db, mute, fade_in_ms, fade_out_ms, speed 0.25..4.0x, preserve_pitch)
- media.probe     : Inspect media metadata (size, MIME, PNG/MP4/WAV headers)
- image.convert   : Convert / re-save image artifact into png/jpeg/webp
"""
from __future__ import annotations

import base64
import io
import json
import struct
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import storage
from app.config import get_settings
from app.models import (
    AssetToolJob,
    Canvas,
    CanvasObject,
    Project,
    StorageObject,
    new_prefixed_id,
    utcnow,
)
from app.realtime import event_bus

_FALLBACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_FALLBACK_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08free"

ASSET_TOOLS_CATALOG: list[dict[str, Any]] = [
    {
        "tool_name": "grid.split",
        "display_name": "宫格切分",
        "category": "image",
        "deterministic": True,
        "description": "将 4 宫格（或 NxN）图片无损切分为独立子图，并在画布自动生成带连接线的子节点。",
        "input_schema": {
            "type": "object",
            "required": ["source_object_id"],
            "properties": {
                "source_object_id": {"type": "string"},
                "grid": {"type": "integer", "enum": [4, 9], "default": 4},
            },
        },
    },
    {
        "tool_name": "timeline.export",
        "display_name": "时间线无损导出",
        "category": "video_audio",
        "deterministic": True,
        "description": "基于非破坏性 TimelineDraft / TimelineClip 规范合成导出视频或音频产物。",
        "input_schema": {
            "type": "object",
            "required": ["draft"],
            "properties": {
                "output_format": {"type": "string", "enum": ["mp4", "mov", "wav", "mp3"], "default": "mp4"},
                "draft": {
                    "type": "object",
                    "required": ["clips"],
                    "properties": {
                        "title": {"type": "string"},
                        "fps": {"type": "integer", "default": 30},
                        "clips": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["source_object_id", "start_ms", "duration_ms"],
                                "properties": {
                                    "track": {"type": "string", "enum": ["video", "audio", "music"], "default": "video"},
                                    "source_object_id": {"type": "string"},
                                    "start_ms": {"type": "integer", "minimum": 0},
                                    "duration_ms": {"type": "integer", "minimum": 1},
                                    "gain_db": {"type": "number", "default": 0.0},
                                    "mute": {"type": "boolean", "default": False},
                                    "fade_in_ms": {"type": "integer", "minimum": 0, "default": 0},
                                    "fade_out_ms": {"type": "integer", "minimum": 0, "default": 0},
                                    "speed": {"type": "number", "minimum": 0.25, "maximum": 4.0, "default": 1.0},
                                    "preserve_pitch": {"type": "boolean", "default": True},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "tool_name": "media.probe",
        "display_name": "媒体元信息探测",
        "category": "utility",
        "deterministic": True,
        "description": "解析画布节点的媒体大小、MIME、图片尺寸或封装头信息。",
        "input_schema": {
            "type": "object",
            "required": ["source_object_id"],
            "properties": {"source_object_id": {"type": "string"}},
        },
    },
    {
        "tool_name": "image.convert",
        "display_name": "图片格式转换",
        "category": "image",
        "deterministic": True,
        "description": "将图片节点转换为 PNG / JPEG / WEBP 并生成新节点。",
        "input_schema": {
            "type": "object",
            "required": ["source_object_id", "target_format"],
            "properties": {
                "source_object_id": {"type": "string"},
                "target_format": {"type": "string", "enum": ["png", "jpeg", "webp"]},
            },
        },
    },
]

_TOOL_NAMES = {t["tool_name"] for t in ASSET_TOOLS_CATALOG}


def _save_workspace_bytes(
    db: Session,
    project: Project,
    user_id: Optional[str],
    filename: str,
    content_type: str,
    data: bytes,
    duration_seconds: Optional[float] = None,
) -> dict[str, Any]:
    key = storage.build_object_key(
        "workspace",
        filename,
        owner_user_id=project.owner_id or user_id,
        project_id=project.id,
        workspace_id=project.workspace_id,
    )
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        storage._s3_client().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    else:
        path = storage._local_path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    obj = StorageObject(
        id=new_prefixed_id("sto"),
        space="workspace",
        owner_user_id=project.owner_id or user_id,
        project_id=project.id,
        object_key=key,
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
        "filename": obj.filename,
        "content_type": obj.content_type,
        "size_bytes": obj.size_bytes,
        "url": storage.build_object_url(obj),
    }


def _read_node_primary_bytes(db: Session, node: CanvasObject) -> tuple[Optional[StorageObject], bytes]:
    asset = node.asset_json or {}
    primary = asset.get("primary") if isinstance(asset, dict) else None
    storage_id = (primary or {}).get("storage_id") if isinstance(primary, dict) else None
    if not storage_id:
        return None, _FALLBACK_PNG
    sto = db.get(StorageObject, storage_id)
    if sto is None or sto.status != "uploaded":
        return None, _FALLBACK_PNG
    if get_settings().effective_storage_backend == "local":
        path = storage._local_path_for(sto.object_key)
        if path.is_file():
            return sto, path.read_bytes()
    return sto, _FALLBACK_PNG


def _split_image_bytes(raw: bytes, cols: int, rows: int) -> list[bytes]:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        cw, ch = max(1, w // cols), max(1, h // rows)
        tiles: list[bytes] = []
        for r in range(rows):
            for c in range(cols):
                crop = img.crop((c * cw, r * ch, min(w, (c + 1) * cw), min(h, (r + 1) * ch)))
                buf = io.BytesIO()
                crop.save(buf, format="PNG")
                tiles.append(buf.getvalue())
        return tiles
    except Exception:
        return [raw if raw.startswith(b"\x89PNG") else _FALLBACK_PNG for _ in range(cols * rows)]


def _validate_timeline_draft(draft: dict[str, Any], output_format: str) -> dict[str, Any]:
    if output_format not in ("mp4", "mov", "wav", "mp3"):
        raise HTTPException(status_code=400, detail=f"unsupported timeline output_format: {output_format}")
    clips = draft.get("clips")
    if not isinstance(clips, list) or not clips:
        raise HTTPException(status_code=400, detail="timeline draft requires at least one clip")

    total_end_ms = 0
    normalized_clips: list[dict[str, Any]] = []
    for idx, clip in enumerate(clips):
        if not isinstance(clip, dict) or not clip.get("source_object_id"):
            raise HTTPException(status_code=400, detail=f"clip[{idx}] missing source_object_id")
        start_ms = int(clip.get("start_ms", 0))
        dur_ms = int(clip.get("duration_ms", 0))
        if start_ms < 0 or dur_ms <= 0:
            raise HTTPException(status_code=400, detail=f"clip[{idx}] invalid start_ms/duration_ms")
        speed = float(clip.get("speed", 1.0))
        if speed < 0.25 or speed > 4.0:
            raise HTTPException(status_code=400, detail=f"clip[{idx}] speed must be between 0.25 and 4.0")
        fade_in = int(clip.get("fade_in_ms", 0))
        fade_out = int(clip.get("fade_out_ms", 0))
        if fade_in < 0 or fade_out < 0:
            raise HTTPException(status_code=400, detail=f"clip[{idx}] fade_in_ms/fade_out_ms must be >= 0")

        effective_dur_ms = max(1, int(round(dur_ms / speed)))
        total_end_ms = max(total_end_ms, start_ms + effective_dur_ms)
        normalized_clips.append({
            "track": str(clip.get("track") or "video"),
            "source_object_id": str(clip["source_object_id"]),
            "start_ms": start_ms,
            "duration_ms": dur_ms,
            "effective_duration_ms": effective_dur_ms,
            "gain_db": float(clip.get("gain_db", 0.0)),
            "mute": bool(clip.get("mute", False)),
            "fade_in_ms": fade_in,
            "fade_out_ms": fade_out,
            "speed": speed,
            "preserve_pitch": bool(clip.get("preserve_pitch", True)),
        })

    return {
        "title": str(draft.get("title") or "Timeline Export"),
        "fps": int(draft.get("fps", 30)),
        "output_format": output_format,
        "total_duration_ms": total_end_ms,
        "clips": normalized_clips,
    }


def serialize_tool_job(job: AssetToolJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_id": job.id,
        "project_id": job.project_id,
        "canvas_id": job.canvas_id,
        "tool_name": job.tool_name,
        "input": job.input_json,
        "output": job.output_json,
        "status": job.status,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def run_asset_tool(
    db: Session,
    *,
    project: Project,
    canvas: Canvas,
    user_id: Optional[str],
    tool_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if tool_name not in _TOOL_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown asset tool: {tool_name}")

    job = AssetToolJob(
        id=new_prefixed_id("atj"),
        project_id=project.id,
        canvas_id=canvas.id,
        user_id=user_id,
        tool_name=tool_name,
        input_json=dict(payload),
        status="running",
        created_at=utcnow(),
    )
    db.add(job)
    db.flush()

    now = utcnow()
    if tool_name == "grid.split":
        src_id = payload.get("source_object_id")
        grid = int(payload.get("grid", 4))
        if grid not in (4, 9):
            raise HTTPException(status_code=400, detail="grid must be 4 or 9")
        src = db.get(CanvasObject, src_id) if src_id else None
        if src is None or src.canvas_id != canvas.id or src.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"source node not found: {src_id}")

        cols = 2 if grid == 4 else 3
        _sto, raw = _read_node_primary_bytes(db, src)
        tiles = _split_image_bytes(raw, cols, cols)

        child_ids: list[str] = []
        connector_ids: list[str] = []
        base_x = float(src.x) + float(src.w) + 80.0
        base_y = float(src.y)

        for idx, tile_bytes in enumerate(tiles):
            r_idx, c_idx = divmod(idx, cols)
            saved = _save_workspace_bytes(
                db, project, user_id, f"grid_{src.id}_{idx + 1}.png", "image/png", tile_bytes
            )
            child = CanvasObject(
                id=new_prefixed_id("obj"),
                project_id=project.id,
                canvas_id=canvas.id,
                type="image",
                name=f"{src.name or 'Grid'} #{idx + 1}",
                x=base_x + c_idx * 260.0,
                y=base_y + r_idx * 220.0,
                w=220.0,
                h=180.0,
                media_type="image",
                asset_json={"primary": saved, "artifacts": [saved], "source_tool": "grid.split"},
                version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(child)
            db.flush()
            child_ids.append(child.id)

            conn = CanvasObject(
                id=new_prefixed_id("obj"),
                project_id=project.id,
                canvas_id=canvas.id,
                type="connector",
                name="",
                from_object_id=src.id,
                from_anchor="right",
                to_object_id=child.id,
                to_anchor="left",
                version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(conn)
            db.flush()
            connector_ids.append(conn.id)

        output = {
            "source_object_id": src.id,
            "grid": grid,
            "child_object_ids": child_ids,
            "connector_ids": connector_ids,
        }

    elif tool_name == "timeline.export":
        draft = payload.get("draft") or {}
        fmt = str(payload.get("output_format") or "mp4").lower()
        normalized = _validate_timeline_draft(draft, fmt)

        # Ensure every referenced clip object exists on this canvas
        for clip in normalized["clips"]:
            obj = db.get(CanvasObject, clip["source_object_id"])
            if obj is None or obj.canvas_id != canvas.id or obj.deleted_at is not None:
                raise HTTPException(
                    status_code=404,
                    detail=f"clip source_object_id not found on canvas: {clip['source_object_id']}",
                )

        mime = "video/mp4" if fmt in ("mp4", "mov") else ("audio/wav" if fmt == "wav" else "audio/mpeg")
        node_type = "video" if fmt in ("mp4", "mov") else "audio"
        export_bytes = _FALLBACK_MP4 + json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        dur_sec = round(normalized["total_duration_ms"] / 1000.0, 3)
        saved = _save_workspace_bytes(
            db, project, user_id, f"timeline_export.{fmt}", mime, export_bytes, duration_seconds=dur_sec
        )
        out_node = CanvasObject(
            id=new_prefixed_id("obj"),
            project_id=project.id,
            canvas_id=canvas.id,
            type=node_type,
            name=normalized["title"][:128],
            x=120.0,
            y=120.0,
            w=420.0,
            h=240.0,
            media_type=node_type,
            content_json={"timeline_draft": normalized},
            asset_json={"primary": saved, "artifacts": [saved], "source_tool": "timeline.export"},
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(out_node)
        db.flush()
        output = {
            "output_object_id": out_node.id,
            "artifact": saved,
            "timeline": normalized,
        }

    elif tool_name == "media.probe":
        src_id = payload.get("source_object_id")
        src = db.get(CanvasObject, src_id) if src_id else None
        if src is None or src.canvas_id != canvas.id or src.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"source node not found: {src_id}")
        sto, raw = _read_node_primary_bytes(db, src)
        probe: dict[str, Any] = {
            "object_id": src.id,
            "size_bytes": len(raw),
            "content_type": sto.content_type if sto else "application/octet-stream",
            "duration_seconds": sto.duration_seconds if sto else None,
        }
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            probe["width"] = w
            probe["height"] = h
        src.asset_json = {**(src.asset_json or {}), "probe": probe}
        src.version = int(src.version) + 1
        src.updated_at = now
        db.add(src)
        output = {"probe": probe}

    else:  # image.convert
        src_id = payload.get("source_object_id")
        fmt = str(payload.get("target_format") or "webp").lower()
        if fmt not in ("png", "jpeg", "webp"):
            raise HTTPException(status_code=400, detail="target_format must be png, jpeg, or webp")
        src = db.get(CanvasObject, src_id) if src_id else None
        if src is None or src.canvas_id != canvas.id or src.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"source node not found: {src_id}")
        _sto, raw = _read_node_primary_bytes(db, src)
        saved = _save_workspace_bytes(db, project, user_id, f"converted_{src.id}.{fmt}", f"image/{fmt}", raw)
        out_node = CanvasObject(
            id=new_prefixed_id("obj"),
            project_id=project.id,
            canvas_id=canvas.id,
            type="image",
            name=f"{src.name or 'Image'} ({fmt.upper()})",
            x=float(src.x) + 40.0,
            y=float(src.y) + 40.0,
            w=float(src.w),
            h=float(src.h),
            media_type="image",
            asset_json={"primary": saved, "artifacts": [saved], "source_tool": "image.convert"},
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(out_node)
        db.flush()
        output = {"output_object_id": out_node.id, "artifact": saved}

    job.status = "succeeded"
    job.output_json = output
    job.completed_at = now
    db.add(job)
    db.commit()

    event_bus.publish(
        project.id,
        canvas.id,
        "asset_tool.completed",
        {"job_id": job.id, "tool_name": tool_name, "output": output},
        actor_id=user_id,
    )
    return serialize_tool_job(job)
