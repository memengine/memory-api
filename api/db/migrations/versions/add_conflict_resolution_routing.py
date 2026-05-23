"""add conflict resolution routing fields

Revision ID: add_conflict_resolution_routing
Revises: add_clarification_queue
Create Date: 2026-05-08 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "add_conflict_resolution_routing"
down_revision = "add_clarification_queue"
branch_labels = None
depends_on = None


EXTRA_ENTITY_TYPES = [
    "organisation_policy",
    "team_language",
    "shared_resource",
    "exam_date",
    "grade_level",
    "personal_skill",
    "personal_preference",
    "individual_goal",
    "learning_style",
    "personal_fact",
    "marks_target",
    "study_schedule",
]


def upgrade() -> None:
    for value in EXTRA_ENTITY_TYPES:
        op.execute(f"ALTER TYPE shared_context_entity_type_enum ADD VALUE IF NOT EXISTS '{value}'")

    op.execute(
        "ALTER TYPE cross_user_conflict_status_enum ADD VALUE IF NOT EXISTS 'clarification_queued'"
    )
    op.execute(
        "ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'conflict_resolved_by_tenant'"
    )
    # PostgreSQL cannot use newly added enum values in constraints until the
    # ALTER TYPE transaction is committed.
    op.execute("COMMIT")

    op.drop_constraint(
        "ck_cross_user_conflicts_status",
        "cross_user_conflicts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_cross_user_conflicts_status",
        "cross_user_conflicts",
        "status IN ('pending', 'clarification_queued', 'resolved', 'ignored')",
    )

    op.add_column(
        "cross_user_conflicts",
        sa.Column("resolution_path", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column("resolved_by", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column("resolution", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "cross_user_conflicts",
        sa.Column("resolution_reason", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_cross_user_conflicts_resolution_path",
        "cross_user_conflicts",
        "resolution_path IS NULL OR resolution_path IN ('user_session', 'tenant_review')",
    )
    op.create_check_constraint(
        "ck_cross_user_conflicts_resolved_by",
        "cross_user_conflicts",
        "resolved_by IS NULL OR resolved_by IN ('user_session', 'tenant')",
    )
    op.create_check_constraint(
        "ck_cross_user_conflicts_resolution",
        "cross_user_conflicts",
        "resolution IS NULL OR resolution IN ('A', 'B', 'both_valid')",
    )

    op.drop_constraint(
        "ck_memory_versions_change_type",
        "memory_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_versions_change_type",
        "memory_versions",
        "change_type IN ('created', 'conflict_update', 'manual_edit', "
        "'importance_decay', 'importance_boost', 'archived', 'conflict_resolved')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_versions_change_type", "memory_versions", type_="check")
    op.create_check_constraint(
        "ck_memory_versions_change_type",
        "memory_versions",
        "change_type IN ('created', 'conflict_update', 'manual_edit', "
        "'importance_decay', 'importance_boost', 'archived')",
    )
    op.drop_constraint("ck_cross_user_conflicts_resolution", "cross_user_conflicts", type_="check")
    op.drop_constraint("ck_cross_user_conflicts_resolved_by", "cross_user_conflicts", type_="check")
    op.drop_constraint("ck_cross_user_conflicts_resolution_path", "cross_user_conflicts", type_="check")
    op.drop_column("cross_user_conflicts", "resolution_reason")
    op.drop_column("cross_user_conflicts", "resolution")
    op.drop_column("cross_user_conflicts", "resolved_by")
    op.drop_column("cross_user_conflicts", "resolved_at")
    op.drop_column("cross_user_conflicts", "resolution_path")
    op.drop_constraint("ck_cross_user_conflicts_status", "cross_user_conflicts", type_="check")
    op.create_check_constraint(
        "ck_cross_user_conflicts_status",
        "cross_user_conflicts",
        "status IN ('pending', 'resolved', 'ignored')",
    )
