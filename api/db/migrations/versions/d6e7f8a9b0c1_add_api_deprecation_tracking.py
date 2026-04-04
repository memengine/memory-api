"""add api deprecation tracking

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-04-02 22:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


api_version_enum = postgresql.ENUM(
    "v1",
    "v2",
    name="api_version_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    api_version_enum.create(bind, checkfirst=True)

    op.create_table(
        "api_deprecated_fields",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("api_version", api_version_enum, nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sunset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replacement_field", sa.String(length=255), nullable=True),
        sa.Column("migration_guide_url", sa.String(length=500), nullable=False),
        sa.CheckConstraint(
            "sunset_at >= deprecated_at + INTERVAL '180 days'",
            name="ck_api_deprecated_fields_minimum_sunset_window",
        ),
    )
    op.create_index(
        "ix_api_deprecated_fields_version_path",
        "api_deprecated_fields",
        ["api_version", "field_path"],
        unique=True,
    )

    op.create_table(
        "tenant_deprecation_usage",
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
            nullable=False,
        ),
        sa.Column("api_version", api_version_enum, nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("first_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_tenant_deprecation_usage_tenant_version_path",
        "tenant_deprecation_usage",
        ["tenant_id", "api_version", "field_path"],
        unique=True,
    )
    op.create_index(
        "ix_tenant_deprecation_usage_last_used",
        "tenant_deprecation_usage",
        ["tenant_id", "last_used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_deprecation_usage_last_used", table_name="tenant_deprecation_usage")
    op.drop_index(
        "ix_tenant_deprecation_usage_tenant_version_path",
        table_name="tenant_deprecation_usage",
    )
    op.drop_table("tenant_deprecation_usage")

    op.drop_index("ix_api_deprecated_fields_version_path", table_name="api_deprecated_fields")
    op.drop_table("api_deprecated_fields")

    bind = op.get_bind()
    api_version_enum.drop(bind, checkfirst=True)
