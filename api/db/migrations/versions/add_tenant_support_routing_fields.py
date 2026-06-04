"""Add tenant support routing mode and allow-list.

Revision ID: support_routing_fields
Revises: add_tenant_support_type
Create Date: 2026-05-25 12:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "support_routing_fields"
down_revision = "add_tenant_support_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("support_type_mode", sa.String(length=20), nullable=False, server_default="single"),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "support_types_allowed",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_tenants_support_type_mode",
        "tenants",
        "support_type_mode IN ('single','multi','auto')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_support_type_mode", "tenants", type_="check")
    op.drop_column("tenants", "support_types_allowed")
    op.drop_column("tenants", "support_type_mode")
