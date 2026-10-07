"""Index bounded preference-confirmation reads without a new table."""
from alembic import op

revision = "preference_confirmation_index"
down_revision = "preference_discovery_index"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_preference_confirmation_owner "
            "ON pending_extraction_candidates (tenant_id, proxy_user_id, status, created_at DESC, id) "
            "WHERE candidate_reason = 'preference_confirmation_pending'")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_preference_confirmation_owner")
