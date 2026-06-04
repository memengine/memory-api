"""Add Customer Support domain memories.

Revision ID: add_support_memories
Revises: edtech_learner_type
Create Date: 2026-05-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_support_memories"
down_revision = "edtech_learner_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "proxy_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proxy_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("support_type", sa.String(length=30), nullable=True),
        sa.Column("support_type_source", sa.String(length=20), nullable=False, server_default="detected"),
        sa.Column(
            "customer_identity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "communication_preference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "language_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("current_open_issue", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "issue_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "resolution_preference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("sentiment_pattern", sa.String(length=50), nullable=True),
        sa.Column(
            "risk_signals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "support_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_extraction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "extraction_source_job_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("proxy_user_id", "tenant_id", name="uq_support_memories_proxy_tenant"),
        sa.CheckConstraint(
            "support_type IS NULL OR support_type IN ('saas','ecommerce','banking_fintech','travel','telecom','edtech_support','general_info')",
            name="ck_support_memories_support_type",
        ),
        sa.CheckConstraint(
            "support_type_source IN ('detected','tenant_configured')",
            name="ck_support_memories_support_type_source",
        ),
    )
    op.create_index("ix_support_memories_tenant_type", "support_memories", ["tenant_id", "support_type"])
    op.create_index("ix_support_memories_tenant_sentiment", "support_memories", ["tenant_id", "sentiment_pattern"])
    op.create_index("ix_support_memories_tenant_updated", "support_memories", ["tenant_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_support_memories_tenant_updated", table_name="support_memories")
    op.drop_index("ix_support_memories_tenant_sentiment", table_name="support_memories")
    op.drop_index("ix_support_memories_tenant_type", table_name="support_memories")
    op.drop_table("support_memories")
