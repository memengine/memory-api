"""Add structured conflict decision evidence.

Revision ID: conflict_decision_evidence
Revises: retrieval_feedback_events
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "conflict_decision_evidence"
down_revision = "retrieval_feedback_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cross_user_conflicts",
        sa.Column(
            "decision_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "memory_claim_revisions",
        sa.Column(
            "decision_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("memory_claim_revisions", "decision_evidence")
    op.drop_column("cross_user_conflicts", "decision_evidence")
