"""Agent ORM model.

A single table holds BOTH main agents (parent_id IS NULL) and sub-agents
(parent_id points at the parent agent). This is the heart of "agents are
data, not code": a main agent and its sub-agents are just rows, linked by
parent_id. Adding agent #101 is an INSERT, never a code change.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.message import Message


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    industry: Mapped[str] = mapped_column(String(160), default="")
    profession: Mapped[str] = mapped_column(String(200), default="")
    tagline: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # THIS column drives ALL persona behaviour. The chat engine reads it,
    # never branches on which agent it is.
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # NULL => main agent; non-NULL => sub-agent of that parent.
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Marks the 5 "fully working / featured" agents we explicitly verify.
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Self-referential adjacency list for the parent/sub-agent tree.
    parent: Mapped[Optional["Agent"]] = relationship(
        "Agent", remote_side="Agent.id", back_populates="sub_agents"
    )
    sub_agents: Mapped[list["Agent"]] = relationship(
        "Agent", back_populates="parent", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="agent", foreign_keys="Message.agent_id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        kind = "sub" if self.parent_id else "main"
        return f"<Agent {kind} {self.slug}>"
