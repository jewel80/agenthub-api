"""Multi-layer cache: in-process L1 (TTLCache) -> Redis L2 -> loader.

`get_or_load` / `invalidate` are the only cache entry point (scale-doc
§2.3.1) — no other module talks to Redis for caching directly. Cache-aside
with versioned namespaces (invalidation = INCR, no KEYS/SCAN deletes), TTL
jitter, a short stampede lock, negative caching, stale-while-revalidate, and
cross-instance L1 invalidation via Redis pub/sub. Any Redis error (or Redis
simply being unconfigured) degrades straight to the loader — a cache outage
must never become an API outage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from cachetools import TTLCache
from pydantic import BaseModel

from app.core import redis as redis_client
from app.core.config import settings

logger = logging.getLogger("agenthub.cache")

_L1_TTL_SECONDS = 30  # scale-doc §2.3.9: L1 is short-lived by design
_STAMPEDE_LOCK_MS = 5000
_STAMPEDE_POLL_INTERVAL = 0.02
_STAMPEDE_POLL_ATTEMPTS = 10  # ~200ms total, per scale-doc §2.3.6
_MAX_STALE_WINDOW_SECONDS = 60
_NULL = "null"  # explicit "cached negative" sentinel, distinct from a miss

_l1: dict[str, TTLCache] = {}
_background_tasks: set[asyncio.Task] = set()


def _env() -> str:
    return settings.ENVIRONMENT


def _l1_cache(namespace: str) -> TTLCache:
    cache = _l1.get(namespace)
    if cache is None:
        cache = TTLCache(maxsize=settings.CACHE_L1_MAX_ITEMS, ttl=_L1_TTL_SECONDS)
        _l1[namespace] = cache
    return cache


def _version_key(namespace: str) -> str:
    return f"agenthub:{_env()}:ver:{namespace}"


def invalidate_channel() -> str:
    return f"agenthub:{_env()}:cache-invalidate"


def make_key(namespace: str, ident: str, *, version: int) -> str:
    """Key convention: agenthub:{env}:{namespace}:v{version}:{ident}."""
    return f"agenthub:{_env()}:{namespace}:v{version}:{ident}"


def hash_query(**kwargs: Any) -> str:
    """Stable short hash of query kwargs — for keys like catalog filters."""
    raw = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _jittered_ttl(ttl: int) -> int:
    pct = settings.CACHE_TTL_JITTER_PCT
    if pct <= 0:
        return ttl
    spread = ttl * pct / 100
    return max(1, int(ttl + random.uniform(-spread, spread)))


def _serialize(value: Any, model: type[BaseModel] | None) -> str:
    """Cache Pydantic DTOs (or plain JSON-safe values), never ORM objects."""
    if model is None:
        return json.dumps(value)
    if isinstance(value, list):
        return json.dumps([v.model_dump(mode="json") for v in value])
    return value.model_dump_json()


def _deserialize(raw: str, model: type[BaseModel] | None) -> Any:
    if model is None:
        return json.loads(raw)
    data = json.loads(raw)
    if isinstance(data, list):
        return [model.model_validate(item) for item in data]
    return model.model_validate(data)


def _read_envelope(raw: str, model: type[BaseModel] | None) -> tuple[Any, float]:
    obj = json.loads(raw)
    if obj["d"] == _NULL:
        return None, obj["fresh_until"]
    return _deserialize(obj["d"], model), obj["fresh_until"]


async def _version(namespace: str) -> int:
    if not settings.CACHE_ENABLED:
        return 0
    raw = await redis_client.call("get", _version_key(namespace))
    # Redis INCR on a missing key starts at 1, so the "never invalidated"
    # default must be 0 — otherwise the first invalidate() would bump the
    # version to 1, which is indistinguishable from the pre-invalidate
    # default and old data would keep matching the "new" key.
    return int(raw) if raw is not None else 0


async def invalidate(namespace: str) -> None:
    """Bump the namespace version and clear it everywhere (scale-doc §2.3.3).

    Old keys become unreachable and simply expire through their own TTL —
    no `KEYS`/`SCAN` deletes. Clears this instance's L1 immediately and
    publishes so other instances clear theirs too (scale-doc §2.3.10).
    """
    if not settings.CACHE_ENABLED:
        return
    await redis_client.call("incr", _version_key(namespace))
    _l1_cache(namespace).clear()
    await redis_client.call("publish", invalidate_channel(), namespace)


def _spawn(coro: Awaitable[None]) -> None:
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _store(
    key: str, value: Any, *, ttl: int, negative: bool, model: type[BaseModel] | None
) -> None:
    if value is None and not negative:
        return  # nothing to cache and negative caching wasn't requested
    actual_ttl = (
        settings.CACHE_NEGATIVE_TTL_SECONDS if value is None else _jittered_ttl(ttl)
    )
    data = _NULL if value is None else _serialize(value, model)
    envelope = json.dumps({"d": data, "fresh_until": time.time() + actual_ttl})
    # Redis TTL outlives "fresh_until" so a stale-but-present value can still
    # be served while a refresh happens in the background (scale-doc §2.3.7).
    stale_window = min(actual_ttl, _MAX_STALE_WINDOW_SECONDS)
    await redis_client.call("set", key, envelope, ex=actual_ttl + stale_window)


async def _revalidate(
    namespace: str,
    ident: str,
    loader: Callable[[], Awaitable[Any]],
    *,
    ttl: int,
    negative: bool,
    model: type[BaseModel] | None,
) -> None:
    version = await _version(namespace)
    key = make_key(namespace, ident, version=version)
    lock_key = f"{key}:revalidate-lock"
    got_lock = await redis_client.call(
        "set", lock_key, "1", nx=True, px=_STAMPEDE_LOCK_MS
    )
    if not got_lock:
        return  # another instance/task is already revalidating this key
    try:
        value = await loader()
        await _store(key, value, ttl=ttl, negative=negative, model=model)
        if settings.CACHE_L1_ENABLED and (value is not None or negative):
            _l1_cache(namespace)[key] = value
    except Exception:
        logger.warning("cache revalidate failed for %s", key, exc_info=True)
    finally:
        if got_lock:
            await redis_client.call("delete", lock_key)


def _handle_hit(
    namespace: str,
    ident: str,
    key: str,
    raw: str,
    loader: Callable[[], Awaitable[Any]],
    *,
    ttl: int,
    negative: bool,
    model: type[BaseModel] | None,
    l1: TTLCache | None,
) -> Any:
    value, fresh_until = _read_envelope(raw, model)
    if l1 is not None and (value is not None or negative):
        l1[key] = value
    if time.time() >= fresh_until:
        _spawn(
            _revalidate(
                namespace, ident, loader, ttl=ttl, negative=negative, model=model
            )
        )
    return value


async def get_or_load(
    namespace: str,
    ident: str,
    loader: Callable[[], Awaitable[Any]],
    *,
    ttl: int | None = None,
    negative: bool = False,
    model: type[BaseModel] | None = None,
) -> Any:
    """Cache-aside: L1 -> L2 (Redis) -> loader (scale-doc §2.3.4).

    `model` is the Pydantic type (or a list of it) used to (de)serialize —
    the cache never stores ORM objects. `negative=True` also caches a
    `None` result (e.g. "agent not found") for `CACHE_NEGATIVE_TTL_SECONDS`.
    """
    if not settings.CACHE_ENABLED:
        return await loader()

    ttl = settings.CACHE_DEFAULT_TTL_SECONDS if ttl is None else ttl
    version = await _version(namespace)
    key = make_key(namespace, ident, version=version)
    l1 = _l1_cache(namespace) if settings.CACHE_L1_ENABLED else None

    if l1 is not None and key in l1:
        return l1[key]

    raw = await redis_client.call("get", key)
    if raw is not None:
        return _handle_hit(
            namespace,
            ident,
            key,
            raw,
            loader,
            ttl=ttl,
            negative=negative,
            model=model,
            l1=l1,
        )

    # Miss: stampede protection (scale-doc §2.3.6) — one loader wins the
    # short lock; others wait briefly for it to populate the cache, or load
    # themselves if Redis is already known down (no point waiting on it).
    lock_key = f"{key}:lock"
    got_lock = await redis_client.call(
        "set", lock_key, "1", nx=True, px=_STAMPEDE_LOCK_MS
    )
    if not got_lock and not redis_client.is_unavailable():
        for _ in range(_STAMPEDE_POLL_ATTEMPTS):
            await asyncio.sleep(_STAMPEDE_POLL_INTERVAL)
            raw = await redis_client.call("get", key)
            if raw is not None:
                return _handle_hit(
                    namespace,
                    ident,
                    key,
                    raw,
                    loader,
                    ttl=ttl,
                    negative=negative,
                    model=model,
                    l1=l1,
                )
        # still empty after waiting (lock holder stalled) — load ourselves.

    try:
        value = await loader()
        await _store(key, value, ttl=ttl, negative=negative, model=model)
        if l1 is not None and (value is not None or negative):
            l1[key] = value
        return value
    finally:
        if got_lock:
            await redis_client.call("delete", lock_key)


def _handle_invalidation_message(namespace: str) -> None:
    """Clear this instance's L1 for `namespace` (scale-doc §2.3.10). Split
    out from the listener loop so it's directly unit-testable."""
    _l1_cache(namespace).clear()


async def run_invalidation_listener() -> None:
    """Subscribe to the cross-instance invalidation channel for the process
    lifetime, clearing this instance's L1 whenever another instance bumps a
    namespace's version. Reconnects lazily; never raises into the caller —
    a Redis outage degrades this to a no-op, not a crash.
    """
    from redis import exceptions as redis_exceptions

    while True:
        client = await redis_client.get_redis()
        if client is None:
            await asyncio.sleep(5)
            continue
        try:
            pubsub = client.pubsub()
            async with pubsub:
                await pubsub.subscribe(invalidate_channel())
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    _handle_invalidation_message(message["data"])
        except asyncio.CancelledError:
            raise
        except redis_exceptions.RedisError:
            await asyncio.sleep(2)
        except Exception:
            logger.warning("cache invalidation listener error", exc_info=True)
            await asyncio.sleep(2)
