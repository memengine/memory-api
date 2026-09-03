"""prepare encrypted envelopes for Passport memory text

Revision ID: universal_memory_text_envelopes
Revises: core_text_envelopes

These nullable columns support an opt-in dual-write rollout. They deliberately
do not change existing plaintext reads or writes, so applying this migration is
safe before a later, separately reviewed encryption backfill and read cutover.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision: str = "universal_memory_text_envelopes"
down_revision: str | None = "core_text_envelopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("universal_memories", sa.Column("content_envelope", postgresql.JSONB(), nullable=True))
    op.add_column(
        "universal_memory_versions",
        sa.Column("content_envelope", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("universal_memory_versions", "content_envelope")
    op.drop_column("universal_memories", "content_envelope")
