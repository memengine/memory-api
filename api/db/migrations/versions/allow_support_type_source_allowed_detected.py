"""allow allowlisted Support type detection provenance

Revision ID: allow_support_type_source
Revises: add_scale_plan_tier
"""

from alembic import op
import sqlalchemy as sa


revision = "allow_support_type_source"
down_revision = "add_scale_plan_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_support_memories_support_type_source",
        "support_memories",
        type_="check",
    )
    op.create_check_constraint(
        "ck_support_memories_support_type_source",
        "support_memories",
        "support_type_source IN ('detected','allowed_detected','tenant_configured')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_support_memories_support_type_source",
        "support_memories",
        type_="check",
    )
    op.create_check_constraint(
        "ck_support_memories_support_type_source",
        "support_memories",
        "support_type_source IN ('detected','tenant_configured')",
    )
