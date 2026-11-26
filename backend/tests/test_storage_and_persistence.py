"""P0 tests: runtime-config, storage quotas/accounting, HMAC security, persistence & Alembic."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from app import auth, db, permissions
from app.main import app


def _make_user(client, email: str, name: str):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "code": code,
        "display_name": name,
    })
    assert r.status_code == 201
    return r.json()["user"]["user_id"], {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_runtime_config_and_health(strict_client):
    r = strict_client.get("/api/runtime-config")
    assert r.status_code == 200
    assert r.json() == {"system_timezone": "Asia/Shanghai"}

    h = strict_client.get("/health").json()
    assert h["status"] == "ok"
    assert h["db"] == "sqlite"
    assert h["storage_backend"] == "local"


def test_storage_hmac_quota_and_project_space_separation(strict_client, monkeypatch):
    monkeypatch.setenv("USER_STORAGE_QUOTA_BYTES", "100")
    owner_id, owner_h = _make_user(strict_client, "st_owner@example.com", "Owner")
    editor_id, editor_h = _make_user(strict_client, "st_editor@example.com", "Editor")

    pid = strict_client.post("/api/projects", json={"name": "Storage Proj"}, headers=owner_h).json()["id"]
    strict_client.post(f"/api/projects/{pid}/members", json={"user_id": editor_id, "role": "editor"}, headers=owner_h)

    # Editor uploads 40 bytes into public workspace (billed to owner)
    up_w = strict_client.post(
        f"/api/projects/{pid}/storage/upload-url",
        json={"filename": "scene.png", "content_type": "image/png", "size_bytes": 40, "space": "workspace"},
        headers=editor_h,
    ).json()

    # Tampered signature rejected with 403
    parsed = urlparse(up_w["upload_url"])
    bad_path = parsed.path + "?" + parsed.query[:-4] + "0000"
    assert strict_client.put(bad_path, content=b"x" * 40).status_code == 403

    good_path = parsed.path + "?" + parsed.query
    assert strict_client.put(good_path, content=b"x" * 40).status_code == 200
    assert strict_client.post(
        f"/api/projects/{pid}/storage/confirm", json={"storage_id": up_w["storage_id"]}, headers=editor_h
    ).status_code == 200

    # Editor uploads 25 bytes into private_runtime (billed to editor)
    up_p = strict_client.post(
        f"/api/projects/{pid}/storage/upload-url",
        json={"filename": "ctx.json", "content_type": "application/json", "size_bytes": 25, "space": "private_runtime"},
        headers=editor_h,
    ).json()
    p_parsed = urlparse(up_p["upload_url"])
    assert strict_client.put(p_parsed.path + "?" + p_parsed.query, content=b"y" * 25).status_code == 200
    assert strict_client.post(
        f"/api/projects/{pid}/storage/confirm", json={"storage_id": up_p["storage_id"]}, headers=editor_h
    ).status_code == 200

    # Verify separation: owner has 40 workspace bytes; editor has 25 private bytes
    owner_storage = strict_client.get("/api/user/storage", headers=owner_h).json()
    assert owner_storage["used_bytes"] == 40
    assert owner_storage["quota_bytes"] == 100

    proj_st_editor = strict_client.get(f"/api/projects/{pid}/storage", headers=editor_h).json()
    assert proj_st_editor["workspace_used_bytes"] == 40
    assert proj_st_editor["workspace_billed_to"] == owner_id
    assert proj_st_editor["my_private_used_bytes"] == 25

    # Exceeding owner's 100-byte quota fails with 413
    r = strict_client.post(
        f"/api/projects/{pid}/storage/upload-url",
        json={"filename": "big.bin", "content_type": "application/octet-stream", "size_bytes": 70, "space": "workspace"},
        headers=owner_h,
    )
    assert r.status_code == 413


def test_file_sqlite_persistence_survives_engine_restart(monkeypatch, tmp_path):
    db_file = tmp_path / "persist.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ALLOW_LEGACY_ROLE_HEADER", "false")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    db.reset_engine()
    permissions.reset_approval_state()

    client = TestClient(app)
    _uid, headers = _make_user(client, "persist@example.com", "Persist")
    pid = client.post("/api/projects", json={"name": "Durable Project"}, headers=headers).json()["id"]
    client.post(f"/api/projects/{pid}/canvases", json={"name": "Act 2"}, headers=headers)
    client.post(f"/api/projects/{pid}/approval-policy", json={"tool": "add_node", "tier": "auto"}, headers=headers)

    # Simulate server restart by disposing SQLAlchemy engine and clearing in-memory policy mirror
    db.reset_engine()
    permissions._policy_overrides.clear()

    client2 = TestClient(app)
    proj = client2.get(f"/api/projects/{pid}", headers=headers).json()
    assert proj["name"] == "Durable Project"
    assert proj["canvas_count"] == 2
    assert permissions.tier_of("add_node", pid) == "auto"

    # Cleanup back to default in-memory DB
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()


def test_alembic_upgrade_and_downgrade_head(monkeypatch, tmp_path):
    db_file = tmp_path / "alembic_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))

    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")
    alembic_command.upgrade(cfg, "head")
    assert db_file.is_file()

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
