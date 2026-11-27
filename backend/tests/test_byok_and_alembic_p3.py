"""P3 tests: BYOK entitlement gate, Fernet write-only API key encryption, offerings verify & Alembic 0004."""
from __future__ import annotations

from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app import auth, byok, db
from app.models import ByokChannel, User


def _make_user(client, email: str, name: str, is_admin: bool = False):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
        "code": code,
        "display_name": name,
    })
    assert r.status_code == 201
    uid = r.json()["user"]["user_id"]
    if is_admin:
        with db.session_scope() as s:
            u = s.get(User, uid)
            u.roles = ["user", "admin"]
            s.add(u)
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_byok_entitlement_encryption_channels_and_offerings_verify(strict_client, monkeypatch):
    uid, h = _make_user(strict_client, "byok@example.com", "ProUser")
    _adm, h_admin = _make_user(strict_client, "adm_byok@example.com", "Admin", is_admin=True)

    # Default entitlement is disabled (BYOK_ENTITLEMENT_NOT_CONFIGURED)
    ent = strict_client.get("/api/user/model-platform/byok-entitlement", headers=h).json()
    assert ent["enabled"] is False
    assert ent["code"] == "BYOK_ENTITLEMENT_NOT_CONFIGURED"
    assert ent["max_channels"] == 0

    # Creating channel before entitlement -> 403
    assert strict_client.post(
        "/api/user/model-platform/channels",
        json={
            "name": "My Ark",
            "protocol_profile": "ark_images",
            "base_url": "https://ark.example.com/api/v3",
            "api_key": "sk-secret-plain-value-9876",
        },
        headers=h,
    ).status_code == 403

    # Admin enables entitlement (max 1 channel, max 2 offerings)
    r_ent = strict_client.put(
        f"/api/admin/model-platform/byok-entitlement/{uid}",
        json={"enabled": True, "max_channels": 1, "max_offerings_per_channel": 2},
        headers=h_admin,
    )
    assert r_ent.status_code == 200 and r_ent.json()["enabled"] is True

    # Create encrypted channel -> plaintext key NEVER returned or stored in plaintext
    secret_key = "sk-secret-plain-value-9876"
    ch = strict_client.post(
        "/api/user/model-platform/channels",
        json={
            "name": "My Ark",
            "protocol_profile": "ark_images",
            "base_url": "https://ark.example.com/api/v3",
            "api_key": secret_key,
            "rpm_limit": 120,
        },
        headers=h,
    )
    assert ch.status_code == 201, ch.text
    ch_data = ch.json()
    assert secret_key not in str(ch_data)
    assert ch_data["api_key_masked"] == "sk-s****9876"

    # Verify DB ciphertext is encrypted and decrypts back to original secret
    with db.session_scope() as s:
        row = s.get(ByokChannel, ch_data["id"])
        assert row.api_key_encrypted != secret_key
        assert byok.decrypt_api_key(row.api_key_encrypted) == secret_key

    # Second channel exceeds max_channels=1 -> 403
    assert strict_client.post(
        "/api/user/model-platform/channels",
        json={
            "name": "Second",
            "protocol_profile": "openlux_gpt_images",
            "base_url": "https://openlux.example.com",
            "api_key": "sk-second-key-1234",
        },
        headers=h,
    ).status_code == 403

    # Create offering mapping canonical model -> upstream model & verify connectivity
    off = strict_client.post(
        "/api/user/model-platform/offerings",
        json={
            "channel_id": ch_data["id"],
            "canonical_model_key": "doubao-seedream-5-0-260128",
            "upstream_model_id": "ep-2026-seedream-5",
        },
        headers=h,
    )
    assert off.status_code == 201
    off_id = off.json()["id"]
    assert off.json()["verified_status"] == "unverified"

    monkeypatch.setattr(byok, "probe_upstream_channel", lambda base, key, model: (key == secret_key, None))
    ver = strict_client.post(f"/api/user/model-platform/offerings/{off_id}/verify", headers=h)
    assert ver.status_code == 200
    assert ver.json()["verified_status"] == "verified"

    # Delete offering & channel
    assert strict_client.delete(f"/api/user/model-platform/offerings/{off_id}", headers=h).status_code == 200
    assert strict_client.delete(f"/api/user/model-platform/channels/{ch_data['id']}", headers=h).status_code == 200


def test_alembic_0004_upgrade_and_downgrade(monkeypatch, tmp_path):
    db_file = tmp_path / "alembic_p3.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))

    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "0003_p2_agents_sandbox_tools")
    alembic_command.upgrade(cfg, "head")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
