"""Project-level roles and the tool allowlist (server-side enforcement).

Roles: owner / editor / viewer (alias: read-only -> viewer).
Tools: read_canvas, add_node, update_node, list_project_files,
       delegate_task, run_code, generate_image, generate_video,
       toggle_skill, delete_node, open_sandbox, manage_subagent,
       update_plan.

Approval tiers (Pecan-style, three tiers):
  AUTO    -- auto-approved, no confirmation ever:
             read_canvas, list_project_files
  SESSION -- first use in a project needs one confirmation; after a
             session grant (project_id + tool, in-memory) later uses
             in the same project are auto-approved:
             add_node, update_node, delegate_task, toggle_skill, run_code
  ALWAYS  -- every use needs confirmation:
             generate_image, generate_video, delete_node,
             open_sandbox, manage_subagent, update_plan

Per-project tier overrides are persisted to the database table
`project_approval_policies` whenever the project exists in DB, with an
in-memory mirror so synthetic unit-test project IDs also work cleanly.
Session grants stay in-memory (a new server session re-confirms).
"""
from __future__ import annotations

from sqlalchemy import delete, select

from app.db import session_scope
from app.models import Project, ProjectApprovalPolicy, utcnow

ROLES = ("owner", "editor", "viewer")

ROLE_ALIASES = {
    "owner": "owner",
    "editor": "editor",
    "viewer": "viewer",
    "read-only": "viewer",
    "readonly": "viewer",
    "read_only": "viewer",
}

ALL_TOOLS = (
    "read_canvas", "add_node", "update_node", "list_project_files",
    "delegate_task", "run_code", "generate_image", "generate_video",
    "toggle_skill", "delete_node", "open_sandbox", "manage_subagent",
    "update_plan",
)

DESTRUCTIVE_TOOLS = frozenset({"run_code", "generate_image", "generate_video"})

TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "owner": frozenset(ALL_TOOLS),
    "editor": frozenset(ALL_TOOLS),
    "viewer": frozenset({"read_canvas", "list_project_files"}),
}

WRITE_TOOLS = frozenset(
    {"add_node", "update_node", "delegate_task", "run_code",
     "generate_image", "generate_video", "toggle_skill", "delete_node",
     "open_sandbox", "manage_subagent", "update_plan"}
)

# --- approval tiers -------------------------------------------------------
TIERS = ("auto", "session", "always")

AUTO_TOOLS = frozenset({"read_canvas", "list_project_files"})
SESSION_TOOLS = frozenset(
    {"add_node", "update_node", "delegate_task", "toggle_skill", "run_code"})
ALWAYS_TOOLS = frozenset(
    {"generate_image", "generate_video", "delete_node", "open_sandbox",
     "manage_subagent", "update_plan"})

_DEFAULT_TIER: dict[str, str] = {}
for _t in AUTO_TOOLS:
    _DEFAULT_TIER[_t] = "auto"
for _t in SESSION_TOOLS:
    _DEFAULT_TIER[_t] = "session"
for _t in ALWAYS_TOOLS:
    _DEFAULT_TIER[_t] = "always"

_policy_overrides: dict[str, dict[str, str]] = {}  # project_id -> {tool: tier}
_grants: set[tuple[str, str]] = set()  # (project_id, tool)


def normalize_role(role: str | None) -> str:
    raw = (role or "").strip().lower()
    return ROLE_ALIASES.get(raw, raw)


def project_permission_flags(role: str) -> dict[str, bool]:
    r = normalize_role(role)
    is_owner = r == "owner"
    is_writer = r in ("owner", "editor")
    is_reader = r in ("owner", "editor", "viewer")
    return {
        "can_read": is_reader,
        "can_rename": is_writer,
        "can_delete": is_owner,
        "can_run": is_writer,
        "can_manage_members": is_owner,
    }


def _db_overrides(project_id: str) -> dict[str, str]:
    try:
        with session_scope() as db:
            rows = db.scalars(
                select(ProjectApprovalPolicy)
                .where(ProjectApprovalPolicy.project_id == project_id)
            ).all()
            return {row.tool: row.tier for row in rows}
    except Exception:
        return {}


