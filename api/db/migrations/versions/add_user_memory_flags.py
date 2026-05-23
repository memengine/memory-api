"""add user memory flags

Revision ID: add_user_memory_flags
Revises: add_conflict_resolution_routing
Create Date: 2026-05-11 16:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_user_memory_flags"
down_revision = "add_conflict_resolution_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "universal_memories",
        sa.Column("is_flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "user_memory_flags",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("universal_memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_uui_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("universal_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("flagged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('incorrect','outdated','never_said_this')",
            name="ck_user_memory_flags_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved','dismissed')",
            name="ck_user_memory_flags_status",
        ),
    )
    op.create_index(
        "ix_user_memory_flags_user_status",
        "user_memory_flags",
        ["user_uui_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_memory_flags_user_status", table_name="user_memory_flags")
    op.drop_table("user_memory_flags")
    op.drop_column("universal_memories", "is_flagged")
