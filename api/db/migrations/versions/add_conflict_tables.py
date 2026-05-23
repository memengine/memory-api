"""add shared context conflict tables

Revision ID: add_conflict_tables
Revises: add_job_error_type_payload
Create Date: 2026-05-04 10:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_conflict_tables"
down_revision = "add_job_error_type_payload"
branch_labels = None
depends_on = None


SHARED_CONTEXT_ENTITY_TYPE = postgresql.ENUM(
    "tech_stack",
    "company_fact",
    "product_feature",
    "team_process",
    "shared_goal",
    name="shared_context_entity_type_enum",
    create_type=False,
)
CROSS_USER_CONFLICT_STATUS = postgresql.ENUM(
    "pending",
    "resolved",
    "ignored",
    name="cross_user_conflict_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    SHARED_CONTEXT_ENTITY_TYPE.create(bind, checkfirst=True)
    CROSS_USER_CONFLICT_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "shared_context_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", SHARED_CONTEXT_ENTITY_TYPE, nullable=False),
        sa.Column("entity_value", sa.Text(), nullable=False),
        sa.Column("source_proxy_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.75")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_superseded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("superseded_by_signal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_proxy_user_id"], ["proxy_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by_signal_id"], ["shared_context_signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shared_context_signals_tenant_type_value",
        "shared_context_signals",
        ["tenant_id", "entity_type", "entity_value"],
        unique=False,
    )

    op.create_table(
        "cross_user_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_a_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_b_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_type", SHARED_CONTEXT_ENTITY_TYPE, nullable=False),
        sa.Column("entity_value_a", sa.Text(), nullable=False),
        sa.Column("entity_value_b", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", CROSS_USER_CONFLICT_STATUS, nullable=False, server_default=sa.text("'pending'")),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'ignored')",
            name="ck_cross_user_conflicts_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_a_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_b_memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cross_user_conflicts_tenant_status",
        "cross_user_conflicts",
        ["tenant_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cross_user_conflicts_tenant_status", table_name="cross_user_conflicts")
    op.drop_table("cross_user_conflicts")
    op.drop_index("ix_shared_context_signals_tenant_type_value", table_name="shared_context_signals")
    op.drop_table("shared_context_signals")
    CROSS_USER_CONFLICT_STATUS.drop(op.get_bind(), checkfirst=True)
    SHARED_CONTEXT_ENTITY_TYPE.drop(op.get_bind(), checkfirst=True)
