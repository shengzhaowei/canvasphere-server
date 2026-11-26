"""LangGraph checkpointer: Postgres when DATABASE_URL works, else MemorySaver.

Also provides save_task_state / load_task_state for Session Host
restart recovery: Postgres-backed table when a live connection is
available, otherwise an in-memory dict (explicitly logged as such,
so a restart without Postgres really does lose task state).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_memory_states: dict[str, dict[str, Any]] = {}
_checkpointer: Any = None
_checkpointer_kind: Optional[str] = None
_checkpointer_pool: Any = None  # psycopg_pool ConnectionPool, if any


def _make_postgres_saver(url: str) -> Any:
    """Build a long-lived PostgresSaver for DATABASE_URL.

    Why not PostgresSaver.from_conn_string(url):
    in langgraph-checkpoint-postgres 3.2.1 that classmethod is a
    @contextmanager -- it returns a _GeneratorContextManager, not a
    saver, and the yielded saver dies when the `with` block exits
    (the underlying Connection is closed on exit). Calling .setup()
    on it raises AttributeError and the old code silently fell back
    to memory even when Postgres was reachable.

    The fix constructs the saver directly from a ConnectionPool:
    PostgresSaver.__init__ accepts a Connection | ConnectionPool
    (see langgraph/checkpoint/postgres/__init__.py and
    _internal.get_connection, which checks a connection out of the
    pool per operation). The pool stays open for the process
    lifetime, so the cached saver stays usable after this function
    returns. kwargs mirror from_conn_string (autocommit,
    prepare_threshold=0, dict_row).
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # Fast pre-flight probe: a ConnectionPool takes seconds to give up
    # on an unreachable host (worker retries + checkout timeout), so
    # fail fast with one short direct connect first. A refused /
    # unroutable host raises here in ~0-2s instead of ~15s.
    import psycopg
    probe = psycopg.connect(url, connect_timeout=2, autocommit=True)
    probe.close()

    pool = ConnectionPool(
        url, min_size=1, max_size=4, timeout=5, open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0,
                "row_factory": dict_row})
    try:
        saver = PostgresSaver(pool)
        saver.setup()  # raises promptly if Postgres is unreachable
    except Exception:
        pool.close()
        raise
    global _checkpointer_pool
    _checkpointer_pool = pool
    return saver


def get_checkpointer() -> tuple[Any, str]:
    """Return (checkpointer, kind) where kind is 'postgres' or 'memory'.

    Cached after first call. No DATABASE_URL, a failed connection, or
    a missing langgraph-checkpoint-postgres install all fall back to
    MemorySaver, and the fallback is logged plainly.
    """
    global _checkpointer, _checkpointer_kind
    if _checkpointer is not None:
        return _checkpointer, _checkpointer_kind or "memory"

    from langgraph.checkpoint.memory import MemorySaver

    url = os.environ.get("DATABASE_URL")
    if url:
        try:
            saver = _make_postgres_saver(url)
            _checkpointer, _checkpointer_kind = saver, "postgres"
            logger.info("checkpointer: Postgres (%s)", url.split("@")[-1])
            return _checkpointer, "postgres"
        except Exception as exc:  # noqa: BLE001 - fallback is the point
            logger.warning(
                "Postgres checkpointer unavailable (%s); "
                "falling back to in-memory MemorySaver", exc)
    else:
        logger.info("no DATABASE_URL set; using in-memory MemorySaver checkpointer")
    _checkpointer = MemorySaver()
    _checkpointer_kind = "memory"
    return _checkpointer, "memory"


def reset_checkpointer_cache() -> None:
    global _checkpointer, _checkpointer_kind, _checkpointer_pool
    _checkpointer, _checkpointer_kind = None, None
    if _checkpointer_pool is not None:
        try:
            _checkpointer_pool.close()
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass
        _checkpointer_pool = None


def _postgres_conn():
    """psycopg connection to DATABASE_URL, or None if unusable."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg
        conn = psycopg.connect(url, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS task_checkpoints ("
                " task_id TEXT PRIMARY KEY,"
                " state JSONB NOT NULL,"
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        return conn
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres task checkpoint unavailable: %s", exc)
        return None


def save_task_state(task_id: str, state: dict[str, Any]) -> str:
    """Persist one task's state. Returns 'postgres' or 'memory'."""
    conn = _postgres_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_checkpoints (task_id, state) "
                    "VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (task_id) DO UPDATE SET state = EXCLUDED.state, "
                    "updated_at = now()",
                    (task_id, json.dumps(state)))
            return "postgres"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Postgres save_task_state failed (%s); using memory", exc)
        finally:
            conn.close()
    _memory_states[task_id] = dict(state)
    return "memory"


def load_task_state(task_id: str) -> Optional[dict[str, Any]]:
    """Load one task's state, or None if never saved."""
    conn = _postgres_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM task_checkpoints WHERE task_id = %s",
                    (task_id,))
                row = cur.fetchone()
            if row is not None:
                return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Postgres load_task_state failed (%s); using memory", exc)
        finally:
            conn.close()
    state = _memory_states.get(task_id)
    return dict(state) if state is not None else None
