"""Outbox repository — all DB access for the transactional outbox
(scale-doc §3). No other module writes to `outbox_events` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.outbox import OutboxEvent


async def add_event(
    db: AsyncSession, *, event_type: str, payload: dict
) -> OutboxEvent | None:
    """Add an event to the *caller's* transaction (flushes, doesn't commit —
    the caller's own commit is what makes the event durable atomically with
    the business change it describes). Returns None (no-op) when the
    feature is toggled off.
    """
    if not settings.OUTBOX_ENABLED:
        return None
    event = OutboxEvent(event_type=event_type, payload=payload)
    db.add(event)
    await db.flush()
    return event


async def claim_batch(
    db: AsyncSession, *, batch_size: int | None = None
) -> Sequence[OutboxEvent]:
    """Lock and return up to `batch_size` unpublished rows, oldest first.

    `FOR UPDATE SKIP LOCKED` (scale-doc §3 point 3): safe for multiple relay
    instances to poll concurrently — each gets a disjoint batch instead of
    blocking on or double-processing the same rows. Caller must be on the
    writer and commit (or rollback) promptly to release the row locks.
    """
    limit = batch_size if batch_size is not None else settings.OUTBOX_BATCH_SIZE
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.seq)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    res = await db.execute(stmt)
    return res.scalars().all()


async def mark_published(db: AsyncSession, event: OutboxEvent) -> None:
    event.published_at = datetime.now(UTC)


async def record_failure(db: AsyncSession, event: OutboxEvent, error: str) -> None:
    """One failed publish attempt. Callers decide (via `attempts`) when to
    give up and move the event to the DLQ instead of retrying forever."""
    event.attempts += 1
    event.last_error = error[:2000]  # bound: never let one error balloon a row


async def cleanup_published(
    db: AsyncSession, *, older_than_days: int | None = None
) -> int:
    """Delete published events older than the retention window (scale-doc
    §3 point 6). Returns the number of rows deleted."""
    days = (
        older_than_days
        if older_than_days is not None
        else settings.OUTBOX_RETENTION_DAYS
    )
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await db.execute(
        delete(OutboxEvent).where(
            OutboxEvent.published_at.is_not(None),
            OutboxEvent.published_at < cutoff,
        )
    )
    return result.rowcount or 0


async def get_by_id(db: AsyncSession, event_id: uuid.UUID) -> OutboxEvent | None:
    res = await db.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
    return res.scalars().first()
