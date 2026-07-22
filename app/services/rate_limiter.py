"""Simple in-memory rate limiter (sliding window, per key).

Used to guard LLM cost: a per-user per-minute cap on chat calls. Configured
via RATE_LIMIT_PER_MIN (0 disables). Limitation: state is per-process, so on a
multi-instance deploy you'd swap this for Redis. Fine for a free-tier demo.
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock

from app.core.config import settings


class RateLimiter:
    def __init__(self, max_per_min: int, window_seconds: float = 60.0) -> None:
        self.max_per_min = max_per_min
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        if self.max_per_min <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max_per_min:
                return False
            dq.append(now)
            return True


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(settings.RATE_LIMIT_PER_MIN)
    return _limiter
