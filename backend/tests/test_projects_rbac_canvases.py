"""P0 tests: projects filtering/pagination, cover upload, RBAC members, invitations & multi-canvas."""
from __future__ import annotations

from urllib.parse import urlparse

from app import auth


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
    data = r.json()
    return data["user"]["user_id"], {"Authorization": f"Bearer {data['access_token']}"}


def test_projects_crud_filtering_pagination_and_cover(strict_client):
    uid, headers = _make_user(strict_client, "owner@example.com", "Owner")

    p1 = strict_client.post("/api/projects", json={"name": "Alpha Film"}, headers=headers).json()
    p2 = strict_client.post("/api/projects", json={"name": "Beta Comic"}, headers=headers).json()
    assert p1["id"].startswith("prj_")
    assert p1["workspace_id"].startswith("wsp_")
    assert p1["canvas_count"] == 1
    assert p1["my_role"] == "owner"
    assert p1["can_manage_members"] is True

    # Filter by keyword + pagination
    r = strict_client.get("/api/projects?status=active&keyword=Beta&page=1&size=4&order_by=recent", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == p2["id"]

    # Rename project
    r = strict_client.patch(f"/api/projects/{p1['id']}", json={"name": "Alpha Director Cut"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Alpha Director Cut"

    # Cover video > 30s rejected, <= 30s allowed
    r = strict_client.post(
        f"/api/projects/{p1['id']}/cover/upload-url",
        json={"filename": "long.mp4", "content_type": "video/mp4", "size_bytes": 1024, "duration_seconds": 35.0},
        headers=headers,
    )
    assert r.status_code == 400

    up = strict_client.post(
        f"/api/projects/{p1['id']}/cover/upload-url",
        json={"filename": "clip.mp4", "content_type": "video/mp4", "size_bytes": 16, "duration_seconds": 12.5},
        headers=headers,
    ).json()
    put_path = urlparse(up["upload_url"]).path + "?" + urlparse(up["upload_url"]).query
    assert strict_client.put(put_path, content=b"0000ftypmp420000").status_code == 200
    r = strict_client.post(f"/api/projects/{p1['id']}/cover/confirm", json={"storage_id": up["storage_id"]}, headers=headers)
    assert r.status_code == 200
    assert r.json()["cover_type"] == "video"
    assert r.json()["cover_url"]

    # Soft delete transitions to status=deleting
    assert strict_client.delete(f"/api/projects/{p2['id']}", headers=headers).status_code == 200
    r = strict_client.get("/api/projects?status=deleting", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [p2["id"]]


def test_members_rbac_transfer_leave_and_invitations(strict_client):
    owner_id, owner_h = _make_user(strict_client, "o@example.com", "Owner")
    editor_id, editor_h = _make_user(strict_client, "e@example.com", "Editor")
    viewer_id, viewer_h = _make_user(strict_client, "v@example.com", "Viewer")

    pid = strict_client.post("/api/projects", json={"name": "Collab Studio"}, headers=owner_h).json()["id"]

    # Non-member cannot access
    assert strict_client.get(f"/api/projects/{pid}", headers=editor_h).status_code == 403
    assert strict_client.get(f"/api/projects/{pid}/canvas", headers=viewer_h).status_code == 403

    # Owner adds viewer using read-only alias
    r = strict_client.post(
        f"/api/projects/{pid}/members",
        json={"email": "v@example.com", "role": "read-only"},
        headers=owner_h,
    )
    assert r.status_code == 201
    assert r.json()["role"] == "viewer"

    # Viewer can read project & canvas, but gets 403 on write/rename/task
    detail_v = strict_client.get(f"/api/projects/{pid}", headers=viewer_h).json()
    assert detail_v["can_read"] is True
    assert detail_v["can_run"] is False
    assert strict_client.patch(f"/api/projects/{pid}", json={"name": "hack"}, headers=viewer_h).status_code == 403
    assert strict_client.post(f"/api/projects/{pid}/tasks", json={"prompt": "写剧本"}, headers=viewer_h).status_code == 403

    # Owner creates invite link -> refresh invalidates old token -> editor joins with new token
    inv = strict_client.post(f"/api/projects/{pid}/invitations", json={"role": "editor"}, headers=owner_h).json()
    old_token = inv["token"]
    refreshed = strict_client.post(
        f"/api/projects/{pid}/invitations/{inv['id']}/refresh", headers=owner_h
    ).json()
    new_token = refreshed["token"]
    assert new_token != old_token
    assert strict_client.post(f"/api/invitations/{old_token}/accept", headers=editor_h).status_code == 404

    r = strict_client.post(f"/api/invitations/{new_token}/accept", headers=editor_h)
    assert r.status_code == 200
    assert r.json()["role"] == "editor"

    # Scope filter: editor sees project under scope=joined, not scope=owned
    assert strict_client.get("/api/projects?scope=owned", headers=editor_h).json()["total"] == 0
    assert strict_client.get("/api/projects?scope=joined", headers=editor_h).json()["total"] == 1

    # Revoke invitation -> cannot be used anymore
    assert strict_client.delete(f"/api/projects/{pid}/invitations/{inv['id']}", headers=owner_h).status_code == 200
    assert strict_client.post(f"/api/invitations/{new_token}/accept", headers=viewer_h).status_code == 404

    # Owner cannot leave before transferring ownership
    assert strict_client.post(f"/api/projects/{pid}/leave", headers=owner_h).status_code == 400

    # Transfer ownership to editor -> old owner becomes editor and can now leave
    r = strict_client.post(
        f"/api/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": editor_id},
        headers=owner_h,
    )
    assert r.status_code == 200
    assert strict_client.get(f"/api/projects/{pid}", headers=editor_h).json()["my_role"] == "owner"
    assert strict_client.post(f"/api/projects/{pid}/leave", headers=owner_h).status_code == 200
    assert strict_client.get(f"/api/projects/{pid}", headers=owner_h).status_code == 403


def test_multi_canvas_lifecycle_and_objects(strict_client):
    _uid, headers = _make_user(strict_client, "canvas@example.com", "Creator")
    pid = strict_client.post("/api/projects", json={"name": "Storyboard"}, headers=headers).json()["id"]

    canvases = strict_client.get(f"/api/projects/{pid}/canvases", headers=headers).json()
    assert len(canvases) == 1
    c1 = canvases[0]["id"]

    # Cannot delete the last remaining canvas
    assert strict_client.delete(f"/api/projects/{pid}/canvases/{c1}", headers=headers).status_code == 400

    # Create second canvas and rename it
    c2 = strict_client.post(f"/api/projects/{pid}/canvases", json={"name": "镜头二"}, headers=headers).json()["id"]
    r = strict_client.patch(f"/api/projects/{pid}/canvases/{c2}", json={"name": "第二幕分镜"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "第二幕分镜"

    # Objects stay isolated per canvas
    strict_client.post(
        f"/api/projects/{pid}/canvases/{c2}/objects",
        json={"type": "image", "name": "关键帧1", "x": 10, "y": 20, "w": 400, "h": 300},
        headers=headers,
    )
    assert len(strict_client.get(f"/api/projects/{pid}/canvases/{c1}/objects", headers=headers).json()["objects"]) == 0
    objs_c2 = strict_client.get(f"/api/projects/{pid}/canvases/{c2}/objects", headers=headers).json()["objects"]
    assert len(objs_c2) == 1
    assert objs_c2[0]["name"] == "关键帧1"

    # Deleting default canvas promotes remaining canvas
    assert strict_client.delete(f"/api/projects/{pid}/canvases/{c1}", headers=headers).status_code == 200
    rem = strict_client.get(f"/api/projects/{pid}/canvases", headers=headers).json()
    assert len(rem) == 1 and rem[0]["id"] == c2 and rem[0]["is_default"] is True
