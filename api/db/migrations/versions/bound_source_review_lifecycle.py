"""Index bounded source-review lookup, admission and maintenance.

Revision ID: bound_source_review_lifecycle
Revises: backfill_api_key_permissions
"""
from alembic import op
from sqlalchemy import text

revision = "bound_source_review_lifecycle"
down_revision = "backfill_api_key_permissions"
branch_labels = None
depends_on = None

PREDICATE = "candidate_reason = 'source_decision_pending' AND (metadata['extraction_evidence']['source_review'] ->> 'kind') = 'restate_source'"


def upgrade():
    # Large existing tables must not block ingestion while indexes are built.
    with op.get_context().autocommit_block():
        definitions = {
            "ix_source_review_owner": "tenant_id, proxy_user_id, status, (metadata['extraction_evidence'] ->> 'preference_fingerprint'), created_at DESC, id",
            "ix_source_review_age": "status, created_at, id",
        }
        for name, columns in definitions.items():
            valid = op.get_bind().execute(text("""SELECT i.indisvalid FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :name AND n.nspname = current_schema()"""), {"name": name}).scalar_one_or_none()
            if valid is True:
                continue
            if valid is False:
                # Interrupted concurrent builds leave an INVALID index behind.
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON pending_extraction_candidates ({columns}) WHERE {PREDICATE}")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_source_review_age")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_source_review_owner")
