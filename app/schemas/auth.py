"""Pydantic DTOs for authentication."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.types import Email


class SignupIn(BaseModel):
    """Signup is always scoped to a chosen agent (agent_slug in the path)."""
    email: Email
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    agent_id: UUID
    agent_slug: str
    agent_profession: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    agent_id: UUID
    created_at: datetime
