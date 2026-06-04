"""Add tenant-configured support type.

Revision ID: add_tenant_support_type
Revises: add_support_memories
Create Date: 2026-05-24 00:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "add_tenant_support_type"
down_revision = "add_support_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("support_type_configured", sa.String(length=30), nullable=True))
    op.create_check_constraint(
        "ck_tenants_support_type_configured",
        "tenants",
        "support_type_configured IS NULL OR support_type_configured IN ('saas','ecommerce','banking_fintech','travel','telecom','edtech_support','general_info')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_support_type_configured", "tenants", type_="check")
    op.drop_column("tenants", "support_type_configured")
