"""prepare encrypted envelopes for core memory text

Revision ID: core_text_envelopes
Revises: cross_user_conflict_pair_indexes

The nullable envelope columns make a staged, reversible migration possible.
Plaintext remains in place until the application has been switched to
ciphertext reads/writes and existing data has been safely backfilled.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision: str = "core_text_envelopes"
down_revision: str | None = "cross_user_conflict_pair_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("content_envelope", postgresql.JSONB(), nullable=True))
    op.add_column("memory_versions", sa.Column("content_envelope", postgresql.JSONB(), nullable=True))
    op.add_column("extraction_jobs", sa.Column("payload_envelope", postgresql.JSONB(), nullable=True))
    op.add_column("extraction_jobs", sa.Column("result_envelope", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("extraction_jobs", "result_envelope")
    op.drop_column("extraction_jobs", "payload_envelope")
    op.drop_column("memory_versions", "content_envelope")
    op.drop_column("memories", "content_envelope")
