"""Add user-owned organisation connections for Memory Passport.

Revision ID: add_org_connections
Revises: memory_provenance_phase2
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_org_connections"
down_revision = "memory_provenance_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organisation_directory",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("website_url", sa.String(length=500), nullable=True),
        sa.Column("category", sa.String(length=50), server_default="other", nullable=False),
        sa.Column("oauth_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("oauth_client_id", sa.String(length=500), nullable=True),
        sa.Column("oauth_client_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("oauth_authorization_url", sa.String(length=1000), nullable=True),
        sa.Column("oauth_token_url", sa.String(length=1000), nullable=True),
        sa.Column("oauth_userinfo_url", sa.String(length=1000), nullable=True),
        sa.Column(
            "oauth_scopes",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("link_token_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "category IN ('ecommerce','banking','travel','telecom','edtech','saas','other')",
            name="ck_organisation_directory_category",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_organisation_directory_tenant"),
    )
    op.create_index(
        "ix_organisation_directory_public_category",
        "organisation_directory",
        ["is_public", "category"],
    )

    op.create_table(
        "verified_org_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_uui_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_directory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("connection_method", sa.String(length=30), nullable=False),
        sa.Column("external_account_ref", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=20), nullable=True),
        sa.CheckConstraint(
            "connection_method IN ('oauth','oidc','link_token')",
            name="ck_verified_org_connections_method",
        ),
        sa.CheckConstraint(
            "revoked_by IS NULL OR revoked_by IN ('user','system')",
            name="ck_verified_org_connections_revoked_by",
        ),
        sa.ForeignKeyConstraint(["org_directory_id"], ["organisation_directory.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proxy_user_id"], ["proxy_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_uui_id"], ["universal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_uui_id",
            "org_directory_id",
            name="uq_verified_org_connections_user_org",
        ),
    )
    op.create_index(
        "ix_verified_org_connections_user_active",
        "verified_org_connections",
        ["user_uui_id", "is_active"],
    )
    op.create_index(
        "ix_verified_org_connections_tenant_active",
        "verified_org_connections",
        ["tenant_id", "is_active"],
    )

    op.add_column(
        "universal_memories",
        sa.Column("source_type", sa.String(length=30), server_default="passport_agent", nullable=False),
    )
    op.add_column(
        "universal_memories",
        sa.Column("source_org_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.drop_constraint(
        "universal_memories_source_agent_id_fkey",
        "universal_memories",
        type_="foreignkey",
    )
    op.alter_column("universal_memories", "source_agent_id", existing_type=postgresql.UUID(), nullable=True)
    op.create_foreign_key(
        "universal_memories_source_agent_id_fkey",
        "universal_memories",
        "global_agents",
        ["source_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_universal_memories_source_org_connection",
        "universal_memories",
        "verified_org_connections",
        ["source_org_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_universal_memories_source_type",
        "universal_memories",
        "source_type IN ('passport_agent','org_connection','user_correction','system')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_universal_memories_source_type", "universal_memories", type_="check")
    op.drop_constraint(
        "fk_universal_memories_source_org_connection",
        "universal_memories",
        type_="foreignkey",
    )
    op.drop_constraint(
        "universal_memories_source_agent_id_fkey",
        "universal_memories",
        type_="foreignkey",
    )
    op.execute(
        """
        DELETE FROM universal_memories
        WHERE source_agent_id IS NULL
        """
    )
    op.alter_column("universal_memories", "source_agent_id", existing_type=postgresql.UUID(), nullable=False)
    op.create_foreign_key(
        "universal_memories_source_agent_id_fkey",
        "universal_memories",
        "global_agents",
        ["source_agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("universal_memories", "source_org_connection_id")
    op.drop_column("universal_memories", "source_type")

    op.drop_index("ix_verified_org_connections_tenant_active", table_name="verified_org_connections")
    op.drop_index("ix_verified_org_connections_user_active", table_name="verified_org_connections")
    op.drop_table("verified_org_connections")
    op.drop_index("ix_organisation_directory_public_category", table_name="organisation_directory")
    op.drop_table("organisation_directory")