def reset_approval_state() -> None:
    """Clear all per-project overrides and grants. Used by tests."""
    _policy_overrides.clear()
    _grants.clear()
    try:
        with session_scope() as db:
            db.execute(delete(ProjectApprovalPolicy))
    except Exception:
        pass


def tier_of(tool: str, project_id: str | None = None) -> str:
    """Effective tier for `tool` in `project_id` (override wins)."""
    if project_id is not None:
        merged = _db_overrides(project_id)
        merged.update(_policy_overrides.get(project_id, {}))
        override = merged.get(tool)
        if override is not None:
            return override
    return _DEFAULT_TIER.get(tool, "always")


def get_policy(project_id: str) -> dict[str, object]:
    """Full policy view for one project: tiers + overrides + grants."""
    overrides = _db_overrides(project_id)
    overrides.update(_policy_overrides.get(project_id, {}))
    tiers: dict[str, list[str]] = {"auto": [], "session": [], "always": []}
    for tool in ALL_TOOLS:
        effective = overrides.get(tool, _DEFAULT_TIER.get(tool, "always"))
        tiers[effective].append(tool)
    return {
        "project_id": project_id,
        "tiers": tiers,
        "overrides": overrides,
        "grants": sorted(t for (pid, t) in _grants if pid == project_id),
    }


def set_tool_tier(project_id: str, tool: str, tier: str) -> str:
    """Set a per-project tier override (persisted if project exists in DB)."""
    if tool not in ALL_TOOLS:
        raise ValueError(f"unknown tool (not in allowlist): {tool}")
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier!r} (expected one of {TIERS})")
    _policy_overrides.setdefault(project_id, {})[tool] = tier
    with session_scope() as db:
        if db.get(Project, project_id) is not None:
            existing = db.scalar(
                select(ProjectApprovalPolicy)
                .where(
                    ProjectApprovalPolicy.project_id == project_id,
                    ProjectApprovalPolicy.tool == tool,
                )
            )
            if existing is None:
                db.add(ProjectApprovalPolicy(
                    project_id=project_id,
                    tool=tool,
                    tier=tier,
                    updated_at=utcnow(),
                ))
            else:
                existing.tier = tier
                existing.updated_at = utcnow()
    return tier


def grant_tool(project_id: str, tool: str) -> bool:
    """Record a session grant for (project_id, tool). Returns True."""
    if tool not in ALL_TOOLS:
        raise ValueError(f"unknown tool (not in allowlist): {tool}")
    _grants.add((project_id, tool))
    return True


def has_grant(project_id: str, tool: str) -> bool:
    return (project_id, tool) in _grants


def revoke_grant(project_id: str, tool: str) -> None:
    _grants.discard((project_id, tool))


def check_permission(role: str, tool: str) -> bool:
    """True iff `role` may use `tool`. Unknown role/tool -> False."""
    r = normalize_role(role)
    if r not in TOOL_ALLOWLIST:
        return False
    if tool not in ALL_TOOLS:
        return False
    return tool in TOOL_ALLOWLIST[r]


def require_approval(tool: str, project_id: str | None = None) -> bool:
    """True iff using `tool` must go through human approval now."""
    tier = tier_of(tool, project_id) if tool in ALL_TOOLS else "always"
    if tier == "auto":
        return False
    if tier == "always":
        return True
    if project_id is None:
        return True
    return not has_grant(project_id, tool)


def can_write(role: str) -> bool:
    """Viewer cannot write at all; owner/editor can (subject to tool checks)."""
    return normalize_role(role) in ("owner", "editor")


def assert_permission(role: str, tool: str) -> None:
    """Raise PermissionError with a clear message when not allowed."""
    r = normalize_role(role)
    if tool not in ALL_TOOLS:
        raise PermissionError(f"unknown tool (not in allowlist): {tool}")
    if r not in TOOL_ALLOWLIST:
        raise PermissionError(f"unknown role: {role}")
    if tool not in TOOL_ALLOWLIST[r]:
        raise PermissionError(
            f"role '{r}' is not allowed to use tool '{tool}'")
