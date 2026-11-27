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

from app import auth, db, observability, permissions, realtime, redis_client, sandbox, store  # noqa: E402
from app.main import app  # noqa: E402

_P0_STRICT_MODULES = {
    "test_auth_and_profile.py",
    "test_projects_rbac_canvases.py",
    "test_storage_and_persistence.py",
    "test_canvas_objects_leases_sse.py",
    "test_billing_wallet_redeem.py",
    "test_multimodal_generations.py",
    "test_studio_agents_and_runs.py",
    "test_sandbox_lifecycle_and_billing.py",
    "test_asset_tools_and_alembic_p2.py",
    "test_skills_upload_and_install.py",
    "test_mcp_dual_oauth_dcr.py",
    "test_byok_and_alembic_p3.py",
    "test_p4_production_hardening.py",
    "test_p5_security_admin_providers.py",
    "test_p6_react_ffmpeg_keepalive_obs.py",
    "test_p7_ops_readiness_and_cli.py",
    "test_p8_frontend_p0_alignment.py",
    "test_p9_snapshots_comments_shares_skills.py",
}


@pytest.fixture(autouse=True)
def _p0_test_isolation(request, monkeypatch, tmp_path):
    """Isolate storage directory, email sink, realtime/rate-limit state, and legacy auth header flag."""
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-canvasphere-p0-unit-tests-32b")
    monkeypatch.delenv("MODEL_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("ASYNC_WORKER_ENABLED", "false")
    monkeypatch.setattr(sandbox, "ensure_keepalive_container", lambda *a, **kw: None)
    monkeypatch.setattr(sandbox, "destroy_container", lambda *a, **kw: False)
    filename = Path(str(request.fspath)).name
    if filename in _P0_STRICT_MODULES:
        monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
    else:
        monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "true")

    auth.clear_email_test_sink()
    realtime.reset_realtime_state()
    redis_client.rate_limiter.reset()
    redis_client.reset_redis_clients()
    observability.metrics_registry.reset()
    db.init_db()
    yield
    auth.clear_email_test_sink()
    realtime.reset_realtime_state()
    redis_client.rate_limiter.reset()
    redis_client.reset_redis_clients()
    observability.metrics_registry.reset()


@pytest.fixture()
def strict_client(monkeypatch, tmp_path):
    """TestClient running in strict JWT production mode with fresh DB & storage."""
    monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_GATEWAY_API_KEY", raising=False)
    db.reset_engine()
    store.reset_store()
    permissions.reset_approval_state()
    auth.clear_email_test_sink()
    realtime.reset_realtime_state()
    client = TestClient(app)
    yield client
    store.reset_store()
    permissions.reset_approval_state()
    auth.clear_email_test_sink()
    realtime.reset_realtime_state()
