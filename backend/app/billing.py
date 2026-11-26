"""Dual-pool credit wallet, pre-freeze/settle/release transactions, rate estimation & redeem codes.

Exchange rate: 100 credits = 1 CNY.
All arithmetic uses Decimal quantized to 4 decimal places (0.0001 credits).
Deduction priority: bonus_credits first, then cash_credits.
"""
from __future__ import annotations

import math
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import lock_for_update
from app.models import (
    BillingChargeRecord,
    BillingUsageRecord,
    CreditReservation,
    CreditWallet,
    RateCard,
    RedeemCode,
    RedeemCodeRedemption,
    UserVoiceActivation,
    new_prefixed_id,
    utcnow,
)
from app.rate_cards_seed import MJ_ACTIONS_MULTIPLIER, SUNO_OPERATIONS_PRICING, ensure_rate_cards_seeded

Q4 = Decimal("0.0001")
ZERO = Decimal("0.0000")


def _d(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Q4, rounding=ROUND_HALF_UP)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def get_or_create_wallet(db: Session, user_id: str, *, for_update: bool = False) -> CreditWallet:
    stmt = select(CreditWallet).where(CreditWallet.user_id == user_id)
    if for_update:
        stmt = lock_for_update(stmt, db)
    wallet = db.scalar(stmt)
    if wallet is None:
        bonus = _d(get_settings().default_signup_bonus_credits)
        wallet = CreditWallet(
            user_id=user_id,
            bonus_credits=bonus,
            cash_credits=ZERO,
            reserved_bonus_credits=ZERO,
            reserved_cash_credits=ZERO,
            updated_at=utcnow(),
        )
        db.add(wallet)
        db.flush()
    return wallet


def serialize_wallet(wallet: CreditWallet) -> dict[str, Any]:
    avail_bonus = max(ZERO, _d(wallet.bonus_credits) - _d(wallet.reserved_bonus_credits))
    avail_cash = max(ZERO, _d(wallet.cash_credits) - _d(wallet.reserved_cash_credits))
    total_avail = (avail_bonus + avail_cash).quantize(Q4)
    return {
        "user_id": wallet.user_id,
        "exchange_rate": "100 credits = 1 CNY",
        "credits_per_cny": 100,
        "bonus_credits": float(_d(wallet.bonus_credits)),
        "cash_credits": float(_d(wallet.cash_credits)),
        "reserved_bonus_credits": float(_d(wallet.reserved_bonus_credits)),
        "reserved_cash_credits": float(_d(wallet.reserved_cash_credits)),
        "available_bonus_credits": float(avail_bonus),
        "available_cash_credits": float(avail_cash),
        "available_credits": float(total_avail),
        "updated_at": wallet.updated_at.isoformat() if wallet.updated_at else None,
    }


def has_activated_voice(db: Session, user_id: str, voice_id: str) -> bool:
    if not voice_id:
        return True
    row = db.scalar(
        select(UserVoiceActivation).where(
            UserVoiceActivation.user_id == user_id,
            UserVoiceActivation.voice_id == voice_id,
        )
    )
    return row is not None


