"""Session Host: task state machine, async scheduling, lifecycle, restore.

States: pending -> running -> waiting_approval -> done / failed
        pending -> running -> done / failed
        (waiting_approval -> done on approve, -> failed on reject)

TaskStateMachine is pure logic with no I/O, directly unit-testable.
SessionHost adds asyncio scheduling, an event log (append_event),
start/stop lifecycle, and restore_from_checkpoint(state) for restart
recovery (state produced by checkpoint.save_task_state / snapshot()).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"waiting_approval", "done", "failed"}),
    "waiting_approval": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}

TERMINAL = frozenset({"done", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskStateMachine:
    """Pure state machine for one task. No I/O, no store dependency."""

    task_id: str
    status: str = "pending"
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None

    def __post_init__(self) -> None:
        if self.status not in TRANSITIONS:
            raise ValueError(f"unknown status: {self.status}")

    def append_event(self, event_type: str, detail: Any = None) -> dict[str, Any]:
        ev = {"task_id": self.task_id, "type": event_type,
              "detail": detail, "at": _now()}
        self.events.append(ev)
        return ev

    def can_transition(self, to: str) -> bool:
        return to in TRANSITIONS.get(self.status, frozenset())

    def transition(self, to: str, detail: Any = None) -> str:
        if to not in TRANSITIONS:
            raise ValueError(f"unknown target status: {to}")
        if not self.can_transition(to):
            raise ValueError(
                f"illegal transition: {self.status} -> {to} "
                f"(task {self.task_id})")
        old = self.status
        self.status = to
        self.append_event("transition", {"from": old, "to": to, "detail": detail})
        return self.status

    def snapshot(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status,
                "events": list(self.events), "result": self.result}

    @classmethod
    def restore(cls, state: dict[str, Any]) -> "TaskStateMachine":
        if not isinstance(state, dict) or "task_id" not in state:
            raise ValueError("checkpoint state must be a dict with 'task_id'")
        status = state.get("status", "pending")
        if status not in TRANSITIONS:
            raise ValueError(f"checkpoint has unknown status: {status}")
        m = cls(task_id=state["task_id"], status=status,
                result=state.get("result"))
        m.events = list(state.get("events") or [])
        # A task found mid-flight after a restart cannot be resumed
        # mid-step: mark it failed explicitly instead of pretending.
        if status in ("running", "waiting_approval"):
            m.append_event("restored",
                           {"note": "restarted while in-flight; "
                                    "left in restored status for operator review"})
        else:
            m.append_event("restored", None)
        return m


class SessionHost:
    """Owns TaskStateMachines, runs handlers async, keeps an event log."""

    def __init__(self) -> None:
        self.machines: dict[str, TaskStateMachine] = {}
        self._asyncio_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self.events: list[dict[str, Any]] = []

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._running = True
        logger.info("session host started")

    def stop(self) -> None:
        self._running = False
        for t in list(self._asyncio_tasks.values()):
            if not t.done():
                t.cancel()
        self._asyncio_tasks.clear()
        logger.info("session host stopped")

    @property
    def running(self) -> bool:
        return self._running

    # -- events ---------------------------------------------------------
    # Single source of truth: SessionHost.events. Machine-local
    # `events` lists are mirrors of the same event objects (appended
    # by reference), never a second source -- API responses read only
    # self.events, filtered by task_id and deduped by identity.
    def append_event(self, task_id: str, event_type: str,
                     detail: Any = None) -> dict[str, Any]:
        ev = {"task_id": task_id, "type": event_type,
              "detail": detail, "at": _now()}
        self.events.append(ev)
        m = self.machines.get(task_id)
        if m is not None:
            m.events.append(ev)
        return ev

    def events_for(self, task_id: str) -> list[dict[str, Any]]:
        """All events for one task, in order, no duplicates."""
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for ev in self.events:
            if ev.get("task_id") != task_id:
                continue
            if id(ev) in seen:
                continue
            seen.add(id(ev))
            out.append(ev)
        return out

    def _sync_machine_events(self, task_id: str) -> None:
        """Copy any machine-local events (e.g. transitions) into the
        host log. Same event objects, so no duplicates by identity."""
        m = self.machines.get(task_id)
        if m is None:
            return
        for ev in m.events:
            if not any(existing is ev for existing in self.events):
                self.events.append(ev)

    # -- tasks ----------------------------------------------------------
    def create(self, task_id: str) -> TaskStateMachine:
        if task_id in self.machines:
            raise ValueError(f"task already exists: {task_id}")
        m = TaskStateMachine(task_id=task_id)
        self.machines[task_id] = m
        self.append_event(task_id, "created")
        return m

    def get(self, task_id: str) -> Optional[TaskStateMachine]:
        return self.machines.get(task_id)

    def transition(self, task_id: str, to: str, detail: Any = None) -> str:
        m = self.machines.get(task_id)
        if m is None:
            raise KeyError(f"unknown task: {task_id}")
        status = m.transition(to, detail)
        self._sync_machine_events(task_id)
        return status

    def reset_for_retry(self, task_id: str) -> str:
        """Re-arm a failed task's machine for a retry run.

        Terminal states have no outgoing transitions, so a retry
        resets the machine to pending and records a `retry` event in
        the single event log. History is kept, not wiped.
        """
        m = self.machines.get(task_id)
        if m is None:
            raise KeyError(f"unknown task: {task_id}")
        if m.status != "failed":
            raise ValueError(
                f"only a failed task can be reset for retry "
                f"(task {task_id} is {m.status})")
        m.status = "pending"
        self.append_event(task_id, "retry", {"from": "failed", "to": "pending"})
        return m.status

    def snapshot(self) -> dict[str, Any]:
        return {tid: m.snapshot() for tid, m in self.machines.items()}

    def restore_from_checkpoint(self, state: dict[str, Any]) -> list[str]:
        """Restore machines from a checkpoint snapshot.

        `state` is {task_id: task_state_dict} (as produced by snapshot()
        / checkpoint.save_task_state for a batch). Returns restored ids.
        Running tasks are restored in their recorded status and flagged
        in the event log; they are NOT silently marked done.
        """
        if not isinstance(state, dict):
            raise ValueError("checkpoint state must be a dict")
        restored = []
        for task_id, task_state in state.items():
            if not isinstance(task_state, dict):
                raise ValueError(
                    f"checkpoint entry for task {task_id!r} must be a dict")
            task_state = dict(task_state)
            task_state.setdefault("task_id", task_id)
            self.machines[task_id] = TaskStateMachine.restore(task_state)
            self._sync_machine_events(task_id)
            restored.append(task_id)
        logger.info("restored %d task(s) from checkpoint", len(restored))
        return restored

    async def execute(self, task_id: str,
                      handler: Callable[[], Any]) -> Any:
        """Run an async handler for a task through the state machine.

        handler() returns a result dict; if it sets approval_required
        the task waits for approval, otherwise it completes. Exceptions
        mark the task failed (and are re-raised so callers see them).
        """
        m = self.machines.get(task_id) or self.create(task_id)
        if m.status == "pending":
            self.transition(task_id, "running")
        self.append_event(task_id, "started")
        try:
            result = handler()
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - record, then re-raise
            if m.status in ("pending", "running"):
                self.transition(task_id, "failed", {"error": str(exc)})
            self.append_event(task_id, "failed", {"error": str(exc)})
            raise
        m.result = result
        if isinstance(result, dict) and result.get("approval_required"):
            self.transition(task_id, "waiting_approval")
            self.append_event(task_id, "waiting_approval")
        else:
            self.transition(task_id, "done")
            self.append_event(task_id, "done")
        return result

    def schedule(self, task_id: str, handler: Callable[[], Any]) -> asyncio.Task:
        """Schedule execute() as a background asyncio task."""
        if not self._running:
            raise RuntimeError("session host is not started")
        if task_id in self._asyncio_tasks and not self._asyncio_tasks[task_id].done():
            raise ValueError(f"task already scheduled: {task_id}")
        t = asyncio.create_task(self.execute(task_id, handler))
        self._asyncio_tasks[task_id] = t
        return t
