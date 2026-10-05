"""Persist runtime routing and an indexed, atomic applied-receipt projection.

Revision ID: runtime_pending_visibility
Revises: preference_intent_admission
"""
import sqlalchemy as sa
from alembic import op

revision = "runtime_pending_visibility"
down_revision = "preference_intent_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("runtime_route", sa.String(32), nullable=True))
    op.add_column("extraction_jobs", sa.Column("runtime_applied_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("extraction_jobs", "runtime_applied_at")
    op.drop_column("extraction_jobs", "runtime_route")
