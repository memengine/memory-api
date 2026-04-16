from __future__ import annotations

from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

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
                SELECT tenant_id
                FROM tenant_budgets
                WHERE monthly_call_limit IS NULL
                  AND (plan_tier = 'starter' OR plan_tier IS NULL)
                """
            )
        ).all()
        for row in rows:
            tenant_id = str(row.tenant_id if hasattr(row, "tenant_id") else row[0])
            apply_plan_limits(tenant_id, "starter", session)
            updated += 1

    print(f"Updated {updated} tenants with starter limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
