"""add clarification queue and automatic conflict resolution fields

Revision ID: add_clarification_queue
Revises: add_conflict_tables
Create Date: 2026-05-05 10:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_clarification_queue"
down_revision = "add_conflict_tables"
branch_labels = None
depends_on = None


CLARIFICATION_STATUS = postgresql.ENUM(
    "pending",
    "triggered",
    "resolved",
    "expired",
    name="clarification_queue_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    CLARIFICATION_STATUS.create(bind, checkfirst=True)

    op.add_column(
        "cross_user_conflicts",
        sa.Column("auto_resolution", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column("auto_resolution_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column(
            "requires_attention",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "clarification_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_context", sa.Text(), nullable=False),
        sa.Column("conflict_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_on", sa.String(length=20), nullable=False, server_default=sa.text("'next_session'")),
        sa.Column("status", CLARIFICATION_STATUS, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '30 days'"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'triggered', 'resolved', 'expired')",
            name="ck_clarification_queue_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["proxy_user_id"], ["proxy_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conflict_id"], ["cross_user_conflicts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_clarification_queue_proxy_status",
        "clarification_queue",
        ["proxy_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_clarification_queue_tenant_status",
        "clarification_queue",
        ["tenant_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_clarification_queue_tenant_status", table_name="clarification_queue")
    op.drop_index("ix_clarification_queue_proxy_status", table_name="clarification_queue")
    op.drop_table("clarification_queue")
    op.drop_column("cross_user_conflicts", "requires_attention")
    op.drop_column("cross_user_conflicts", "auto_resolution_at")
    op.drop_column("cross_user_conflicts", "auto_resolution")
    CLARIFICATION_STATUS.drop(op.get_bind(), checkfirst=True)
