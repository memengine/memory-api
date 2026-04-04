"""add quota mode fields to tenant budgets

Revision ID: a9f3c6d1e2b4
Revises: 4c7a2737b1e7
Create Date: 2026-03-30 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "a9f3c6d1e2b4"
down_revision = "4c7a2737b1e7"
branch_labels = None
depends_on = None


quota_mode_enum = postgresql.ENUM(
    "FULL",
    "PASSTHROUGH",
    "DEGRADED_RETRIEVE",
    "BLOCKED",
    name="quota_mode_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    quota_mode_enum.create(bind, checkfirst=True)

    op.add_column("tenant_budgets", sa.Column("write_calls", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("tenant_budgets", sa.Column("write_limit", sa.Integer(), nullable=True))
    op.add_column("tenant_budgets", sa.Column("read_calls", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("tenant_budgets", sa.Column("read_limit", sa.Integer(), nullable=True))
    op.add_column("tenant_budgets", sa.Column("alert_webhook_url", sa.String(length=2048), nullable=True))
    op.add_column("tenant_budgets", sa.Column("last_notified_mode", quota_mode_enum, nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_budgets", "last_notified_mode")
    op.drop_column("tenant_budgets", "alert_webhook_url")
    op.drop_column("tenant_budgets", "read_limit")
    op.drop_column("tenant_budgets", "read_calls")
    op.drop_column("tenant_budgets", "write_limit")
    op.drop_column("tenant_budgets", "write_calls")

    bind = op.get_bind()
    quota_mode_enum.drop(bind, checkfirst=True)
