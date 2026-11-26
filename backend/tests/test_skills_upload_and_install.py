"""P3 tests: user skill .zip upload (Zip Slip defense), 9 categories & project installation."""
from __future__ import annotations

import io
import zipfile

from app import auth, skills


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


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_skill_zip_upload_zip_slip_defense_categories_and_project_install(strict_client):
    _uid, h = _make_user(strict_client, "skl@example.com", "SkillMaker")
    proj = strict_client.post("/api/projects", json={"name": "Skill Project"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Zip Slip traversal archive rejected with 400
    evil_zip = _make_zip({"../escape.txt": "pwn", "SKILL.md": "---\nname: evil\n---\nhi"})
    r_bad = strict_client.post(
        "/api/user/skills/upload",
        files={"file": ("evil.zip", evil_zip, "application/zip")},
        headers=h,
    )
    assert r_bad.status_code == 400

    # Valid skill zip upload into one of the 9 categories
    good_zip = _make_zip({
        "cinematic-megastructure/SKILL.md": (
            "---\n"
            "name: cinematic-megastructure\n"
            "description: 电影感巨构建筑构图与光影规范\n"
            "category: 画面与风格\n"
            "---\n"
            "# Cinematic Megastructure\n遵循大景深、低机位与体积光原则。"
        ),
        "cinematic-megastructure/references/lens.md": "24mm anamorphic lens",
    })
    r_up = strict_client.post(
        "/api/user/skills/upload",
        files={"file": ("cinematic-megastructure.zip", good_zip, "application/zip")},
        headers=h,
    )
    assert r_up.status_code == 201, r_up.text
    sk = r_up.json()
    assert sk["name"] == "cinematic-megastructure"
    assert sk["category"] == "画面与风格"
    assert len(sk["files"]) == 2

    # List user skills with 9 categories & keyword filter
    listed = strict_client.get("/api/user/skills?page=1&limit=6&keyword=megastructure", headers=h).json()
    assert len(listed["categories"]) == 9
    assert listed["total"] == 1 and listed["items"][0]["id"] == sk["id"]

    # Install into project and invoke in Studio chat via @cinematic-megastructure
    r_inst = strict_client.post(
        f"/api/projects/{pid}/skills/installed",
        json={"skill_id": sk["id"]},
        headers=h,
    )
    assert r_inst.status_code == 201
    installed = strict_client.get(f"/api/projects/{pid}/skills/installed", headers=h).json()
    assert installed["total"] == 1 and installed["items"][0]["skill_name"] == "cinematic-megastructure"

    # Uploaded DB skill resolves in Studio @skill mention!
    strict_client.post(f"/api/projects/{pid}/approval-policy", json={"tool": "add_node", "tier": "auto"}, headers=h)
    chat = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        json={"prompt": "使用 @cinematic-megastructure 写一个场景设定", "agent": "orange"},
        headers=h,
    )
    assert chat.status_code == 201
    assert "cinematic-megastructure" in chat.json()["run"]["frozen_tree"]["skills"]

    # Uninstall & delete user skill
    assert strict_client.delete(f"/api/projects/{pid}/skills/installed/cinematic-megastructure", headers=h).status_code == 200
    assert strict_client.delete(f"/api/user/skills/{sk['id']}", headers=h).status_code == 200
    assert skills.SKILLS_DIR.is_dir()
