"""Shared async Redis client with safe degradation.

Redis is an OPTIONAL dependency at runtime: when REDIS_URL is empty, or the
server is unreachable, every helper here reports "unavailable" quickly and
callers fall back to their in-process behavior (in-memory rate limiting,
no caching). A Redis outage must never become an API outage (roadmap §5,
scale doc §2.3.12).
"""
from __future__ import annotations

import logging
from typing import Any

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis, from_url

from app.core.config import settings

logger = logging.getLogger("agenthub.redis")

# Fail fast: a slow Redis must not add latency to every request.
_REDIS_TIMEOUT_SECONDS = 0.2

_client: Redis | None = None
_unavailable = False


def _build_client() -> Redis | None:
    if not settings.REDIS_URL:
        return None
    return from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        health_check_interval=30,
    )


async def get_redis() -> Redis | None:
    """Return a shared async client, or None when Redis is not configured."""
    global _client, _unavailable
    if _unavailable or not settings.REDIS_URL:
        return None
    if _client is None:
        _client = _build_client()
    return _client


async def ping() -> bool:
    """True when Redis answers PING within the timeout; marks availability."""
    global _unavailable
    client = await get_redis()
    if client is None:
        return False
    try:
        await client.ping()
        _unavailable = False
        return True
    except redis_exceptions.RedisError:
        _unavailable = True
        return False


async def call(cmd: str, *args: Any, **kwargs: Any) -> Any:
    """Run a client command; returns None on any Redis error (degrade).

    Usage: `await redis.call("set", key, value, ex=60)`.
    Errors are logged once per state change at WARNING, never raised.
    """
    global _unavailable
    client = await get_redis()
    if client is None:
        return None
    try:
        result = await getattr(client, cmd)(*args, **kwargs)
        _unavailable = False
        return result
    except redis_exceptions.RedisError as exc:
        if not _unavailable:
            logger.warning("redis unavailable, degrading: %s", type(exc).__name__)
        _unavailable = True
        return None


def is_unavailable() -> bool:
    """Fast, non-blocking check: True once Redis has been marked unavailable.

    Lets callers (e.g. the cache service's stampede wait) skip an extra
    round of polling during a known outage instead of adding latency to
    every request while Redis is down.
    """
    return _unavailable


async def close() -> None:
    """Close the shared client (app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
