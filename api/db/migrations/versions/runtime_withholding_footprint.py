"""Retain a bounded decision footprint independently of raw payload retention.

Revision ID: runtime_withholding_footprint
Revises: runtime_pending_indexes
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "runtime_withholding_footprint"
down_revision = "runtime_pending_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("runtime_withholding_footprint",
                  postgresql.JSONB(none_as_null=True), nullable=True))
    # No table scan on deployment. New/changed rows still enforce the bound;
    # preexisting rows have NULL, so a later validation can be scheduled safely.
    op.execute("ALTER TABLE extraction_jobs ADD CONSTRAINT ck_runtime_withholding_footprint_bound "
               "CHECK (runtime_withholding_footprint IS NULL OR "
               "(jsonb_typeof(runtime_withholding_footprint) = 'object' "
               "AND octet_length(runtime_withholding_footprint::text) <= 8192)) NOT VALID")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_runtime_withholding_footprint_bound", "extraction_jobs", type_="check")
    op.drop_column("extraction_jobs", "runtime_withholding_footprint")
