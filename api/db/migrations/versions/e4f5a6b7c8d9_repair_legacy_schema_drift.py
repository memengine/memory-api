"""repair legacy schema drift

Revision ID: e4f5a6b7c8d9
Revises: c3f4d5e6a7b8
Create Date: 2026-03-30 22:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "e4f5a6b7c8d9"
down_revision = "c3f4d5e6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "memory_count" not in user_columns:
        op.add_column(
            "users",
            sa.Column("memory_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )

    audit_log_columns = {column["name"] for column in inspector.get_columns("audit_logs")}
    if "metadata" not in audit_log_columns:
        op.add_column(
            "audit_logs",
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    audit_log_columns = {column["name"] for column in inspector.get_columns("audit_logs")}
    if "metadata" in audit_log_columns:
        op.drop_column("audit_logs", "metadata")

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "memory_count" in user_columns:
        op.drop_column("users", "memory_count")
