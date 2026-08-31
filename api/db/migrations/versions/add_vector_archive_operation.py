"""add vector archive operation

Revision ID: add_vector_archive_operation
Revises: add_temporal_validity_fields
"""

from collections.abc import Sequence

from alembic import op


revision: str = "add_vector_archive_operation"
down_revision: str | None = "add_temporal_validity_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE vector_sync_operation_enum ADD VALUE IF NOT EXISTS 'archive'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be safely removed in place. Older application versions
    # continue to ignore completed archive rows; keep the value during downgrade.
    pass
