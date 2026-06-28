"""Add schema and processor versions to claim revisions.

Revision ID: claim_revision_versions
Revises: universal_claim_ledger
Create Date: 2026-06-23 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "claim_revision_versions"
down_revision = "universal_claim_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("memory_claim_revisions", "universal_memory_claim_revisions"):
        op.add_column(
            table_name,
            sa.Column(
                "schema_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "processor_version",
                sa.String(length=100),
                nullable=False,
                server_default=sa.text("'legacy'"),
            ),
        )
        op.create_check_constraint(
            f"ck_{table_name}_schema_version_positive",
            table_name,
            "schema_version > 0",
        )
        op.create_index(
            f"ix_{table_name}_versions",
            table_name,
            ["schema_version", "processor_version"],
        )


def downgrade() -> None:
    for table_name in ("universal_memory_claim_revisions", "memory_claim_revisions"):
        op.drop_index(f"ix_{table_name}_versions", table_name=table_name)
        op.drop_constraint(
            f"ck_{table_name}_schema_version_positive",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "processor_version")
        op.drop_column(table_name, "schema_version")