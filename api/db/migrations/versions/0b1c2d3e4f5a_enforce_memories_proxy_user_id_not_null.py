"""enforce memories proxy user id not null

Revision ID: 0b1c2d3e4f5a
Revises: f6a7b8c9d0e1
Create Date: 2026-03-30 23:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0b1c2d3e4f5a"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        sa.text(
            """
            INSERT INTO tenants (id, company_name, is_active, created_at)
            VALUES (CAST(:tenant_id AS uuid), 'Legacy User Migration', TRUE, NOW())
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(tenant_id=LEGACY_TENANT_ID)
    )

    op.execute(
        sa.text(
            """
            WITH legacy_map AS (
                SELECT DISTINCT
                    m.user_id AS legacy_user_id,
                    COALESCE(u.external_id, m.user_id::text) AS external_user_id,
                    encode(
                        digest(
                            convert_to(
                                :tenant_id || ':' || COALESCE(u.external_id, m.user_id::text),
                                'UTF8'
                            ),
                            'sha256'
                        ),
                        'hex'
                    ) AS external_user_id_hash
                FROM memories m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.proxy_user_id IS NULL
            )
            INSERT INTO proxy_users (
                tenant_id,
                external_user_id,
                external_user_id_hash,
                metadata
            )
            SELECT
                CAST(:tenant_id AS uuid),
                legacy_map.external_user_id,
                legacy_map.external_user_id_hash,
                jsonb_build_object(
                    'migration_source', 'legacy_user_backfill',
                    'legacy_user_id', legacy_map.legacy_user_id::text
                )
            FROM legacy_map
            ON CONFLICT (tenant_id, external_user_id_hash)
            DO UPDATE SET last_active_at = NOW()
            """
        ).bindparams(tenant_id=LEGACY_TENANT_ID)
    )

    op.execute(
        sa.text(
            """
            WITH legacy_map AS (
                SELECT DISTINCT
                    m.user_id AS legacy_user_id,
                    encode(
                        digest(
                            convert_to(
                                :tenant_id || ':' || COALESCE(u.external_id, m.user_id::text),
                                'UTF8'
                            ),
                            'sha256'
                        ),
                        'hex'
                    ) AS external_user_id_hash
                FROM memories m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.proxy_user_id IS NULL
            )
            UPDATE memories AS m
            SET proxy_user_id = pu.id
            FROM legacy_map
            JOIN proxy_users pu
              ON pu.tenant_id = CAST(:tenant_id AS uuid)
             AND pu.external_user_id_hash = legacy_map.external_user_id_hash
            WHERE m.user_id = legacy_map.legacy_user_id
              AND m.proxy_user_id IS NULL
            """
        ).bindparams(tenant_id=LEGACY_TENANT_ID)
    )

    op.execute(
        """
        UPDATE proxy_users AS pu
        SET memory_count = counts.memory_count
        FROM (
            SELECT proxy_user_id, COUNT(*)::integer AS memory_count
            FROM memories
            WHERE proxy_user_id IS NOT NULL
            GROUP BY proxy_user_id
        ) AS counts
        WHERE pu.id = counts.proxy_user_id
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_proxy_users_tenant_active
        ON proxy_users (tenant_id, last_active_at DESC)
        """
    )

    op.alter_column("memories", "proxy_user_id", existing_type=sa.UUID(), nullable=False)


def downgrade() -> None:
    op.alter_column("memories", "proxy_user_id", existing_type=sa.UUID(), nullable=True)
    op.execute("DROP INDEX IF EXISTS ix_proxy_users_tenant_active")
