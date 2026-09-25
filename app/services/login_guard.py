"""Login brute-force protection (roadmap §5).

Exponential lockout per (email, agent): after LOGIN_MAX_FAILURES failures in
the tracking window the account key is locked for 30s doubling up to 15 min.
In-process state (single instance); the Redis limiter covers cross-instance
protection for the request rate itself.
"""
from __future__ import annotations

import math
import time
from collections import deque
from threading import Lock

from app.core.config import settings


class LoginGuard:
    def __init__(
        self,
        max_failures: int | None = None,
        base_lock_seconds: float = 30.0,
        max_lock_seconds: float = 900.0,
        window_seconds: float = 900.0,
    ) -> None:
        self.max_failures = max_failures or settings.LOGIN_MAX_FAILURES
        self.base_lock = base_lock_seconds
        self.max_lock = max_lock_seconds
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def retry_after(self, key: str) -> int | None:
        """Seconds the key is still locked for, or None when unlocked."""
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            remaining = until - time.monotonic()
            if remaining > 0:
                return max(1, math.ceil(remaining))
            self._locked_until.pop(key, None)
            return None

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            dq = self._failures.setdefault(key, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            dq.append(now)
            if len(dq) >= self.max_failures:
                # exponential: 30s, 60s, 120s ... capped at 15 min
                exponent = len(dq) - self.max_failures
                lock_for = min(self.base_lock * (2**exponent), self.max_lock)
                self._locked_until[key] = now + lock_for
                dq.clear()

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


_guard: LoginGuard | None = None


def get_login_guard() -> LoginGuard:
    global _guard
    if _guard is None:
        _guard = LoginGuard()
    return _guard
