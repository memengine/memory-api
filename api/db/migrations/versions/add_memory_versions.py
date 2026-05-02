"""add memory versions

Revision ID: add_memory_versions
Revises: fix_cross_agent_schema_drift
Create Date: 2026-04-28 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_memory_versions"
down_revision = "fix_cross_agent_schema_drift"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("change_type", sa.String(length=50), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(length=50), nullable=False, server_default=sa.text("'system'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "change_type IN ('created', 'conflict_update', 'manual_edit', 'importance_decay', 'importance_boost', 'archived')",
            name="ck_memory_versions_change_type",
        ),
        sa.CheckConstraint(
            "changed_by IN ('system', 'user', 'operator')",
            name="ck_memory_versions_changed_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", "version_number", name="uq_memory_versions_memory_version"),
    )
    op.create_index("ix_memory_versions_memory_id", "memory_versions", ["memory_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_versions_memory_id", table_name="memory_versions")
    op.drop_table("memory_versions")
