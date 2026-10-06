"""Separate committed processing from unresolved read currentness, without backfill."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "runtime_fact_visibility"
down_revision = "runtime_release_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("runtime_visibility_finalized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_jobs", sa.Column("runtime_residual_hold", postgresql.JSONB(), nullable=True))
    op.execute("ALTER TABLE extraction_jobs ADD CONSTRAINT ck_runtime_residual_hold_bound CHECK "
        "(runtime_residual_hold IS NULL OR (jsonb_typeof(runtime_residual_hold) = 'object' "
        "AND octet_length(runtime_residual_hold::text) <= 8192)) NOT VALID")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_runtime_residual_hold_bound", "extraction_jobs", type_="check")
    op.drop_column("extraction_jobs", "runtime_residual_hold")
    op.drop_column("extraction_jobs", "runtime_visibility_finalized_at")
