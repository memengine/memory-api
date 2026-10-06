"""Bounded release proofs; neither historical backfill nor applied receipts."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "runtime_target_releases"
down_revision = "runtime_job_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("runtime_hold_released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runtime_job_reconciliations", sa.Column("release_json", postgresql.JSONB(), nullable=True))
    op.execute("ALTER TABLE runtime_job_reconciliations ADD CONSTRAINT ck_runtime_release_bound "
        "CHECK (release_json IS NULL OR (jsonb_typeof(release_json) = 'object' "
        "AND octet_length(release_json::text) <= 32768)) NOT VALID")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_runtime_release_bound", "runtime_job_reconciliations", type_="check")
    op.drop_column("runtime_job_reconciliations", "release_json")
    op.drop_column("extraction_jobs", "runtime_hold_released_at")
