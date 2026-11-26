import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import checkpoint  # noqa: E402


def test_save_load_roundtrip_memory():
    os.environ.pop("DATABASE_URL", None)
    checkpoint.reset_checkpointer_cache()
    state = {"task_id": "t1", "status": "done", "result": "ok", "events": []}
    backend = checkpoint.save_task_state("t1", state)
    assert backend == "memory"  # no DATABASE_URL in this test env
    loaded = checkpoint.load_task_state("t1")
    assert loaded == state
    assert checkpoint.load_task_state("never-saved") is None


def test_memorysaver_fallback_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    checkpoint.reset_checkpointer_cache()
    cp, kind = checkpoint.get_checkpointer()
    assert kind == "memory"
    from langgraph.checkpoint.memory import MemorySaver
    assert isinstance(cp, MemorySaver)
    # second call returns the cached instance
    cp2, kind2 = checkpoint.get_checkpointer()
    assert cp2 is cp and kind2 == "memory"
    checkpoint.reset_checkpointer_cache()


def test_memorysaver_fallback_on_bad_database_url(monkeypatch):
    # Unreachable Postgres must fall back to memory, not raise.
    monkeypatch.setenv("DATABASE_URL", "postgres://bad:bad@127.0.0.1:1/canvas")
    checkpoint.reset_checkpointer_cache()
    try:
        cp, kind = checkpoint.get_checkpointer()
        assert kind == "memory"
    finally:
        checkpoint.reset_checkpointer_cache()
        monkeypatch.delenv("DATABASE_URL", raising=False)


def test_postgres_saver_built_from_pool_not_contextmanager(monkeypatch):
    """Regression test for the from_conn_string bug: in
    langgraph-checkpoint-postgres 3.2.1, from_conn_string() returns a
    @contextmanager, not a saver, so .setup() on it raises
    AttributeError. The fix must construct PostgresSaver(pool)
    directly and call setup() on the instance -- verified here with
    fakes, no live Postgres needed."""
    import langgraph.checkpoint.postgres as pg_mod
    import psycopg
    import psycopg_pool

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn())

    calls: dict[str, object] = {}

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            calls["conninfo"] = conninfo
            calls["pool_kwargs"] = kwargs
            self.closed = False

        def close(self):
            self.closed = True

    class FakeSaver:
        def __init__(self, conn):
            calls["saver_conn"] = conn
            self.setup_called = False

        def setup(self):
            self.setup_called = True
            calls["setup_called"] = True

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    monkeypatch.setattr(pg_mod, "PostgresSaver", FakeSaver)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@127.0.0.1:5432/canvas")
    checkpoint.reset_checkpointer_cache()
    try:
        cp, kind = checkpoint.get_checkpointer()
        assert kind == "postgres"
        assert isinstance(cp, FakeSaver)
        assert calls["setup_called"] is True
        assert isinstance(calls["saver_conn"], FakePool)
        assert calls["conninfo"] == "postgres://u:p@127.0.0.1:5432/canvas"
        # the pool is held for the saver lifetime, not closed
        assert calls["saver_conn"].closed is False
        # second call returns the cached instance
        cp2, kind2 = checkpoint.get_checkpointer()
        assert cp2 is cp and kind2 == "postgres"
    finally:
        checkpoint.reset_checkpointer_cache()
        monkeypatch.delenv("DATABASE_URL", raising=False)
    # reset closes the pool
    assert calls["saver_conn"].closed is True


def test_postgres_pool_closed_on_setup_failure(monkeypatch):
    """If setup() fails (Postgres unreachable after pool creation),
    the pool must be closed and the checkpointer must fall back."""
    import langgraph.checkpoint.postgres as pg_mod
    import psycopg
    import psycopg_pool

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn())

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            self.closed = False

        def close(self):
            self.closed = True

    pool_holder: dict[str, FakePool] = {}

    class FailingSaver:
        def __init__(self, conn):
            pool_holder["pool"] = conn

        def setup(self):
            raise ConnectionError("could not connect")

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    monkeypatch.setattr(pg_mod, "PostgresSaver", FailingSaver)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@127.0.0.1:5432/canvas")
    checkpoint.reset_checkpointer_cache()
    try:
        cp, kind = checkpoint.get_checkpointer()
        assert kind == "memory"
        assert pool_holder["pool"].closed is True
    finally:
        checkpoint.reset_checkpointer_cache()
        monkeypatch.delenv("DATABASE_URL", raising=False)
