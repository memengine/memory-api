"""add agent api keys

Revision ID: add_agent_api_keys
Revises: add_cross_agent_tables
Create Date: 2026-04-19 11:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "add_agent_api_keys"
down_revision = "add_cross_agent_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_api_keys",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("global_agent_id", sa.UUID(), nullable=False),
        sa.Column("key_hash", sa.String(length=60), nullable=False),
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["global_agent_id"], ["global_agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_api_keys_key_prefix", "agent_api_keys", ["key_prefix"], unique=False)
    op.create_index(
        "ix_agent_api_keys_global_agent_active",
        "agent_api_keys",
        ["global_agent_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_api_keys_global_agent_active", table_name="agent_api_keys")
    op.drop_index("ix_agent_api_keys_key_prefix", table_name="agent_api_keys")
    op.drop_table("agent_api_keys")
