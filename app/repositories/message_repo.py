"""Message repository — chat history persistence."""
from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


async def add_message(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    sub_agent_id: uuid.UUID | None,
    role: str,
    content: str,
) -> Message:
    msg = Message(
        user_id=user_id,
        agent_id=agent_id,
        sub_agent_id=sub_agent_id,
        role=role,
        content=content,
    )
    db.add(msg)
    await db.flush()
    return msg


async def get_recent_history(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    sub_agent_id: uuid.UUID | None,
    limit: int = 20,
) -> Sequence[Message]:
    """Recent turns for the conversation with this (sub-)agent, oldest-first.

    History is scoped to (user, main_agent, target sub-agent) so each sub-agent
    keeps its own thread — different specialisations get differentiated context.
    """
    stmt = select(Message).where(
        Message.user_id == user_id, Message.agent_id == agent_id
    )
    if sub_agent_id is None:
        stmt = stmt.where(Message.sub_agent_id.is_(None))
    else:
        stmt = stmt.where(Message.sub_agent_id == sub_agent_id)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    res = await db.execute(stmt)
    rows = res.scalars().all()
    return list(reversed(rows))
