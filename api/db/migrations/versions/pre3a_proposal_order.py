"""add deterministic proposal grouping and ordering

Revision ID: pre3a_proposal_order
Revises: phase26b_evidence_ledger
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "pre3a_proposal_order"
down_revision: str | None = "phase26b_evidence_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memory_proposals", sa.Column("proposal_group_id", sa.String(length=300), nullable=True))
    op.add_column("memory_proposals", sa.Column("proposal_ordinal", sa.Integer(), nullable=True))
    op.execute(
        """
        WITH ordered AS (
            SELECT id, extraction_job_id::text AS group_id,
                   row_number() OVER (
                       PARTITION BY tenant_id, proxy_user_id, conversation_scope_id, extraction_job_id
                       ORDER BY created_at, id
                   ) AS ordinal
            FROM memory_proposals
        )
        UPDATE memory_proposals AS proposal
        SET proposal_group_id = ordered.group_id,
            proposal_ordinal = ordered.ordinal
        FROM ordered
        WHERE proposal.id = ordered.id
        """
    )
    op.alter_column("memory_proposals", "proposal_group_id", nullable=False)
    op.alter_column("memory_proposals", "proposal_ordinal", nullable=False)
    op.create_unique_constraint(
        "uq_memory_proposals_group_ordinal",
        "memory_proposals",
        ["tenant_id", "proxy_user_id", "conversation_scope_id", "proposal_group_id", "proposal_ordinal"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_memory_proposals_group_ordinal", "memory_proposals", type_="unique")
    op.drop_column("memory_proposals", "proposal_ordinal")
    op.drop_column("memory_proposals", "proposal_group_id")
