"""CacheService: hit/miss/invalidate, stampede protection, negative caching,
Redis-down degradation, and cross-instance L1 invalidation (scale-doc §2.8).

Real-Redis tests run only when Redis is reachable at 127.0.0.1:6380 (same
convention as tests/test_redis_limiter.py, including the 127.0.0.1-not-
localhost note there); the module also has to work (correctly, just
without L2/pub-sub) when Redis is absent, covered below.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import BaseModel

from app.core import redis as redis_mod
from app.core.config import settings
from app.services import cache

TEST_REDIS_URL = "redis://127.0.0.1:6380/0"


def _fresh_namespace(label: str) -> str:
    """A per-test-run-unique namespace. Real Redis (unlike the ephemeral
    fakeredis used for local dev smoke checks) persists data across test
    runs with a genuine TTL, so a fixed namespace name can collide with
    leftover data from a previous run within that TTL — a fresh unique
    namespace sidesteps that entirely rather than trying to enumerate and
    delete every versioned key by hand."""
    return f"t-{label}-{uuid.uuid4().hex[:12]}"


class _Item(BaseModel):
    id: int
    name: str


@pytest.fixture(autouse=True)
def _cache_enabled(monkeypatch):
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "CACHE_L1_ENABLED", True)
    cache._l1.clear()
    yield
    cache._l1.clear()


@pytest.fixture
async def live_redis():
    """Point the shared client at the local test Redis; skip if unreachable."""
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


async def test_hit_miss_invalidate_cycle(live_redis):
    ns = _fresh_namespace("cycle")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return _Item(id=1, name=f"call-{calls}")

    first = await cache.get_or_load(ns, "k1", loader, model=_Item)
    assert first.name == "call-1"
    assert calls == 1

    # L1 hit: no loader call, no Redis round trip needed.
    second = await cache.get_or_load(ns, "k1", loader, model=_Item)
    assert second.name == "call-1"
    assert calls == 1

    # Force an L2-only hit by clearing L1 but leaving Redis populated.
    cache._l1.clear()
    third = await cache.get_or_load(ns, "k1", loader, model=_Item)
    assert third.name == "call-1"
    assert calls == 1

    # Invalidate (version bump) -> old data is unreachable, loader runs again.
    await cache.invalidate(ns)
    fourth = await cache.get_or_load(ns, "k1", loader, model=_Item)
    assert fourth.name == "call-2"
    assert calls == 2


async def test_negative_caching_avoids_repeat_loads(live_redis):
    ns = _fresh_namespace("neg")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return None  # "not found"

    first = await cache.get_or_load(ns, "missing", loader, negative=True, model=_Item)
    assert first is None
    assert calls == 1

    cache._l1.clear()  # force an L2 read
    second = await cache.get_or_load(ns, "missing", loader, negative=True, model=_Item)
    assert second is None
    assert calls == 1  # negative result served from Redis, loader not re-run


async def test_stampede_50_concurrent_misses_one_load(live_redis):
    ns = _fresh_namespace("stampede")
    calls = 0

    async def slow_loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return _Item(id=1, name="loaded-once")

    results = await asyncio.gather(
        *(cache.get_or_load(ns, "hot-key", slow_loader, model=_Item) for _ in range(50))
    )
    assert all(r.name == "loaded-once" for r in results)
    assert calls == 1


async def test_redis_down_still_returns_correct_data(monkeypatch):
    """A Redis outage degrades to the loader every time — never wrong data,
    never a crash (scale-doc §2.3.12)."""
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = "redis://localhost:6399/0"  # nothing listens there
    redis_mod._client = None
    redis_mod._unavailable = False
    # Isolate: prove the L2/Redis path itself degrades safely (L1 alone
    # would otherwise mask a broken Redis path after the first call).
    monkeypatch.setattr(settings, "CACHE_L1_ENABLED", False)
    try:
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return _Item(id=2, name="from-db")

        for _ in range(3):
            result = await cache.get_or_load(
                "t-cache-down", "k", loader, model=_Item
            )
            assert result.name == "from-db"
        assert calls == 3  # no L1, Redis down -> every call reloads, correctly
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False


def test_invalidation_message_clears_l1_for_that_namespace():
    """Simulates a second app instance's listener receiving the pub/sub
    message: it must clear only the named namespace's L1 (scale-doc §2.3.10)."""
    cache._l1_cache("ns-a")["some-key"] = "cached-value"
    cache._l1_cache("ns-b")["other-key"] = "other-value"

    cache._handle_invalidation_message("ns-a")

    assert "some-key" not in cache._l1_cache("ns-a")
    assert cache._l1_cache("ns-b")["other-key"] == "other-value"


async def test_invalidate_publishes_to_the_cross_instance_channel(live_redis):
    """A second instance subscribed to the channel receives the namespace
    name when this instance calls invalidate() (scale-doc §2.3.10)."""
    ns = _fresh_namespace("pubsub")
    client = await redis_mod.get_redis()
    pubsub = client.pubsub()
    async with pubsub:
        await pubsub.subscribe(cache.invalidate_channel())
        await pubsub.get_message(timeout=1)  # discard the subscribe confirmation

        await cache.invalidate(ns)

        message = None
        for _ in range(20):
            message = await pubsub.get_message(timeout=0.2)
            if message and message["type"] == "message":
                break
        assert message is not None
        assert message["data"] == ns
