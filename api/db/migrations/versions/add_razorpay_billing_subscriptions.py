"""add Razorpay billing subscriptions and webhook receipts

Revision ID: razorpay_billing_subscriptions
Revises: universal_memory_text_envelopes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "razorpay_billing_subscriptions"
down_revision: str | None = "universal_memory_text_envelopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "provider", sa.String(length=30), server_default="razorpay", nullable=False
        ),
        sa.Column("provider_subscription_id", sa.String(length=100), nullable=False),
        sa.Column("provider_customer_id", sa.String(length=100), nullable=True),
        sa.Column(
            "plan_tier",
            postgresql.ENUM(
                "free",
                "starter",
                "growth",
                "scale",
                "enterprise",
                name="plan_tier_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("billing_interval", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status", sa.String(length=30), server_default="created", nullable=False
        ),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("current_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_subscription_id", name="uq_billing_provider_subscription"
        ),
    )
    op.create_index(
        "ix_billing_subscriptions_tenant_id", "billing_subscriptions", ["tenant_id"]
    )
    op.create_table(
        "billing_webhook_events",
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("provider_event_id"),
    )


def downgrade() -> None:
    op.drop_table("billing_webhook_events")
    op.drop_index(
        "ix_billing_subscriptions_tenant_id", table_name="billing_subscriptions"
    )
    op.drop_table("billing_subscriptions")
