"""P2 tests: Asset Tools catalog, 4-grid split, non-destructive timeline.export, media.probe & Alembic 0003."""
from __future__ import annotations

from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app import auth, db


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


def test_asset_tools_grid_split_timeline_export_and_probe(strict_client):
    _uid, h = _make_user(strict_client, "tools@example.com", "Editor")
    proj = strict_client.post("/api/projects", json={"name": "Post Studio"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Catalog includes grid.split, timeline.export, media.probe, image.convert
    cat = strict_client.get(f"/api/projects/{pid}/asset-tools/catalog", headers=h).json()
    names = {t["tool_name"] for t in cat["tools"]}
    assert {"grid.split", "timeline.export", "media.probe", "image.convert"}.issubset(names)

    # Generate a mock MJ image node first
    mj = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/generations/image",
        json={"model_key": "midjourney-v8.2", "prompt": "4-grid concept art"},
        headers=h,
    ).json()
    src_obj_id = mj["target_object_id"]

    # Run grid.split (Grid = 4) -> spawns 4 child image nodes + 4 connectors
    split = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/asset-tools/run",
        json={"tool_name": "grid.split", "payload": {"source_object_id": src_obj_id, "grid": 4}},
        headers=h,
    )
    assert split.status_code == 201, split.text
    out = split.json()["output"]
    assert len(out["child_object_ids"]) == 4
    assert len(out["connector_ids"]) == 4

    # Run media.probe on first child -> detects 1x1 PNG dimensions
    probe = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/asset-tools/run",
        json={"tool_name": "media.probe", "payload": {"source_object_id": out["child_object_ids"][0]}},
        headers=h,
    ).json()["output"]["probe"]
    assert probe["width"] == 1 and probe["height"] == 1

    # timeline.export rejects invalid speed (>4.0)
    bad_tl = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/asset-tools/run",
        json={
            "tool_name": "timeline.export",
            "payload": {
                "output_format": "mp4",
                "draft": {
                    "title": "Bad Speed",
                    "clips": [{"source_object_id": out["child_object_ids"][0], "start_ms": 0, "duration_ms": 4000, "speed": 5.0}],
                },
            },
        },
        headers=h,
    )
    assert bad_tl.status_code == 400

    # Valid non-destructive timeline.export with gain, fade, speed 2.0x, preserve_pitch
    good_tl = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/asset-tools/run",
        json={
            "tool_name": "timeline.export",
            "payload": {
                "output_format": "mp4",
                "draft": {
                    "title": "Final Cut v1",
                    "fps": 24,
                    "clips": [
                        {
                            "track": "video",
                            "source_object_id": out["child_object_ids"][0],
                            "start_ms": 0,
                            "duration_ms": 4000,
                            "speed": 2.0,
                            "fade_in_ms": 200,
                            "fade_out_ms": 300,
                            "gain_db": -1.5,
                            "mute": False,
                            "preserve_pitch": True,
                        }
                    ],
                },
            },
        },
        headers=h,
    )
    assert good_tl.status_code == 201, good_tl.text
    tl_out = good_tl.json()["output"]
    assert tl_out["timeline"]["total_duration_ms"] == 2000
    assert tl_out["output_object_id"]

    # Tool job history recorded all 3 successful runs
    jobs = strict_client.get(f"/api/projects/{pid}/asset-tools/jobs", headers=h).json()
    assert jobs["total"] == 3


def test_alembic_0003_upgrade_and_downgrade(monkeypatch, tmp_path):
    db_file = tmp_path / "alembic_p2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))

    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "0002_p1_canvas_billing_models")
    alembic_command.upgrade(cfg, "head")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db.reset_engine()