def estimate_generation_cost(
    db: Session,
    model_key: str,
    params: Optional[dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    ensure_rate_cards_seeded(db)
    card = db.scalar(select(RateCard).where(RateCard.model_key == model_key, RateCard.enabled.is_(True)))
    if card is None:
        raise HTTPException(status_code=404, detail=f"rate card not found or disabled: {model_key}")

    p = params or {}
    base = _d(card.unit_price_credits)
    surcharge = ZERO
    breakdown: list[dict[str, Any]] = []

    if card.modality == "image":
        if card.protocol_profile == "midjourney_proxy":
            action = str(p.get("mj_action") or "imagine").lower()
            mult = _d(MJ_ACTIONS_MULTIPLIER.get(action, "1.0"))
            gen_cost = (base * mult).quantize(Q4)
            breakdown.append({"item": f"mj:{action}", "credits": float(gen_cost)})
        else:
            count = max(1, int(p.get("count", 1)))
            gen_cost = (base * Decimal(count)).quantize(Q4)
            breakdown.append({"item": f"images x{count}", "credits": float(gen_cost)})
    elif card.modality == "video":
        if card.billing_mode == "per_second":
            dur = max(1, int(p.get("duration_seconds", 5)))
            gen_cost = (base * Decimal(dur)).quantize(Q4)
            breakdown.append({"item": f"video {dur}s", "credits": float(gen_cost)})
        else:
            gen_cost = base
            breakdown.append({"item": "video per_call", "credits": float(gen_cost)})
    elif card.modality == "tts":
        mode = str(p.get("mode") or "tts").lower()
        if mode == "voice_design":
            gen_cost = base
            breakdown.append({"item": "voice_design", "credits": float(gen_cost)})
        else:
            text_len = max(1, len(str(p.get("text") or p.get("prompt") or "")))
            k_chars = Decimal(max(1, math.ceil(text_len / 1000)))
            gen_cost = (base * k_chars).quantize(Q4)
            breakdown.append({"item": f"tts {text_len} chars", "credits": float(gen_cost)})
            voice_id = str(p.get("voice_id") or "").strip()
            if mode == "tts" and voice_id and user_id and not has_activated_voice(db, user_id, voice_id):
                surcharge = _d(
                    (card.pricing_meta_json or {}).get(
                        "first_voice_surcharge_credits", get_settings().voice_first_use_credits
                    )
                )
                breakdown.append({"item": f"voice_first_use:{voice_id}", "credits": float(surcharge)})
    elif card.modality == "asr":
        dur = max(1, int(p.get("duration_seconds", 10)))
        gen_cost = (base * Decimal(dur)).quantize(Q4)
        breakdown.append({"item": f"asr {dur}s", "credits": float(gen_cost)})
    elif card.modality == "music":
        op = str(p.get("operation") or p.get("mode") or "song_bgm").lower()
        ops = (card.pricing_meta_json or {}).get("operations") or SUNO_OPERATIONS_PRICING
        if op not in ops:
            raise HTTPException(status_code=400, detail=f"unsupported suno operation: {op}")
        gen_cost = _d(ops[op])
        breakdown.append({"item": f"suno:{op}", "credits": float(gen_cost)})
    else:
        input_tokens = Decimal(int(p.get("input_tokens", 1000)))
        output_tokens = Decimal(int(p.get("output_tokens", 500)))
        out_price = _d(card.output_price_credits or card.unit_price_credits)
        gen_cost = ((input_tokens * base + output_tokens * out_price) / Decimal(1_000_000)).quantize(Q4)
        breakdown.append({"item": "llm_tokens", "credits": float(gen_cost)})

    total = (gen_cost + surcharge).quantize(Q4)
    return {
        "model_key": card.model_key,
        "display_name": card.display_name,
        "modality": card.modality,
        "billing_mode": card.billing_mode,
        "generation_credits": float(gen_cost),
        "surcharge_credits": float(surcharge),
        "estimated_credits": float(total),
        "breakdown": breakdown,
    }


def reserve_credits(
    db: Session,
    *,
    user_id: str,
    estimated_credits: Decimal,
    source_type: str,
    source_id: str,
) -> CreditReservation:
    amount = _d(estimated_credits)
    wallet = get_or_create_wallet(db, user_id, for_update=True)
    avail_bonus = max(ZERO, _d(wallet.bonus_credits) - _d(wallet.reserved_bonus_credits))
    avail_cash = max(ZERO, _d(wallet.cash_credits) - _d(wallet.reserved_cash_credits))
    total_avail = avail_bonus + avail_cash

    if amount > total_avail:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "INSUFFICIENT_CREDITS",
                "message": "insufficient available credits",
                "required_credits": float(amount),
                "available_credits": float(total_avail),
            },
        )

    res_bonus = min(avail_bonus, amount)
    res_cash = (amount - res_bonus).quantize(Q4)

    wallet.reserved_bonus_credits = (_d(wallet.reserved_bonus_credits) + res_bonus).quantize(Q4)
    wallet.reserved_cash_credits = (_d(wallet.reserved_cash_credits) + res_cash).quantize(Q4)
    wallet.updated_at = utcnow()
    db.add(wallet)

    reservation = CreditReservation(
        id=new_prefixed_id("res"),
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
        estimated_credits=amount,
        reserved_bonus=res_bonus,
        reserved_cash=res_cash,
        settled_bonus=ZERO,
        settled_cash=ZERO,
        status="reserved",
        created_at=utcnow(),
    )
    db.add(reservation)
    db.flush()
    return reservation


