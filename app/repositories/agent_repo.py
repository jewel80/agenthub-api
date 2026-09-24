"""Agent repository — all DB access for agents/sub-agents.

Config/data is the product; this is the single place that reads agent rows.
No per-agent logic lives here.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent


async def list_main_agents(
    db: AsyncSession,
    *,
    industry: str | None = None,
    q: str | None = None,
    featured: bool | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> Sequence[Agent]:
    """Active main agents with filtering/paging done in SQL (fix-doc F8).

    `q` matches profession OR industry case-insensitively; `industry` is an
    exact (case-insensitive) match. `limit=None` returns the full list.
    """
    stmt = (
        select(Agent)
        .where(Agent.parent_id.is_(None), Agent.is_active.is_(True))
        .order_by(Agent.sort_order, Agent.profession)
        .options(selectinload(Agent.sub_agents))
    )
    if industry:
        stmt = stmt.where(func.lower(Agent.industry) == industry.lower())
    if featured is not None:
        stmt = stmt.where(Agent.is_featured.is_(featured))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(Agent.profession.ilike(pattern), Agent.industry.ilike(pattern))
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)
    res = await db.execute(stmt)
    return res.scalars().all()


async def list_industries(db: AsyncSession) -> Sequence[str]:
    """Distinct industries of active main agents, sorted (DISTINCT in SQL)."""
    res = await db.execute(
        select(Agent.industry)
        .distinct()
        .where(Agent.parent_id.is_(None), Agent.is_active.is_(True))
        .order_by(Agent.industry)
    )
    return [row[0] for row in res.all()]


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
