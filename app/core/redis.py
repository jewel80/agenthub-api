"""Shared async Redis client with safe degradation.

Redis is an OPTIONAL dependency at runtime: when REDIS_URL is empty, or the
server is unreachable, every helper here reports "unavailable" quickly and
callers fall back to their in-process behavior (in-memory rate limiting,
no caching). A Redis outage must never become an API outage (roadmap §5,
scale doc §2.3.12) — and, just as importantly, must never become a
*permanent* one once Redis comes back (see `_RETRY_COOLDOWN_SECONDS` below).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis, from_url

from app.core.config import settings

logger = logging.getLogger("agenthub.redis")

# Fail fast: a slow Redis must not add latency to a request (roadmap §5 /
# scale-doc §2.3.12 — "timeout <= 200ms" is about a single command's
# latency once connected). Establishing a brand-new connection gets more
# room: a burst of many concurrent first-time callers (e.g. the cache
# stampede guard's 50-concurrent-miss case) can legitimately take longer
# than 200ms to all complete their TCP handshake even against a healthy,
# fast Redis — a tighter shared deadline here caused exactly that (see
# docs/PROGRESS.md M3 §1 "Corrections"): a few slow-to-connect callers
# tripped the circuit breaker for everyone, well before Redis was actually
# unavailable.
_REDIS_COMMAND_TIMEOUT_SECONDS = 0.2
_REDIS_CONNECT_TIMEOUT_SECONDS = 2.0

# Once marked unavailable, how long to wait before allowing a single retry
# attempt through (a classic half-open circuit-breaker step). Without this,
# get_redis() would refuse *forever* after the first failure — nothing else
# in this module ever calls it again to notice Redis came back (see
# docs/PROGRESS.md M3 §3 "Corrections": found via a consumer loop that
# tripped this once and then stayed degraded for the rest of the process).
_RETRY_COOLDOWN_SECONDS = 5.0

_client: Redis | None = None
_unavailable = False
_unavailable_since: float | None = None


def _build_client() -> Redis | None:
    if not settings.REDIS_URL:
        return None
    return from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=_REDIS_COMMAND_TIMEOUT_SECONDS,
        socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SECONDS,
        health_check_interval=30,
    )


def _mark_available() -> None:
    global _unavailable, _unavailable_since
    _unavailable = False
    _unavailable_since = None


def _mark_unavailable() -> None:
    global _unavailable, _unavailable_since
    _unavailable = True
    _unavailable_since = time.monotonic()


def _should_skip_attempt() -> bool:
    """True while still within the post-failure cooldown — skip trying
    again. Once it elapses, let exactly one attempt through; if that also
    fails, `_mark_unavailable()` restarts the cooldown clock."""
    if not _unavailable:
        return False
    since = _unavailable_since or 0.0
    return (time.monotonic() - since) < _RETRY_COOLDOWN_SECONDS


async def get_redis() -> Redis | None:
    """Return a shared async client, or None when Redis is not configured
    or (still, per the cooldown) marked unavailable."""
    global _client
    if not settings.REDIS_URL or _should_skip_attempt():
        return None
    if _client is None:
        _client = _build_client()
    return _client


async def ping() -> bool:
    """True when Redis answers PING within the timeout; marks availability."""
    client = await get_redis()
    if client is None:
        return False
    try:
        await client.ping()
        _mark_available()
        return True
    except redis_exceptions.RedisError:
        _mark_unavailable()
        return False


async def call(cmd: str, *args: Any, **kwargs: Any) -> Any:
    """Run a client command; returns None on any Redis error (degrade).

    Usage: `await redis.call("set", key, value, ex=60)`.
    Errors are logged once per state change at WARNING, never raised.

    A `ResponseError` (the server answered, but the command itself failed —
    e.g. `BUSYGROUP` from a redundant `XGROUP CREATE`, `WRONGTYPE`) does
    *not* trip the `_unavailable` circuit breaker: Redis is reachable and
    healthy, only this one command failed for a domain reason. Only a
    connection-level error (can't reach/talk to the server at all) means
    Redis is actually down. Conflating the two previously meant one
    expected `ResponseError` — e.g. a consumer group that already existed —
    made every *other* Redis-backed feature in the app degrade until the
    next unrelated successful call happened to reset the flag.
    """
    client = await get_redis()
    if client is None:
        return None
    try:
        result = await getattr(client, cmd)(*args, **kwargs)
        _mark_available()
        return result
    except redis_exceptions.ResponseError as exc:
        logger.warning("redis command error (%s): %s", cmd, exc)
        return None
    except redis_exceptions.RedisError as exc:
        if not _unavailable:
            logger.warning("redis unavailable, degrading: %s", type(exc).__name__)
        _mark_unavailable()
        return None


def is_unavailable() -> bool:
    """Fast, non-blocking check: True while Redis is marked unavailable and
    still within its retry cooldown (matches what `get_redis()` would do
    right now, without actually attempting anything).

    Lets callers (e.g. the cache service's stampede wait) skip an extra
    round of polling during a known outage instead of adding latency to
    every request while Redis is down.
    """
    return _should_skip_attempt()


async def close() -> None:
    """Close the shared client (app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
