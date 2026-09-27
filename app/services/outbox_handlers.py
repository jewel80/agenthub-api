"""Concrete outbox event handlers (scale-doc §3 point 5).

Kept separate from outbox_consumer.py so the consumer machinery stays
generic and each handler stays a small, focused, idempotent function (the
consumer's dedupe-on-event-id already guarantees at-most-once execution
per event id; a handler still shouldn't assume it's never called twice for
the same underlying business fact — see the qualifiers below).
"""

from __future__ import annotations

from app.core import redis as redis_client
from app.core.config import settings


async def handle_chat_completed(event_id: str, payload: dict) -> None:
    """Durable, cross-instance chat-usage counter.

    Addresses a known limitation already flagged in
    app/services/observability.py ("same per-process limitation as the
    rate limiter; for production, emit these counters to a metrics
    backend") — this is that backend, for the by-agent breakdown
    specifically. Keyed the same way as the in-process UsageTracker so the
    two are directly comparable.
    """
    agent_slug = payload.get("agent_slug")
    if not agent_slug:
        return
    sub_agent_slug = payload.get("sub_agent_slug")
    key_suffix = f"{agent_slug}::{sub_agent_slug}" if sub_agent_slug else agent_slug
    await redis_client.call(
        "incr", f"agenthub:{settings.ENVIRONMENT}:usage:{key_suffix}"
    )
