"""add outbox_events table (transactional outbox, scale-doc §3)

Revision ID: d1e2f3a4b5c6
Revises: c7d2e3f4a5b6
Create Date: 2026-09-27 12:00:00.000000+00:00

New table only (expand-only, no existing data affected). `seq` (identity)
is the ordering key, not `created_at`: several events committed in one
transaction share a `created_at` (Postgres now() is transaction-start
time) — same rationale as messages.seq. Partial index on `seq` WHERE
published_at IS NULL: the relay worker's poll query is exactly
"unpublished rows, oldest first" (scale-doc §3 point 1).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c7d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
