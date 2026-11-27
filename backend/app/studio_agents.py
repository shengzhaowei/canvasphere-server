"""Pecan Studio 5-agent orchestrator, multi-turn ReAct tool-calling loop, canvas DAG context,
LLM semantic compression, TaskRun generation fences & SSE streaming generator.

5 Specialized Agents (aligned with ai.pecan.wang):
- pecan     : Pecan (主控统筹：拆解任务树、委派专家、汇总交付)
- orange    : 橘子  (故事与剧本 / story)
- xiaoa     : 小a   (视觉、风格圣经与 3D HTML 预演 / visual)
- blueberry : 蓝莓  (视频、分镜与剪辑 / video)
- yueyue    : 悦悦  (音乐与声音配乐 / audio)
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Generator, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agents, asset_tools, model_platform, permissions, sandbox_manager, skills, storage
from app.config import get_settings
from app.models import (
    AgentMessage,
    AgentSession,
    Canvas,
    CanvasObject,
    Project,
    StorageObject,
    TaskRun,
    new_prefixed_id,
    utcnow,
)
from app.realtime import event_bus

logger = logging.getLogger(__name__)

STUDIO_AGENTS: dict[str, dict[str, Any]] = {
    "pecan": {
        "id": "pecan",
        "name": "Pecan",
        "role": "主控统筹",
        "legacy_domain": "story",
        "default_tool": "update_plan",
        "description": "负责目标拆解、任务树规划、子专家委派与结果汇总。",
    },
    "orange": {
        "id": "orange",
        "name": "橘子",
        "role": "故事与剧本",
        "legacy_domain": "story",
        "default_tool": "add_node",
        "description": "负责世界观、剧本大纲、对白台词与项目风格圣经文本创作。",
    },
    "xiaoa": {
        "id": "xiaoa",
        "name": "小a",
        "role": "视觉与 3D 预演",
        "legacy_domain": "visual",
        "default_tool": "generate_image",
        "description": "负责关键帧视觉设计、角色造型、画面风格与 3D HTML 预演。",
    },
    "blueberry": {
        "id": "blueberry",
        "name": "蓝莓",
        "role": "视频与分镜剪辑",
        "legacy_domain": "video",
        "default_tool": "generate_video",
        "description": "负责分镜脚本、动态视频生成、运镜与非破坏性时间线剪辑。",
    },
    "yueyue": {
        "id": "yueyue",
        "name": "悦悦",
        "role": "音乐与声音",
        "legacy_domain": "audio",
        "default_tool": "add_node",
        "description": "负责配音旁白、音效设计、主题配乐与 Suno 歌曲创作。",
    },
}

_AGENT_ALIASES = {
    "pecan": "pecan",
    "orange": "orange",
    "橘子": "orange",
    "story": "orange",
    "xiaoa": "xiaoa",
    "小a": "xiaoa",
    "visual": "xiaoa",
    "blueberry": "blueberry",
    "蓝莓": "blueberry",
    "video": "blueberry",
    "yueyue": "yueyue",
    "悦悦": "yueyue",
    "audio": "yueyue",
}

_SKILL_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")

# OpenAI-compatible JSON tool definitions bound to LLM in multi-turn ReAct mode
STUDIO_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_canvas",
            "description": "Read current objects and connectors on the active canvas.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_files",
            "description": "List uploaded workspace files in the current project.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_node",
            "description": "Create a text/document/media node on the canvas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "default": "text"},
                    "name": {"type": "string"},
                    "text": {"type": "string"},
                    "x": {"type": "number", "default": 80},
                    "y": {"type": "number", "default": 80},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_node",
            "description": "Update an existing canvas node's text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["node_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image on the canvas via the multimodal model platform.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_key": {"type": "string", "default": "gpt-image-2.6-flare-ox"},
                    "prompt": {"type": "string"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generate a video on the canvas via the multimodal model platform.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_key": {"type": "string", "default": "seedance-2.5-pro-zh-tt-720"},
                    "prompt": {"type": "string"},
                    "duration_seconds": {"type": "integer", "default": 5},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Execute Python code in the project sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
]


@dataclass
class ModelTurnResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"id", "name", "args"}]
    mock: bool = True
    usage: Optional[dict[str, Any]] = None


def normalize_agent_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _AGENT_ALIASES.get(raw.strip().lower())


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 2) // 3)


def extract_skill_mentions(prompt: str, explicit_skills: Optional[list[str]] = None) -> list[str]:
    found = list(explicit_skills or [])
    for m in _SKILL_MENTION_RE.findall(prompt or ""):
        if m not in found:
            found.append(m)
    return found


def resolve_enabled_skills(project_id: str, skill_names: list[str]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for name in skill_names:
        try:
            sk = skills.load_skill(name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not skills.is_skill_enabled(project_id, name):
            raise HTTPException(status_code=400, detail=f"skill is disabled in this project: {name}")
        resolved.append({"name": sk["name"], "description": sk.get("description", "")})
    return resolved


# --- Canvas Graph Context Engine (Upstream BFS along connectors) ----------
def gather_canvas_graph_context(
    db: Session,
    project_id: str,
    canvas_id: str,
    seed_object_ids: list[str],
    *,
    max_depth: int = 4,
    max_nodes: int = 30,
) -> dict[str, Any]:
    if not seed_object_ids:
        return {"nodes": [], "edges": [], "prompt_context": ""}

    all_objs = db.scalars(
        select(CanvasObject).where(
            CanvasObject.project_id == project_id,
            CanvasObject.canvas_id == canvas_id,
            CanvasObject.deleted_at.is_(None),
        )
    ).all()
    by_id = {o.id: o for o in all_objs}
    incoming: dict[str, list[CanvasObject]] = {}
    for o in all_objs:
        if o.type == "connector" and o.from_object_id and o.to_object_id:
            incoming.setdefault(o.to_object_id, []).append(o)

    visited: set[str] = set()
    ordered_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    queue: deque[tuple[str, int]] = deque((oid, 0) for oid in seed_object_ids if oid in by_id)

    while queue and len(ordered_nodes) < max_nodes:
        cur_id, depth = queue.popleft()
        if cur_id in visited:
            continue
        visited.add(cur_id)
        node = by_id[cur_id]
        ordered_nodes.append({
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "text": (node.text or "")[:400],
            "depth": depth,
            "has_asset": bool(node.asset_json),
        })
        if depth < max_depth:
            for edge in incoming.get(cur_id, []):
                edges.append({
                    "connector_id": edge.id,
                    "from": edge.from_object_id,
                    "to": edge.to_object_id,
                })
                if edge.from_object_id and edge.from_object_id in by_id and edge.from_object_id not in visited:
                    queue.append((edge.from_object_id, depth + 1))

    ordered_nodes.sort(key=lambda n: n["depth"], reverse=True)
    lines = []
    for n in ordered_nodes:
        label = n["name"] or n["id"]
        snippet = n["text"] or ("(媒体资产)" if n["has_asset"] else "(空节点)")
        lines.append(f"- [{n['type']}] {label}: {snippet}")
    prompt_context = "\n[画布引用上下文]\n" + "\n".join(lines) if lines else ""
    return {"nodes": ordered_nodes, "edges": edges, "prompt_context": prompt_context}


# --- LLM Semantic Summarization + Token-Aware Context Compression ---------
def summarize_messages_for_compression(
    previous_summary: str,
    messages: list[AgentMessage],
) -> tuple[str, bool]:
    """Generate semantic memory summary via LLM when LLM_API_KEY is set, else deterministic fallback."""
    transcript_lines = [f"[{m.role}/{m.agent_name or 'user'}] {m.content[:240]}" for m in messages]
    raw_block = "\n".join(transcript_lines)
    if agents.is_live_mode():
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(
                base_url=os.environ.get("LLM_BASE_URL") or None,
                api_key=os.environ["LLM_API_KEY"],
                model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
                timeout=20,
            )
            prompt = (
                "请将以下多智能体画布创作对话历史压缩为简洁的记忆摘要（不超过 300 字），"
                "务必保留关键角色设定、视觉风格、已生成资产与待确认决策：\n"
                f"已有摘要：{previous_summary or '（无）'}\n\n新增对话：\n{raw_block}"
            )
            msg = llm.invoke(prompt)
            summary = str(getattr(msg, "content", "") or "").strip()
            if summary:
                return summary[:4000], True
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM context summarization failed (%s); falling back to deterministic summary", exc)

    chunk = " | ".join(f"[{m.role}/{m.agent_name or 'user'}] {m.content[:120]}" for m in messages)
    merged = f"{previous_summary}\n{chunk}".strip() if previous_summary else chunk
    return merged[-4000:], False


def compress_session_context_if_needed(
    db: Session,
    session: AgentSession,
    project: Project,
    user_id: Optional[str],
) -> bool:
    threshold = get_settings().context_compress_threshold_tokens
    msgs = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.session_id == session.id, AgentMessage.compressed.is_(False))
        .order_by(AgentMessage.created_at.asc())
    ).all()
    total_tokens = sum(int(m.token_estimate) for m in msgs)
    session.token_count = total_tokens
    if total_tokens <= threshold or len(msgs) <= 2:
        db.add(session)
        return False

    to_compress = msgs[:-2]
    kept = msgs[-2:]

    archive_payload = {
        "session_id": session.id,
        "project_id": project.id,
        "compressed_at": utcnow().isoformat(),
        "previous_summary": session.context_summary,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "agent_name": m.agent_name,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in to_compress
        ],
    }
    raw_bytes = json.dumps(archive_payload, ensure_ascii=False, indent=2).encode("utf-8")
    obj_key = storage.build_object_key(
        "private_runtime",
        f"context_{session.id}_c{session.compressed_count + 1}.json",
        owner_user_id=user_id,
        project_id=project.id,
    )
    settings = get_settings()
    if settings.effective_storage_backend == "s3":
        storage._s3_client().put_object(
            Bucket=settings.s3_bucket,
            Key=obj_key,
            Body=raw_bytes,
            ContentType="application/json",
        )
    else:
        path = storage._local_path_for(obj_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_bytes)

    sto = StorageObject(
        id=new_prefixed_id("sto"),
        space="private_runtime",
        owner_user_id=user_id,
        project_id=project.id,
        object_key=obj_key,
        filename=f"context_{session.id}_c{session.compressed_count + 1}.json",
        content_type="application/json",
        size_bytes=len(raw_bytes),
        status="uploaded",
        created_at=utcnow(),
    )
    db.add(sto)
    db.flush()

    summary_text, _used_llm = summarize_messages_for_compression(session.context_summary, to_compress)
    session.context_summary = summary_text
    session.summary_storage_id = sto.id
    session.compressed_count = int(session.compressed_count) + 1
    session.token_count = sum(int(m.token_estimate) for m in kept) + estimate_tokens(session.context_summary)
    session.updated_at = utcnow()
    for m in to_compress:
        m.compressed = True
        db.add(m)
    db.add(session)
    db.flush()
    return True


# --- TaskRun Lease, Generation Fence & Recovery Bundle --------------------
def acquire_run_lease(db: Session, run: TaskRun, owner: str, *, bump_fence: bool = False) -> TaskRun:
    ttl = get_settings().task_run_lease_ttl_seconds
    now = utcnow()
    if bump_fence:
        run.generation_fence = int(run.generation_fence) + 1
    run.lease_owner = owner
    run.lease_expires_at = now + timedelta(seconds=ttl)
    run.updated_at = now
    db.add(run)
    db.flush()
    return run


def assert_generation_fence(run: TaskRun, expected_fence: int) -> None:
    if int(run.generation_fence) != int(expected_fence):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXECUTION_FENCE_STALE",
                "message": f"TaskRun {run.id} fence mismatch (expected {expected_fence}, current {run.generation_fence})",
                "current_fence": int(run.generation_fence),
            },
        )


def build_recovery_bundle(db: Session, run: TaskRun) -> dict[str, Any]:
    session = db.get(AgentSession, run.session_id) if run.session_id else None
    return {
        "run_id": run.id,
        "request_id": run.request_id,
        "status": run.status,
        "generation_fence": int(run.generation_fence),
        "primary_agent": run.primary_agent,
        "frozen_tree": run.frozen_tree_json,
        "pending_tool": run.pending_tool_json,
        "context_summary": session.context_summary if session else "",
        "summary_storage_id": session.summary_storage_id if session else None,
        "captured_at": utcnow().isoformat(),
    }


def recover_stale_runs(db: Session, project_id: str, new_owner: str = "recovery-worker") -> list[dict[str, Any]]:
    now = utcnow()
    runs = db.scalars(
        select(TaskRun).where(
            TaskRun.project_id == project_id,
            TaskRun.status == "running",
        )
    ).all()
    recovered: list[dict[str, Any]] = []
    for r in runs:
        exp = r.lease_expires_at
        if exp is None or (exp.replace(tzinfo=now.tzinfo) if exp.tzinfo is None else exp) <= now:
            acquire_run_lease(db, r, new_owner, bump_fence=True)
            r.status = "failed"
            r.error_message = "lease expired during execution; fenced & recovered"
            r.recovery_bundle_json = build_recovery_bundle(db, r)
            r.completed_at = now
            db.add(r)
            recovered.append({"run_id": r.id, "generation_fence": int(r.generation_fence), "status": r.status})
    if recovered:
        db.commit()
    return recovered


# --- Multi-Turn ReAct Tool-Calling Engine ---------------------------------
def invoke_tool_calling_model(
    *,
    agent_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> ModelTurnResult:
    """Invoke ChatOpenAI.bind_tools(tools) when LLM_API_KEY is set; pluggable hook for tests."""
    if not agents.is_live_mode():
        # Offline/CI default: return no extra tool calls so standard specialist pipeline executes
        return ModelTurnResult(content="", tool_calls=[], mock=True)
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            base_url=os.environ.get("LLM_BASE_URL") or None,
            api_key=os.environ["LLM_API_KEY"],
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            timeout=30,
        ).bind_tools(tools)
        spec = STUDIO_AGENTS.get(agent_id, STUDIO_AGENTS["pecan"])
        sys_msg = {
            "role": "system",
            "content": (
                f"你是 Pecan Studio 的「{spec['name']}」（{spec['role']}）。"
                f"{spec['description']} 你可以按需调用画布工具，最后给出简洁清晰的中文回复。"
            ),
        }
        msg = llm.invoke([sys_msg, *messages])
        raw_tcs = getattr(msg, "tool_calls", None) or []
        norm_tcs = [
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "name": tc.get("name"),
                "args": tc.get("args") or {},
            }
            for tc in raw_tcs
            if isinstance(tc, dict) and tc.get("name")
        ]
        meta = getattr(msg, "response_metadata", {}) or {}
        usage = meta.get("token_usage") if isinstance(meta.get("token_usage"), dict) else None
        return ModelTurnResult(
            content=str(getattr(msg, "content", "") or "").strip(),
            tool_calls=norm_tcs,
            mock=False,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("bind_tools LLM call failed (%s); degrading cleanly", exc)
        return ModelTurnResult(content="", tool_calls=[], mock=True)


def _execute_studio_tool(
    db: Session,
    *,
    project: Project,
    canvas: Canvas,
    user_id: Optional[str],
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute one validated Studio tool against real DB/storage/model_platform/sandbox."""
    now = utcnow()
    if tool_name == "read_canvas":
        objs = db.scalars(
            select(CanvasObject).where(
                CanvasObject.project_id == project.id,
                CanvasObject.canvas_id == canvas.id,
                CanvasObject.deleted_at.is_(None),
            )
        ).all()
        return {
            "count": len(objs),
            "objects": [{"id": o.id, "type": o.type, "name": o.name, "text": o.text[:200]} for o in objs],
        }
    if tool_name == "list_project_files":
        files = db.scalars(
            select(StorageObject).where(
                StorageObject.project_id == project.id,
                StorageObject.space == "workspace",
                StorageObject.status == "uploaded",
            )
        ).all()
        return {"files": [{"storage_id": f.id, "filename": f.filename, "size_bytes": f.size_bytes} for f in files]}
    if tool_name == "add_node":
        obj = CanvasObject(
            id=new_prefixed_id("obj"),
            project_id=project.id,
            canvas_id=canvas.id,
            type=str(args.get("type") or "text"),
            name=str(args.get("name") or "Agent 节点")[:128],
            x=float(args.get("x", 80)),
            y=float(args.get("y", 80)),
            w=float(args.get("w", 340)),
            h=float(args.get("h", 180)),
            text=str(args.get("text") or ""),
            version=1,
            created_at=now,
            updated_at=now,
        )
        db.add(obj)
        db.flush()
        event_bus.publish(project.id, canvas.id, "object.created", {"object_id": obj.id, "type": obj.type}, actor_id=user_id)
        return {"object_id": obj.id, "type": obj.type, "name": obj.name}
    if tool_name == "update_node":
        node = db.get(CanvasObject, args.get("node_id", ""))
        if node is None or node.canvas_id != canvas.id or node.deleted_at is not None:
            return {"error": f"node not found: {args.get('node_id')}"}
        node.text = str(args.get("text") or "")
        node.version = int(node.version) + 1
        node.updated_at = now
        db.add(node)
        db.flush()
        return {"object_id": node.id, "version": int(node.version)}
    if tool_name == "generate_image":
        res = model_platform.run_generation_pipeline(
            db,
            project=project,
            canvas=canvas,
            user_id=user_id,
            modality="image",
            model_key=str(args.get("model_key") or "gpt-image-2.6-flare-ox"),
            mode="imagine",
            prompt=str(args.get("prompt") or ""),
            params={"count": 1},
        )
        return {"job_id": res["id"], "status": res["status"], "target_object_id": res.get("target_object_id")}
    if tool_name == "generate_video":
        res = model_platform.run_generation_pipeline(
            db,
            project=project,
            canvas=canvas,
            user_id=user_id,
            modality="video",
            model_key=str(args.get("model_key") or "seedance-2.5-pro-zh-tt-720"),
            mode="t2v",
            prompt=str(args.get("prompt") or ""),
            params={"resolution": "720P", "duration_seconds": int(args.get("duration_seconds", 5))},
        )
        return {"job_id": res["id"], "status": res["status"], "target_object_id": res.get("target_object_id")}
    if tool_name == "run_code":
        res = sandbox_manager.execute_in_project_sandbox(
            db,
            project=project,
            user_id=user_id,
            code=str(args.get("code") or "print('ok')"),
        )
        return {"session_id": res["session_id"], "stdout": res["execution"].get("stdout"), "exit_code": res["execution"].get("exit_code")}
    return {"error": f"unsupported tool: {tool_name}"}


