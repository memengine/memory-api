from __future__ import annotations

import os
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.config.plan_limits import apply_plan_limits
from api.db.database import get_sync_database_url


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_env()
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    updated = 0
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT
                    t.id AS tenant_id,
                    tb.tenant_id AS budget_tenant_id,
                    tb.plan_tier AS budget_plan_tier
                FROM tenants t
                LEFT JOIN tenant_budgets tb ON tb.tenant_id = t.id
                WHERE t.clerk_org_id IS NOT NULL
                  AND (tb.tenant_id IS NULL OR tb.plan_tier IS NULL)
                """
            )
        ).mappings()

        for row in rows:
            tenant_id = str(row["tenant_id"])
            if row["budget_tenant_id"] is None:
                session.execute(
                    text(
                        """
                        INSERT INTO tenant_budgets (tenant_id, plan_tier)
                        VALUES (:tenant_id, 'free')
                        ON CONFLICT (tenant_id) DO NOTHING
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            elif row["budget_plan_tier"] is None:
                session.execute(
                    text(
                        """
                        UPDATE tenant_budgets
                        SET plan_tier = 'free'
                        WHERE tenant_id = :tenant_id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )

            apply_plan_limits(tenant_id, "free", session)
            updated += 1

    print(f"Backfilled {updated} tenants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
