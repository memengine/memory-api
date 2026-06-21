"""Add Phase 3 memory claim ledger.

Revision ID: memory_claim_ledger
Revises: add_org_connections
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "memory_claim_ledger"
down_revision = "add_org_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                "preference",
                "fact",
                "goal",
                "procedure",
                "relationship",
                "expertise",
                name="memory_category_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("claim_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("subject_key", sa.String(length=255), nullable=False),
        sa.Column("predicate_key", sa.String(length=255), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("active_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default=sa.text("'active'"), nullable=False),
        sa.Column("active_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("winning_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("authority_priority", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','superseded','disputed','archived')", name="ck_memory_claims_status"),
        sa.ForeignKeyConstraint(["active_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proxy_user_id"], ["proxy_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "proxy_user_id", "claim_fingerprint", name="uq_memory_claims_tenant_user_fingerprint"),
    )
    op.create_index("ix_memory_claims_active_memory", "memory_claims", ["active_memory_id"])
    op.create_index("ix_memory_claims_tenant_category", "memory_claims", ["tenant_id", "category"])
    op.create_index("ix_memory_claims_tenant_user_status", "memory_claims", ["tenant_id", "proxy_user_id", "status"])

    op.create_table(
        "memory_claim_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_writer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asserted_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("authority_priority", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('asserted','activated','superseded','rejected','disputed','archived')",
            name="ck_memory_claim_revisions_status",
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["memory_claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_event_id"], ["memory_source_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_writer_id"], ["service_writers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_claim_revisions_claim_created", "memory_claim_revisions", ["claim_id", "created_at"])
    op.create_index("ix_memory_claim_revisions_memory", "memory_claim_revisions", ["memory_id"])
    op.create_index("ix_memory_claim_revisions_source_event", "memory_claim_revisions", ["source_event_id"])

    op.create_foreign_key(
        "fk_memory_claims_winning_revision_id",
        "memory_claims",
        "memory_claim_revisions",
        ["winning_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_memory_claims_winning_revision_id", "memory_claims", type_="foreignkey")
    op.drop_index("ix_memory_claim_revisions_source_event", table_name="memory_claim_revisions")
    op.drop_index("ix_memory_claim_revisions_memory", table_name="memory_claim_revisions")
    op.drop_index("ix_memory_claim_revisions_claim_created", table_name="memory_claim_revisions")
    op.drop_table("memory_claim_revisions")
    op.drop_index("ix_memory_claims_tenant_user_status", table_name="memory_claims")
    op.drop_index("ix_memory_claims_tenant_category", table_name="memory_claims")
    op.drop_index("ix_memory_claims_active_memory", table_name="memory_claims")
    op.drop_table("memory_claims")
