"""Production operations & bootstrap CLI for Canvasphere Server.

Examples (run inside the `api` container):
  python -m app.cli bootstrap-admin --email admin@example.com --password 'StrongPassword123!'
  python -m app.cli create-redeem-code --code LAUNCH500 --credits 500 --max-uses 100
  python -m app.cli grant-byok --email user@example.com --max-channels 10
  python -m app.cli check-env --strict
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import sys
from typing import Any, Optional

from sqlalchemy import select, text

from app import auth, billing, byok, db
from app.config import get_settings, validate_production_readiness
from app.models import RedeemCode, User, new_prefixed_id, utcnow
from app.redis_client import redis_status


def cmd_bootstrap_admin(
    *,
    email: str,
    password: str,
    display_name: str = "Platform Admin",
) -> dict[str, Any]:
    """Idempotently create or promote a platform administrator account."""
    norm_email = email.strip().lower()
    if "@" not in norm_email or len(password) < 8:
        raise ValueError("valid email and password (>= 8 chars) are required")

    db.init_db()
    with db.session_scope() as session:
        user = session.scalar(select(User).where(User.email == norm_email))
        now = utcnow()
        created = False
        if user is None:
            user = User(
                id=new_prefixed_id("usr"),
                email=norm_email,
                password_hash=auth.hash_password(password),
                display_name=display_name.strip() or "Platform Admin",
                is_active=True,
                roles=["user", "admin"],
                token_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            created = True
        else:
            roles = list(user.roles or ["user"])
            if "admin" not in roles:
                roles.append("admin")
            user.roles = roles
            user.is_active = True
            user.password_hash = auth.hash_password(password)
            user.updated_at = now
            session.add(user)
            session.flush()

        billing.get_or_create_wallet(session, user.id)
        session.commit()
        return {
            "action": "created" if created else "promoted",
            "user_id": user.id,
            "email": user.email,
            "roles": list(user.roles or []),
            "is_active": bool(user.is_active),
        }


def cmd_create_redeem_code(
    *,
    code: str,
    credits: float,
    max_uses: int = 1,
    credit_pool: str = "bonus",
) -> dict[str, Any]:
    """Create or update a redeemable credit code."""
    norm_code = code.strip().upper()
    pool = credit_pool.strip().lower()
    if not norm_code or credits <= 0 or max_uses < 1 or pool not in ("bonus", "cash"):
        raise ValueError("code must be non-empty, credits > 0, max_uses >= 1, pool in (bonus, cash)")

    db.init_db()
    with db.session_scope() as session:
        existing = session.scalar(select(RedeemCode).where(RedeemCode.code == norm_code))
        amount = Decimal(str(credits)).quantize(Decimal("0.0001"))
        if existing is None:
            row = RedeemCode(
                id=new_prefixed_id("rdc"),
                code=norm_code,
                credit_pool=pool,
                credits_amount=amount,
                max_uses=int(max_uses),
                used_count=0,
                is_active=True,
                created_at=utcnow(),
            )
            session.add(row)
            action = "created"
        else:
            existing.credit_pool = pool
            existing.credits_amount = amount
            existing.max_uses = int(max_uses)
            existing.is_active = True
            session.add(existing)
            row = existing
            action = "updated"
        session.commit()
        return {
            "action": action,
            "code": row.code,
            "credit_pool": row.credit_pool,
            "credits": float(row.credits_amount),
            "max_uses": int(row.max_uses),
            "used_count": int(row.used_count),
            "is_active": bool(row.is_active),
        }


def cmd_grant_byok(
    *,
    email: str,
    max_channels: int = 10,
    max_offerings_per_channel: int = 20,
    enabled: bool = True,
) -> dict[str, Any]:
    """Enable or update BYOK entitlement for a user by email."""
    norm_email = email.strip().lower()
    db.init_db()
    with db.session_scope() as session:
        user = session.scalar(select(User).where(User.email == norm_email))
        if user is None:
            raise ValueError(f"user not found: {norm_email}")
        ent = byok.set_entitlement(
            session,
            user.id,
            enabled=enabled,
            max_channels=max_channels,
            max_offerings_per_channel=max_offerings_per_channel,
        )
        return {
            "user_id": user.id,
            "email": user.email,
            "enabled": ent["enabled"],
            "max_channels": ent["max_channels"],
            "max_offerings_per_channel": ent["max_offerings_per_channel"],
        }


def cmd_check_env() -> dict[str, Any]:
    """Inspect runtime configuration, DB connectivity, and Redis status."""
    settings = get_settings()
    issues = validate_production_readiness(settings)
    db_ok = True
    db_error: Optional[str] = None
    try:
        db.init_db()
        with db.session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)

    r_status = redis_status()
    return {
        "db_ok": db_ok,
        "db_kind": db.db_kind(),
        "db_error": db_error,
        "redis_status": r_status,
        "storage_backend": settings.effective_storage_backend,
        "async_worker_enabled": settings.async_worker_enabled,
        "production_strict": settings.production_strict,
        "issues": issues,
        "ok": db_ok and len(issues) == 0,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Canvasphere production ops CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_admin = sub.add_parser("bootstrap-admin", help="Create or promote a platform admin user")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--password", required=True)
    p_admin.add_argument("--display-name", default="Platform Admin")

    p_rdm = sub.add_parser("create-redeem-code", help="Create or update a credit redeem code")
    p_rdm.add_argument("--code", required=True)
    p_rdm.add_argument("--credits", type=float, required=True)
    p_rdm.add_argument("--max-uses", type=int, default=1)
    p_rdm.add_argument("--pool", default="bonus", choices=["bonus", "cash"])

    p_byok = sub.add_parser("grant-byok", help="Enable BYOK entitlement for a user")
    p_byok.add_argument("--email", required=True)
    p_byok.add_argument("--max-channels", type=int, default=10)
    p_byok.add_argument("--disable", action="store_true")

    p_chk = sub.add_parser("check-env", help="Validate production readiness and dependencies")
    p_chk.add_argument("--strict", action="store_true", help="Exit 1 if any readiness issue is found")

    args = parser.parse_args(argv)
    if args.command == "bootstrap-admin":
        res = cmd_bootstrap_admin(email=args.email, password=args.password, display_name=args.display_name)
    elif args.command == "create-redeem-code":
        res = cmd_create_redeem_code(code=args.code, credits=args.credits, max_uses=args.max_uses, credit_pool=args.pool)
    elif args.command == "grant-byok":
        res = cmd_grant_byok(email=args.email, max_channels=args.max_channels, enabled=not args.disable)
    else:
        res = cmd_check_env()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if args.strict and not res["ok"]:
            return 1
        return 0

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
