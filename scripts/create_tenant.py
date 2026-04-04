from __future__ import annotations

import argparse
import os
import secrets
import uuid
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import ApiKey
from api.db.models import PlanTier
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.utils.crypto import api_key_prefix
from api.utils.crypto import hash_api_key
from api.services.webhook_event_service import generate_webhook_secret


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


def generate_api_key() -> str:
    return f"mem_{secrets.token_urlsafe(24)}"


def create_tenant_with_api_key(*, session: Session, company_name: str, api_key_name: str) -> tuple[Tenant, str]:
    raw_api_key = generate_api_key()
    tenant = Tenant(
        id=uuid.uuid4(),
        company_name=company_name,
        plan_tier=PlanTier.starter,
        is_active=True,
        metadata_json={},
    )
    session.add(tenant)
    session.flush()

    tenant_budget = TenantBudget(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        plan_tier=PlanTier.starter,
        webhook_secret=generate_webhook_secret(),
    )
    session.add(tenant_budget)

    api_key = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=None,
        key_hash=hash_api_key(raw_api_key),
        key_prefix=api_key_prefix(raw_api_key),
        name=api_key_name,
        permissions=["write"],
        rate_limit_per_minute=60,
        is_active=True,
    )
    session.add(api_key)
    session.commit()
    session.refresh(tenant)
    return tenant, raw_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a MemoryOS tenant and its first API key.")
    parser.add_argument("company_name", help="Tenant company name.")
    parser.add_argument(
        "--api-key-name",
        default="Primary SDK Key",
        help="Display name for the generated API key.",
    )
    args = parser.parse_args()

    load_env()
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        tenant, raw_api_key = create_tenant_with_api_key(
            session=session,
            company_name=args.company_name,
            api_key_name=args.api_key_name,
        )

    print(f"Tenant created: {tenant.company_name}")
    print(f"Tenant ID: {tenant.id}")
    print("API key (shown once):")
    print(raw_api_key)
    print("Store this key securely. It is not persisted in plaintext.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
