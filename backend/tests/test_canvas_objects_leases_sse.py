"""P1 tests: 10-type canvas objects, connectors, leases (409), optimistic versions, SSE, presence & files."""
from __future__ import annotations

from urllib.parse import urlparse

from app import auth
from app.realtime import event_bus


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


def test_all_10_object_types_connectors_duplicate_and_browser_filters(strict_client):
    _uid, h = _make_user(strict_client, "c1@example.com", "Creator")
    proj = strict_client.post("/api/projects", json={"name": "Infinite"}, headers=h).json()
    pid, cid = proj["id"], proj["default_canvas_id"]

    # Reject invalid object type, rect fill preset, line thickness
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "3d_alien"}, headers=h).status_code == 400
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "rect", "style": {"fill_preset": "neon_pink"}}, headers=h).status_code == 400
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "line", "style": {"thickness": 99}}, headers=h).status_code == 400

    # Create 9 non-connector types + 1 connector = all 10 types
    n_text = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "text", "name": "Script Note", "text": "Opening scene"}, headers=h).json()
    n_rect = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "rect", "name": "Act 1 Area", "style": {"fill_preset": "deep_sea"}}, headers=h).json()
    n_line = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "line", "style": {"thickness": 2, "line_style": "dashed"}}, headers=h).json()
    n_img = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "image", "name": "Hero Poster"}, headers=h).json()
    for t in ("video", "audio", "music", "document", "file"):
        r = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": t, "name": f"Node {t}"}, headers=h)
        assert r.status_code == 201
        assert set(r.json()["properties"].keys()) == {"basic", "content", "asset", "geometry", "style", "collaboration"}

    # Self-loop connector rejected
    assert strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        json={"type": "connector", "from_object_id": n_text["id"], "to_object_id": n_text["id"]},
        headers=h,
    ).status_code == 400

    # Valid connector text -> image
    n_conn = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        json={"type": "connector", "from_object_id": n_text["id"], "from_anchor": "right", "to_object_id": n_img["id"], "to_anchor": "left"},
        headers=h,
    ).json()
    assert n_conn["type"] == "connector"

    # Node browser filtering
    r = strict_client.get(f"/api/projects/{pid}/canvases/{cid}/objects?keyword=Hero&type=image", headers=h)
    assert r.json()["total"] == 1
    assert r.json()["objects"][0]["id"] == n_img["id"]

    # Duplicate shifts geometry by (+24, +24)
    dup = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects/{n_rect['id']}/duplicate", headers=h).json()
    assert dup["x"] == n_rect["x"] + 24.0
    assert dup["y"] == n_rect["y"] + 24.0
    assert dup["version"] == 1

    # Deleting source node cascades dangling connector
    del_res = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects/deletions",
        json={"object_ids": [n_text["id"]]},
        headers=h,
    ).json()
    assert n_text["id"] in del_res["deleted_ids"]
    assert n_conn["id"] in del_res["cascaded_connector_ids"]


def test_object_leases_and_optimistic_version_conflicts_return_409(strict_client):
    _u1, h1 = _make_user(strict_client, "u1@example.com", "Alice")
    u2, h2 = _make_user(strict_client, "u2@example.com", "Bob")
    proj = strict_client.post("/api/projects", json={"name": "Locks"}, headers=h1).json()
    pid, cid = proj["id"], proj["default_canvas_id"]
    strict_client.post(f"/api/projects/{pid}/members", json={"user_id": u2, "role": "editor"}, headers=h1)

    obj = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects", json={"type": "image", "name": "Shot 1"}, headers=h1).json()
    oid = obj["id"]

    # Alice acquires lease
    r = strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects/leases", json={"object_ids": [oid]}, headers=h1)
    assert r.status_code == 200
    assert r.json()["leases"][0]["object_id"] == oid

    # Bob cannot acquire lease, update, or delete while Alice holds lease -> 409
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects/leases", json={"object_ids": [oid]}, headers=h2).status_code == 409
    assert strict_client.patch(f"/api/projects/{pid}/canvases/{cid}/objects/{oid}", json={"name": "Bob edit"}, headers=h2).status_code == 409
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid}/objects/deletions", json={"object_ids": [oid]}, headers=h2).status_code == 409

    # Alice updates with matching version -> version increments to 2
    r = strict_client.patch(f"/api/projects/{pid}/canvases/{cid}/objects/{oid}", json={"name": "Shot 1 v2", "expected_version": 1}, headers=h1)
    assert r.status_code == 200 and r.json()["version"] == 2

    # Stale expected_version -> 409 even for lease holder
    r = strict_client.patch(f"/api/projects/{pid}/canvases/{cid}/objects/{oid}", json={"name": "Stale", "expected_version": 1}, headers=h1)
    assert r.status_code == 409

    # Alice releases lease -> Bob can now edit
    assert strict_client.delete(f"/api/projects/{pid}/canvases/{cid}/objects/leases?object_ids={oid}", headers=h1).status_code == 200
    assert strict_client.patch(f"/api/projects/{pid}/canvases/{cid}/objects/{oid}", json={"name": "Bob v3", "expected_version": 2}, headers=h2).status_code == 200


