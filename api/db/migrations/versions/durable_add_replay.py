"""Durable tenant request receipts on existing jobs; no historical backfill."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "durable_add_replay"
down_revision = "preference_confirmation_index"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("request_key_sha256", sa.String(64), nullable=True))
    op.add_column("extraction_jobs", sa.Column("request_binding", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint("ck_extraction_jobs_request_binding", "extraction_jobs",
        "(request_key_sha256 IS NULL AND request_binding IS NULL) OR "
        "(request_key_sha256 IS NOT NULL AND request_key_sha256 ~ '^[a-f0-9]{64}$' AND request_binding IS NOT NULL "
        "AND jsonb_typeof(request_binding) = 'object' "
        "AND octet_length(request_binding::text) <= 4096)")
    op.create_unique_constraint("uq_extraction_jobs_tenant_request_key", "extraction_jobs",
                               ["tenant_id", "request_key_sha256"])


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("uq_extraction_jobs_tenant_request_key", "extraction_jobs", type_="unique")
    op.drop_constraint("ck_extraction_jobs_request_binding", "extraction_jobs", type_="check")
    op.drop_column("extraction_jobs", "request_binding")
    op.drop_column("extraction_jobs", "request_key_sha256")
