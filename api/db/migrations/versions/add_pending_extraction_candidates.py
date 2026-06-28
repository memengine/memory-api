"""Add pending extraction candidates.

Revision ID: pending_extraction_candidates
Revises: claim_revision_versions
Create Date: 2026-06-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "pending_extraction_candidates"
down_revision = "claim_revision_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_extraction_candidates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proxy_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proxy_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("extraction_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_source_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                "preference",
                "fact",
                "goal",
                "procedure",
                "relationship",
                "expertise",
                name="memory_category_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "candidate_reason",
            sa.String(length=80),
            nullable=False,
            server_default=sa.text("'confidence_below_store_threshold'"),
        ),
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("reinforcement_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','promoted','dismissed','expired')",
            name="ck_pending_extraction_candidates_status",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_pending_extraction_candidates_confidence",
        ),
        sa.CheckConstraint(
            "importance_score >= 1 AND importance_score <= 10",
            name="ck_pending_extraction_candidates_importance",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "proxy_user_id",
            "candidate_fingerprint",
            name="uq_pending_extraction_candidates_fingerprint",
        ),
    )
    op.create_index(
        "ix_pending_extraction_candidates_tenant_status",
        "pending_extraction_candidates",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_pending_extraction_candidates_proxy_user",
        "pending_extraction_candidates",
        ["proxy_user_id", "status"],
    )
    op.create_index(
        "ix_pending_extraction_candidates_job",
        "pending_extraction_candidates",
        ["extraction_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_extraction_candidates_job", table_name="pending_extraction_candidates")
    op.drop_index("ix_pending_extraction_candidates_proxy_user", table_name="pending_extraction_candidates")
    op.drop_index("ix_pending_extraction_candidates_tenant_status", table_name="pending_extraction_candidates")
    op.drop_table("pending_extraction_candidates")