def settle_reservation(
    db: Session,
    reservation_id: str,
    *,
    actual_credits: Decimal,
    project_id: Optional[str] = None,
    job_id: Optional[str] = None,
    modality: str = "image",
    model_key: str = "",
    payment_source: str = "platform_credits",
    quantity: Optional[dict[str, Any]] = None,
    status_label: str = "settled",
    surcharge_charge: Optional[dict[str, Any]] = None,
) -> BillingUsageRecord:
    res = db.scalar(lock_for_update(select(CreditReservation).where(CreditReservation.id == reservation_id), db))
    if res is None:
        raise HTTPException(status_code=404, detail=f"reservation not found: {reservation_id}")
    if res.status != "reserved":
        raise HTTPException(status_code=409, detail=f"reservation already {res.status}")

    wallet = get_or_create_wallet(db, res.user_id, for_update=True)
    actual = max(ZERO, _d(actual_credits))
    # Cap settlement to reserved amount or deduct additional available if actual > reserved
    res_bonus = _d(res.reserved_bonus)
    res_cash = _d(res.reserved_cash)

    # First release the reservation hold
    wallet.reserved_bonus_credits = max(ZERO, (_d(wallet.reserved_bonus_credits) - res_bonus).quantize(Q4))
    wallet.reserved_cash_credits = max(ZERO, (_d(wallet.reserved_cash_credits) - res_cash).quantize(Q4))

    # Deduct bonus first, then cash
    deduct_bonus = min(_d(wallet.bonus_credits), actual)
    remaining = (actual - deduct_bonus).quantize(Q4)
    deduct_cash = min(_d(wallet.cash_credits), remaining)

    wallet.bonus_credits = (_d(wallet.bonus_credits) - deduct_bonus).quantize(Q4)
    wallet.cash_credits = (_d(wallet.cash_credits) - deduct_cash).quantize(Q4)
    wallet.updated_at = utcnow()
    db.add(wallet)

    now = utcnow()
    res.settled_bonus = deduct_bonus
    res.settled_cash = deduct_cash
    res.status = "settled"
    res.settled_at = now
    db.add(res)

    surcharge_amt = ZERO
    if surcharge_charge and _d(surcharge_charge.get("credits", 0)) > ZERO:
        surcharge_amt = min(actual, _d(surcharge_charge["credits"]))
        # Split surcharge bonus/cash proportionally from settled total
        sc_bonus = min(deduct_bonus, surcharge_amt)
        sc_cash = (surcharge_amt - sc_bonus).quantize(Q4)
        db.add(BillingChargeRecord(
            id=new_prefixed_id("chg"),
            user_id=res.user_id,
            project_id=project_id,
            charge_type=str(surcharge_charge.get("charge_type", "service")),
            description=str(surcharge_charge.get("description", "service charge")),
            credits_charged=surcharge_amt,
            bonus_charged=sc_bonus,
            cash_charged=sc_cash,
            meta_json=dict(surcharge_charge.get("meta") or {}),
            created_at=now,
        ))
        usage_total = max(ZERO, (actual - surcharge_amt).quantize(Q4))
        usage_bonus = max(ZERO, (deduct_bonus - sc_bonus).quantize(Q4))
        usage_cash = max(ZERO, (deduct_cash - sc_cash).quantize(Q4))
    else:
        usage_total = actual
        usage_bonus = deduct_bonus
        usage_cash = deduct_cash

    usage = BillingUsageRecord(
        id=new_prefixed_id("usg"),
        user_id=res.user_id,
        project_id=project_id,
        job_id=job_id,
        modality=modality,
        model_key=model_key,
        payment_source=payment_source,
        quantity_json=dict(quantity or {}),
        credits_charged=usage_total,
        bonus_charged=usage_bonus,
        cash_charged=usage_cash,
        status=status_label,
        created_at=now,
    )
    db.add(usage)
    db.flush()
    return usage


