"""add temporal validity fields

Revision ID: add_temporal_validity_fields
Revises: single_activated_claim_revision
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "add_temporal_validity_fields"
down_revision: str | None = "single_activated_claim_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_memories_effective_interval",
        "memories",
        "effective_from IS NULL OR effective_until IS NULL OR effective_until > effective_from",
    )
    op.add_column(
        "memory_claim_revisions",
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_claim_revisions",
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_memory_claim_revisions_effective_interval",
        "memory_claim_revisions",
        "effective_from IS NULL OR effective_until IS NULL OR effective_until > effective_from",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_memory_claim_revisions_effective_interval",
        "memory_claim_revisions",
        type_="check",
    )
    op.drop_column("memory_claim_revisions", "effective_until")
    op.drop_column("memory_claim_revisions", "effective_from")
    op.drop_constraint("ck_memories_effective_interval", "memories", type_="check")
    op.drop_column("memories", "effective_until")
    op.drop_column("memories", "effective_from")
