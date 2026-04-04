"""add_tenant_budgets_and_call_quality_log

Revision ID: 4c7a2737b1e7
Revises: 7d4c2f8a9b1e
Create Date: 2026-03-30 18:39:55.633059
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "4c7a2737b1e7"
down_revision = "7d4c2f8a9b1e"
branch_labels = None
depends_on = None


plan_tier_enum = postgresql.ENUM(
    "free",
    "starter",
    "growth",
    "enterprise",
    name="plan_tier_enum",
    create_type=False,
)
overage_policy_enum = postgresql.ENUM(
    "block",
    "warn",
    "charge",
    name="overage_policy_enum",
    create_type=False,
)
call_quality_blocked_layer_enum = postgresql.ENUM(
    "L1",
    "L2",
    "L3",
    "L4",
    "NONE",
    name="call_quality_blocked_layer_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        plan_tier_enum,
        overage_policy_enum,
        call_quality_blocked_layer_enum,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "tenant_budgets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_tier",
            plan_tier_enum,
            nullable=False,
            server_default=sa.text("'starter'::plan_tier_enum"),
        ),
        sa.Column("monthly_call_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("current_month_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("current_month_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "rate_limit_per_user_per_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
        sa.Column(
            "overage_policy",
            overage_policy_enum,
            nullable=False,
            server_default=sa.text("'warn'::overage_policy_enum"),
        ),
        sa.Column("alert_threshold_pct", sa.Float(), nullable=False, server_default=sa.text("0.8")),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id"),
    )

    op.create_table(
        "call_quality_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "layer_blocked_at",
            call_quality_blocked_layer_enum,
            nullable=False,
            server_default=sa.text("'NONE'::call_quality_blocked_layer_enum"),
        ),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("semantic_similarity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS call_quality_log")
    op.execute("DROP TABLE IF EXISTS tenant_budgets")

    bind = op.get_bind()
    for enum_type in (
        call_quality_blocked_layer_enum,
        overage_policy_enum,
        plan_tier_enum,
    ):
        enum_type.drop(bind, checkfirst=True)
