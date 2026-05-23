"""add edtech memories

Revision ID: add_edtech_memories
Revises: add_universal_memory_versions
Create Date: 2026-05-17 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "add_edtech_memories"
down_revision = "add_universal_memory_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edtech_memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "proxy_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proxy_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grade_level", sa.String(length=50), nullable=True),
        sa.Column("board_or_curriculum", sa.String(length=100), nullable=True),
        sa.Column("subjects", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("syllabus_stage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("strong_topics", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("weak_topics", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("concept_gaps", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("misconceptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("explanation_style", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("session_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("language_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("peak_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("exam_name", sa.String(length=200), nullable=True),
        sa.Column("exam_date", sa.Date(), nullable=True),
        sa.Column("marks_target", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mock_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("forgetting_stages", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("improvement_velocity", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("streak", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_topic_studied", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_extraction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "extraction_source_job_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("proxy_user_id", "tenant_id", name="uq_edtech_memories_proxy_tenant"),
    )
    op.create_index("ix_edtech_memories_tenant_exam_date", "edtech_memories", ["tenant_id", "exam_date"])
    op.create_index(
        "ix_edtech_memories_tenant_last_extraction",
        "edtech_memories",
        ["tenant_id", "last_extraction_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_edtech_memories_tenant_last_extraction", table_name="edtech_memories")
    op.drop_index("ix_edtech_memories_tenant_exam_date", table_name="edtech_memories")
    op.drop_table("edtech_memories")
