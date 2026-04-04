"""add regions and tenant region

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Create Date: 2026-04-02 19:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regions",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column("aws_region", sa.String(length=50), nullable=False),
        sa.Column("postgres_url_secret", sa.String(length=100), nullable=False),
        sa.Column("qdrant_url_secret", sa.String(length=100), nullable=False),
        sa.Column("redis_url_secret", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("gdpr_compliant", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.execute(
        """
        INSERT INTO regions (
            id,
            aws_region,
            postgres_url_secret,
            qdrant_url_secret,
            redis_url_secret,
            is_active,
            gdpr_compliant
        )
        VALUES
            ('IN1', 'ap-south-1', 'memoryos/regions/IN1/postgres_url', 'memoryos/regions/IN1/qdrant_url', 'memoryos/regions/IN1/redis_url', TRUE, FALSE),
            ('EU1', 'eu-central-1', 'memoryos/regions/EU1/postgres_url', 'memoryos/regions/EU1/qdrant_url', 'memoryos/regions/EU1/redis_url', TRUE, TRUE),
            ('US1', 'us-east-1', 'memoryos/regions/US1/postgres_url', 'memoryos/regions/US1/qdrant_url', 'memoryos/regions/US1/redis_url', TRUE, FALSE)
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.add_column(
        "tenants",
        sa.Column("region_id", sa.String(length=20), nullable=True, server_default=sa.text("'IN1'")),
    )
    op.execute("UPDATE tenants SET region_id = 'IN1' WHERE region_id IS NULL")
    op.alter_column("tenants", "region_id", nullable=False, server_default=sa.text("'IN1'"))
    op.create_foreign_key(
        "fk_tenants_region_id_regions",
        "tenants",
        "regions",
        ["region_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tenants_region_id", "tenants", ["region_id"])


def downgrade() -> None:
    op.drop_index("ix_tenants_region_id", table_name="tenants")
    op.drop_constraint("fk_tenants_region_id_regions", "tenants", type_="foreignkey")
    op.drop_column("tenants", "region_id")
    op.drop_table("regions")
