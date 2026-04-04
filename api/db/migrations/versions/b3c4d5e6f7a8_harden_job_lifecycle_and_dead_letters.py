"""harden job lifecycle and dead letters

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-02 18:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b3c4d5e6f7a8"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
    )
    op.add_column(
        "extraction_jobs",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column(
        "extraction_jobs",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "extraction_jobs",
        sa.Column("stale_after", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE extraction_jobs
        SET
            created_at = COALESCE(queued_at, NOW()),
            processing_started_at = COALESCE(started_at, processing_started_at)
        """
    )

    op.create_table(
        "dead_letter_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("extraction_jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "proxy_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proxy_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_extraction_jobs_status_stale_after",
        "extraction_jobs",
        ["status", "stale_after"],
    )
    op.create_index(
        "ix_dead_letter_jobs_tenant_created",
        "dead_letter_jobs",
        ["tenant_id", "created_at"],
    )

    op.execute(
        """
        INSERT INTO dead_letter_jobs (
            job_id,
            tenant_id,
            proxy_user_id,
            celery_task_id,
            attempts,
            payload,
            error,
            created_at
        )
        SELECT
            id,
            tenant_id,
            proxy_user_id,
            celery_task_id,
            attempts,
            payload,
            error,
            COALESCE(dead_lettered_at, updated_at, NOW())
        FROM extraction_jobs
        WHERE status = 'dead'::extraction_job_status_enum
        ON CONFLICT (job_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_jobs_tenant_created", table_name="dead_letter_jobs")
    op.drop_index("ix_extraction_jobs_status_stale_after", table_name="extraction_jobs")
    op.drop_table("dead_letter_jobs")
    op.drop_column("extraction_jobs", "stale_after")
    op.drop_column("extraction_jobs", "processing_started_at")
    op.drop_column("extraction_jobs", "created_at")
    op.drop_column("extraction_jobs", "max_attempts")
