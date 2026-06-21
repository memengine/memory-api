"""Add domain field coordinates to claim revisions.

Revision ID: domain_claim_provenance
Revises: memory_claim_ledger
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "domain_claim_provenance"
down_revision = "memory_claim_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_claim_revisions",
        sa.Column("source_domain", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "memory_claim_revisions",
        sa.Column("source_domain_record_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "memory_claim_revisions",
        sa.Column("source_field", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_memory_claim_revisions_domain_field",
        "memory_claim_revisions",
        ["source_domain", "source_field"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_claim_revisions_domain_field",
        table_name="memory_claim_revisions",
    )
    op.drop_column("memory_claim_revisions", "source_field")
    op.drop_column("memory_claim_revisions", "source_domain_record_id")
    op.drop_column("memory_claim_revisions", "source_domain")