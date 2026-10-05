"""Build visibility indexes online, separately from transactional columns.

Revision ID: runtime_pending_indexes
Revises: runtime_pending_visibility
"""
import sqlalchemy as sa
from alembic import op

revision = "runtime_pending_indexes"
down_revision = "runtime_pending_visibility"
branch_labels = None
depends_on = None

PREDICATE = "(runtime_route IS NOT NULL OR preference_intent_admission ? 'runtime_source_sha256')"
INDEXES = {"ix_extraction_jobs_runtime_owner": PREDICATE,
           "ix_extraction_jobs_runtime_pending": PREDICATE + " AND runtime_applied_at IS NULL"}


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            for name, predicate in INDEXES.items():
                # Interrupted builds may leave invalid indexes; repair only
                # these exact names on this exact managed table.
                valid = op.get_bind().execute(sa.text("""SELECT i.indisvalid FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE c.oid = to_regclass(:name) AND i.indrelid = 'extraction_jobs'::regclass"""),
                    {"name": name}).scalar_one_or_none()
                if valid is False:
                    op.execute(f"DROP INDEX CONCURRENTLY {name}")
                op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                           f"ON extraction_jobs (tenant_id, proxy_user_id) WHERE {predicate}")
        finally:
            op.execute("RESET lock_timeout")


def downgrade():
    with op.get_context().autocommit_block():
        for name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
