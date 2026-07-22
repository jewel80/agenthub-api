"""Basic in-memory usage observability — which agents/sub-agents get used.

Exposes a snapshot via `GET /meta/usage`. Same per-process limitation as the
rate limiter; for production, emit these counters to a metrics backend.
"""
from __future__ import annotations

from threading import Lock


class UsageTracker:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._total = 0
        self._lock = Lock()

    def record(self, agent_slug: str, sub_agent_slug: str | None = None) -> None:
        key = agent_slug + (f"::{sub_agent_slug}" if sub_agent_slug else "")
        with self._lock:
            self._total += 1
            self._counts[key] = self._counts.get(key, 0) + 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            by_target = dict(
                sorted(self._counts.items(), key=lambda kv: kv[1], reverse=True)
            )
        return {"total_chats": self._total, "by_target": by_target}


_tracker = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    return _tracker
