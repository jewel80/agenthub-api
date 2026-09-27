"""Public catalog endpoints — browse all agents (no auth).

Cached via the shared CacheService (scale-doc §2) and served with a strong
ETag + `Cache-Control: public` so clients/CDNs can 304 on repeat requests
(scale-doc §2.5 / roadmap §4.3).
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.repositories import agent_repo
from app.schemas.agent import AgentListItem, AgentOut, SubAgentOut
from app.services import cache

router = APIRouter()

_CATALOG_TTL_SECONDS = 300
_AGENT_TTL_SECONDS = 600
_INDUSTRIES_TTL_SECONDS = 1800
_CATALOG_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


def _etag_for(payload_json: str) -> str:
    return 'W/"' + hashlib.sha256(payload_json.encode()).hexdigest()[:32] + '"'


def _catalog_headers(payload_json: str) -> tuple[str, dict[str, str]]:
    etag = _etag_for(payload_json)
    return etag, {"Cache-Control": _CATALOG_CACHE_CONTROL, "ETag": etag}


@router.get("/agents", response_model=list[AgentListItem], summary="List agents")
async def list_agents(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    industry: str | None = Query(None, description="Filter by exact industry."),
    q: str | None = Query(None, description="Search profession/industry."),
    featured: bool | None = Query(None, description="Only featured agents."),
    limit: int | None = Query(
        None, ge=1, le=200, description="Page size (default: full list)."
    ),
    offset: int = Query(0, ge=0, description="Page offset."),
):
    async def _load() -> list[AgentListItem]:
        # Filtering/paging happens in SQL (fix-doc F8); default = full list so
        # the response contract is unchanged.
        agents = await agent_repo.list_main_agents(
            db, industry=industry, q=q, featured=featured, limit=limit, offset=offset
        )
        return [
            AgentListItem(
                id=a.id,
                slug=a.slug,
                industry=a.industry,
                profession=a.profession,
                tagline=a.tagline,
                is_featured=a.is_featured,
                sub_agent_count=len([s for s in a.sub_agents if s.is_active]),
            )
            for a in agents
        ]

    ident = cache.hash_query(
        industry=industry, q=q, featured=featured, limit=limit, offset=offset
    )
    items = await cache.get_or_load(
        "catalog", ident, _load, ttl=_CATALOG_TTL_SECONDS, model=AgentListItem
    )

    payload_json = json.dumps(
        [i.model_dump(mode="json") for i in items], sort_keys=True
    )
    etag, headers = _catalog_headers(payload_json)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return items


@router.get("/agents/{slug}", response_model=AgentOut, summary="Get one agent")
async def get_agent(
    slug: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    async def _load() -> AgentOut | None:
        # Unknown and deactivated agents are both "not found" (fix-doc F4);
        # cached as a negative result so repeat 404s skip the DB too.
        agent = await agent_repo.get_main_agent_by_slug(db, slug)
        if agent is None or not agent.is_active:
            return None
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

    result = await cache.get_or_load(
        "agent", slug, _load, ttl=_AGENT_TTL_SECONDS, negative=True, model=AgentOut
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")

    etag, headers = _catalog_headers(result.model_dump_json())
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return result


@router.get("/industries", response_model=list[str], summary="List industries")
async def list_industries(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    async def _load() -> list[str]:
        return list(await agent_repo.list_industries(db))

    items = await cache.get_or_load(
        "industries", "all", _load, ttl=_INDUSTRIES_TTL_SECONDS
    )
    payload_json = json.dumps(items, sort_keys=True)
    etag, headers = _catalog_headers(payload_json)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return items
