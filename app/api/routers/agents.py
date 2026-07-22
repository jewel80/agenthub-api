"""Public catalog endpoints — browse all agents (no auth)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.repositories import agent_repo
from app.schemas.agent import AgentListItem, AgentOut, SubAgentOut

router = APIRouter()


@router.get("/agents", response_model=list[AgentListItem], summary="List agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    industry: str | None = Query(None, description="Filter by exact industry."),
    q: str | None = Query(None, description="Search profession/industry."),
    featured: bool | None = Query(None, description="Only featured agents."),
):
    agents = await agent_repo.get_main_agents(db)
    q_lower = q.lower() if q else None
    items: list[AgentListItem] = []
    for a in agents:
        if industry and a.industry.lower() != industry.lower():
            continue
        if featured is True and not a.is_featured:
            continue
        if q_lower and q_lower not in f"{a.profession} {a.industry}".lower():
            continue
        active_subs = [s for s in a.sub_agents if s.is_active]
        items.append(
            AgentListItem(
                id=a.id,
                slug=a.slug,
                industry=a.industry,
                profession=a.profession,
                tagline=a.tagline,
                is_featured=a.is_featured,
                sub_agent_count=len(active_subs),
            )
        )
    return items


@router.get("/agents/{slug}", response_model=AgentOut, summary="Get one agent")
async def get_agent(slug: str, db: AsyncSession = Depends(get_db)):
    agent = await agent_repo.get_main_agent_by_slug(db, slug)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    active_subs = sorted(
        [s for s in agent.sub_agents if s.is_active], key=lambda s: s.sort_order
    )
    return AgentOut(
        id=agent.id,
        slug=agent.slug,
        industry=agent.industry,
        profession=agent.profession,
        tagline=agent.tagline,
        description=agent.description,
        is_featured=agent.is_featured,
        sub_agents=[
            SubAgentOut(
                id=s.id,
                slug=s.slug,
                profession=s.profession,
                tagline=s.tagline,
                description=s.description,
                sort_order=s.sort_order,
            )
            for s in active_subs
        ],
    )


@router.get("/industries", response_model=list[str], summary="List industries")
async def list_industries(db: AsyncSession = Depends(get_db)):
    agents = await agent_repo.get_main_agents(db)
    return sorted({a.industry for a in agents})
