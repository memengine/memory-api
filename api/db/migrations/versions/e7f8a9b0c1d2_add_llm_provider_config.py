"""add llm provider config

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-04-03 10:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


llm_provider_name_enum = postgresql.ENUM(
    "gemini",
    "anthropic",
    "cohere",
    "local",
    name="llm_provider_name_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    llm_provider_name_enum.create(bind, checkfirst=True)

    op.create_table(
        "llm_provider_config",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "embed_provider_primary",
            llm_provider_name_enum,
            nullable=False,
            server_default=sa.text("'gemini'::llm_provider_name_enum"),
        ),
        sa.Column(
            "embed_provider_fallback",
            llm_provider_name_enum,
            nullable=False,
            server_default=sa.text("'cohere'::llm_provider_name_enum"),
        ),
        sa.Column(
            "extract_provider_primary",
            llm_provider_name_enum,
            nullable=False,
            server_default=sa.text("'gemini'::llm_provider_name_enum"),
        ),
        sa.Column(
            "extract_provider_fallback",
            llm_provider_name_enum,
            nullable=False,
            server_default=sa.text("'anthropic'::llm_provider_name_enum"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_llm_provider_config_tenant_id", "llm_provider_config", ["tenant_id"], unique=True)
    op.execute(
        """
        INSERT INTO llm_provider_config (
            tenant_id,
            embed_provider_primary,
            embed_provider_fallback,
            extract_provider_primary,
            extract_provider_fallback
        )
        VALUES (
            NULL,
            'gemini',
            'cohere',
            'gemini',
            'anthropic'
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_llm_provider_config_tenant_id", table_name="llm_provider_config")
    op.drop_table("llm_provider_config")
    bind = op.get_bind()
    llm_provider_name_enum.drop(bind, checkfirst=True)
