"""Project-level roles and the tool allowlist (server-side enforcement).

Roles: owner / editor / viewer.
Tools: read_canvas, add_node, update_node, list_project_files,
       delegate_task, run_code, generate_image, generate_video,
       toggle_skill, delete_node, open_sandbox, manage_subagent,
       update_plan.
Destructive / money-spending tools always require approval, whatever
the role. Viewer is read-only: it may not create tasks or change the
canvas. The frontend must never be trusted to enforce this; every
mutating route in main.py calls check_permission / require_approval.

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
Per-project tier overrides (owner only, in-memory) and session grants
live in this module. require_approval(tool, project_id) applies the
effective tier + grant state.
"""
from __future__ import annotations

ROLES = ("owner", "editor", "viewer")

ALL_TOOLS = (
    "read_canvas", "add_node", "update_node", "list_project_files",
    "delegate_task", "run_code", "generate_image", "generate_video",
    "toggle_skill", "delete_node", "open_sandbox", "manage_subagent",
    "update_plan",
)

# Money-spending or destructive: always approval-gated.
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

# In-memory per-project state (restart loses it; that is intentional
# for session grants -- a new session re-confirms).
_policy_overrides: dict[str, dict[str, str]] = {}  # project_id -> {tool: tier}
_grants: set[tuple[str, str]] = set()  # (project_id, tool)


def reset_approval_state() -> None:
    """Clear all per-project overrides and grants. Used by tests."""
    _policy_overrides.clear()
    _grants.clear()


def tier_of(tool: str, project_id: str | None = None) -> str:
    """Effective tier for `tool` in `project_id` (override wins)."""
    if project_id is not None:
        override = _policy_overrides.get(project_id, {}).get(tool)
        if override is not None:
            return override
    return _DEFAULT_TIER.get(tool, "always")


def get_policy(project_id: str) -> dict[str, object]:
    """Full policy view for one project: tiers + overrides + grants."""
    tiers: dict[str, list[str]] = {"auto": [], "session": [], "always": []}
    for tool in ALL_TOOLS:
        tiers[tier_of(tool, project_id)].append(tool)
    return {
        "project_id": project_id,
        "tiers": tiers,
        "overrides": dict(_policy_overrides.get(project_id, {})),
        "grants": sorted(t for (pid, t) in _grants if pid == project_id),
    }


def set_tool_tier(project_id: str, tool: str, tier: str) -> str:
    """Set a per-project tier override. Returns the effective tier."""
    if tool not in ALL_TOOLS:
        raise ValueError(f"unknown tool (not in allowlist): {tool}")
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier!r} (expected one of {TIERS})")
    _policy_overrides.setdefault(project_id, {})[tool] = tier
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
    if role not in TOOL_ALLOWLIST:
        return False
    if tool not in ALL_TOOLS:
        return False
    return tool in TOOL_ALLOWLIST[role]


def require_approval(tool: str, project_id: str | None = None) -> bool:
    """True iff using `tool` must go through human approval now.

    AUTO never needs approval. ALWAYS always does. SESSION needs
    approval until a session grant exists for (project_id, tool);
    without a project_id there is no grant to consult, so it needs
    approval. Unknown tools fail closed (approval required).
    """
    tier = tier_of(tool, project_id) if tool in ALL_TOOLS else "always"
    if tier == "auto":
        return False
    if tier == "always":
        return True
    # session tier
    if project_id is None:
        return True
    return not has_grant(project_id, tool)


def can_write(role: str) -> bool:
    """Viewer cannot write at all; owner/editor can (subject to tool checks)."""
    return role in ("owner", "editor")


def assert_permission(role: str, tool: str) -> None:
    """Raise PermissionError with a clear message when not allowed."""
    if tool not in ALL_TOOLS:
        raise PermissionError(f"unknown tool (not in allowlist): {tool}")
    if role not in TOOL_ALLOWLIST:
        raise PermissionError(f"unknown role: {role}")
    if tool not in TOOL_ALLOWLIST[role]:
        raise PermissionError(
            f"role '{role}' is not allowed to use tool '{tool}'")
