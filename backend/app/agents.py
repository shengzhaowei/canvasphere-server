"""LangGraph Supervisor + 4 domain agents: story / visual / video / audio.

Routing is keyword/intent based (see route_agent). Each agent node
either calls a real LLM once (ChatOpenAI, OpenAI-compatible, only when
LLM_API_KEY is set) or returns a mock placeholder product labelled
mock:true. Token usage / cost is NEVER invented: live runs report only
what the provider returned, mock runs report nothing.
Approval: visual and video agents set approval_required=True.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

AGENTS = ("story", "visual", "video", "audio")

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "story": ("剧本", "故事", "剧情", "台词", "大纲", "小说", "story", "script", "plot"),
    "visual": ("图", "视觉", "画面", "海报", "插画", "封面", "image", "picture", "visual", "poster"),
    "video": ("分镜", "视频", "短片", "镜头", "video", "storyboard", "shot"),
    "audio": ("音乐", "声音", "配乐", "音效", "旁白", "audio", "music", "sound"),
}


def _skill_context(skills: Optional[list[dict[str, Any]]]) -> str:
    """One line per enabled skill: name + one-sentence description.

    Only names + descriptions go into the prompt; full skill bodies
    are never pasted into the prompt.
    """
    if not skills:
        return ""
    lines = []
    for s in skills:
        if isinstance(s, str):
            lines.append(f"- {s}")
        elif isinstance(s, dict):
            desc = (s.get("description") or "").strip().splitlines()
            lines.append(f"- {s.get('name', '')}: {desc[0] if desc else ''}".rstrip(": "))
    if not lines:
        return ""
    return "\n\n[已启用 Skill（仅名字+一句话描述）]\n" + "\n".join(lines)


def route_agent(prompt: str, hint: Optional[str] = None) -> str:
    """Supervisor routing. Explicit hint wins if it names a known agent."""
    if hint and hint.strip().lower() in AGENTS:
        return hint.strip().lower()
    text = prompt or ""
    for agent in ("video", "visual", "audio", "story"):
        if any(k in text for k in _KEYWORDS[agent]):
            return agent
    return "story"


def is_live_mode() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def _mock_product(agent: str, prompt: str) -> dict[str, Any]:
    labels = {
        "story": ("故事/剧本", "text"),
        "visual": ("视觉/图片", "image"),
        "video": ("视频/分镜", "video"),
        "audio": ("音乐/声音", "audio"),
    }
    label, el_type = labels[agent]
    text = "【mock】" + label + "占位产物：" + prompt
    return {
        "agent": agent,
        "result": text,
        "text": text,
        "mock": True,
        "approval_required": agent in ("visual", "video"),
        "element": {"type": el_type, "x": 40, "y": 40, "w": 320, "h": 180, "text": text},
        "usage": None,
    }


def _live_product(agent: str, prompt: str) -> dict[str, Any]:
    """One real LLM call. Any failure degrades to mock (labelled)."""
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            base_url=os.environ.get("LLM_BASE_URL") or None,
            api_key=os.environ["LLM_API_KEY"],
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            timeout=30,
        )
        msg = llm.invoke(
            "你是 Canvasphere 的 " + agent + " 领域 Agent。"
            "针对以下需求给出简短可直接用的产出（不超过 300 字）：" + prompt)
        content = getattr(msg, "content", "") or ""
        usage = None
        meta = getattr(msg, "response_metadata", {}) or {}
        if isinstance(meta.get("token_usage"), dict):
            usage = meta["token_usage"]
        text = str(content).strip() or ("（模型返回为空，需求：" + prompt + "）")
        el_type = {"story": "text", "visual": "image", "video": "video", "audio": "audio"}[agent]
        return {
            "agent": agent, "result": text, "text": text, "mock": False,
            "approval_required": agent in ("visual", "video"),
            "element": {"type": el_type, "x": 40, "y": 40, "w": 320, "h": 180, "text": text[:200]},
            "usage": usage,
        }
    except Exception as exc:
        logger.warning("LLM call failed (%s); degrading to mock", exc)
        out = _mock_product(agent, prompt)
        out["llm_error"] = str(exc)
        return out


class AgentState(TypedDict, total=False):
    prompt: str
    agent: str
    result: str
    mock: bool
    approval_required: bool
    element: dict[str, Any]
    usage: Any


def build_graph():
    """Supervisor graph: supervisor -> one domain agent -> END."""
    from langgraph.graph import END, StateGraph
    from app.checkpoint import get_checkpointer

    def supervisor(state: AgentState) -> AgentState:
        # run_task_sync already routed on the ORIGINAL prompt (with the
        # explicit hint); respect that preset. Only route here when no
        # preset exists (e.g. direct graph use), so skill descriptions
        # appended to the prompt can never re-route the task — a skill
        # named "storyboard" used to flip a story task to video because
        # its description contains "分镜".
        preset = state.get("agent")
        if preset in AGENTS:
            return {"agent": preset}
        return {"agent": route_agent(state["prompt"])}

    def make_agent(agent: str):
        def _node(state: AgentState) -> AgentState:
            product = _live_product(agent, state["prompt"]) if is_live_mode() else _mock_product(agent, state["prompt"])
            return {"agent": agent, "result": product["result"],
                    "mock": product["mock"],
                    "approval_required": product["approval_required"],
                    "element": product["element"],
                    "usage": product.get("usage")}
        return _node

    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor)
    for agent in AGENTS:
        graph.add_node(agent, make_agent(agent))
    graph.set_entry_point("supervisor")
    graph.add_conditional_edges("supervisor", lambda s: s.get("agent", "story"), {a: a for a in AGENTS})
    for agent in AGENTS:
        graph.add_edge(agent, END)
    checkpointer, _kind = get_checkpointer()
    return graph.compile(checkpointer=checkpointer)


def run_task_sync(prompt: str, agent_hint: Optional[str] = None,
                  skills: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Run the graph synchronously; fall back to direct agent on graph failure.

    `skills` is a list of {name, description} for enabled skills; only
    names + one-line descriptions are appended to the prompt context.
    """
    agent = route_agent(prompt, agent_hint)
    effective_prompt = prompt + _skill_context(skills)
    try:
        graph = build_graph()
        out = graph.invoke({"prompt": effective_prompt, "agent": agent}, config={"configurable": {"thread_id": "task-" + uuid.uuid4().hex}})
        product_agent = out.get("agent", agent)
        text = out.get("result", "")
        return {
            "agent": product_agent, "result": text, "text": text,
            "mock": bool(out.get("mock", not is_live_mode())),
            "approval_required": bool(out.get("approval_required", False)),
            "element": out.get("element") or _mock_product(product_agent, effective_prompt)["element"],
            "usage": out.get("usage"),
        }
    except Exception as exc:
        logger.warning("LangGraph run failed (%s); direct agent fallback", exc)
        return _live_product(agent, effective_prompt) if is_live_mode() else _mock_product(agent, effective_prompt)


async def run_task(prompt: str, agent_hint: Optional[str] = None,
                   skills: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return await asyncio.to_thread(run_task_sync, prompt, agent_hint, skills)
