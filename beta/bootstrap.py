"""Explicit one-time synthetic tenant creation in the dedicated beta database."""
import argparse
import json
import secrets
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from api.config.plan_limits import apply_plan_limits
from api.db.database import get_sync_database_url
from api.db.models import ApiKey, PlanTier, Tenant, TenantBudget
from api.services.webhook_event_service import generate_webhook_secret
from api.utils.crypto import api_key_prefix, hash_api_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true", required=True)
    parser.add_argument("--output", default="/tmp/beta-credentials.local.json")
    args = parser.parse_args()
    destination = Path(args.output)
    if destination.exists():
        parser.error("Credential output already exists; refusing to overwrite or create another tenant.")
    engine = create_engine(get_sync_database_url())
    if engine.url.host != "postgres" or engine.url.database != "memoryos_beta":
        parser.error("Refusing any database except the dedicated beta Compose database.")
    raw_key = "mem_" + secrets.token_urlsafe(24)
    with Session(engine) as session, session.begin():
        if session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")):
            raise RuntimeError("The beta runtime credential must not be a superuser.")
        tenant = Tenant(id=uuid.uuid4(), company_name="Synthetic local developer beta", plan_tier=PlanTier.starter)
        session.add(tenant)
        session.flush()
        session.add(TenantBudget(id=uuid.uuid4(), tenant_id=tenant.id, plan_tier=PlanTier.starter,
            webhook_secret=generate_webhook_secret()))
        session.flush()
        apply_plan_limits(str(tenant.id), "starter", session)
        session.add(ApiKey(id=uuid.uuid4(), tenant_id=tenant.id, name="Local beta operator",
            key_hash=hash_api_key(raw_key), key_prefix=api_key_prefix(raw_key), permissions=["admin"]))
        tenant_id = str(tenant.id)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump({"tenant_id": tenant_id, "admin_api_key": raw_key}, handle, indent=2)
    engine.dispose()
    print(json.dumps({"tenant_id": tenant_id, "credentials_file": str(destination)}))


if __name__ == "__main__":
    main()
