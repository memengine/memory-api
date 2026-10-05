"""Minimal job reconciliation records; no guard release or historical backfill.

Revision ID: runtime_job_reconciliation
Revises: runtime_withholding_footprint
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "runtime_job_reconciliation"
down_revision = "runtime_withholding_footprint"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table("runtime_job_reconciliations",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("proxy_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("record_json", postgresql.JSONB(), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("audit_logs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("jsonb_typeof(record_json) = 'object' AND octet_length(record_json::text) <= 4096",
                           name="ck_runtime_reconciliation_bound"))


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("runtime_job_reconciliations")
