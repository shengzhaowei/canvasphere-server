"""P1 tests: protocol profiles, 4-modality generations, MJ actions, TTS first-voice fee, Suno ops & Alembic 0002."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app import auth, db, model_platform
from app.models import User


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


def test_catalog_protocol_profiles_image_and_midjourney_actions(strict_client):
    _uid, h = _make_user(strict_client, "img@example.com", "ImgUser")
    proj = strict_client.post("/api/projects", json={"name": "Gen Studio"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Protocol profiles & filtered models
    profs = strict_client.get("/api/model-platform/protocol-profiles").json()
    keys = {p["profile_key"] for p in profs["profiles"]}
    assert {"zongheng_video", "ark_images", "openlux_gpt_images", "midjourney_proxy", "suno_music"}.issubset(keys)

    vids = strict_client.get("/api/model-platform/models?modality=video&billing_mode=per_second").json()
    assert vids["total"] > 10

    # Generate 2 images with GPT Image 2.6 -> attaches artifact to new image node + mock refunds credits
    r = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "gpt-image-2.6-flare", "prompt": "Cyberpunk rainy street", "count": 2, "output_format": "png"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "succeeded"
    assert job["mock"] is True
    assert len(job["output"]["artifacts"]) == 2
    # Artifact URL is downloadable from local storage
    art_url = job["output"]["artifacts"][0]["url"]
    p_art = urlparse(art_url)
    assert strict_client.get(p_art.path + "?" + p_art.query).status_code == 200

    # Midjourney imagine + downstream U1 & zoom_2x actions
    mj = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "midjourney-v8.2", "prompt": "Megastructure --ar 16:9", "prompt_mode": "hybrid", "stylize": 250},
        headers=h,
    ).json()
    assert mj["status"] == "succeeded"
    assert mj["output"]["artifacts"][0]["meta"]["grid_count"] == 4

    u1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/{mj['id']}/mj-action",
        json={"action": "u1"},
        headers=h,
    )
    assert u1.status_code == 201
    assert u1.json()["parent_job_id"] == mj["id"]
    assert u1.json()["mode"] == "u1"


def test_video_6_guidance_modes_and_slot_validation(strict_client):
    _uid, h = _make_user(strict_client, "vid@example.com", "VidUser")
    proj = strict_client.post("/api/projects", json={"name": "Video Lab"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Missing frame refs for first_last_frame -> 400
    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/video",
        json={"model_key": "wan3.0-video-720p", "mode": "first_last_frame", "first_frame_object_id": "obj_1"},
        headers=h,
    ).status_code == 400

    # Exceeding Grok's max 6 ref images -> 400
    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/video",
        json={"model_key": "grok-zh-ttp-720p", "mode": "multimodal_ref", "ref_image_ids": [f"i{n}" for n in range(7)]},
        headers=h,
    ).status_code == 400

    # Valid T2V generation
    r = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/video",
        json={"model_key": "seedance-2.5-pro-zh-tt-720", "mode": "t2v", "prompt": "Drone flythrough", "resolution": "720P", "duration_seconds": 5},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["status"] == "succeeded"


def test_tts_first_voice_surcharge_suno_ops_and_failure_refund(strict_client, monkeypatch):
    uid, h = _make_user(strict_client, "aud@example.com", "AudioUser")
    _adm, h_admin = _make_user(strict_client, "adm3@example.com", "Admin", is_admin=True)
    proj = strict_client.post("/api/projects", json={"name": "Audio & Suno"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Suno all_stem_separation costs 355.2 > 300 default bonus -> 402 Insufficient Credits
    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/music",
        json={"mode": "song_bgm", "operation": "all_stem_separation", "prompt": "Split stems"},
        headers=h,
    ).status_code == 402

    # Redeem 2000 bonus credits so user has 2300
    rc = strict_client.post("/api/admin/billing/redeem-codes", json={"credits_amount": 2000}, headers=h_admin).json()["codes"][0]["code"]
    strict_client.post("/api/user/billing/redeem", json={"code": rc}, headers=h)

    # Mock live provider (mock=False) to verify real settlement + TTS first-voice ¥10 (1000 credits) surcharge
    def fake_live_provider(*, card, mode, prompt, params):
        return model_platform.ProviderInvocationResult(
            mock=False,
            artifacts=[{"filename": "out.mp3", "content_type": "audio/mpeg", "bytes": b"ID3LIVE", "meta": {}}],
            metadata={"live": True},
        )

    monkeypatch.setattr(model_platform, "invoke_provider", fake_live_provider)

    # First TTS call with voice "Airheaded Girl": charges 12 + 1000 surcharge = 1012
    r1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/audio",
        json={"model_key": "speech-2.8-hd", "mode": "tts", "prompt": "Hello world", "voice_id": "Airheaded Girl", "emotion": "happy"},
        headers=h,
    )
    assert r1.status_code == 201 and r1.json()["mock"] is False
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 1288.0
    charges = strict_client.get("/api/user/billing/charge-records", headers=h).json()
    assert charges["total"] == 1 and charges["items"][0]["charge_type"] == "voice_first_use"

    # Second TTS call with same voice: NO 1000 surcharge, only 12 credits
    r2 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/audio",
        json={"model_key": "speech-2.8-hd", "mode": "tts", "prompt": "Second line", "voice_id": "Airheaded Girl"},
        headers=h,
    )
    assert r2.status_code == 201
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 1276.0

    # Suno free operation (lyrics_gen = 0 credits) + paid operation (sfx = 14.2 credits)
    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/music",
        json={"mode": "expand_style", "operation": "lyrics_gen", "prompt": "Neon night"},
        headers=h,
    ).status_code == 201
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 1276.0

    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/music",
        json={"mode": "sfx", "operation": "sfx", "prompt": "Thunderclap", "loop_sfx": False},
        headers=h,
    ).status_code == 201
    assert strict_client.get("/api/user/billing/wallet", headers=h).json()["available_credits"] == 1261.8

    # Upstream failure must refund 100% of pre-frozen credits
    def failing_provider(*, card, mode, prompt, params):
        raise RuntimeError("upstream 503 overloaded")

    monkeypatch.setattr(model_platform, "invoke_provider", failing_provider)
    rf = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/music",
        json={"mode": "song_bgm", "operation": "all_stem_separation", "prompt": "Will fail"},
        headers=h,
    )
    assert rf.status_code == 201
    assert rf.json()["status"] == "failed"
    w_after = strict_client.get("/api/user/billing/wallet", headers=h).json()
    assert w_after["available_credits"] == 1261.8
    assert w_after["reserved_bonus_credits"] == 0.0


def test_alembic_0001_to_0002_upgrade_and_downgrade(monkeypatch, tmp_path):
    db_file = tmp_path / "alembic_p1.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))

    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "0001_p0_core_tables")
    alembic_command.upgrade(cfg, "head")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
