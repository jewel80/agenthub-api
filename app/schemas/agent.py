"""Pydantic DTOs for agents (catalog + config)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SubAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    profession: str
    tagline: str
    description: str
    sort_order: int


class AgentOut(BaseModel):
    """Public catalog representation (no system_prompt leaked to clients)."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    industry: str
    profession: str
    tagline: str
    description: str
    is_featured: bool
    sub_agents: list[SubAgentOut] = []


class AgentListItem(BaseModel):
    """Lightweight row for the catalog grid."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    industry: str
    profession: str
    tagline: str
    is_featured: bool
    sub_agent_count: int = 0
