"""Message repository — chat history persistence."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

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
    pair_safe: bool = False,
) -> Sequence[Message]:
    """Recent turns for the conversation with this (sub-)agent, oldest-first.

    History is scoped to (user, main_agent, target sub-agent) so each sub-agent
    keeps its own thread — different specialisations get differentiated context.
    Ordering is by the monotonic `seq` (created_at ties cannot scramble it).

    With ``pair_safe=True`` (LLM context), a window that starts with an
    assistant turn (because LIMIT cut between a user turn and its reply) drops
    that leading assistant message, so the context always opens with a user
    message as the providers require.
    """
    stmt = select(Message).where(
        Message.user_id == user_id, Message.agent_id == agent_id
    )
    if sub_agent_id is None:
        stmt = stmt.where(Message.sub_agent_id.is_(None))
    else:
        stmt = stmt.where(Message.sub_agent_id == sub_agent_id)
    stmt = stmt.order_by(Message.seq.desc()).limit(limit)
    res = await db.execute(stmt)
    rows = list(reversed(res.scalars().all()))
    if pair_safe and rows and rows[0].role == "assistant":
        rows = rows[1:]
    return rows
