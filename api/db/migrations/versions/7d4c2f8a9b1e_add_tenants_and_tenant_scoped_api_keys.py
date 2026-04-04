"""add tenants and tenant-scoped api keys

Revision ID: 7d4c2f8a9b1e
Revises: 1bdace9e2f3a
Create Date: 2026-03-30 21:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "7d4c2f8a9b1e"
down_revision = "1bdace9e2f3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column("api_keys", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("api_keys", sa.Column("key_prefix", sa.String(length=8), nullable=True))
    op.alter_column("api_keys", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.alter_column("api_keys", "key_hash", existing_type=sa.String(length=255), type_=sa.String(length=60))
    op.alter_column("api_keys", "name", existing_type=sa.String(length=255), type_=sa.String(length=100))
    op.create_foreign_key(
        "fk_api_keys_tenant_id_tenants",
        "api_keys",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_constraint("fk_api_keys_tenant_id_tenants", "api_keys", type_="foreignkey")
    op.alter_column("api_keys", "name", existing_type=sa.String(length=100), type_=sa.String(length=255))
    op.alter_column("api_keys", "key_hash", existing_type=sa.String(length=60), type_=sa.String(length=255))
    op.alter_column("api_keys", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_column("api_keys", "key_prefix")
    op.drop_column("api_keys", "tenant_id")
    op.drop_table("tenants")
