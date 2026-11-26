"""FastMCP servers for Canvasphere & Pecan Studio dual entrypoints.

1. `mcp` (`canvasphere`)       -> mounted at `/mcp` (legacy compatibility + stdio)
2. `mcp_canvas` (`pecan-canvas`) -> mounted at `/mcp/canvas/` (direct canvas operations)
3. `mcp_pecan` (`pecan-studio`)  -> mounted at `/mcp/pecan/`  (delegate to Pecan orchestrator)
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from app import agents, store
from app.db import session_scope
from app.models import Canvas, Project, TaskRun

# --- Legacy combined MCP server (/mcp & stdio) ----------------------------
mcp = FastMCP("canvasphere")


@mcp.tool
def read_canvas(project_id: str) -> dict[str, Any]:
    """List the canvas elements of a project."""
    try:
        return {"project_id": project_id, "elements": store.list_elements(project_id)}
    except KeyError as exc:
        return {"error": str(exc)}


@mcp.tool
def add_node(project_id: str, type: str, x: float, y: float,
             text: str = "") -> dict[str, Any]:
    """Add a node to a project's canvas (default size 320x180)."""
    try:
        return store.add_element(project_id, type, x, y, 320, 180, text)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool
def update_node(project_id: str, node_id: str, text: str) -> dict[str, Any]:
    """Update the text of a canvas node."""
    try:
        return store.update_element(project_id, node_id, text)
    except KeyError as exc:
        return {"error": str(exc)}


@mcp.tool
def list_project_files(project_id: str) -> dict[str, Any]:
    """List a project's tasks and canvas elements."""
    if store.get_project(project_id) is None:
        return {"error": f"project not found: {project_id}"}
    return {"project_id": project_id,
            "tasks": store.list_tasks(project_id),
            "canvas_elements": store.list_elements(project_id),
            "files": []}


@mcp.tool
def delegate_task(project_id: str, prompt: str,
                  agent: Optional[str] = None) -> dict[str, Any]:
    """Create a task and run the Supervisor -> domain agent pipeline."""
    try:
        task = store.create_task(project_id, prompt, agent)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}
    result = agents.run_task_sync(prompt, agent)
    updated = store.apply_agent_result(task["task_id"], result)
    if not result.get("approval_required"):
        el = result.get("element")
        if isinstance(el, dict) and el.get("type"):
            store.add_element(project_id, el["type"], el.get("x", 40),
                              el.get("y", 40), el.get("w", 320),
                              el.get("h", 180), el.get("text", ""))
    return {"task_id": task["task_id"], "request_id": updated.get("request_id"),
            "agent": updated["agent"],
            "status": updated["status"],
            "approval_required": updated["approval_required"],
            "mock": updated["mock"],
            "element": updated.get("element"),
            "usage": updated.get("usage"),
            "estimated_cost": updated.get("estimated_cost")}


# --- Dual Entrypoint 1: /mcp/canvas/ (pecan-canvas) -----------------------
mcp_canvas = FastMCP("pecan-canvas")


@mcp_canvas.tool
def pecan_list_projects() -> dict[str, Any]:
    """List active projects accessible on this Canvasphere instance."""
    return {"projects": store.list_projects()}


@mcp_canvas.tool
def pecan_canvas_read(project_id: str, canvas_id: Optional[str] = None) -> dict[str, Any]:
    """Read objects on a project's canvas."""
    try:
        return {"project_id": project_id, "canvas_id": canvas_id, "objects": store.list_elements(project_id, canvas_id=canvas_id)}
    except KeyError as exc:
        return {"error": str(exc)}


@mcp_canvas.tool
def directory_list(project_id: str) -> dict[str, Any]:
    """List canvases and artefacts in a project directory."""
    proj = store.get_project(project_id)
    if proj is None:
        return {"error": f"project not found: {project_id}"}
    with session_scope() as db:
        from sqlalchemy import select
        canvases = db.scalars(select(Canvas).where(Canvas.project_id == project_id)).all()
        return {
            "project_id": project_id,
            "workspace_id": proj.get("workspace_id"),
            "canvases": [{"id": c.id, "name": c.name, "is_default": c.is_default} for c in canvases],
        }


@mcp_canvas.tool
def pecan_canvas_add_node(project_id: str, type: str, x: float, y: float, text: str = "", canvas_id: Optional[str] = None) -> dict[str, Any]:
    """Create a node directly on a project's canvas."""
    try:
        return store.add_element(project_id, type, x, y, 320, 180, text, canvas_id=canvas_id)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}


# --- Dual Entrypoint 2: /mcp/pecan/ (pecan-orchestrator) ------------------
mcp_pecan = FastMCP("pecan-orchestrator")


@mcp_pecan.tool
def pecan_delegate_task(project_id: str, prompt: str, agent: Optional[str] = None) -> dict[str, Any]:
    """Delegate a multi-agent creative task to Pecan Studio orchestrator."""
    return delegate_task(project_id, prompt, agent)


@mcp_pecan.tool
def pecan_get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    """Inspect a TaskRun's status, fence, and result."""
    with session_scope() as db:
        r = db.get(TaskRun, run_id)
        if r is None or r.project_id != project_id:
            return {"error": f"task run not found: {run_id}"}
        return {
            "run_id": r.id,
            "status": r.status,
            "primary_agent": r.primary_agent,
            "generation_fence": int(r.generation_fence),
            "result": r.result_json,
        }


if __name__ == "__main__":
    mcp.run()
