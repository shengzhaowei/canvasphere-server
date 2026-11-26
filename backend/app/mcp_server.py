"""FastMCP server 'canvasphere'.

Tools reuse app.store (the single shared memory store) and the agent
runner from app.agents -- no second copy of storage logic.
Run:  python -m app.mcp_server   (stdio transport, for MCP clients)
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from app import agents, store

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
    """List a project's tasks and canvas elements (no real filesystem yet).

    The project has no on-disk files in this backend; this returns the
    project's stored artefacts (tasks + canvas elements) honestly
    instead of inventing file paths.
    """
    if store.get_project(project_id) is None:
        return {"error": f"project not found: {project_id}"}
    return {"project_id": project_id,
            "tasks": store.list_tasks(project_id),
            "canvas_elements": store.list_elements(project_id),
            "files": []}


@mcp.tool
def delegate_task(project_id: str, prompt: str,
                  agent: Optional[str] = None) -> dict[str, Any]:
    """Create a task and run the Supervisor -> domain agent pipeline.

    Approval-gated products (visual/video) stop at waiting_approval
    and only flow onto the canvas after POST .../approve in the API.
    """
    try:
        task = store.create_task(project_id, prompt, agent)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}
    result = agents.run_task_sync(prompt, agent)
    updated = store.apply_agent_result(task["task_id"], result)
    if not result.get("approval_required"):
        # Flow the agent's complete element dict onto the canvas
        # verbatim (same element stored in the task by
        # apply_agent_result); approval-gated products wait for approve.
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


if __name__ == "__main__":
    mcp.run()
