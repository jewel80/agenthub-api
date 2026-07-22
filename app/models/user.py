"""User ORM model — tenant-scoped auth.

A single `users` table is shared across ALL agents. The composite unique
constraint UNIQUE(email, agent_id) means the SAME email can register
independently under different agents (like logging into different apps),
and a credential set is only ever valid within the agent it was created
for. This is the "no 100 copies" auth design.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.message import Message


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Same email may exist once per agent.
        UniqueConstraint("email", "agent_id", name="uq_users_email_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # The agent this credential was created for — the login's scope.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    agent: Mapped["Agent"] = relationship("Agent", foreign_keys=[agent_id])
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} @ agent {self.agent_id}>"
