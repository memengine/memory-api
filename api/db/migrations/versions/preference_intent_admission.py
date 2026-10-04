"""Nullable, bounded server-only extraction intent admission.

Revision ID: preference_intent_admission
Revises: bound_source_review_lifecycle
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "preference_intent_admission"
down_revision = "bound_source_review_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    # No default, backfill or whole-table validation scan. Fail briefly rather
    # than waiting indefinitely for the production table's DDL lock.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("extraction_jobs", sa.Column("preference_intent_admission", JSONB(), nullable=True))
    op.add_column("extraction_jobs", sa.Column("preference_intent_admission_envelope", JSONB(), nullable=True))
    op.execute("""ALTER TABLE extraction_jobs ADD CONSTRAINT ck_preference_intent_admission_bound
        CHECK (preference_intent_admission IS NULL OR
            (jsonb_typeof(preference_intent_admission) = 'object'
             AND octet_length(preference_intent_admission::text) <= 262144)) NOT VALID""")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_preference_intent_admission_bound", "extraction_jobs", type_="check")
    op.drop_column("extraction_jobs", "preference_intent_admission_envelope")
    op.drop_column("extraction_jobs", "preference_intent_admission")
