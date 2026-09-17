"""add durable conversation evidence and explicit proposal records

Revision ID: phase26b_evidence_ledger
Revises: prefer_openai_embedding_model
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "phase26b_evidence_ledger"
down_revision: str | None = "prefer_openai_embedding_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_evidence_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("proxy_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("extraction_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_scope_id", sa.String(length=320), nullable=False),
        sa.Column("turn_id", sa.String(length=300), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source_kind", sa.String(length=50), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "proxy_user_id", "conversation_scope_id", "turn_id", name="uq_conversation_evidence_turn_scope"),
    )
    op.create_index(
        "ix_conversation_evidence_turns_scope_created",
        "conversation_evidence_turns",
        ["tenant_id", "proxy_user_id", "conversation_scope_id", "created_at"],
    )

    op.create_table(
        "memory_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("proxy_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("extraction_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_scope_id", sa.String(length=320), nullable=False),
        sa.Column("assistant_turn_id", sa.String(length=300), nullable=False),
        sa.Column("assistant_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'active'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','accepted','cancelled','expired','superseded')", name="ck_memory_proposals_status"),
        sa.UniqueConstraint("tenant_id", "proxy_user_id", "conversation_scope_id", "assistant_turn_id", name="uq_memory_proposals_assistant_turn"),
    )
    op.create_index(
        "ix_memory_proposals_active_scope",
        "memory_proposals",
        ["tenant_id", "proxy_user_id", "conversation_scope_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_proposals_active_scope", table_name="memory_proposals")
    op.drop_table("memory_proposals")
    op.drop_index("ix_conversation_evidence_turns_scope_created", table_name="conversation_evidence_turns")
    op.drop_table("conversation_evidence_turns")