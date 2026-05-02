"""add cross agent tables

Revision ID: add_cross_agent_tables
Revises: b8c9d0e1f2a
Create Date: 2026-04-19 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_cross_agent_tables"
down_revision = "b8c9d0e1f2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universal_users",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("uui_token", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True, unique=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("otp_code", sa.String(length=6), nullable=True),
        sa.Column("otp_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_universal_users_uui_token", "universal_users", ["uui_token"], unique=True)
    op.create_index("ix_universal_users_email", "universal_users", ["email"], unique=True)

    op.create_table(
        "global_agents",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("website_url", sa.String(length=500), nullable=True),
        sa.Column(
            "default_categories_requested",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False, server_default=sa.text("''")),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(["owner_tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "permission_grants",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_uui_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("categories_allowed", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("access_type", sa.String(length=20), nullable=False, server_default=sa.text("'read_only'")),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("access_type IN ('read_only', 'read_write')", name="ck_permission_grants_access_type"),
        sa.ForeignKeyConstraint(["agent_id"], ["global_agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_uui_id"], ["universal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_uui_id", "agent_id", name="uq_permission_grants_user_agent"),
    )
    op.create_index(
        "ix_permission_grants_user_active",
        "permission_grants",
        ["user_uui_id", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_permission_grants_agent_active",
        "permission_grants",
        ["agent_id", "is_active"],
        unique=False,
    )

    op.create_table(
        "universal_memories",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_uui_id", sa.UUID(), nullable=False),
        sa.Column("source_agent_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("embedding_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "category IN ('preference','fact','goal','procedure','relationship','expertise')",
            name="ck_universal_memories_category",
        ),
        sa.CheckConstraint("importance_score > 0 AND importance_score <= 10", name="ck_universal_memories_importance"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_universal_memories_confidence"),
        sa.ForeignKeyConstraint(["source_agent_id"], ["global_agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_uui_id"], ["universal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_universal_memories_user_category_archived",
        "universal_memories",
        ["user_uui_id", "category", "is_archived"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_universal_memories_user_importance_desc
            ON universal_memories (user_uui_id, importance_score DESC)
            """
        )
    )
    op.create_index("ix_universal_memories_source_agent_id", "universal_memories", ["source_agent_id"], unique=False)

    op.create_table(
        "uui_proxy_link",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("proxy_user_id", sa.UUID(), nullable=False),
        sa.Column("user_uui_id", sa.UUID(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["proxy_user_id"], ["proxy_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_uui_id"], ["universal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "proxy_user_id", name="uq_uui_proxy_link_tenant_proxy_user"),
        sa.UniqueConstraint("proxy_user_id", name="uq_uui_proxy_link_proxy_user"),
    )


def downgrade() -> None:
    op.drop_table("uui_proxy_link")
    op.drop_index("ix_universal_memories_source_agent_id", table_name="universal_memories")
    op.execute(sa.text("DROP INDEX IF EXISTS ix_universal_memories_user_importance_desc"))
    op.drop_index("ix_universal_memories_user_category_archived", table_name="universal_memories")
    op.drop_table("universal_memories")
    op.drop_index("ix_permission_grants_agent_active", table_name="permission_grants")
    op.drop_index("ix_permission_grants_user_active", table_name="permission_grants")
    op.drop_table("permission_grants")
    op.drop_table("global_agents")
    op.drop_index("ix_universal_users_email", table_name="universal_users")
    op.drop_index("ix_universal_users_uui_token", table_name="universal_users")
    op.drop_table("universal_users")
