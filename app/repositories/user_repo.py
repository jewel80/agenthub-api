"""User repository — tenant-scoped credential storage."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_user_by_email_and_agent(
    db: AsyncSession, email: str, agent_id: uuid.UUID
) -> User | None:
    """Lookup scoped to a specific agent — the core of auth isolation."""
    res = await db.execute(
        select(User).where(User.email == email, User.agent_id == agent_id)
    )
    return res.scalars().first()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    res = await db.execute(select(User).where(User.id == user_id))
    return res.scalars().first()


async def create_user(
    db: AsyncSession, *, email: str, password_hash: str, agent_id: uuid.UUID
) -> User:
    user = User(email=email, password_hash=password_hash, agent_id=agent_id)
    db.add(user)
    await db.flush()
    return user
