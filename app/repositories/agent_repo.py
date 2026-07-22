"""Agent repository — all DB access for agents/sub-agents.

Config/data is the product; this is the single place that reads agent rows.
No per-agent logic lives here.
"""
from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent


async def get_main_agents(db: AsyncSession) -> Sequence[Agent]:
    """All active main agents (parent_id IS NULL), eager-loading sub-agents."""
    stmt = (
        select(Agent)
        .where(Agent.parent_id.is_(None), Agent.is_active.is_(True))
        .order_by(Agent.sort_order, Agent.profession)
        .options(selectinload(Agent.sub_agents))
    )
    res = await db.execute(stmt)
    return res.scalars().all()


async def get_main_agent_by_slug(db: AsyncSession, slug: str) -> Agent | None:
    stmt = (
        select(Agent)
        .where(Agent.slug == slug, Agent.parent_id.is_(None))
        .options(selectinload(Agent.sub_agents))
    )
    res = await db.execute(stmt)
    return res.scalars().first()


async def get_main_agent_by_id(db: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    stmt = (
        select(Agent)
        .where(Agent.id == agent_id, Agent.parent_id.is_(None))
        .options(selectinload(Agent.sub_agents))
    )
    res = await db.execute(stmt)
    return res.scalars().first()


async def get_sub_agent_by_slug(
    db: AsyncSession, parent_id: uuid.UUID, sub_slug: str
) -> Agent | None:
    stmt = select(Agent).where(
        Agent.parent_id == parent_id, Agent.slug == sub_slug
    )
    res = await db.execute(stmt)
    return res.scalars().first()


async def get_agent_by_id(db: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    res = await db.execute(select(Agent).where(Agent.id == agent_id))
    return res.scalars().first()


async def count_main_agents(db: AsyncSession) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(Agent)
        .where(Agent.parent_id.is_(None))
    )
    return int(res.scalar_one())


async def upsert_agent(db: AsyncSession, **fields) -> Agent:
    """Idempotent insert/update keyed on slug. Used by the content pipeline."""
    slug = fields["slug"]
    existing = await db.execute(select(Agent).where(Agent.slug == slug))
    agent = existing.scalars().first()
    if agent is None:
        agent = Agent(**fields)
        db.add(agent)
    else:
        for k, v in fields.items():
            setattr(agent, k, v)
    await db.flush()
    return agent
