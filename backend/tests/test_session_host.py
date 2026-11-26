import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.session_host import SessionHost, TaskStateMachine  # noqa: E402


def test_full_flow_pending_running_done():
    m = TaskStateMachine(task_id="t1")
    assert m.status == "pending"
    m.transition("running")
    m.transition("done")
    assert m.status == "done"
    assert [e["detail"]["to"] for e in m.events if e["type"] == "transition"] == ["running", "done"]


def test_approval_flow():
    m = TaskStateMachine(task_id="t2")
    m.transition("running")
    m.transition("waiting_approval")
    m.transition("done")
    assert m.status == "done"


def test_illegal_transitions_rejected():
    m = TaskStateMachine(task_id="t3")
    with pytest.raises(ValueError):
        m.transition("done")  # pending -> done is illegal
    with pytest.raises(ValueError):
        m.transition("waiting_approval")  # pending -> waiting_approval illegal
    m.transition("running")
    m.transition("done")
    with pytest.raises(ValueError):
        m.transition("running")  # terminal: no outgoing transitions
    with pytest.raises(ValueError):
        m.transition("bogus-status")


def test_append_event_records():
    m = TaskStateMachine(task_id="t4")
    ev = m.append_event("note", {"x": 1})
    assert ev["type"] == "note" and ev["task_id"] == "t4"
    assert len(m.events) == 1


def test_restore_from_checkpoint():
    host = SessionHost()
    restored = host.restore_from_checkpoint({
        "a": {"task_id": "a", "status": "done", "result": "ok", "events": []},
        "b": {"task_id": "b", "status": "waiting_approval", "result": None, "events": []},
    })
    assert restored == ["a", "b"]
    assert host.get("a").status == "done"
    assert host.get("a").result == "ok"
    # in-flight task is restored as-is, flagged in its event log, not faked done
    assert host.get("b").status == "waiting_approval"
    assert any(e["type"] == "restored" for e in host.get("b").events)
    # restored approval task can still complete
    host.get("b").transition("done")
    assert host.get("b").status == "done"
    with pytest.raises(ValueError):
        host.restore_from_checkpoint({"not-a-dict": "x"})
    with pytest.raises(ValueError):
        host.restore_from_checkpoint("nope")


def test_host_execute_async_success_and_failure():
    host = SessionHost()
    host.start()
    try:
        async def ok_handler():
            return {"approval_required": False, "result": "done!"}
        res = asyncio.run(host.execute("e1", ok_handler))
        assert res["result"] == "done!"
        assert host.get("e1").status == "done"

        async def gated():
            return {"approval_required": True}
        asyncio.run(host.execute("e2", gated))
        assert host.get("e2").status == "waiting_approval"

        async def boom():
            raise RuntimeError("kaboom")
        with pytest.raises(RuntimeError):
            asyncio.run(host.execute("e3", boom))
        assert host.get("e3").status == "failed"
    finally:
        host.stop()
        assert host.running is False


def test_host_schedule_requires_start():
    host = SessionHost()
    with pytest.raises(RuntimeError):
        host.schedule("s1", lambda: None)


def test_events_single_source_host_log_is_truth():
    """SessionHost.events is the only event source: transitions made
    via host.transition() land in host.events exactly once, and
    events_for() returns them in order with no duplicates."""
    host = SessionHost()
    host.start()
    try:
        host.create("t-ev")
        host.transition("t-ev", "running")
        host.transition("t-ev", "done")
        host.append_event("t-ev", "agent_finished", {"mock": True})
        host_events = [e for e in host.events if e["task_id"] == "t-ev"]
        via_for = host.events_for("t-ev")
        assert via_for == host_events
        # no duplicate event objects / keys
        keys = [(e["type"], e["at"], repr(e["detail"])) for e in via_for]
        assert len(keys) == len(set(keys))
        # transitions are in the host log too (not machine-only)
        assert [e["type"] for e in via_for].count("transition") == 2
        assert via_for[0]["type"] == "created"
        assert via_for[-1]["type"] == "agent_finished"
        # machine mirror holds the same objects, not copies
        assert host.get("t-ev").events[0] is via_for[0]
    finally:
        host.stop()


def test_reset_for_retry_only_failed():
    host = SessionHost()
    host.start()
    try:
        host.create("t-re")
        host.transition("t-re", "running")
        with pytest.raises(ValueError):
            host.reset_for_retry("t-re")  # running, not failed
        host.transition("t-re", "failed", {"error": "boom"})
        assert host.reset_for_retry("t-re") == "pending"
        assert host.get("t-re").status == "pending"
        assert any(e["type"] == "retry" for e in host.events_for("t-re"))
        host.transition("t-re", "running")
        host.transition("t-re", "done")
        assert host.get("t-re").status == "done"
    finally:
        host.stop()
