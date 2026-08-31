"""index and constrain unordered cross-user conflict memory pairs

Revision ID: cross_user_conflict_pair_indexes
Revises: add_vector_archive_operation
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "cross_user_conflict_pair_indexes"
down_revision: str | None = "add_vector_archive_operation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve any legacy duplicate audit rows instead of deleting history. New rows use
    # the server default below and are protected by the unordered-pair invariant.
    op.add_column(
        "cross_user_conflicts",
        sa.Column(
            "pair_dedup_enforced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column(
        "cross_user_conflicts",
        "pair_dedup_enforced",
        server_default=sa.text("true"),
    )
    op.create_index(
        "ix_cross_user_conflicts_tenant_type_memory_a",
        "cross_user_conflicts",
        ["tenant_id", "entity_type", "user_a_memory_id"],
        unique=False,
    )
    op.create_index(
        "ix_cross_user_conflicts_tenant_type_memory_b",
        "cross_user_conflicts",
        ["tenant_id", "entity_type", "user_b_memory_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_cross_user_conflicts_unordered_memory_pair
        ON cross_user_conflicts (
            tenant_id,
            entity_type,
            LEAST(user_a_memory_id, user_b_memory_id),
            GREATEST(user_a_memory_id, user_b_memory_id)
        )
        WHERE pair_dedup_enforced
          AND user_a_memory_id IS NOT NULL
          AND user_b_memory_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "uq_cross_user_conflicts_unordered_memory_pair",
        table_name="cross_user_conflicts",
    )
    op.drop_index(
        "ix_cross_user_conflicts_tenant_type_memory_b",
        table_name="cross_user_conflicts",
    )
    op.drop_index(
        "ix_cross_user_conflicts_tenant_type_memory_a",
        table_name="cross_user_conflicts",
    )
    op.drop_column("cross_user_conflicts", "pair_dedup_enforced")
