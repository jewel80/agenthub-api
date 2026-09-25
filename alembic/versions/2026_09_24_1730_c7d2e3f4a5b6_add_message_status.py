"""add message status column (streaming lifecycle)

Revision ID: c7d2e3f4a5b6
Revises: b3f1a2c4d5e6
Create Date: 2026-09-24 17:30:00.000000+00:00

Expand-only: nullable=False with server_default 'complete' so existing rows
backfill in one step and the non-streaming path is unchanged (roadmap §3.3.6).
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c7d2e3f4a5b6"
down_revision = "b3f1a2c4d5e6"
branch_labels = None
depends_on = None

_VALID_STATUSES = ("complete", "interrupted", "failed")


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="complete",
        ),
    )
    # cheap integrity guard for the values we persist
    op.create_check_constraint(
        "ck_messages_status",
        "messages",
        "status IN ('complete', 'interrupted', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_status", "messages", type_="check")
    op.drop_column("messages", "status")
