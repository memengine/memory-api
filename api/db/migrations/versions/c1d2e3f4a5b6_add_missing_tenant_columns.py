"""add missing tenant columns

Revision ID: c1d2e3f4a5b6
Revises: 0b1c2d3e4f5a
Create Date: 2026-03-31 11:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c1d2e3f4a5b6"
down_revision = "0b1c2d3e4f5a"
branch_labels = None
depends_on = None


plan_tier_enum = postgresql.ENUM(
    "free",
    "starter",
    "growth",
    "enterprise",
    name="plan_tier_enum",
    create_type=False,
)


def upgrade() -> None:
    op.add_column("tenants", sa.Column("clerk_org_id", sa.String(length=255), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "plan_tier",
            plan_tier_enum,
            nullable=False,
            server_default=sa.text("'starter'::plan_tier_enum"),
        ),
    )
    op.add_column("tenants", sa.Column("alert_webhook_url", sa.String(length=500), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index("ix_tenants_clerk_org_id", "tenants", ["clerk_org_id"], unique=True)

    op.execute(
        """
        UPDATE tenants AS t
        SET plan_tier = tb.plan_tier,
            alert_webhook_url = tb.alert_webhook_url
        FROM tenant_budgets AS tb
        WHERE tb.tenant_id = t.id
        """
    )

    op.alter_column("tenants", "plan_tier", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_tenants_clerk_org_id", table_name="tenants")
    op.drop_column("tenants", "metadata")
    op.drop_column("tenants", "alert_webhook_url")
    op.drop_column("tenants", "plan_tier")
    op.drop_column("tenants", "clerk_org_id")