def release_reservation(
    db: Session,
    reservation_id: str,
    *,
    record_refund_usage: bool = False,
    project_id: Optional[str] = None,
    job_id: Optional[str] = None,
    modality: str = "image",
    model_key: str = "",
    status_label: str = "refunded",
) -> None:
    res = db.scalar(lock_for_update(select(CreditReservation).where(CreditReservation.id == reservation_id), db))
    if res is None or res.status != "reserved":
        return
    wallet = get_or_create_wallet(db, res.user_id, for_update=True)
    wallet.reserved_bonus_credits = max(ZERO, (_d(wallet.reserved_bonus_credits) - _d(res.reserved_bonus)).quantize(Q4))
    wallet.reserved_cash_credits = max(ZERO, (_d(wallet.reserved_cash_credits) - _d(res.reserved_cash)).quantize(Q4))
    wallet.updated_at = utcnow()
    db.add(wallet)

    now = utcnow()
    res.status = "released"
    res.settled_at = now
    db.add(res)

    if record_refund_usage:
        db.add(BillingUsageRecord(
            id=new_prefixed_id("usg"),
            user_id=res.user_id,
            project_id=project_id,
            job_id=job_id,
            modality=modality,
            model_key=model_key,
            payment_source="platform_credits",
            quantity_json={},
            credits_charged=ZERO,
            bonus_charged=ZERO,
            cash_charged=ZERO,
            status=status_label,
            created_at=now,
        ))
    db.flush()


def create_redeem_codes(
    db: Session,
    *,
    credits_amount: Decimal,
    count: int = 1,
    credit_pool: str = "bonus",
    max_uses: int = 1,
    prefix: str = "PCN",
    created_by: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> list[RedeemCode]:
    if credit_pool not in ("bonus", "cash"):
        raise HTTPException(status_code=400, detail="credit_pool must be bonus or cash")
    amt = _d(credits_amount)
    if amt <= ZERO:
        raise HTTPException(status_code=400, detail="credits_amount must be positive")
    if count < 1 or count > 100:
        raise HTTPException(status_code=400, detail="count must be 1..100")

    rows: list[RedeemCode] = []
    for _ in range(count):
        token = f"{prefix.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        rc = RedeemCode(
            id=new_prefixed_id("rdc"),
            code=token,
            credit_pool=credit_pool,
            credits_amount=amt,
            max_uses=max(1, int(max_uses)),
            used_count=0,
            is_active=True,
            expires_at=expires_at,
            created_by=created_by,
            created_at=utcnow(),
        )
        db.add(rc)
        rows.append(rc)
    db.commit()
    return rows


def redeem_code(db: Session, user_id: str, raw_code: str) -> dict[str, Any]:
    norm = (raw_code or "").strip().upper()
    if not norm:
        raise HTTPException(status_code=400, detail="redeem code is required")
    rc = db.scalar(lock_for_update(select(RedeemCode).where(RedeemCode.code == norm), db))
    now = utcnow()
    if rc is None or not rc.is_active:
        raise HTTPException(status_code=404, detail="redeem code not found or inactive")
    if rc.expires_at is not None and _as_utc(rc.expires_at) <= now:
        raise HTTPException(status_code=400, detail="redeem code has expired")
    if int(rc.used_count) >= int(rc.max_uses):
        raise HTTPException(status_code=409, detail="redeem code has already been fully redeemed")

    already = db.scalar(
        select(RedeemCodeRedemption).where(
            RedeemCodeRedemption.redeem_code_id == rc.id,
            RedeemCodeRedemption.user_id == user_id,
        )
    )
    if already is not None:
        raise HTTPException(status_code=409, detail="you have already redeemed this code")

    wallet = get_or_create_wallet(db, user_id, for_update=True)
    amt = _d(rc.credits_amount)
    if rc.credit_pool == "cash":
        wallet.cash_credits = (_d(wallet.cash_credits) + amt).quantize(Q4)
    else:
        wallet.bonus_credits = (_d(wallet.bonus_credits) + amt).quantize(Q4)
    wallet.updated_at = now
    db.add(wallet)

    rc.used_count = int(rc.used_count) + 1
    if rc.used_count >= rc.max_uses:
        rc.is_active = False
    db.add(rc)

    db.add(RedeemCodeRedemption(
        id=new_prefixed_id("rdr"),
        redeem_code_id=rc.id,
        user_id=user_id,
        credits_credited=amt,
        credit_pool=rc.credit_pool,
        redeemed_at=now,
    ))
    db.commit()
    return {
        "redeemed": True,
        "code": rc.code,
        "credit_pool": rc.credit_pool,
        "credits_credited": float(amt),
        "wallet": serialize_wallet(wallet),
    }
