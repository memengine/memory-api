"""add universal memory versions

Revision ID: add_universal_memory_versions
Revises: add_user_memory_flags
Create Date: 2026-05-11 17:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_universal_memory_versions"
down_revision = "add_user_memory_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universal_memory_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "universal_memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("universal_memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("change_type", sa.String(length=50), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(length=50), nullable=False),
        sa.Column(
            "changed_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("global_agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "change_type IN ('created','user_corrected','user_removed','conflict_resolved','agent_updated','importance_decay','importance_boost','archived')",
            name="ck_universal_memory_versions_change_type",
        ),
        sa.CheckConstraint(
            "changed_by IN ('user','system','agent')",
            name="ck_universal_memory_versions_changed_by",
        ),
        sa.UniqueConstraint(
            "universal_memory_id",
            "version_number",
            name="uq_universal_memory_versions_memory_version",
        ),
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_universal_memory_versions_memory_created "
            "ON universal_memory_versions (universal_memory_id, created_at DESC)"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_universal_memory_versions_memory_created", table_name="universal_memory_versions")
    op.drop_table("universal_memory_versions")
