"""add_call_quality_reason

Revision ID: a7b8c9d0e1f2
Revises: f9a0b1c2d3e4
Create Date: 2026-04-04 17:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_quality_log", sa.Column("reason", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE call_quality_log
        SET reason = CASE layer_blocked_at::text
            WHEN 'L1' THEN 'rate_limit_exceeded'
            WHEN 'L2' THEN 'low_quality'
            WHEN 'L3' THEN 'duplicate_query'
            WHEN 'L4' THEN 'budget_exhausted'
            ELSE NULL
        END
        WHERE reason IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("call_quality_log", "reason")