def test_sse_stream_presence_cameras_online_members_and_workspace_files(strict_client):
    _u1, h1 = _make_user(strict_client, "p1@example.com", "Alice")
    u2, h2 = _make_user(strict_client, "p2@example.com", "Bob")
    u3, _h3 = _make_user(strict_client, "p3@example.com", "Charlie")

    proj = strict_client.post("/api/projects", json={"name": "Live Room"}, headers=h1).json()
    pid, cid1 = proj["id"], proj["default_canvas_id"]
    cid2 = strict_client.post(f"/api/projects/{pid}/canvases", json={"name": "Canvas 2"}, headers=h1).json()["id"]
    strict_client.post(f"/api/projects/{pid}/members", json={"user_id": u2, "role": "editor"}, headers=h1)
    strict_client.post(f"/api/projects/{pid}/members", json={"user_id": u3, "role": "viewer"}, headers=h1)

    # SSE endpoint returns text/event-stream + connected event
    r = strict_client.get(f"/api/projects/{pid}/canvases/{cid1}/events?once=true", headers=h1)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert r.headers.get("x-accel-buffering") == "no"
    assert "event: connected" in r.text

    # Presence heartbeats: Alice on cid1, Bob on cid2, Charlie offline
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid1}/camera/presence", json={"x": 100, "y": 200, "zoom": 1.25, "cursor_x": 50, "cursor_y": 60}, headers=h1).status_code == 204
    assert strict_client.post(f"/api/projects/{pid}/canvases/{cid2}/camera/presence", json={"x": -20, "y": 40, "zoom": 0.8}, headers=h2).status_code == 204

    cams = strict_client.get(f"/api/projects/{pid}/canvases/{cid1}/cameras", headers=h1).json()["cameras"]
    assert len(cams) == 1 and cams[0]["display_name"] == "Alice" and cams[0]["camera"]["zoom"] == 1.25

    online = strict_client.get(f"/api/projects/{pid}/online-members?canvas_id={cid1}", headers=h1).json()
    assert len(online["in_current_canvas"]) == 1
    assert len(online["in_other_canvases"]) == 1
    assert len(online["offline"]) == 1
    assert online["online_count"] == 2

    # Recent event history recorded camera.updated
    assert any(ev["type"] == "camera.updated" for ev in event_bus.recent_events(pid, cid1))

    # Upload workspace file and verify GET /api/projects/{pid}/files search
    up = strict_client.post(
        f"/api/projects/{pid}/storage/upload-url",
        json={"filename": "style_bible.md", "content_type": "text/markdown", "size_bytes": 12, "space": "workspace"},
        headers=h1,
    ).json()
    parsed = urlparse(up["upload_url"])
    assert strict_client.put(parsed.path + "?" + parsed.query, content=b"# Style Doc!").status_code == 200
    assert strict_client.post(f"/api/projects/{pid}/storage/confirm", json={"storage_id": up["storage_id"]}, headers=h1).status_code == 200

    files = strict_client.get(f"/api/projects/{pid}/files?keyword=bible", headers=h1).json()
    assert files["total"] == 1
    assert files["files"][0]["filename"] == "style_bible.md"
    assert files["files"][0]["url"]
