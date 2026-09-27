"""Transactional outbox (scale-doc §3) — event rows written in the same DB
transaction as the business change they describe, so an event can never be
lost even if the relay/consumer side is down when it's written.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.models.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    # Monotonic ordering key — same rationale as Message.seq: several
    # events committed in one transaction share a `created_at` (Postgres
    # now() is transaction-start time), so the relay's poll order (oldest
    # first) needs an identity column, not created_at (+ id, which is a
    # random UUID with no relationship to insertion order).
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), unique=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Set by the relay worker once published (or moved to the DLQ); rows
    # with published_at IS NULL are what the relay polls (partial index).
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OutboxEvent {self.event_type} id={self.id}>"
