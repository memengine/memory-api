"""add job error type and ensure payload columns

Revision ID: add_job_error_type_payload
Revises: add_memory_versions
Create Date: 2026-04-30 12:55:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_job_error_type_payload"
down_revision = "add_memory_versions"
branch_labels = None
depends_on = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column.name},
    ).scalar_one_or_none()
    if exists is None:
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing(
        "extraction_jobs",
        sa.Column("error_type", sa.String(length=60), nullable=True),
    )
    _add_column_if_missing(
        "extraction_jobs",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    _add_column_if_missing(
        "dead_letter_jobs",
        sa.Column("error_type", sa.String(length=60), nullable=True),
    )
    _add_column_if_missing(
        "dead_letter_jobs",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dead_letter_jobs", "error_type")
    op.drop_column("extraction_jobs", "error_type")
