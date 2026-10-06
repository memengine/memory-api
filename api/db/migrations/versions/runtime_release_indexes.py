"""Keep unresolved-owner probes indexed as discharged history grows."""
import sqlalchemy as sa
from alembic import op

revision = "runtime_release_indexes"
down_revision = "runtime_target_releases"
branch_labels = None
depends_on = None
NAME = "ix_extraction_jobs_runtime_unreleased"
PREDICATE = "(runtime_route IS NOT NULL OR preference_intent_admission ? 'runtime_source_sha256') AND runtime_applied_at IS NULL AND runtime_hold_released_at IS NULL"


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            valid = op.get_bind().execute(sa.text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.oid = to_regclass(:name) AND i.indrelid = 'extraction_jobs'::regclass"), {"name": NAME}).scalar_one_or_none()
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY {NAME}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} ON extraction_jobs (tenant_id, proxy_user_id) WHERE {PREDICATE}")
        finally:
            op.execute("RESET lock_timeout")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
