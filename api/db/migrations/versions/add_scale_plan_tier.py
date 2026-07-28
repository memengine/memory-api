"""Add scale plan tier.

Revision ID: add_scale_plan_tier
Revises: conflict_decision_evidence
Create Date: 2026-07-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op


revision = "add_scale_plan_tier"
down_revision = "conflict_decision_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE plan_tier_enum ADD VALUE IF NOT EXISTS 'scale'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without recreating the type.
    pass