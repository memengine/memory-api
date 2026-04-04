"""add embedding model versioning

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-01 20:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


DEFAULT_EMBEDDING_MODEL_ID = "gemini-embedding-001-v1"


def upgrade() -> None:
    provider_enum = postgresql.ENUM(
        "gemini",
        "openai",
        "local",
        name="embedding_provider_enum",
    )
    provider_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "embedding_models",
        sa.Column("id", sa.String(length=50), nullable=False),
        sa.Column(
            "provider",
            postgresql.ENUM(
                "gemini",
                "openai",
                "local",
                name="embedding_provider_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("qdrant_collection", sa.String(length=100), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qdrant_collection"),
    )
    op.create_index(
        "ix_embedding_models_single_active",
        "embedding_models",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO embedding_models (
                id,
                provider,
                model_name,
                dimensions,
                qdrant_collection,
                is_active
            ) VALUES (
                :id,
                'gemini',
                'gemini-embedding-001',
                1536,
                'memories',
                TRUE
            )
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=DEFAULT_EMBEDDING_MODEL_ID)
    )

    op.add_column("memories", sa.Column("embedding_model_id", sa.String(length=50), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE memories
            SET embedding_model_id = :model_id
            WHERE embedding_model_id IS NULL
            """
        ).bindparams(model_id=DEFAULT_EMBEDDING_MODEL_ID)
    )
    op.alter_column("memories", "embedding_model_id", nullable=False)
    op.create_foreign_key(
        "fk_memories_embedding_model_id",
        "memories",
        "embedding_models",
        ["embedding_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_memories_embedding_model_id", "memories", ["embedding_model_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memories_embedding_model_id", table_name="memories")
    op.drop_constraint("fk_memories_embedding_model_id", "memories", type_="foreignkey")
    op.drop_column("memories", "embedding_model_id")
    op.drop_index("ix_embedding_models_single_active", table_name="embedding_models")
    op.drop_table("embedding_models")
    provider_enum = postgresql.ENUM(
        "gemini",
        "openai",
        "local",
        name="embedding_provider_enum",
    )
    provider_enum.drop(op.get_bind(), checkfirst=True)
