"""Redis-backed rate limiting + safe degradation (roadmap §5).

Real Redis tests run only when REDIS_URL is reachable (docker compose -f
docker-compose.dev.yml up -d starts one on 127.0.0.1:6380); otherwise they
skip so the suite stays runnable anywhere.

Uses 127.0.0.1, not "localhost": on this Windows dev environment, Python's
getaddrinfo resolves "localhost" to ::1 (IPv6) first, and a Redis server
bound only to IPv4 then times out rather than falling back — a real
connectivity gap, not a typo. See docs/PROGRESS.md M3 §1 notes.
"""
from __future__ import annotations

import pytest

from app.core import redis as redis_mod
from app.core.config import settings
from app.services.rate_limiter import HybridRateLimiter, RedisRateLimiter

TEST_REDIS_URL = "redis://127.0.0.1:6380/0"


@pytest.fixture
async def live_redis():
    """Point the shared client at the local test Redis; skip if unreachable."""
    import asyncio

    saved_url = settings.REDIS_URL
    settings.REDIS_URL = TEST_REDIS_URL
    redis_mod._client = None
    redis_mod._unavailable = False
    try:
        ok = await redis_mod.ping()
        if not ok:
            pytest.skip("Redis not reachable at 127.0.0.1:6380")
        yield redis_mod
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False
        await asyncio.sleep(0)


async def test_redis_ping_and_call(live_redis):
    assert await live_redis.ping() is True
    await live_redis.call("set", "agenthub:test:hello", "1", ex=5)
    assert await live_redis.call("get", "agenthub:test:hello") == "1"
    await live_redis.call("delete", "agenthub:test:hello")


async def test_redis_rate_limiter_blocks_at_limit(live_redis):
    lim = RedisRateLimiter(max_per_min=3, window_seconds=60)
    key = "test-rl-key"
    await live_redis.call("delete", f"agenthub:{settings.ENVIRONMENT}:rl:{key}")
    states = [await lim.hit(key) for _ in range(4)]
    assert [s.allowed for s in states] == [True, True, True, False]
    assert states[0].limit == 3
    assert states[2].remaining == 0
    assert states[3].reset_after >= 1


async def test_redis_rate_limiter_window_expires(live_redis):
    lim = RedisRateLimiter(max_per_min=1, window_seconds=1)
    key = "test-rl-window"
    await live_redis.call("delete", f"agenthub:{settings.ENVIRONMENT}:rl:{key}")
    assert (await lim.hit(key)).allowed is True
    assert (await lim.hit(key)).allowed is False
    import asyncio

    await asyncio.sleep(1.1)
    assert (await lim.hit(key)).allowed is True


async def test_hybrid_uses_redis_when_available(live_redis):
    lim = HybridRateLimiter(max_per_min=2)
    key = "test-hybrid"
    await live_redis.call("delete", f"agenthub:{settings.ENVIRONMENT}:rl:{key}")
    assert (await lim.hit(key)).allowed is True
    assert (await lim.hit(key)).allowed is True
    blocked = await lim.hit(key)
    assert blocked.allowed is False
    assert blocked.limit == 2


async def test_degradation_when_redis_down(monkeypatch):
    """Unreachable Redis -> requests are still allowed (memory backstop)."""
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = "redis://localhost:6399/0"  # nothing listens there
    redis_mod._client = None
    redis_mod._unavailable = False
    try:
        lim = HybridRateLimiter(max_per_min=2)
        results = [(await lim.hit("degrade")).allowed for _ in range(3)]
        assert results == [True, True, False]  # in-memory backstop enforced
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False


async def test_no_redis_configured_is_fine():
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = ""
    redis_mod._client = None
    redis_mod._unavailable = False
    try:
        assert await redis_mod.ping() is False
        assert await redis_mod.call("get", "anything") is None
    finally:
        settings.REDIS_URL = saved_url
        redis_mod._client = None
