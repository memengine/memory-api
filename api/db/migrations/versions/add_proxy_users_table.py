"""add proxy users table

Revision ID: c3f4d5e6a7b8
Revises: b7d1e4c2f8a9
Create Date: 2026-03-30 15:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c3f4d5e6a7b8"
down_revision = "b7d1e4c2f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proxy_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("external_user_id_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "external_user_id_hash", name="uq_proxy_users_tenant_hash"),
    )
    op.create_index("ix_proxy_users_tenant_hash", "proxy_users", ["tenant_id", "external_user_id_hash"], unique=True)
    op.create_index("ix_proxy_users_tenant_active", "proxy_users", ["tenant_id", "last_active_at"], unique=False)

    op.add_column("memories", sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_memories_proxy_user_id_proxy_users",
        "memories",
        "proxy_users",
        ["proxy_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_memories_proxy_user_category", "memories", ["proxy_user_id", "category"], unique=False)
    op.create_index("ix_memories_proxy_user_importance_score_desc", "memories", ["proxy_user_id", "importance_score"], unique=False)
    op.create_index("ix_memories_proxy_user_last_accessed_at_desc", "memories", ["proxy_user_id", "last_accessed_at"], unique=False)
    op.create_index("ix_memories_proxy_user_is_archived", "memories", ["proxy_user_id", "is_archived"], unique=False)

    op.alter_column("audit_logs", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column("audit_logs", sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_audit_logs_proxy_user_id_proxy_users",
        "audit_logs",
        "proxy_users",
        ["proxy_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_audit_logs_proxy_user_id_proxy_users", "audit_logs", type_="foreignkey")
    op.drop_column("audit_logs", "proxy_user_id")
    op.alter_column("audit_logs", "user_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)

    op.drop_index("ix_memories_proxy_user_is_archived", table_name="memories")
    op.drop_index("ix_memories_proxy_user_last_accessed_at_desc", table_name="memories")
    op.drop_index("ix_memories_proxy_user_importance_score_desc", table_name="memories")
    op.drop_index("ix_memories_proxy_user_category", table_name="memories")
    op.drop_constraint("fk_memories_proxy_user_id_proxy_users", "memories", type_="foreignkey")
    op.drop_column("memories", "proxy_user_id")

    op.drop_index("ix_proxy_users_tenant_active", table_name="proxy_users")
    op.drop_index("ix_proxy_users_tenant_hash", table_name="proxy_users")
    op.drop_table("proxy_users")
