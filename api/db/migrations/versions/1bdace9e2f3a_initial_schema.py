"""initial_schema

Revision ID: 1bdace9e2f3a
Revises:
Create Date: 2026-03-22 10:22:38.218176
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "1bdace9e2f3a"
down_revision = None
branch_labels = None
depends_on = None


agent_memory_scope_enum = postgresql.ENUM(
    "private",
    "shared",
    name="agent_memory_scope_enum",
    create_type=False,
)
conversation_processing_status_enum = postgresql.ENUM(
    "queued",
    "processing",
    "done",
    "failed",
    name="conversation_processing_status_enum",
    create_type=False,
)
memory_category_enum = postgresql.ENUM(
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
    name="memory_category_enum",
    create_type=False,
)
audit_action_enum = postgresql.ENUM(
    "memory_created",
    "updated",
    "archived",
    "deleted",
    "retrieved",
    name="audit_action_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        agent_memory_scope_enum,
        conversation_processing_status_enum,
        memory_category_enum,
        audit_action_enum,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("external_id"),
    )

    op.create_table(
        "agents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("memory_scope", agent_memory_scope_enum, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "processing_status",
            conversation_processing_status_enum,
            nullable=False,
            server_default=sa.text("'queued'::conversation_processing_status_enum"),
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", memory_category_enum, nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("embedding_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["previous_version_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_index("ix_memories_user_category", "memories", ["user_id", "category"], unique=False)
    op.create_index(
        "ix_memories_user_importance_score_desc",
        "memories",
        ["user_id", sa.desc(sa.column("importance_score"))],
        unique=False,
    )
    op.create_index(
        "ix_memories_user_last_accessed_at_desc",
        "memories",
        ["user_id", sa.desc(sa.column("last_accessed_at"))],
        unique=False,
    )
    op.create_index("ix_memories_user_is_archived", "memories", ["user_id", "is_archived"], unique=False)
    op.create_index("ix_memories_metadata_gin", "memories", ["metadata"], unique=False, postgresql_using="gin")

    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", audit_action_enum, nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("audit_logs")
    op.drop_index("ix_memories_metadata_gin", table_name="memories")
    op.drop_index("ix_memories_user_is_archived", table_name="memories")
    op.drop_index("ix_memories_user_last_accessed_at_desc", table_name="memories")
    op.drop_index("ix_memories_user_importance_score_desc", table_name="memories")
    op.drop_index("ix_memories_user_category", table_name="memories")
    op.drop_table("memories")
    op.drop_table("conversations")
    op.drop_table("api_keys")
    op.drop_table("agents")
    op.drop_table("users")

    for enum_type in (
        audit_action_enum,
        memory_category_enum,
        conversation_processing_status_enum,
        agent_memory_scope_enum,
    ):
        enum_type.drop(bind, checkfirst=True)
