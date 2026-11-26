"""Skill loader: reads ../../skills/<name>/SKILL.md + DB-backed custom skills.

Supports 9 Pecan Studio categories and persists per-project enable/disable
state in `project_installed_skills` when the project exists in DB, while
keeping an in-memory mirror so unit tests without DB rows still pass cleanly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from app.db import session_scope
from app.models import Project, ProjectInstalledSkill, UserSkill, new_prefixed_id, utcnow

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

SKILL_CATEGORIES = (
    "剧本与故事",
    "分镜与摄影",
    "画面与风格",
    "视频创作",
    "动作与特效",
    "声音与配乐",
    "素材与后期",
    "研究与辅助",
    "未分类",
)

# (project_id, skill_name) -> enabled
_overrides: dict[tuple[str, str], bool] = {}


def normalize_category(raw: Optional[str]) -> str:
    val = (raw or "").strip()
    return val if val in SKILL_CATEGORIES else "未分类"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse a leading --- ... --- block of `key: value` lines."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    block = text[3:end].strip()
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta, text[end + 4:].lstrip("\n")


def _skill_path(name: str, skills_dir: Optional[Path] = None) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid skill name: {name!r}")
    base = Path(skills_dir) if skills_dir else SKILLS_DIR
    return base / name / "SKILL.md"


def list_skills(skills_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """All built-in skills found under skills_dir (default: ../../skills/)."""
    base = Path(skills_dir) if skills_dir else SKILLS_DIR
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        path = d / "SKILL.md"
        if not path.is_file():
            out.append({"name": d.name, "description": "",
                        "path": str(path), "missing_file": True})
            continue
        try:
            out.append(load_skill(d.name, skills_dir=skills_dir))
        except Exception as exc:  # noqa: BLE001 - surface per-skill error
            out.append({"name": d.name, "description": "",
                        "path": str(path), "error": str(exc)})
    return out


def load_skill(name: str, skills_dir: Optional[Path] = None) -> dict[str, Any]:
    """Load one skill from filesystem, falling back to DB custom skills when skills_dir is default."""
    path = _skill_path(name, skills_dir)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return {
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
            "category": normalize_category(meta.get("category")),
            "path": str(path),
            "content": body.strip(),
            "frontmatter": meta,
        }
    if skills_dir is None:
        try:
            with session_scope() as db:
                row = db.scalar(
                    select(UserSkill)
                    .where(UserSkill.name == name)
                    .order_by(UserSkill.updated_at.desc())
                    .limit(1)
                )
                if row is not None:
                    return {
                        "id": row.id,
                        "name": row.name,
                        "description": row.description,
                        "category": normalize_category(row.category),
                        "path": f"db://user_skills/{row.id}",
                        "content": row.content,
                        "frontmatter": dict(row.frontmatter_json or {}),
                    }
        except Exception:
            pass
    raise FileNotFoundError(f"skill not found: {name!r} (expected {path})")


def is_skill_enabled(project_id: str, name: str) -> bool:
    if (project_id, name) in _overrides:
        return _overrides[(project_id, name)]
    try:
        with session_scope() as db:
            row = db.scalar(
                select(ProjectInstalledSkill).where(
                    ProjectInstalledSkill.project_id == project_id,
                    ProjectInstalledSkill.skill_name == name,
                )
            )
            if row is not None:
                return bool(row.enabled)
    except Exception:
        pass
    return True


def set_skill_enabled(project_id: str, name: str, enabled: bool) -> bool:
    val = bool(enabled)
    _overrides[(project_id, name)] = val
    try:
        with session_scope() as db:
            if db.get(Project, project_id) is not None:
                row = db.scalar(
                    select(ProjectInstalledSkill).where(
                        ProjectInstalledSkill.project_id == project_id,
                        ProjectInstalledSkill.skill_name == name,
                    )
                )
                if row is None:
                    db.add(ProjectInstalledSkill(
                        id=new_prefixed_id("psk"),
                        project_id=project_id,
                        skill_name=name,
                        enabled=val,
                        installed_at=utcnow(),
                    ))
                else:
                    row.enabled = val
                    db.add(row)
    except Exception:
        pass
    return val
