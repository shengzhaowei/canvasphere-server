"""SQLAlchemy 2.0 database engine and session management.

Dual-engine design:
- Production (DATABASE_URL=postgresql://...): PostgreSQL via psycopg v3
  (normalized from postgresql:// -> postgresql+psycopg://).
- Local tests / CI (DATABASE_URL unset or sqlite://...): SQLite in-memory
  with StaticPool so all connections share one DB cleanly.

Automatically detects DATABASE_URL changes so tests can switch between
in-memory SQLite and file-backed SQLite / PostgreSQL seamlessly.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Optional[Engine] = None
_engine_url: Optional[str] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def _normalize_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return "sqlite:///:memory:"
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {
            "connect_args": {"check_same_thread": False},
            "future": True,
        }
        if url in ("sqlite:///:memory:", "sqlite://"):
            kwargs["poolclass"] = StaticPool
        eng = create_engine(url, **kwargs)

        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return eng
    settings = get_settings()
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
        future=True,
    )


def lock_for_update(stmt, db: Session):
    """Apply SELECT ... FOR UPDATE on PostgreSQL; safe no-op on SQLite."""
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        return stmt.with_for_update()
    return stmt


def get_engine() -> Engine:
    global _engine, _engine_url, _SessionLocal
    target = _normalize_url(get_settings().database_url)
    if _engine is None or _engine_url != target:
        if _engine is not None:
            _engine.dispose()
        _engine = _build_engine(target)
        _engine_url = target
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
        # Ensure tables exist for SQLite / auto-init environments
        import app.models  # noqa: F401
        Base.metadata.create_all(bind=_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=get_engine())


def reset_engine() -> None:
    global _engine, _engine_url, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _SessionLocal = None


def db_kind() -> str:
    url = _normalize_url(get_settings().database_url)
    return "postgres" if url.startswith("postgresql") else "sqlite"
