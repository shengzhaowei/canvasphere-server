"""P1 tests: dual-pool credit wallet, pre-freeze/settle/refund, 64 rate cards & redeem codes."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app import auth, billing, db
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


def test_wallet_defaults_rate_cards_count_and_redeem_codes(strict_client):
    uid, h_user = _make_user(strict_client, "buyer@example.com", "Buyer")
    _adm, h_admin = _make_user(strict_client, "admin@example.com", "Admin", is_admin=True)

    # New user starts with 300 bonus credits
    w = strict_client.get("/api/user/billing/wallet", headers=h_user).json()
    assert w["bonus_credits"] == 300.0
    assert w["cash_credits"] == 0.0
    assert w["available_credits"] == 300.0
    assert w["exchange_rate"] == "100 credits = 1 CNY"

    # Price explanation lists all 64 enabled rate cards
    pe = strict_client.get("/api/user/billing/price-explanation").json()
    assert pe["total_enabled_rate_cards"] == 64
    assert pe["counts_by_modality"]["text"] == 4
    assert pe["counts_by_modality"]["image"] == 15
    assert pe["counts_by_modality"]["video"] == 40
    assert pe["counts_by_modality"]["tts"] == 3
    assert pe["counts_by_modality"]["asr"] == 1
    assert pe["counts_by_modality"]["music"] == 1

    # Non-admin cannot create redeem codes -> 403
    assert strict_client.post("/api/admin/billing/redeem-codes", json={"credits_amount": 500}, headers=h_user).status_code == 403

    # Admin creates cash redeem code -> user redeems once -> replay returns 404/409
    rc = strict_client.post(
        "/api/admin/billing/redeem-codes",
        json={"credits_amount": 250, "credit_pool": "cash", "max_uses": 1},
        headers=h_admin,
    ).json()["codes"][0]["code"]

    r = strict_client.post("/api/user/billing/redeem", json={"code": rc}, headers=h_user)
    assert r.status_code == 200
    assert r.json()["credits_credited"] == 250.0
    assert r.json()["wallet"]["cash_credits"] == 250.0
    assert r.json()["wallet"]["available_credits"] == 550.0

    # Re-redeeming exhausted single-use code fails
    assert strict_client.post("/api/user/billing/redeem", json={"code": rc}, headers=h_user).status_code in (404, 409)


def test_pre_freeze_bonus_first_settlement_and_release_transactions(strict_client):
    uid, h_user = _make_user(strict_client, "txn@example.com", "TxnUser")
    _adm, h_admin = _make_user(strict_client, "adm2@example.com", "Admin", is_admin=True)

    # Top up 100 cash credits (total 300 bonus + 100 cash = 400)
    rc = strict_client.post(
        "/api/admin/billing/redeem-codes",
        json={"credits_amount": 100, "credit_pool": "cash"},
        headers=h_admin,
    ).json()["codes"][0]["code"]
    strict_client.post("/api/user/billing/redeem", json={"code": rc}, headers=h_user)

    with db.session_scope() as s:
        # Insufficient credits (>400) raises 402
        with pytest.raises(HTTPException) as exc_info:
            billing.reserve_credits(s, user_id=uid, estimated_credits=Decimal("500"), source_type="generation", source_id="g0")
        assert exc_info.value.status_code == 402

        # Reserve 350 -> freezes 300 bonus + 50 cash
        res = billing.reserve_credits(s, user_id=uid, estimated_credits=Decimal("350"), source_type="generation", source_id="g1")
        assert res.reserved_bonus == Decimal("300.0000")
        assert res.reserved_cash == Decimal("50.0000")

        # Settle actual 320 -> deducts 300 bonus + 20 cash, releases remaining 30 cash
        billing.settle_reservation(s, res.id, actual_credits=Decimal("320"), modality="video", model_key="wan3.0-video-720p")

    w = strict_client.get("/api/user/billing/wallet", headers=h_user).json()
    assert w["bonus_credits"] == 0.0
    assert w["cash_credits"] == 80.0
    assert w["reserved_bonus_credits"] == 0.0
    assert w["reserved_cash_credits"] == 0.0
    assert w["available_credits"] == 80.0

    # Reserve 50 and release on failure -> wallet untouched
    with db.session_scope() as s:
        res2 = billing.reserve_credits(s, user_id=uid, estimated_credits=Decimal("50"), source_type="generation", source_id="g2")
        billing.release_reservation(s, res2.id, record_refund_usage=True, modality="video", model_key="wan3.0-video-720p")

    w2 = strict_client.get("/api/user/billing/wallet", headers=h_user).json()
    assert w2["available_credits"] == 80.0

    # Usage records endpoint filters cleanly
    usage = strict_client.get("/api/user/billing/usage-records?modality=video&payment_source=platform_credits", headers=h_user).json()
    assert usage["total"] == 2
    assert strict_client.get("/api/user/billing/charge-records", headers=h_user).status_code == 200
    assert strict_client.get("/api/user/billing/sandbox-records", headers=h_user).status_code == 200
