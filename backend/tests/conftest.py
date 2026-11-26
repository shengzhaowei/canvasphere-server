"""Pytest fixtures: preserve legacy X-Role compatibility for pre-P0 tests while
providing strict-JWT fixtures for all new P0+ tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import auth, db, permissions, store  # noqa: E402
from app.main import app  # noqa: E402

_P0_STRICT_MODULES = {
    "test_auth_and_profile.py",
    "test_projects_rbac_canvases.py",
    "test_storage_and_persistence.py",
}


@pytest.fixture(autouse=True)
def _p0_test_isolation(request, monkeypatch, tmp_path):
    """Isolate storage directory, email sink, and legacy auth header flag."""
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-canvasphere-p0-unit-tests-32b")
    filename = Path(str(request.fspath)).name
    if filename in _P0_STRICT_MODULES:
        monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
    else:
        monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "true")

    auth.clear_email_test_sink()
    db.init_db()
    yield
    auth.clear_email_test_sink()


@pytest.fixture()
def strict_client(monkeypatch, tmp_path):
    """TestClient running in strict JWT production mode with fresh DB & storage."""
    monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
    store.reset_store()
    permissions.reset_approval_state()
    auth.clear_email_test_sink()
    client = TestClient(app)
    yield client
    store.reset_store()
    permissions.reset_approval_state()
    auth.clear_email_test_sink()
