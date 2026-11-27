"""User skill library (.zip upload, 9 categories) & project-installed skills routes."""
from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import auth, skills
from app.config import get_settings
from app.db import get_db
from app.models import ProjectInstalledSkill, User, UserSkill, new_prefixed_id, utcnow
from app.permissions import can_write
from app.skills_seed import ensure_builtin_skills_seeded

router = APIRouter(tags=["skills_mgmt"])


class ProjectSkillInstallRequest(BaseModel):
    skill_id: Optional[str] = None
    skill_name: Optional[str] = None


def _serialize_user_skill(s: UserSkill) -> dict[str, Any]:
    return {
        "id": s.id,
        "skill_id": s.id,
        "owner_user_id": s.owner_user_id,
        "name": s.name,
        "display_name": s.display_name or s.name,
        "description": s.description,
        "category": s.category,
        "files": list(s.files_json or []),
        "is_builtin": bool(s.is_builtin),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _safe_parse_skill_zip(raw_bytes: bytes, fallback_name: Optional[str], category_override: Optional[str]) -> dict[str, Any]:
    max_bytes = get_settings().skill_zip_max_bytes
    if len(raw_bytes) == 0 or len(raw_bytes) > max_bytes:
        raise HTTPException(status_code=413, detail=f"skill archive must be 1..{max_bytes} bytes")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid .zip archive") from exc

    files_meta: list[dict[str, Any]] = []
    skill_md_text: Optional[str] = None
    total_uncompressed = 0

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Zip Slip path traversal defense
            norm = PurePosixPath(info.filename.replace("\\", "/"))
            if norm.is_absolute() or ".." in norm.parts:
                raise HTTPException(status_code=400, detail=f"unsafe path in zip archive: {info.filename}")
            total_uncompressed += int(info.file_size)
            if total_uncompressed > max_bytes * 2:
                raise HTTPException(status_code=413, detail="uncompressed skill archive exceeds safety limit")
            files_meta.append({"path": str(norm), "size_bytes": int(info.file_size)})
            if norm.name.upper() == "SKILL.MD" and skill_md_text is None:
                skill_md_text = zf.read(info).decode("utf-8", errors="replace")

    if skill_md_text is None:
        raise HTTPException(status_code=400, detail="archive must contain a SKILL.md file")

    meta, body = skills._parse_frontmatter(skill_md_text)
    skill_name = (meta.get("name") or fallback_name or "").strip()
    if not skill_name or "/" in skill_name or "\\" in skill_name or len(skill_name) > 64:
        raise HTTPException(status_code=400, detail="SKILL.md must declare a valid frontmatter `name` (1..64 chars)")

    category = skills.normalize_category(category_override or meta.get("category"))
    return {
        "name": skill_name,
        "display_name": (meta.get("display_name") or skill_name)[:128],
        "description": meta.get("description", ""),
        "category": category,
        "content": body.strip(),
        "frontmatter": meta,
        "files": files_meta,
    }


@router.get("/api/user/skills")
def list_user_skills(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=6, ge=1, le=100),
    keyword: str = Query(default=""),
    category: Optional[str] = Query(default=None),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_builtin_skills_seeded(db)
    stmt = select(UserSkill).where(
        or_(UserSkill.owner_user_id == user.id, UserSkill.is_builtin.is_(True))
    )
    if category and category.strip():
        stmt = stmt.where(UserSkill.category == category.strip())
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                UserSkill.name.ilike(kw),
                UserSkill.display_name.ilike(kw),
                UserSkill.description.ilike(kw),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(UserSkill.updated_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    return {
        "categories": list(skills.SKILL_CATEGORIES),
        "items": [_serialize_user_skill(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.post("/api/user/skills/upload", status_code=201)
async def upload_user_skill_zip(
    file: UploadFile = File(...),
    category: Optional[str] = Form(default=None),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw = await file.read()
    fallback = (file.filename or "").removesuffix(".zip") or None
    parsed = _safe_parse_skill_zip(raw, fallback_name=fallback, category_override=category)

    existing = db.scalar(
        select(UserSkill).where(UserSkill.owner_user_id == user.id, UserSkill.name == parsed["name"])
    )
    now = utcnow()
    if existing is None:
        row = UserSkill(
            id=new_prefixed_id("skl"),
            owner_user_id=user.id,
            name=parsed["name"],
            display_name=parsed["display_name"],
            description=parsed["description"],
            category=parsed["category"],
            content=parsed["content"],
            frontmatter_json=parsed["frontmatter"],
            files_json=parsed["files"],
            is_builtin=False,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        existing.display_name = parsed["display_name"]
        existing.description = parsed["description"]
        existing.category = parsed["category"]
        existing.content = parsed["content"]
        existing.frontmatter_json = parsed["frontmatter"]
        existing.files_json = parsed["files"]
        existing.updated_at = now
        row = existing
        db.add(row)
    db.commit()
    return _serialize_user_skill(row)


@router.delete("/api/user/skills/{skill_id}")
def delete_user_skill(
    skill_id: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(UserSkill, skill_id)
    if row is None or row.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="skill not found")
    db.delete(row)
    db.commit()
    return {"deleted": True, "skill_id": skill_id}


@router.get("/api/projects/{project_id}/skills/installed")
def list_project_installed_skills(
    project_id: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    auth.resolve_project_actor(project_id, request, db, x_role)
    rows = db.scalars(
        select(ProjectInstalledSkill)
        .where(ProjectInstalledSkill.project_id == project_id)
        .order_by(ProjectInstalledSkill.installed_at.asc())
    ).all()
    items: list[dict[str, Any]] = []
    for r in rows:
        meta = None
        if r.skill_id:
            us = db.get(UserSkill, r.skill_id)
            if us is not None:
                meta = _serialize_user_skill(us)
        items.append({
            "id": r.id,
            "project_id": r.project_id,
            "skill_id": r.skill_id,
            "skill_name": r.skill_name,
            "enabled": bool(r.enabled),
            "installed_by": r.installed_by,
            "installed_at": r.installed_at.isoformat(),
            "skill": meta,
        })
    return {"project_id": project_id, "items": items, "total": len(items)}


@router.post("/api/projects/{project_id}/skills/installed", status_code=201)
def install_project_skill(
    project_id: str,
    body: ProjectSkillInstallRequest,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot install project skills")

    skill_id: Optional[str] = None
    skill_name: Optional[str] = None
    if body.skill_id:
        us = db.get(UserSkill, body.skill_id)
        if us is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {body.skill_id}")
        if us.owner_user_id and user and us.owner_user_id != user.id and not us.is_builtin:
            raise HTTPException(status_code=403, detail="cannot install another user's private skill")
        skill_id = us.id
        skill_name = us.name
    elif body.skill_name:
        try:
            loaded = skills.load_skill(body.skill_name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        skill_name = loaded["name"]
        skill_id = loaded.get("id")
    else:
        raise HTTPException(status_code=400, detail="provide skill_id or skill_name")

    existing = db.scalar(
        select(ProjectInstalledSkill).where(
            ProjectInstalledSkill.project_id == project_id,
            ProjectInstalledSkill.skill_name == skill_name,
        )
    )
    if existing is None:
        existing = ProjectInstalledSkill(
            id=new_prefixed_id("psk"),
            project_id=project_id,
            skill_id=skill_id,
            skill_name=skill_name,
            enabled=True,
            installed_by=user.id if user else None,
            installed_at=utcnow(),
        )
    else:
        existing.enabled = True
        existing.skill_id = skill_id or existing.skill_id
    db.add(existing)
    db.commit()
    skills._overrides[(project_id, skill_name)] = True
    return {
        "id": existing.id,
        "project_id": project_id,
        "skill_id": existing.skill_id,
        "skill_name": existing.skill_name,
        "enabled": True,
        "installed": True,
    }


@router.delete("/api/projects/{project_id}/skills/installed/{skill_name}")
def uninstall_project_skill(
    project_id: str,
    skill_name: str,
    request: Request,
    x_role: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _user, role = auth.resolve_project_actor(project_id, request, db, x_role)
    if not can_write(role):
        raise HTTPException(status_code=403, detail="viewer cannot uninstall project skills")
    row = db.scalar(
        select(ProjectInstalledSkill).where(
            ProjectInstalledSkill.project_id == project_id,
            ProjectInstalledSkill.skill_name == skill_name,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"installed skill not found: {skill_name}")
    db.delete(row)
    db.commit()
    skills._overrides.pop((project_id, skill_name), None)
    return {"project_id": project_id, "skill_name": skill_name, "uninstalled": True}
