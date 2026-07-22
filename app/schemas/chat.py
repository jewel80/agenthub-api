"""Pydantic DTOs for the chat endpoint."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatMessageIn(BaseModel):
    """A single user turn. sub_agent_slug is optional (main agent if absent)."""
    message: str = Field(min_length=1, max_length=8000)
    sub_agent_slug: str | None = None


class ChatTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    created_at: datetime
    sub_agent_id: UUID | None = None


class ChatResponse(BaseModel):
    reply: str
    agent_slug: str
    sub_agent_slug: str | None
