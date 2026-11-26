"""Skill loader: reads ../../skills/<name>/SKILL.md.

Parses the YAML-ish frontmatter (name, description) plus the body
(the skill's workflow). Missing skills raise FileNotFoundError with
a clear message -- never silently skipped. Per-project enable/disable
is kept in memory (default: enabled).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

# (project_id, skill_name) -> enabled
_overrides: dict[tuple[str, str], bool] = {}


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
    """All skills found under skills_dir (default: ../../skills/).

    Directories without SKILL.md are skipped and reported in the
    entry as missing_file so a half-built skill is visible, not hidden.
    """
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
    """Load one skill. Raises FileNotFoundError if it does not exist."""
    path = _skill_path(name, skills_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"skill not found: {name!r} (expected {path})")
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    return {
        "name": meta.get("name", name),
        "description": meta.get("description", ""),
        "path": str(path),
        "content": body.strip(),
        "frontmatter": meta,
    }


def is_skill_enabled(project_id: str, name: str) -> bool:
    return _overrides.get((project_id, name), True)


def set_skill_enabled(project_id: str, name: str, enabled: bool) -> bool:
    _overrides[(project_id, name)] = bool(enabled)
    return bool(enabled)
