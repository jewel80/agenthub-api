"""Rate limiting & quotas (roadmap §5).

Layers:
- per-user chat rate limit (sliding window, per minute)
- per-user daily token quota (cost guardrail)
- global LLM concurrency cap (semaphore, protects the provider rate limit)

State lives in Redis when REDIS_URL points at a reachable server so limits
hold across instances; otherwise everything degrades to in-process state
(single-instance behavior). A Redis outage never takes the API down.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import UTC, datetime
from threading import Lock

from app.core import redis
from app.core.config import settings

logger = logging.getLogger("agenthub.ratelimit")

# Sliding-window script: trims old hits, counts, and adds the current one
# only when under the limit — one round trip, atomic.
_ZSET_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local used = redis.call('ZCARD', key)
if used >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  if oldest[2] then
    return {0, limit, math.floor(oldest[2] + window - now)}
  end
  return {0, limit, window}
end
redis.call('ZADD', key, now, now .. '-' .. math.random())
redis.call('PEXPIRE', key, window)
return {1, limit, limit - used - 1}
"""


class LimitState:
    """Result of a rate-limit check, for X-RateLimit-* headers."""

    __slots__ = ("allowed", "limit", "remaining", "reset_after")

    def __init__(
        self, allowed: bool, limit: int, remaining: int, reset_after: int
    ) -> None:
        self.allowed = allowed
        self.limit = limit
        self.remaining = remaining
        self.reset_after = reset_after  # seconds (>=1 when blocked)

    def headers(self, include_reset: bool = True) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if include_reset:
            h["X-RateLimit-Reset"] = str(self.reset_after)
        return h


class RateLimiter:
    """In-memory sliding window (fallback + single-instance default)."""

    def __init__(self, max_per_min: int, window_seconds: float = 60.0) -> None:
        self.max_per_min = max_per_min
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> bool:
        return self.hit(key).allowed

    def hit(self, key: str) -> LimitState:
        if self.max_per_min <= 0:
            return LimitState(True, 0, -1, 0)
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max_per_min:
                reset = max(1, math.ceil(self.window - (now - dq[0])))
                return LimitState(False, self.max_per_min, 0, reset)
            dq.append(now)
            return LimitState(
                True, self.max_per_min, self.max_per_min - len(dq), 0
            )

    def retry_after(self, key: str) -> int:
        if self.max_per_min <= 0:
            return 0
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            remaining = self.window - (time.monotonic() - dq[0])
            return max(1, math.ceil(remaining))


class RedisRateLimiter:
    """Sorted-set sliding window in Redis (shared across instances).

    Degrades per-call: any Redis error logs once and the request is allowed
    (availability over enforcement); the caller also keeps an in-process
    limiter as a backstop.
    """

    def __init__(
        self, max_per_min: int, window_seconds: float = 60.0
    ) -> None:
        self.max_per_min = max_per_min
        self.window = window_seconds
        self._fallback = RateLimiter(max_per_min, window_seconds)

    def check(self, key: str) -> bool:
        return self.hit(key).allowed

    async def hit(self, key: str) -> LimitState:
        if self.max_per_min <= 0:
            return LimitState(True, 0, -1, 0)
        res = await redis.call(
            "eval",
            _ZSET_SCRIPT,
            1,
            f"agenthub:{settings.ENVIRONMENT}:rl:{key}",
            int(time.time() * 1000),
            int(self.window * 1000),
            self.max_per_min,
        )
        if res is None:  # Redis down -> in-process backstop
            return self._fallback.hit(key)
        # The script's 3rd element is overloaded: "remaining" when allowed,
        # "reset seconds" when blocked (see _ZSET_SCRIPT).
        allowed, limit, third = int(res[0]), int(res[1]), int(res[2])
        if allowed:
            return LimitState(True, limit, max(0, third), 0)
        return LimitState(False, limit, 0, third)

    def retry_after(self, key: str) -> int:
        return self._fallback.retry_after(key)  # advisory only


class HybridRateLimiter:
    """Redis-backed limiter with the in-memory limiter as a safety net."""

    def __init__(self, max_per_min: int, window_seconds: float = 60.0) -> None:
        self.max_per_min = max_per_min
        self._redis_impl = RedisRateLimiter(max_per_min, window_seconds)
        self._memory_impl = RateLimiter(max_per_min, window_seconds)

    def check(self, key: str) -> bool:
        return self._memory_impl.check(key)  # sync path (tests / fallback)

    async def hit(self, key: str) -> LimitState:
        state = await self._redis_impl.hit(key)
        if state.allowed:
            # also count in-process so a Redis outage re-enforces locally
            self._memory_impl.hit(key)
        return state

    def retry_after(self, key: str) -> int:
        return self._memory_impl.retry_after(key)


class TokenQuotaLimiter:
    """Per-user daily token quota (roadmap §5/§15).

    Redis counters make it cluster-wide; without Redis an in-process counter
    keeps single-instance enforcement.
    """

    def __init__(self, daily_quota: int) -> None:
        self.daily_quota = daily_quota
        self._local: dict[str, int] = {}
        self._lock = Lock()

    def _key(self, user_id: str) -> str:
        day = datetime.now(UTC).strftime("%Y%m%d")
        return f"agenthub:{settings.ENVIRONMENT}:tokens:{user_id}:{day}"

    def used(self, user_id: str) -> int:
        with self._lock:
            return self._local.get(user_id, 0)

    async def check(self, user_id: str) -> bool:
        """True when the user is still under today's quota."""
        if self.daily_quota <= 0:
            return True
        res = await redis.call("get", self._key(user_id))
        used = int(res) if res is not None else self.used(user_id)
        return used < self.daily_quota

    async def record(self, user_id: str, tokens: int) -> None:
        if self.daily_quota <= 0 or tokens <= 0:
            return
        await redis.call("incrby", self._key(user_id), tokens)
        await redis.call("expire", self._key(user_id), 172800)  # 2 days
        with self._lock:
            self._local[user_id] = self._local.get(user_id, 0) + tokens


_llm_semaphore: asyncio.Semaphore | None = None


def llm_concurrency_cap() -> asyncio.Semaphore | None:
    """Global (per-process) cap on in-flight LLM calls; None disables."""
    global _llm_semaphore
    if settings.GLOBAL_LLM_CONCURRENCY <= 0:
        return None
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(settings.GLOBAL_LLM_CONCURRENCY)
    return _llm_semaphore


_chat_limiter: HybridRateLimiter | None = None


def get_rate_limiter() -> HybridRateLimiter:
    global _chat_limiter
    if _chat_limiter is None:
        _chat_limiter = HybridRateLimiter(settings.RATE_LIMIT_PER_MIN)
    return _chat_limiter


_quota_limiter: TokenQuotaLimiter | None = None


def get_token_quota() -> TokenQuotaLimiter:
    global _quota_limiter
    if _quota_limiter is None:
        _quota_limiter = TokenQuotaLimiter(settings.DAILY_TOKEN_QUOTA_DEFAULT)
    return _quota_limiter


_login_limiter: HybridRateLimiter | None = None


def get_login_rate_limiter() -> HybridRateLimiter:
    """Stricter request-rate limit on login/signup, keyed by (email, agent).

    Separate from LoginGuard's failure-triggered lockout (roadmap §5): this
    caps the raw attempt rate regardless of outcome, so a script hammering
    valid-looking credentials is throttled even before it accumulates enough
    failures to trip the lockout.
    """
    global _login_limiter
    if _login_limiter is None:
        _login_limiter = HybridRateLimiter(settings.LOGIN_RATE_LIMIT_PER_MIN)
    return _login_limiter
