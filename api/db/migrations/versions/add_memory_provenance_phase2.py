"""Add Phase 2 service-writer and memory provenance records.

Revision ID: memory_provenance_phase2
Revises: support_routing_fields
Create Date: 2026-06-11 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "memory_provenance_phase2"
down_revision = "support_routing_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_writers",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "authority_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", name="uq_service_writers_api_key"),
        sa.UniqueConstraint("tenant_id", "service_key", name="uq_service_writers_tenant_service"),
    )
    op.create_index("ix_service_writers_tenant_active", "service_writers", ["tenant_id", "is_active"])

    op.create_table(
        "memory_source_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("writer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_service", sa.String(length=100), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "processing_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proxy_user_id"], ["proxy_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["writer_id"], ["service_writers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_service",
            "source_event_id",
            name="uq_memory_source_events_tenant_service_event",
        ),
    )
    op.create_index(
        "ix_memory_source_events_tenant_observed",
        "memory_source_events",
        ["tenant_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "ix_memory_source_events_proxy_user_observed",
        "memory_source_events",
        ["proxy_user_id", sa.text("observed_at DESC")],
    )

    op.add_column("memories", sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_memories_source_event_id",
        "memories",
        "memory_source_events",
        ["source_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_memories_source_event_id", "memories", ["source_event_id"])

    op.add_column("extraction_jobs", sa.Column("source_event_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("extraction_jobs", sa.Column("raw_payload_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("extraction_jobs", sa.Column("payload_redacted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_extraction_jobs_source_event_id",
        "extraction_jobs",
        "memory_source_events",
        ["source_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("uq_extraction_jobs_source_event_id", "extraction_jobs", ["source_event_id"])
    op.create_index("ix_extraction_jobs_payload_retention", "extraction_jobs", ["raw_payload_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_extraction_jobs_payload_retention", table_name="extraction_jobs")
    op.drop_constraint("uq_extraction_jobs_source_event_id", "extraction_jobs", type_="unique")
    op.drop_constraint("fk_extraction_jobs_source_event_id", "extraction_jobs", type_="foreignkey")
    op.drop_column("extraction_jobs", "payload_redacted_at")
    op.drop_column("extraction_jobs", "raw_payload_expires_at")
    op.drop_column("extraction_jobs", "source_event_id")

    op.drop_index("ix_memories_source_event_id", table_name="memories")
    op.drop_constraint("fk_memories_source_event_id", "memories", type_="foreignkey")
    op.drop_column("memories", "source_event_id")

    op.drop_index("ix_memory_source_events_proxy_user_observed", table_name="memory_source_events")
    op.drop_index("ix_memory_source_events_tenant_observed", table_name="memory_source_events")
    op.drop_table("memory_source_events")
    op.drop_index("ix_service_writers_tenant_active", table_name="service_writers")
    op.drop_table("service_writers")
