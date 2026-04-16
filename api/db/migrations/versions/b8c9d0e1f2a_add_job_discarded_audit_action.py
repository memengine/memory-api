"""add job_discarded audit action

Revision ID: b8c9d0e1f2a
Revises: a7b8c9d0e1f2
Create Date: 2026-04-04 18:45:00
"""

from __future__ import annotations

from alembic import op


revision = "b8c9d0e1f2a"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'job_discarded'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in place.
    pass
