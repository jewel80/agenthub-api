"""Unit tests for the in-memory rate limiter (pure logic)."""
from __future__ import annotations

from app.services.rate_limiter import RateLimiter


def test_allows_up_to_limit():
    rl = RateLimiter(max_per_min=2)
    assert rl.check("u") is True
    assert rl.check("u") is True


def test_blocks_over_limit():
    rl = RateLimiter(max_per_min=2)
    rl.check("u")
    rl.check("u")
    assert rl.check("u") is False


def test_keys_are_isolated():
    rl = RateLimiter(max_per_min=1)
    assert rl.check("a") is True
    assert rl.check("b") is True  # different user, independent budget
    assert rl.check("a") is False


def test_disabled_when_zero():
    rl = RateLimiter(max_per_min=0)
    for _ in range(50):
        assert rl.check("u") is True
