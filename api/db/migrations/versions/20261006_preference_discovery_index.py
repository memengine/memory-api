"""Bound nominated-property scope checks without scanning every owner claim."""
import sqlalchemy as sa
from alembic import op

revision = "preference_discovery_index"
down_revision = "runtime_fact_visibility_index"
branch_labels = None
depends_on = None
NAME = "ix_memory_claims_preference_scopes"


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            valid = op.get_bind().execute(sa.text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE c.oid=to_regclass(:name) AND i.indrelid='memory_claims'::regclass"), {"name": NAME}).scalar_one_or_none()
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY {NAME}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} ON memory_claims (tenant_id, proxy_user_id, predicate_key) "
                "WHERE category = 'preference' AND status IN ('active','disputed')")
        finally:
            op.execute("RESET lock_timeout")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}")
