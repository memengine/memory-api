"""Add retrieval feedback events.

Revision ID: retrieval_feedback_events
Revises: pending_extraction_candidates
Create Date: 2026-06-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "retrieval_feedback_events"
down_revision = "pending_extraction_candidates"
branch_labels = None
depends_on = None


VALID_OUTCOMES = "'used_successfully','used_partially','ignored','not_useful','user_corrected','clarification_needed'"


def upgrade() -> None:
    op.create_table(
        "retrieval_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("proxy_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("query_preview", sa.String(length=160), nullable=True),
        sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retrieved_memory_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("top_relevance_score", sa.Float(), nullable=True),
        sa.Column("low_relevance", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("not_found", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("included_in_prompt", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("quota_mode", sa.String(length=40), nullable=True),
        sa.Column("is_degraded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_retrieval_events_tenant_created", "retrieval_events", ["tenant_id", sa.text("created_at DESC")])
    op.create_index("ix_retrieval_events_proxy_user_created", "retrieval_events", ["proxy_user_id", sa.text("created_at DESC")])
    op.create_index("ix_retrieval_events_query_hash", "retrieval_events", ["tenant_id", "query_hash"])

    op.create_table(
        "retrieval_feedback_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("retrieval_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("retrieval_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("proxy_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("used_memory_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("agent_confidence", sa.Float(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("correction_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"outcome IN ({VALID_OUTCOMES})", name="ck_retrieval_feedback_events_outcome"),
        sa.CheckConstraint("agent_confidence IS NULL OR (agent_confidence >= 0 AND agent_confidence <= 1)", name="ck_retrieval_feedback_events_agent_confidence"),
    )
    op.create_index("ix_retrieval_feedback_events_tenant_created", "retrieval_feedback_events", ["tenant_id", sa.text("created_at DESC")])
    op.create_index("ix_retrieval_feedback_events_outcome", "retrieval_feedback_events", ["tenant_id", "outcome", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_retrieval_feedback_events_outcome", table_name="retrieval_feedback_events")
    op.drop_index("ix_retrieval_feedback_events_tenant_created", table_name="retrieval_feedback_events")
    op.drop_table("retrieval_feedback_events")
    op.drop_index("ix_retrieval_events_query_hash", table_name="retrieval_events")
    op.drop_index("ix_retrieval_events_proxy_user_created", table_name="retrieval_events")
    op.drop_index("ix_retrieval_events_tenant_created", table_name="retrieval_events")
    op.drop_table("retrieval_events")