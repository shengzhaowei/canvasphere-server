"""Phase P9 tests: project description, stroke object, server-side lock (423), group/ungroup/reorder,
canvas version snapshots & restore, threaded comments, public read-only shares,
9 built-in curated Studio skills, and Alembic 0006 upgrade/downgrade.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app import auth


def _register(client, email: str, name: str = "P9 Creator"):
    client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "Password123!", "code": code, "display_name": name},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_project_description_stroke_lock_group_and_reorder(strict_client):
    h = _register(strict_client, "p9-canvas@example.com")

    # Project description create & update
    proj = strict_client.post(
        "/api/projects",
        headers=h,
        json={"name": "Short Film", "description": "赛博朋克短片第一版"},
    )
    assert proj.status_code == 201, proj.text
    assert proj.json()["description"] == "赛博朋克短片第一版"
    pid = proj.json()["id"]
    cid = proj.json()["default_canvas_id"]

    upd = strict_client.patch(
        f"/api/projects/{pid}",
        headers=h,
        json={"description": "已更新项目简介"},
    )
    assert upd.status_code == 200
    assert upd.json()["description"] == "已更新项目简介"
    assert upd.json()["name"] == "Short Film"

    # Freehand stroke object (#3)
    s = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=h,
        json={
            "type": "stroke",
            "name": "分镜手绘草图",
            "content": {"points": [[0, 0], [10, 20], [30, 15]]},
            "style": {"stroke_color": "#a5b4fc", "stroke_width": 3},
        },
    )
    assert s.status_code == 201, s.text
    stroke_id = s.json()["id"]
    assert s.json()["type"] == "stroke"
    assert s.json()["locked"] is False

    # Lock object -> edits and deletes rejected with 423 Locked until unlocked (#5)
    lock_r = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{stroke_id}",
        headers=h,
        json={"expected_version": 1, "locked": True},
    )
    assert lock_r.status_code == 200 and lock_r.json()["locked"] is True

    edit_locked = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{stroke_id}",
        headers=h,
        json={"expected_version": 2, "x": 100},
    )
    assert edit_locked.status_code == 423
    assert edit_locked.json()["detail"]["code"] == "OBJECT_LOCKED"

    del_locked = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects/deletions",
        headers=h,
        json={"object_ids": [stroke_id]},
    )
    assert del_locked.status_code == 423

    unlock_r = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{stroke_id}",
        headers=h,
        json={"expected_version": 2, "locked": False, "x": 50},
    )
    assert unlock_r.status_code == 200
    assert unlock_r.json()["locked"] is False and unlock_r.json()["x"] == 50.0

    # Group, ungroup, and z-index reorder (#5)
    n2 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=h,
        json={"type": "text", "name": "Note 2", "z_index": 5},
    ).json()["id"]

    grp = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects/group",
        headers=h,
        json={"object_ids": [stroke_id, n2]},
    )
    assert grp.status_code == 200, grp.text
    gid = grp.json()["group_id"]
    assert all(o["group_id"] == gid for o in grp.json()["objects"])

    reord = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects/reorder",
        headers=h,
        json={"action": "bring_to_front", "object_ids": [stroke_id]},
    )
    assert reord.status_code == 200
    assert reord.json()["objects"][0]["z_index"] == 6

    ungrp = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects/ungroup",
        headers=h,
        json={"object_ids": [], "group_id": gid},
    )
    assert ungrp.status_code == 200
    assert set(ungrp.json()["ungrouped_ids"]) == {stroke_id, n2}


def test_snapshots_restore_comments_and_public_read_only_share(strict_client):
    h = _register(strict_client, "p9-collab@example.com", "Director")
    proj = strict_client.post("/api/projects", headers=h, json={"name": "Collab Board"}).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    n1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=h,
        json={"type": "text", "name": "Opening", "text": "v1 script"},
    ).json()

    # Create snapshot (#6)
    snap = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/snapshots",
        headers=h,
        json={"name": "定稿 v1"},
    )
    assert snap.status_code == 201, snap.text
    snap_id = snap.json()["id"]
    assert snap.json()["object_count"] == 1

    # Mutate canvas (edit n1 + add n2), then restore snapshot v1
    strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/objects/{n1['id']}",
        headers=h,
        json={"expected_version": 1, "text": "mutated v2"},
    )
    strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=h,
        json={"type": "rect", "name": "Extra"},
    )
    assert strict_client.get(f"/api/projects/{pid}/canvases/{cid}/objects", headers=h).json()["total"] == 2

    rest = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/snapshots/{snap_id}/restore",
        headers=h,
    )
    assert rest.status_code == 200, rest.text
    assert rest.json()["restored_count"] == 1
    objs_after = strict_client.get(f"/api/projects/{pid}/canvases/{cid}/objects", headers=h).json()["objects"]
    assert len(objs_after) == 1
    assert objs_after[0]["text"] == "v1 script"

    # Comments: create, reply, resolve, list filter (#14)
    c1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/comments",
        headers=h,
        json={"content": "这里加一个广角镜头", "object_id": n1["id"], "anchor_x": 120.5, "anchor_y": 88.0},
    )
    assert c1.status_code == 201, c1.text
    c1_id = c1.json()["id"]
    assert c1.json()["resolved"] is False

    res_c = strict_client.patch(
        f"/api/projects/{pid}/canvases/{cid}/comments/{c1_id}",
        headers=h,
        json={"resolved": True},
    )
    assert res_c.status_code == 200 and res_c.json()["resolved"] is True

    unresolved = strict_client.get(
        f"/api/projects/{pid}/canvases/{cid}/comments?resolved=false",
        headers=h,
    ).json()
    assert unresolved["total"] == 0

    # Public read-only share link (#47)
    share = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/public-shares",
        headers=h,
        json={"title": "外部评审版", "expires_in_hours": 48},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]
    share_id = share.json()["id"]
    assert "/share/" in share.json()["share_url"]

    # Unauthenticated visitor can read shared canvas without JWT
    pub = strict_client.get(f"/api/public/canvases/{token}")
    assert pub.status_code == 200, pub.text
    assert pub.json()["read_only"] is True
    assert pub.json()["title"] == "外部评审版"
    assert pub.json()["total"] == 1
    assert "members" not in pub.json()

    # Revoke -> public URL immediately returns 404
    rev = strict_client.delete(f"/api/projects/{pid}/canvases/{cid}/public-shares/{share_id}", headers=h)
    assert rev.status_code == 200
    assert strict_client.get(f"/api/public/canvases/{token}").status_code == 404


def test_builtin_9_skills_seeded_and_alembic_0006_upgrade_downgrade(strict_client, monkeypatch, tmp_path):
    h = _register(strict_client, "p9-skills@example.com")

    # 9 curated built-in skills available out of the box (#28)
    r = strict_client.get("/api/user/skills?page=1&limit=20", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 9
    builtin_names = {it["name"] for it in body["items"] if it["is_builtin"]}
    assert {"script-beat-sheet", "storyboard", "image-prompt", "grid-split-director", "sound-mood-design"}.issubset(
        builtin_names
    )

    # Built-in skill can be installed directly into a project
    proj = strict_client.post("/api/projects", headers=h, json={"name": "Skill Seed Proj"}).json()
    target = next(it for it in body["items"] if it["name"] == "grid-split-director")
    inst = strict_client.post(
        f"/api/projects/{proj['id']}/skills/installed",
        headers=h,
        json={"skill_id": target["id"]},
    )
    assert inst.status_code == 201

    # Alembic 0001 -> 0006 -> 0005 -> head migration verification
    db_file = tmp_path / "alembic_p9.sqlite3"
    with monkeypatch.context() as mp:
        mp.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
        backend_dir = Path(__file__).resolve().parents[1]
        cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "migrations"))
        alembic_command.upgrade(cfg, "head")
        alembic_command.downgrade(cfg, "0005_p5_provider_external_tasks")
        alembic_command.upgrade(cfg, "head")
