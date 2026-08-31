"""enforce one activated revision per memory claim

Revision ID: single_activated_claim_revision
Revises: allow_support_type_source
"""

from alembic import op
import sqlalchemy as sa


revision = "single_activated_claim_revision"
down_revision = "allow_support_type_source"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_memory_claim_revisions_one_activated"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "memory_claim_revisions",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("status = 'activated'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memory_claim_revisions")
