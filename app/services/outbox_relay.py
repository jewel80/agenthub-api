"""Transactional outbox relay worker (scale-doc §3).

Polls unpublished `outbox_events` rows and publishes each to a Redis Stream
named after its event type. Runs as an in-process background task
(wired into app/main.py's lifespan) — no separate worker process/deployment
yet; a real worker fleet is the natural next step once volume justifies one.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import redis as redis_client
from app.core.config import settings
from app.models.outbox import OutboxEvent
from app.repositories import outbox_repo

logger = logging.getLogger("agenthub.outbox.relay")


def stream_name(event_type: str) -> str:
    return f"agenthub:{settings.ENVIRONMENT}:outbox:{event_type}"


def dlq_stream_name(event_type: str) -> str:
    return f"agenthub:{settings.ENVIRONMENT}:outbox:{event_type}:dlq"


async def _publish_one(db: AsyncSession, event: OutboxEvent) -> None:
    fields = {"id": str(event.id), "payload": json.dumps(event.payload)}

    if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
        # Already failed enough times on the normal stream — move to the
        # DLQ instead of retrying forever (scale-doc §3 point 4).
        result = await redis_client.call(
            "xadd", dlq_stream_name(event.event_type), fields
        )
        if result is not None:
            logger.error(
                "outbox event moved to DLQ after %d attempts: "
                "type=%s id=%s last_error=%s",
                event.attempts,
                event.event_type,
                event.id,
                event.last_error,
            )
            await outbox_repo.mark_published(db, event)
        else:
            await outbox_repo.record_failure(
                db, event, "DLQ publish failed (Redis down)"
            )
        return

    result = await redis_client.call("xadd", stream_name(event.event_type), fields)
    if result is not None:
        await outbox_repo.mark_published(db, event)
    else:
        await outbox_repo.record_failure(db, event, "Redis unavailable during publish")


async def relay_once(session_factory: async_sessionmaker) -> int:
    """One relay pass: claim a batch, publish each, commit. Returns the
    number of rows the pass touched (published or moved to the DLQ this
    pass — a row that merely had its `attempts` bumped still counts, since
    the pass did process it)."""
    if not settings.OUTBOX_ENABLED:
        return 0
    async with session_factory() as db:
        batch = await outbox_repo.claim_batch(db)
        for event in batch:
            await _publish_one(db, event)
        await db.commit()
        return len(batch)


async def run_relay_loop(session_factory: async_sessionmaker) -> None:
    """Background task: relay repeatedly until cancelled. Never raises into
    the caller — an unexpected error here must not crash the app. A no-op
    loop (returns immediately) when the feature is toggled off."""
    if not settings.OUTBOX_ENABLED:
        return
    while True:
        try:
            await relay_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("outbox relay pass failed", exc_info=True)
        await asyncio.sleep(settings.OUTBOX_RELAY_INTERVAL_SECONDS)
