"""Redis client factory & sliding-window rate limiter with zero-dep memory fallback.

When REDIS_URL is configured and reachable (production Docker), all rate limits,
leases, presence, and SSE Pub/Sub share state across multiple API/Worker processes.
When REDIS_URL is unset (unit tests / CI), falls back cleanly to thread-safe memory.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

_sync_client: Any = None
_sync_url: Optional[str] = None
_sync_lock = threading.Lock()


def get_redis_sync() -> Any:
    """Return a connected sync redis.Redis client, or None if unavailable."""
    global _sync_client, _sync_url
    url = get_settings().redis_url
    if not url:
        return None
    with _sync_lock:
        if _sync_client is not None and _sync_url == url:
            return _sync_client
        try:
            import redis
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            client.ping()
            _sync_client = client
            _sync_url = url
            return _sync_client
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis unavailable (%s); falling back to in-memory state", exc)
            _sync_client = None
            _sync_url = None
            return None


def get_redis_async() -> Any:
    """Return an async redis.asyncio.Redis client, or None if REDIS_URL is unset."""
    url = get_settings().redis_url
    if not url:
        return None
    try:
        import redis.asyncio as aioredis
        return aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
    except Exception:
        return None


def redis_status() -> str:
    return "redis" if get_redis_sync() is not None else "memory"


def reset_redis_clients() -> None:
    global _sync_client, _sync_url
    with _sync_lock:
        if _sync_client is not None:
            try:
                _sync_client.close()
            except Exception:
                pass
        _sync_client = None
        _sync_url = None


class RateLimiter:
    """Fixed/sliding-window rate limiter backed by Redis or thread-safe memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def check(self, bucket: str, key: str, limit: int, window_seconds: int = 60) -> None:
        if limit <= 0:
            return
        full_key = f"canvasphere:rl:{bucket}:{key}"
        rc = get_redis_sync()
        if rc is not None:
            try:
                count = int(rc.incr(full_key))
                if count == 1:
                    rc.expire(full_key, window_seconds)
                if count > limit:
                    ttl = max(1, int(rc.ttl(full_key) or window_seconds))
                    raise HTTPException(
                        status_code=429,
                        detail=f"rate limit exceeded for {bucket}; retry in {ttl}s",
                        headers={"Retry-After": str(ttl)},
                    )
                return
            except HTTPException:
                raise
            except Exception:
                pass

        now = time.monotonic()
        cutoff = now - float(window_seconds)
        with self._lock:
            dq = self._buckets.setdefault(full_key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry_after = max(1, int(window_seconds - (now - dq[0])))
                raise HTTPException(
                    status_code=429,
                    detail=f"rate limit exceeded for {bucket}; retry in {retry_after}s",
                    headers={"Retry-After": str(retry_after)},
                )
            dq.append(now)


rate_limiter = RateLimiter()
