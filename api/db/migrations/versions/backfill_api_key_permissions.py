"""backfill safe defaults for legacy API keys

Revision ID: backfill_api_key_permissions
Revises: phase3a_structured_claim
"""

from collections.abc import Sequence

from alembic import op

revision: str = "backfill_api_key_permissions"
down_revision: str | None = "phase3a_structured_claim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Permission arrays were historically optional and were not enforced.
    # Give legacy keys the same safe default as newly created SDK keys. More
    # sensitive delete/admin access must be granted explicitly with a new key.
    op.execute(
        """
        UPDATE api_keys
        SET permissions = ARRAY['read', 'write']::varchar[]
        WHERE permissions IS NULL OR cardinality(permissions) = 0
        """
    )


def downgrade() -> None:
    # Do not erase explicit read/write grants when rolling application code
    # back; this data normalization is safe to leave in place.
    pass
