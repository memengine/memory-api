"""align quota field names

Revision ID: b7d1e4c2f8a9
Revises: a9f3c6d1e2b4
Create Date: 2026-03-30 23:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7d1e4c2f8a9"
down_revision = "a9f3c6d1e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tenant_budgets",
        "write_limit",
        new_column_name="write_call_limit",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "tenant_budgets",
        "alert_webhook_url",
        existing_type=sa.String(length=2048),
        type_=sa.String(length=500),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tenant_budgets",
        "alert_webhook_url",
        existing_type=sa.String(length=500),
        type_=sa.String(length=2048),
        existing_nullable=True,
    )
    op.alter_column(
        "tenant_budgets",
        "write_call_limit",
        new_column_name="write_limit",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
