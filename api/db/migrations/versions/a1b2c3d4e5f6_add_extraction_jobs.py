"""add extraction jobs

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-04-02 10:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a1b2c3d4e5f6"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


extraction_job_status_enum = postgresql.ENUM(
    "queued",
    "processing",
    "completed",
    "failed",
    "dead",
    name="extraction_job_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    extraction_job_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "extraction_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "proxy_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proxy_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            extraction_job_status_enum,
            nullable=False,
            server_default=sa.text("'queued'::extraction_job_status_enum"),
        ),
        sa.Column("queue_name", sa.String(length=64), nullable=True),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("memories_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_extraction_jobs_status_updated", "extraction_jobs", ["status", "updated_at"])
    op.create_index("ix_extraction_jobs_tenant_status", "extraction_jobs", ["tenant_id", "status"])
    op.create_index("ix_extraction_jobs_proxy_user", "extraction_jobs", ["proxy_user_id"])


def downgrade() -> None:
    op.drop_index("ix_extraction_jobs_proxy_user", table_name="extraction_jobs")
    op.drop_index("ix_extraction_jobs_tenant_status", table_name="extraction_jobs")
    op.drop_index("ix_extraction_jobs_status_updated", table_name="extraction_jobs")
    op.drop_table("extraction_jobs")
    bind = op.get_bind()
    extraction_job_status_enum.drop(bind, checkfirst=True)