def run_react_tool_loop(
    db: Session,
    *,
    run: TaskRun,
    session: AgentSession,
    project: Project,
    canvas: Canvas,
    messages: list[dict[str, Any]],
    tool_trace: list[dict[str, Any]],
    max_turns: int = 6,
    preapproved_call: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Multi-turn bind_tools ReAct loop with mid-loop tiered approval suspension & resume."""
    working_msgs = list(messages)
    trace = list(tool_trace)

    # If resuming from an approved tool call, execute that tool first and feed result back to LLM
    if preapproved_call is not None:
        t_name = preapproved_call["name"]
        t_args = preapproved_call.get("args") or {}
        t_out = _execute_studio_tool(
            db, project=project, canvas=canvas, user_id=run.user_id, tool_name=t_name, args=t_args
        )
        trace.append({"id": preapproved_call.get("id"), "name": t_name, "args": t_args, "output": t_out, "approved": True})
        working_msgs.append({"role": "tool", "name": t_name, "content": json.dumps(t_out, ensure_ascii=False)})

    for _turn in range(max_turns):
        turn_res = invoke_tool_calling_model(
            agent_id=run.primary_agent,
            messages=working_msgs,
            tools=STUDIO_TOOL_SCHEMAS,
        )
        if not turn_res.tool_calls:
            return {
                "status": "completed",
                "content": turn_res.content,
                "mock": turn_res.mock,
                "usage": turn_res.usage,
                "tool_trace": trace,
                "messages": working_msgs,
            }

        for idx, tc in enumerate(turn_res.tool_calls):
            t_name = tc["name"]
            t_args = tc.get("args") or {}
            if permissions.require_approval(t_name, project.id):
                return {
                    "status": "waiting_approval",
                    "pending_call": tc,
                    "remaining_calls": turn_res.tool_calls[idx + 1:],
                    "tool_trace": trace,
                    "messages": working_msgs,
                }
            t_out = _execute_studio_tool(
                db, project=project, canvas=canvas, user_id=run.user_id, tool_name=t_name, args=t_args
            )
            trace.append({"id": tc.get("id"), "name": t_name, "args": t_args, "output": t_out, "approved": False})
            working_msgs.append({"role": "tool", "name": t_name, "content": json.dumps(t_out, ensure_ascii=False)})

    return {
        "status": "completed",
        "content": "已完成多轮工具协同执行。",
        "mock": False,
        "usage": None,
        "tool_trace": trace,
        "messages": working_msgs,
    }


# --- Multi-Agent Routing, Planning & Approval Execution -------------------
def _plan_specialist_steps(prompt: str, agent_hint: Optional[str]) -> tuple[str, list[dict[str, Any]]]:
    hint = normalize_agent_id(agent_hint)
    if hint and hint != "pecan":
        spec = STUDIO_AGENTS[hint]
        return hint, [
            {"step": 1, "agent": "pecan", "action": "plan_and_delegate", "tool": "read_canvas", "status": "done"},
            {"step": 2, "agent": hint, "action": spec["role"], "tool": spec["default_tool"], "status": "pending"},
        ]

    domain = agents.route_agent(prompt)
    domain_to_agent = {"story": "orange", "visual": "xiaoa", "video": "blueberry", "audio": "yueyue"}
    specialist = domain_to_agent.get(domain, "orange")
    spec = STUDIO_AGENTS[specialist]
    return specialist, [
        {"step": 1, "agent": "pecan", "action": "plan_and_delegate", "tool": "read_canvas", "status": "done"},
        {"step": 2, "agent": specialist, "action": spec["role"], "tool": spec["default_tool"], "status": "pending"},
    ]


def serialize_session(s: AgentSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "session_id": s.id,
        "project_id": s.project_id,
        "canvas_id": s.canvas_id,
        "user_id": s.user_id,
        "title": s.title,
        "context_summary": s.context_summary,
        "summary_storage_id": s.summary_storage_id,
        "token_count": int(s.token_count),
        "compressed_count": int(s.compressed_count),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def serialize_run(r: TaskRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "run_id": r.id,
        "request_id": r.request_id,
        "project_id": r.project_id,
        "canvas_id": r.canvas_id,
        "session_id": r.session_id,
        "primary_agent": r.primary_agent,
        "agent_profile": STUDIO_AGENTS.get(r.primary_agent),
        "prompt": r.prompt,
        "status": r.status,
        "generation_fence": int(r.generation_fence),
        "lease_owner": r.lease_owner,
        "lease_expires_at": r.lease_expires_at.isoformat() if r.lease_expires_at else None,
        "frozen_tree": r.frozen_tree_json,
        "pending_tool": r.pending_tool_json,
        "recovery_bundle": r.recovery_bundle_json,
        "result": r.result_json,
        "error_message": r.error_message,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


def _finalize_react_run(
    db: Session,
    run: TaskRun,
    session: AgentSession,
    *,
    final_text: str,
    mock: bool,
    usage: Any,
    tool_trace: list[dict[str, Any]],
    created_object_id: Optional[str],
    skill_names: list[str],
) -> dict[str, Any]:
    spec = STUDIO_AGENTS.get(run.primary_agent, STUDIO_AGENTS["orange"])
    now = utcnow()
    tree = dict(run.frozen_tree_json or {})
    for st in tree.get("steps") or []:
        st["status"] = "done"
    run.frozen_tree_json = tree
    run.pending_tool_json = None
    run.status = "done"
    run.result_json = {
        "agent": run.primary_agent,
        "agent_name": spec["name"],
        "text": final_text,
        "mock": mock,
        "usage": usage,
        "tool_trace": tool_trace,
        "created_object_id": created_object_id,
    }
    run.completed_at = now
    run.updated_at = now
    run.recovery_bundle_json = build_recovery_bundle(db, run)
    db.add(run)

    assistant_msg = AgentMessage(
        id=new_prefixed_id("msg"),
        session_id=session.id,
        role="assistant",
        agent_name=run.primary_agent,
        content=final_text,
        skills_json=skill_names,
        context_object_ids_json=[created_object_id] if created_object_id else [],
        tool_calls_json=tool_trace,
        token_estimate=estimate_tokens(final_text),
        compressed=False,
        created_at=now,
    )
    db.add(assistant_msg)
    db.flush()

    event_bus.publish(
        run.project_id,
        run.canvas_id or "",
        "studio.run_completed",
        {"run_id": run.id, "agent": run.primary_agent, "created_object_id": created_object_id},
        actor_id=run.user_id,
    )
    return run.result_json


def _materialize_run_output(
    db: Session,
    run: TaskRun,
    session: AgentSession,
    effective_prompt: str,
    skill_ctx: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = STUDIO_AGENTS.get(run.primary_agent, STUDIO_AGENTS["orange"])
    res = agents.run_task_sync(effective_prompt, agent_hint=spec["legacy_domain"], skills=skill_ctx)

    now = utcnow()
    el = res.get("element") or {"type": "text", "x": 60, "y": 60, "w": 360, "h": 200, "text": res.get("result", "")}
    created_obj = CanvasObject(
        id=new_prefixed_id("obj"),
        project_id=run.project_id,
        canvas_id=run.canvas_id,
        type=el.get("type", "text"),
        name=f"{spec['name']} · 产出",
        x=float(el.get("x", 60)),
        y=float(el.get("y", 60)),
        w=float(el.get("w", 360)),
        h=float(el.get("h", 200)),
        text=str(res.get("result") or ""),
        content_json={"agent": run.primary_agent, "run_id": run.id, "mock": bool(res.get("mock", True))},
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(created_obj)
    db.flush()
    return _finalize_react_run(
        db,
        run,
        session,
        final_text=str(res.get("result") or ""),
        mock=bool(res.get("mock", True)),
        usage=res.get("usage"),
        tool_trace=[{"name": spec["default_tool"], "object_id": created_obj.id}],
        created_object_id=created_obj.id,
        skill_names=[s["name"] for s in skill_ctx],
    )


def run_studio_chat(
    db: Session,
    *,
    project: Project,
    canvas: Canvas,
    user_id: Optional[str],
    prompt: str,
    session_id: Optional[str] = None,
    agent_hint: Optional[str] = None,
    explicit_skills: Optional[list[str]] = None,
    context_object_ids: Optional[list[str]] = None,
    request_id: Optional[str] = None,
    worker_id: str = "studio-host-1",
    enable_tool_loop: bool = False,
) -> dict[str, Any]:
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    req_id = (request_id or "").strip() or uuid.uuid4().hex
    existing_run = db.scalar(select(TaskRun).where(TaskRun.request_id == req_id))
    if existing_run is not None:
        sess = db.get(AgentSession, existing_run.session_id) if existing_run.session_id else None
        return {
            "idempotent_replay": True,
            "session": serialize_session(sess) if sess else None,
            "run": serialize_run(existing_run),
        }

    if session_id:
        session = db.get(AgentSession, session_id)
        if session is None or session.project_id != project.id:
            raise HTTPException(status_code=404, detail=f"studio session not found: {session_id}")
    else:
        now = utcnow()
        session = AgentSession(
            id=new_prefixed_id("ses"),
            project_id=project.id,
            canvas_id=canvas.id,
            user_id=user_id,
            title=prompt.strip()[:48],
            created_at=now,
            updated_at=now,
        )
        db.add(session)
        db.flush()

    skill_names = extract_skill_mentions(prompt, explicit_skills)
    skill_ctx = resolve_enabled_skills(project.id, skill_names)
    graph_ctx = gather_canvas_graph_context(db, project.id, canvas.id, list(context_object_ids or []))

    user_msg = AgentMessage(
        id=new_prefixed_id("msg"),
        session_id=session.id,
        role="user",
        agent_name=None,
        content=prompt.strip(),
        skills_json=skill_names,
        context_object_ids_json=list(context_object_ids or []),
        tool_calls_json=[],
        token_estimate=estimate_tokens(prompt + graph_ctx["prompt_context"]),
        compressed=False,
        created_at=utcnow(),
    )
    db.add(user_msg)
    db.flush()

    did_compress = compress_session_context_if_needed(db, session, project, user_id)

    specialist, steps = _plan_specialist_steps(prompt, agent_hint)
    spec = STUDIO_AGENTS[specialist]
    tool_name = spec["default_tool"]

    effective_prompt = prompt.strip()
    if session.context_summary:
        effective_prompt = f"[历史记忆摘要]\n{session.context_summary}\n\n" + effective_prompt
    if graph_ctx["prompt_context"]:
        effective_prompt += graph_ctx["prompt_context"]

    now = utcnow()
    run = TaskRun(
        id=new_prefixed_id("run"),
        request_id=req_id,
        project_id=project.id,
        canvas_id=canvas.id,
        session_id=session.id,
        user_id=user_id,
        primary_agent=specialist,
        prompt=prompt.strip(),
        status="running",
        generation_fence=1,
        frozen_tree_json={
            "orchestrator": "pecan",
            "specialist": specialist,
            "steps": steps,
            "skills": skill_names,
            "graph_context_nodes": [n["id"] for n in graph_ctx["nodes"]],
        },
        pending_tool_json=None,
        recovery_bundle_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.flush()
    acquire_run_lease(db, run, worker_id)

    # Multi-turn ReAct tool-calling loop when enabled or live mode explicitly invokes tools
    if enable_tool_loop:
        loop_res = run_react_tool_loop(
            db,
            run=run,
            session=session,
            project=project,
            canvas=canvas,
            messages=[{"role": "user", "content": effective_prompt}],
            tool_trace=[],
        )
        if loop_res["status"] == "waiting_approval":
            pending_call = loop_res["pending_call"]
            run.status = "waiting_approval"
            run.pending_tool_json = {
                "mode": "react_tool_call",
                "tool": pending_call["name"],
                "tier": permissions.tier_of(pending_call["name"], project.id),
                "agent": specialist,
                "pending_call": pending_call,
                "messages": loop_res["messages"],
                "tool_trace": loop_res["tool_trace"],
                "skills": skill_names,
            }
            run.recovery_bundle_json = build_recovery_bundle(db, run)
            db.add(run)
            db.commit()
            event_bus.publish(
                project.id,
                canvas.id,
                "studio.approval_required",
                {"run_id": run.id, "tool": pending_call["name"], "agent": specialist},
                actor_id=user_id,
            )
            return {
                "idempotent_replay": False,
                "context_compressed": did_compress,
                "graph_context": graph_ctx,
                "session": serialize_session(session),
                "run": serialize_run(run),
            }
        if loop_res["tool_trace"] or loop_res["content"]:
            created_oid = None
            for tr in reversed(loop_res["tool_trace"]):
                out = tr.get("output") or {}
                if out.get("object_id") or out.get("target_object_id"):
                    created_oid = out.get("object_id") or out.get("target_object_id")
                    break
            _finalize_react_run(
                db,
                run,
                session,
                final_text=loop_res["content"] or "已完成工具协同处理。",
                mock=loop_res["mock"],
                usage=loop_res["usage"],
                tool_trace=loop_res["tool_trace"],
                created_object_id=created_oid,
                skill_names=skill_names,
            )
            db.commit()
            return {
                "idempotent_replay": False,
                "context_compressed": did_compress,
                "graph_context": graph_ctx,
                "session": serialize_session(session),
                "run": serialize_run(run),
            }

    needs_approval = permissions.require_approval(tool_name, project.id)
    if needs_approval:
        run.status = "waiting_approval"
        run.pending_tool_json = {
            "tool": tool_name,
            "tier": permissions.tier_of(tool_name, project.id),
            "agent": specialist,
            "effective_prompt": effective_prompt,
            "skills": skill_ctx,
        }
        run.recovery_bundle_json = build_recovery_bundle(db, run)
        db.add(run)
        db.commit()
        event_bus.publish(
            project.id,
            canvas.id,
            "studio.approval_required",
            {"run_id": run.id, "tool": tool_name, "agent": specialist, "tier": run.pending_tool_json["tier"]},
            actor_id=user_id,
        )
    else:
        _materialize_run_output(db, run, session, effective_prompt, skill_ctx)
        db.commit()

    return {
        "idempotent_replay": False,
        "context_compressed": did_compress,
        "graph_context": graph_ctx,
        "session": serialize_session(session),
        "run": serialize_run(run),
    }


def stream_studio_chat_events(chat_result: dict[str, Any]) -> Generator[str, None, None]:
    """Emit structured SSE frames (session, graph_context, plan, tool cards, token chunks, done)."""
    def _sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield _sse("session", {
        "session": chat_result.get("session"),
        "context_compressed": bool(chat_result.get("context_compressed", False)),
    })
    if chat_result.get("graph_context"):
        yield _sse("graph_context", chat_result["graph_context"])

    run = chat_result["run"]
    yield _sse("plan", {"run_id": run["id"], "primary_agent": run["primary_agent"], "frozen_tree": run["frozen_tree"]})

    res = run.get("result") or {}
    for tc in res.get("tool_trace") or []:
        yield _sse("tool_result", tc)

    if run["status"] == "waiting_approval":
        yield _sse("approval_required", {
            "run_id": run["id"],
            "pending_tool": run.get("pending_tool"),
            "generation_fence": run["generation_fence"],
        })
    else:
        text = str(res.get("text") or "")
        chunk_size = 24
        for i in range(0, len(text), chunk_size):
            yield _sse("token", {"delta": text[i: i + chunk_size]})

    yield _sse("done", {"run": run, "session": chat_result.get("session")})


def approve_task_run(
    db: Session,
    run: TaskRun,
    *,
    expected_fence: Optional[int] = None,
    grant_session_tool: bool = False,
) -> dict[str, Any]:
    if expected_fence is not None:
        assert_generation_fence(run, expected_fence)
    if run.status != "waiting_approval":
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not waiting_approval")
    pending = run.pending_tool_json or {}
    tool_name = pending.get("tool")
    if grant_session_tool and tool_name and permissions.tier_of(tool_name, run.project_id) == "session":
        permissions.grant_tool(run.project_id, tool_name)

    session = db.get(AgentSession, run.session_id)
    assert session is not None
    acquire_run_lease(db, run, run.lease_owner or "approval-resumer")

    if pending.get("mode") == "react_tool_call":
        project = db.get(Project, run.project_id)
        canvas = db.get(Canvas, run.canvas_id)
        assert project is not None and canvas is not None
        loop_res = run_react_tool_loop(
            db,
            run=run,
            session=session,
            project=project,
            canvas=canvas,
            messages=pending.get("messages") or [],
            tool_trace=pending.get("tool_trace") or [],
            preapproved_call=pending.get("pending_call"),
        )
        if loop_res["status"] == "waiting_approval":
            next_call = loop_res["pending_call"]
            run.status = "waiting_approval"
            run.pending_tool_json = {
                "mode": "react_tool_call",
                "tool": next_call["name"],
                "tier": permissions.tier_of(next_call["name"], project.id),
                "agent": run.primary_agent,
                "pending_call": next_call,
                "messages": loop_res["messages"],
                "tool_trace": loop_res["tool_trace"],
                "skills": pending.get("skills") or [],
            }
            run.recovery_bundle_json = build_recovery_bundle(db, run)
            db.add(run)
            db.commit()
            return serialize_run(run)

        created_oid = None
        for tr in reversed(loop_res["tool_trace"]):
            out = tr.get("output") or {}
            if out.get("object_id") or out.get("target_object_id"):
                created_oid = out.get("object_id") or out.get("target_object_id")
                break
        _finalize_react_run(
            db,
            run,
            session,
            final_text=loop_res["content"] or "审批通过，已完成工具执行。",
            mock=loop_res["mock"],
            usage=loop_res["usage"],
            tool_trace=loop_res["tool_trace"],
            created_object_id=created_oid,
            skill_names=pending.get("skills") or [],
        )
        db.commit()
        return serialize_run(run)

    _materialize_run_output(
        db,
        run,
        session,
        pending.get("effective_prompt") or run.prompt,
        pending.get("skills") or [],
    )
    db.commit()
    return serialize_run(run)


def reject_task_run(
    db: Session,
    run: TaskRun,
    *,
    reason: str = "已驳回：人工拒绝",
    expected_fence: Optional[int] = None,
) -> dict[str, Any]:
    if expected_fence is not None:
        assert_generation_fence(run, expected_fence)
    if run.status not in ("waiting_approval", "running", "pending"):
        raise HTTPException(status_code=409, detail=f"run is {run.status}, cannot reject")
    run.status = "failed"
    run.error_message = reason
    run.pending_tool_json = None
    run.completed_at = utcnow()
    run.updated_at = run.completed_at
    run.recovery_bundle_json = build_recovery_bundle(db, run)
    db.add(run)
    db.commit()
    return serialize_run(run)
