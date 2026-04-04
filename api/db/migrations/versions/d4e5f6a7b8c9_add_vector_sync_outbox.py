"""add vector sync outbox

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-03-31 22:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "d4e5f6a7b8c9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


vector_sync_operation_enum = postgresql.ENUM(
    "upsert",
    "delete",
    name="vector_sync_operation_enum",
    create_type=False,
)
vector_sync_status_enum = postgresql.ENUM(
    "pending",
    "processing",
    "done",
    "failed",
    name="vector_sync_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    vector_sync_operation_enum.create(bind, checkfirst=True)
    vector_sync_status_enum.create(bind, checkfirst=True)

    if not inspector.has_table("vector_sync_outbox"):
        op.create_table(
            "vector_sync_outbox",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
            sa.Column("operation", vector_sync_operation_enum, nullable=False),
            sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("status", vector_sync_status_enum, nullable=False, server_default=sa.text("'pending'::vector_sync_status_enum")),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("vector_sync_outbox")}
    if "ix_vector_sync_outbox_status_created" not in existing_indexes:
        op.create_index(
            "ix_vector_sync_outbox_status_created",
            "vector_sync_outbox",
            ["status", "created_at"],
            unique=False,
        )
    if "ix_vector_sync_outbox_memory_id" not in existing_indexes:
        op.create_index(
            "ix_vector_sync_outbox_memory_id",
            "vector_sync_outbox",
            ["memory_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    existing_indexes = {index["name"] for index in inspector.get_indexes("vector_sync_outbox")} if inspector.has_table("vector_sync_outbox") else set()
    if "ix_vector_sync_outbox_memory_id" in existing_indexes:
        op.drop_index("ix_vector_sync_outbox_memory_id", table_name="vector_sync_outbox")
    if "ix_vector_sync_outbox_status_created" in existing_indexes:
        op.drop_index("ix_vector_sync_outbox_status_created", table_name="vector_sync_outbox")
    if inspector.has_table("vector_sync_outbox"):
        op.drop_table("vector_sync_outbox")

    vector_sync_status_enum.drop(bind, checkfirst=True)
    vector_sync_operation_enum.drop(bind, checkfirst=True)
