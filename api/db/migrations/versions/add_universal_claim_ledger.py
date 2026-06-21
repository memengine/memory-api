"""Add isolated claim ledger for Memory Passport memories.

Revision ID: universal_claim_ledger
Revises: domain_claim_provenance
Create Date: 2026-06-21 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "universal_claim_ledger"
down_revision = "domain_claim_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universal_memory_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_uui_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("claim_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("subject_key", sa.String(length=255), nullable=False),
        sa.Column("predicate_key", sa.String(length=255), nullable=False),
        sa.Column("active_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default=sa.text("'active'"), nullable=False),
        sa.Column("active_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("winning_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','disputed','archived')", name="ck_universal_memory_claims_status"),
        sa.ForeignKeyConstraint(["active_memory_id"], ["universal_memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_uui_id"], ["universal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_uui_id", "claim_fingerprint", name="uq_universal_memory_claims_user_fingerprint"),
    )
    op.create_index("ix_universal_memory_claims_user_status", "universal_memory_claims", ["user_uui_id", "status"])
    op.create_index("ix_universal_memory_claims_active_memory", "universal_memory_claims", ["active_memory_id"])

    op.create_table(
        "universal_memory_claim_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("universal_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_org_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("permission_grant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("asserted_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('asserted','activated','superseded','disputed','archived')", name="ck_universal_memory_claim_revisions_status"),
        sa.ForeignKeyConstraint(["claim_id"], ["universal_memory_claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_grant_id"], ["permission_grants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_agent_id"], ["global_agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_org_connection_id"], ["verified_org_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["universal_memory_id"], ["universal_memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_universal_memory_claim_revisions_claim_created", "universal_memory_claim_revisions", ["claim_id", "created_at"])
    op.create_index("ix_universal_memory_claim_revisions_memory", "universal_memory_claim_revisions", ["universal_memory_id"])
    op.create_index("ix_universal_memory_claim_revisions_grant", "universal_memory_claim_revisions", ["permission_grant_id"])


def downgrade() -> None:
    op.drop_index("ix_universal_memory_claim_revisions_grant", table_name="universal_memory_claim_revisions")
    op.drop_index("ix_universal_memory_claim_revisions_memory", table_name="universal_memory_claim_revisions")
    op.drop_index("ix_universal_memory_claim_revisions_claim_created", table_name="universal_memory_claim_revisions")
    op.drop_table("universal_memory_claim_revisions")
    op.drop_index("ix_universal_memory_claims_active_memory", table_name="universal_memory_claims")
    op.drop_index("ix_universal_memory_claims_user_status", table_name="universal_memory_claims")
    op.drop_table("universal_memory_claims")
