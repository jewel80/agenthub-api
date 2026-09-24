"""Public catalog endpoints — browse all agents (no auth)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.repositories import agent_repo
from app.schemas.agent import AgentListItem, AgentOut, SubAgentOut
from app.services import agent_service

router = APIRouter()


@router.get("/agents", response_model=list[AgentListItem], summary="List agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    industry: str | None = Query(None, description="Filter by exact industry."),
    q: str | None = Query(None, description="Search profession/industry."),
    featured: bool | None = Query(None, description="Only featured agents."),
    limit: int | None = Query(
        None, ge=1, le=200, description="Page size (default: full list)."
    ),
    offset: int = Query(0, ge=0, description="Page offset."),
):
    # Filtering/paging happens in SQL (fix-doc F8); default = full list so the
    # response contract is unchanged.
    agents = await agent_repo.list_main_agents(
        db, industry=industry, q=q, featured=featured, limit=limit, offset=offset
    )
    items: list[AgentListItem] = []
    for a in agents:
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
    # Shared helper: unknown and deactivated agents both 404 (fix-doc F4).
    agent = await agent_service.get_active_main_agent_or_404(db, slug)
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
    return await agent_repo.list_industries(db)
