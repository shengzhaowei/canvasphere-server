"""P0 tests: email/password auth, dual-token refresh, logout-all, OAuth, profile & avatar."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app import auth
from app.config import get_settings


def _register_user(client, email: str, password: str = "StrongPass123!", name: str = "Alice"):
    r = client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    assert r.status_code == 200
    code = auth.get_last_sent_code(email, "register")
    assert code and len(code) == 6
    r = client.post("/api/auth/register", json={
        "email": email,
        "password": password,
        "code": code,
        "display_name": name,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _oauth_callback_fragment(client, url):
    """GET an OAuth callback without following the redirect and return the
    parsed URL fragment. The callback 302s to {FRONTEND_URL}/auth/callback
    with tokens (login) or the bind result (bind) in the fragment; following
    the redirect would leave the API (the frontend route does not exist here)
    and come back 404."""
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 302, r.text
    parsed = urlparse(r.headers["location"])
    frontend = get_settings().frontend_url
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{frontend}/auth/callback", r.headers["location"]
    return parse_qs(parsed.fragment)


def test_strict_mode_rejects_unauthenticated_requests(strict_client):
    assert strict_client.get("/api/user/profile").status_code == 401
    assert strict_client.get("/api/projects").status_code == 401
    assert strict_client.post("/api/projects", json={"name": "nope"}).status_code == 401
    # Legacy X-Role header must NOT bypass auth in strict mode
    assert strict_client.get("/api/projects", headers={"X-Role": "owner"}).status_code == 401


def test_register_login_refresh_rotation_and_logout_all(strict_client):
    reg = _register_user(strict_client, "alice@example.com")
    assert reg["expires_in"] == 7200
    assert reg["user"]["user_id"].startswith("usr_")
    assert reg["user"]["email_verified"] is True
    assert reg["user"]["has_local_credential"] is True

    access_1 = reg["access_token"]
    refresh_1 = reg["refresh_token"]

    # Profile accessible with access token
    r = strict_client.get("/api/user/profile", headers={"Authorization": f"Bearer {access_1}"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"

    # Refresh rotates token and invalidates old refresh token
    r = strict_client.post("/api/auth/refresh", json={"refresh_token": refresh_1})
    assert r.status_code == 200
    access_2 = r.json()["access_token"]
    refresh_2 = r.json()["refresh_token"]
    assert refresh_2 != refresh_1

    # Replaying old refresh token fails
    assert strict_client.post("/api/auth/refresh", json={"refresh_token": refresh_1}).status_code == 401

    # Logout all immediately invalidates existing access & refresh tokens
    r = strict_client.post("/api/auth/logout-all", headers={"Authorization": f"Bearer {access_2}"})
    assert r.status_code == 200
    assert strict_client.get("/api/user/profile", headers={"Authorization": f"Bearer {access_2}"}).status_code == 401
    assert strict_client.post("/api/auth/refresh", json={"refresh_token": refresh_2}).status_code == 401

    # Can login again with email + password
    r = strict_client.post("/api/auth/login", json={"email": "alice@example.com", "password": "StrongPass123!"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_oauth_login_multibind_email_password_and_lockout_protection(strict_client, monkeypatch):
    async def fake_exchange(provider: str, code: str) -> auth.OAuthProfile:
        if provider == "github":
            return auth.OAuthProfile("github", "gh-1001", "octo", None, "https://example.com/gh.png")
        if provider == "google":
            return auth.OAuthProfile("google", "gg-2002", "Octo Google", "octo@example.com", None)
        return auth.OAuthProfile("linuxdo", "ld-3003", "octo_ld", None, None)

    monkeypatch.setattr(auth, "exchange_oauth_code", fake_exchange)

    # Login via GitHub (no email returned). The callback now 302-redirects to
    # the frontend /auth/callback page with tokens in the URL fragment instead
    # of returning raw JSON.
    r = strict_client.get("/api/auth/oauth/github/authorize?mode=login")
    assert r.status_code == 200
    state = r.json()["state"]
    frag = _oauth_callback_fragment(strict_client, f"/api/auth/oauth/github/callback?code=abc&state={state}")
    assert frag["provider"] == ["github"]
    assert frag["token_type"] == ["Bearer"]
    assert frag["expires_in"] == ["7200"]
    assert frag["refresh_token"][0]
    token = frag["access_token"][0]
    headers = {"Authorization": f"Bearer {token}"}
    r = strict_client.get("/api/user/profile", headers=headers)
    assert r.status_code == 200
    assert r.json()["has_local_credential"] is False
    assert r.json()["email_verified"] is False

    # Cannot set password before verifying an email
    r = strict_client.post("/api/user/profile/password", json={"new_password": "Password123!"}, headers=headers)
    assert r.status_code == 400

    # Cannot unbind the sole OAuth login without a local password
    r = strict_client.delete("/api/user/profile/oauth/bindings/github", headers=headers)
    assert r.status_code == 400

    # Bind Linux DO OAuth
    r = strict_client.get("/api/auth/oauth/linuxdo/authorize?mode=bind", headers=headers)
    assert r.status_code == 200
    bind_state = r.json()["state"]
    frag = _oauth_callback_fragment(strict_client, f"/api/auth/oauth/linuxdo/callback?code=xyz&state={bind_state}")
    assert frag["bound"] == ["true"]
    assert frag["provider"] == ["linuxdo"]

    # Check bindings list includes all 3 available providers
    r = strict_client.get("/api/user/profile/oauth/bindings", headers=headers)
    assert r.status_code == 200
    assert r.json()["available_providers"] == ["github", "google", "linuxdo"]
    assert {b["provider"] for b in r.json()["bindings"]} == {"github", "linuxdo"}

    # Now unbinding github succeeds because linuxdo remains
    assert strict_client.delete("/api/user/profile/oauth/bindings/github", headers=headers).status_code == 200

    # Bind email via verification code, then setting local password succeeds
    strict_client.post("/api/auth/email/send-code", json={"email": "octo@example.com", "purpose": "bind_email"}, headers=headers)
    code = auth.get_last_sent_code("octo@example.com", "bind_email")
    r = strict_client.post(
        "/api/user/profile/email/bind",
        json={"email": "octo@example.com", "code": code, "purpose": "bind_email"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["email_verified"] is True

    r = strict_client.post("/api/user/profile/password", json={"new_password": "Password123!"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["has_local_credential"] is True


def test_avatar_upload_validation_and_immediate_old_url_invalidation(strict_client):
    reg = _register_user(strict_client, "avatar@example.com")
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    # Reject non-image MIME & >2 MiB
    r = strict_client.post(
        "/api/user/profile/avatar/upload-url",
        json={"filename": "bad.pdf", "content_type": "application/pdf", "size_bytes": 100},
        headers=headers,
    )
    assert r.status_code == 400
    r = strict_client.post(
        "/api/user/profile/avatar/upload-url",
        json={"filename": "huge.png", "content_type": "image/png", "size_bytes": 3 * 1024 * 1024},
        headers=headers,
    )
    assert r.status_code == 413

    # Upload first avatar
    r = strict_client.post(
        "/api/user/profile/avatar/upload-url",
        json={"filename": "a1.png", "content_type": "image/png", "size_bytes": 16},
        headers=headers,
    )
    assert r.status_code == 200
    up1 = r.json()
    put_path_1 = urlparse(up1["upload_url"]).path + "?" + urlparse(up1["upload_url"]).query
    assert strict_client.put(put_path_1, content=b"\x89PNG\r\n\x1a\n12345678").status_code == 200

    r = strict_client.post("/api/user/profile/avatar/confirm", json={"storage_id": up1["storage_id"]}, headers=headers)
    assert r.status_code == 200
    url1 = r.json()["avatar_url"]
    get_path_1 = urlparse(url1).path + "?" + urlparse(url1).query
    assert strict_client.get(get_path_1).status_code == 200

    # Replace with second avatar -> old URL immediately invalidates (404)
    up2 = strict_client.post(
        "/api/user/profile/avatar/upload-url",
        json={"filename": "a2.webp", "content_type": "image/webp", "size_bytes": 12},
        headers=headers,
    ).json()
    put_path_2 = urlparse(up2["upload_url"]).path + "?" + urlparse(up2["upload_url"]).query
    assert strict_client.put(put_path_2, content=b"RIFF1234WEBP").status_code == 200
    r = strict_client.post("/api/user/profile/avatar/confirm", json={"storage_id": up2["storage_id"]}, headers=headers)
    assert r.status_code == 200
    url2 = r.json()["avatar_url"]
    get_path_2 = urlparse(url2).path + "?" + urlparse(url2).query
    assert strict_client.get(get_path_2).status_code == 200
    assert strict_client.get(get_path_1).status_code == 404
