"""fix cross agent schema drift

Revision ID: fix_cross_agent_schema_drift
Revises: add_agent_api_keys
Create Date: 2026-04-22 09:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "fix_cross_agent_schema_drift"
down_revision = "add_agent_api_keys"
branch_labels = None
depends_on = None


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    result = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar()
    return bool(result)


def _index_exists(connection, index_name: str) -> bool:
    result = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = :index_name
            """
        ),
        {"index_name": index_name},
    ).scalar()
    return bool(result)


def upgrade() -> None:
    connection = op.get_bind()

    if not _column_exists(connection, "universal_users", "otp_code"):
        op.add_column("universal_users", sa.Column("otp_code", sa.String(length=6), nullable=True))

    if not _column_exists(connection, "universal_users", "otp_expires_at"):
        op.add_column("universal_users", sa.Column("otp_expires_at", sa.DateTime(timezone=True), nullable=True))

    if not _index_exists(connection, "ix_universal_users_email"):
        op.create_index("ix_universal_users_email", "universal_users", ["email"], unique=True)

    if not _column_exists(connection, "global_agents", "redirect_uri"):
        op.add_column(
            "global_agents",
            sa.Column("redirect_uri", sa.String(length=500), nullable=False, server_default=sa.text("''")),
        )

    if not _index_exists(connection, "ix_agent_api_keys_global_agent_active"):
        op.create_index(
            "ix_agent_api_keys_global_agent_active",
            "agent_api_keys",
            ["global_agent_id", "is_active"],
            unique=False,
        )

    connection.execute(sa.text("UPDATE agent_api_keys SET key_prefix = '' WHERE key_prefix IS NULL"))
    connection.execute(sa.text("ALTER TABLE agent_api_keys ALTER COLUMN key_prefix SET NOT NULL"))


def downgrade() -> None:
    connection = op.get_bind()

    connection.execute(sa.text("ALTER TABLE agent_api_keys ALTER COLUMN key_prefix DROP NOT NULL"))

    if _index_exists(connection, "ix_agent_api_keys_global_agent_active"):
        op.drop_index("ix_agent_api_keys_global_agent_active", table_name="agent_api_keys")

    if _column_exists(connection, "global_agents", "redirect_uri"):
        op.drop_column("global_agents", "redirect_uri")

    if _index_exists(connection, "ix_universal_users_email"):
        op.drop_index("ix_universal_users_email", table_name="universal_users")

    if _column_exists(connection, "universal_users", "otp_expires_at"):
        op.drop_column("universal_users", "otp_expires_at")

    if _column_exists(connection, "universal_users", "otp_code"):
        op.drop_column("universal_users", "otp_code")
