"""add structured proposal claims

Revision ID: phase3a_structured_claim
Revises: pre3a_proposal_order
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "phase3a_structured_claim"
down_revision: str | None = "pre3a_proposal_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_proposals",
        sa.Column("proposed_memory_content", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "memory_proposals",
        sa.Column("proposed_memory_category", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_proposals", "proposed_memory_category")
    op.drop_column("memory_proposals", "proposed_memory_content")
