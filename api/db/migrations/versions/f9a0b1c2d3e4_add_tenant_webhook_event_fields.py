"""add tenant webhook event fields

Revision ID: f9a0b1c2d3e4
Revises: e7f8a9b0c1d2
Create Date: 2026-04-03 19:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_budgets", sa.Column("webhook_secret", sa.String(length=64), nullable=True))
    op.add_column("tenant_budgets", sa.Column("last_notified_pct", sa.Float(), nullable=True))
    op.execute(
        """
        UPDATE tenant_budgets
        SET webhook_secret = encode(gen_random_bytes(32), 'hex')
        WHERE webhook_secret IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_budgets", "last_notified_pct")
    op.drop_column("tenant_budgets", "webhook_secret")
