"""Add learner-type context to EdTech memories.

Revision ID: edtech_learner_type
Revises: add_edtech_memories
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "edtech_learner_type"
down_revision = "add_edtech_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("edtech_memories", sa.Column("learner_type", sa.String(length=30), nullable=True))
    op.add_column(
        "edtech_memories",
        sa.Column("learner_type_confidence", sa.String(length=10), server_default="high", nullable=False),
    )
    op.add_column("edtech_memories", sa.Column("primary_goal", sa.Text(), nullable=True))
    op.add_column("edtech_memories", sa.Column("primary_deadline_event", sa.String(length=200), nullable=True))
    op.add_column("edtech_memories", sa.Column("primary_deadline_date", sa.Date(), nullable=True))
    op.add_column(
        "edtech_memories",
        sa.Column("progress_trend", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    for column in (
        "competitive_exam_context",
        "higher_education_context",
        "professional_cert_context",
        "skill_learner_context",
        "medical_context",
    ):
        op.add_column(
            "edtech_memories",
            sa.Column(column, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        )

    op.execute(
        """
        UPDATE edtech_memories
        SET primary_deadline_event = exam_name
        WHERE primary_deadline_event IS NULL
          AND exam_name IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE edtech_memories
        SET primary_deadline_date = exam_date
        WHERE primary_deadline_date IS NULL
          AND exam_date IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_edtech_memories_learner_type",
        "edtech_memories",
        "learner_type IS NULL OR learner_type IN ('school_student','competitive_exam','higher_education','professional_cert','skill_learner','medical_student')",
    )
    op.create_check_constraint(
        "ck_edtech_memories_learner_type_confidence",
        "edtech_memories",
        "learner_type_confidence IN ('high','low')",
    )
    op.create_index(
        "ix_edtech_memories_tenant_primary_deadline_date",
        "edtech_memories",
        ["tenant_id", "primary_deadline_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_edtech_memories_tenant_primary_deadline_date", table_name="edtech_memories")
    op.drop_constraint("ck_edtech_memories_learner_type_confidence", "edtech_memories", type_="check")
    op.drop_constraint("ck_edtech_memories_learner_type", "edtech_memories", type_="check")
    for column in (
        "medical_context",
        "skill_learner_context",
        "professional_cert_context",
        "higher_education_context",
        "competitive_exam_context",
        "progress_trend",
        "primary_deadline_date",
        "primary_deadline_event",
        "primary_goal",
        "learner_type_confidence",
        "learner_type",
    ):
        op.drop_column("edtech_memories", column)
