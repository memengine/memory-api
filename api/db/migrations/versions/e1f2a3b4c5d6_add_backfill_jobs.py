"""add backfill jobs

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-04-01 10:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "e1f2a3b4c5d6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    status_enum = sa.Enum(
        "running",
        "paused",
        "complete",
        "failed",
        name="backfill_job_status_enum",
    )
    status_enum.create(bind, checkfirst=True)

    if not inspector.has_table("backfill_jobs"):
        op.create_table(
            "backfill_jobs",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False, server_default=sa.text("gen_random_uuid()")),
            sa.Column("task_name", sa.String(length=255), nullable=False),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "running",
                    "paused",
                    "complete",
                    "failed",
                    name="backfill_job_status_enum",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column("total_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("processed_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("pct_complete", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("eta_seconds", sa.Integer(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("backfill_jobs")} if inspector.has_table("backfill_jobs") else set()
    if "ix_backfill_jobs_status_started_at" not in existing_indexes:
        op.create_index(
            "ix_backfill_jobs_status_started_at",
            "backfill_jobs",
            ["status", "started_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("backfill_jobs")} if inspector.has_table("backfill_jobs") else set()
    if "ix_backfill_jobs_status_started_at" in existing_indexes:
        op.drop_index("ix_backfill_jobs_status_started_at", table_name="backfill_jobs")
    if inspector.has_table("backfill_jobs"):
        op.drop_table("backfill_jobs")
    sa.Enum(name="backfill_job_status_enum").drop(bind, checkfirst=True)
