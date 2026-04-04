"""repair audit action enum

Revision ID: f6a7b8c9d0e1
Revises: e4f5a6b7c8d9
Create Date: 2026-03-30 22:55:00
"""

from __future__ import annotations

from alembic import op


revision = "f6a7b8c9d0e1"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'proxy_user_deleted'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in place.
    pass
