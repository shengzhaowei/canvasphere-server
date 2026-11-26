"""S3-compatible object storage with automatic signed local-disk fallback.

Storage spaces mirror Pecan Studio's partitioning:
- avatars/{user_id}/...            (user avatar, <= 2 MiB, image only)
- drafts/{user_id}/...             (user drafts)
- workspace/{workspace_id}/...     (project public workspace, billed to owner)
- private/{user_id}/{project_id}/  (private runtime e.g. context compression)
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project, StorageObject, new_prefixed_id, utcnow

ALLOWED_AVATAR_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
ALLOWED_COVER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
ALLOWED_COVER_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime"})

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(filename: str) -> str:
    base = Path(filename or "file").name
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._")
    return (cleaned or "file")[:120]


def _local_path_for(object_key: str) -> Path:
    root = get_settings().local_storage_dir.resolve()
    target = (root / object_key).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=400, detail="invalid storage path")
    return target


def _sign_local(action: str, storage_id: str, expires: int) -> str:
    secret = get_settings().storage_signing_secret.encode("utf-8")
    msg = f"{action}:{storage_id}:{expires}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_local_signature(action: str, storage_id: str, expires: int, sig: str) -> None:
    if int(time.time()) > int(expires):
        raise HTTPException(status_code=403, detail="storage signature expired")
    expected = _sign_local(action, storage_id, int(expires))
    if not hmac.compare_digest(expected, sig or ""):
        raise HTTPException(status_code=403, detail="invalid storage signature")


def _s3_client():
    import boto3
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "aws_access_key_id": settings.s3_access_key_id,
        "aws_secret_access_key": settings.s3_secret_access_key,
        "region_name": settings.s3_region or "auto",
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return boto3.client("s3", **kwargs)


def build_object_url(obj: StorageObject, ttl_seconds: int = 86400) -> str:
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        if settings.s3_public_base_url:
            return f"{settings.s3_public_base_url}/{obj.object_key}"
        client = _s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": obj.object_key},
            ExpiresIn=ttl_seconds,
        )
    expires = int(time.time()) + ttl_seconds
    sig = _sign_local("get", obj.id, expires)
    return f"{settings.app_base_url}/api/storage/local/object/{obj.id}?expires={expires}&sig={sig}"


def build_object_key(
    space: str,
    filename: str,
    owner_user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    safe = _sanitize_filename(filename)
    token = uuid.uuid4().hex[:12]
    if space == "avatars":
        return f"avatars/{owner_user_id or 'anon'}/{token}_{safe}"
    if space == "drafts":
        return f"drafts/{owner_user_id or 'anon'}/{token}_{safe}"
    if space == "workspace":
        wsp = workspace_id or project_id or "default"
        return f"workspace/{wsp}/{token}_{safe}"
    if space == "private_runtime":
        return f"private/{owner_user_id or 'anon'}/{project_id or 'none'}/{token}_{safe}"
    raise HTTPException(status_code=400, detail=f"unsupported storage space: {space}")


def get_user_used_bytes(db: Session, user_id: str) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
        .where(
            StorageObject.owner_user_id == user_id,
            StorageObject.status == "uploaded",
        )
    )
    return int(total or 0)


def check_user_quota(db: Session, user_id: Optional[str], incoming_bytes: int) -> None:
    if not user_id:
        return
    quota = get_settings().user_storage_quota_bytes
    used = get_user_used_bytes(db, user_id)
    if used + int(incoming_bytes) > quota:
        raise HTTPException(
            status_code=413,
            detail=f"storage quota exceeded (used={used}, incoming={incoming_bytes}, quota={quota})",
        )


def create_presigned_upload(
    db: Session,
    *,
    space: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    owner_user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    expires_in: int = 900,
) -> dict[str, Any]:
    if size_bytes <= 0:
        raise HTTPException(status_code=400, detail="size_bytes must be positive")
    check_user_quota(db, owner_user_id, size_bytes)

    object_key = build_object_key(
        space,
        filename,
        owner_user_id=owner_user_id,
        project_id=project_id,
        workspace_id=workspace_id,
    )
    obj = StorageObject(
        id=new_prefixed_id("sto"),
        space=space,
        owner_user_id=owner_user_id,
        project_id=project_id,
        object_key=object_key,
        filename=_sanitize_filename(filename),
        content_type=content_type,
        size_bytes=int(size_bytes),
        duration_seconds=duration_seconds,
        status="pending",
        created_at=utcnow(),
    )
    db.add(obj)
    db.commit()

    settings = get_settings()
    backend = settings.effective_storage_backend
    if backend == "s3":
        client = _s3_client()
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": obj.object_key,
                "ContentType": obj.content_type,
            },
            ExpiresIn=expires_in,
        )
    else:
        expires = int(time.time()) + expires_in
        sig = _sign_local("put", obj.id, expires)
        upload_url = f"{settings.app_base_url}/api/storage/local/upload/{obj.id}?expires={expires}&sig={sig}"

    return {
        "storage_id": obj.id,
        "object_key": obj.object_key,
        "backend": backend,
        "upload_url": upload_url,
        "upload_method": "PUT",
        "headers": {"Content-Type": obj.content_type},
        "expires_in": expires_in,
    }


def write_local_object(db: Session, storage_id: str, data: bytes, expires: int, sig: str) -> StorageObject:
    verify_local_signature("put", storage_id, expires, sig)
    obj = db.get(StorageObject, storage_id)
    if obj is None or obj.status == "deleted":
        raise HTTPException(status_code=404, detail="storage object not found")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="empty upload payload")
    settings = get_settings()
    if obj.space == "avatars" and len(data) > settings.avatar_max_bytes:
        raise HTTPException(status_code=413, detail="avatar exceeds 2 MiB limit")
    if len(data) > max(obj.size_bytes, settings.cover_max_bytes):
        raise HTTPException(status_code=413, detail="uploaded payload exceeds declared limit")
    check_user_quota(db, obj.owner_user_id, len(data))

    path = _local_path_for(obj.object_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    obj.size_bytes = len(data)
    obj.status = "uploaded"
    db.add(obj)
    db.commit()
    return obj


def read_local_object(db: Session, storage_id: str, expires: int, sig: str) -> tuple[StorageObject, bytes]:
    verify_local_signature("get", storage_id, expires, sig)
    obj = db.get(StorageObject, storage_id)
    if obj is None or obj.status != "uploaded":
        raise HTTPException(status_code=404, detail="storage object not found or invalidated")
    path = _local_path_for(obj.object_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="storage file missing on disk")
    return obj, path.read_bytes()


def confirm_upload(
    db: Session,
    storage_id: str,
    *,
    expected_space: Optional[str] = None,
    expected_user_id: Optional[str] = None,
    expected_project_id: Optional[str] = None,
) -> tuple[StorageObject, str]:
    obj = db.get(StorageObject, storage_id)
    if obj is None or obj.status == "deleted":
        raise HTTPException(status_code=404, detail="storage object not found")
    if expected_space and obj.space != expected_space:
        raise HTTPException(status_code=400, detail="storage space mismatch")
    if expected_user_id and obj.owner_user_id != expected_user_id:
        raise HTTPException(status_code=403, detail="storage object belongs to another user")
    if expected_project_id and obj.project_id != expected_project_id:
        raise HTTPException(status_code=403, detail="storage object belongs to another project")

    settings = get_settings()
    if settings.effective_storage_backend == "local":
        path = _local_path_for(obj.object_key)
        if not path.is_file():
            raise HTTPException(status_code=400, detail="file has not been uploaded yet")
        obj.size_bytes = path.stat().st_size
    obj.status = "uploaded"
    db.add(obj)
    db.commit()
    return obj, build_object_url(obj)


def invalidate_object(db: Session, storage_id: Optional[str]) -> None:
    """Immediately mark a storage object deleted and remove underlying bytes."""
    if not storage_id:
        return
    obj = db.get(StorageObject, storage_id)
    if obj is None or obj.status == "deleted":
        return
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        try:
            _s3_client().delete_object(Bucket=settings.s3_bucket, Key=obj.object_key)
        except Exception:
            pass
    else:
        try:
            path = _local_path_for(obj.object_key)
            if path.is_file():
                path.unlink()
        except Exception:
            pass
    obj.status = "deleted"
    obj.deleted_at = utcnow()
    db.add(obj)
    db.flush()


def get_user_storage_summary(db: Session, user_id: str) -> dict[str, Any]:
    quota = get_settings().user_storage_quota_bytes
    rows = db.execute(
        select(StorageObject.space, func.coalesce(func.sum(StorageObject.size_bytes), 0))
        .where(
            StorageObject.owner_user_id == user_id,
            StorageObject.status == "uploaded",
        )
        .group_by(StorageObject.space)
    ).all()
    by_space = {space: int(total) for space, total in rows}
    used = sum(by_space.values())
    return {
        "user_id": user_id,
        "quota_bytes": quota,
        "used_bytes": used,
        "available_bytes": max(0, quota - used),
        "by_space": by_space,
        "backend": get_settings().effective_storage_backend,
    }


def get_project_storage_summary(db: Session, project: Project, user_id: Optional[str]) -> dict[str, Any]:
    workspace_used = int(
        db.scalar(
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
            .where(
                StorageObject.project_id == project.id,
                StorageObject.space == "workspace",
                StorageObject.status == "uploaded",
            )
        )
        or 0
    )
    my_private_used = 0
    if user_id:
        my_private_used = int(
            db.scalar(
                select(func.coalesce(func.sum(StorageObject.size_bytes), 0))
                .where(
                    StorageObject.project_id == project.id,
                    StorageObject.space == "private_runtime",
                    StorageObject.owner_user_id == user_id,
                    StorageObject.status == "uploaded",
                )
            )
            or 0
        )
    return {
        "project_id": project.id,
        "workspace_id": project.workspace_id,
        "workspace_prefix": f"workspace/{project.workspace_id}",
        "workspace_used_bytes": workspace_used,
        "workspace_billed_to": project.owner_id,
        "my_private_used_bytes": my_private_used,
        "backend": get_settings().effective_storage_backend,
    }
