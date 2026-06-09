from __future__ import annotations

import json
import uuid

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.db.database import get_db_session
from api.dependencies import get_webhook_service
from api.errors import APIError
from api.routers.webhooks import router
from api.routers.common import get_request_id


SVIX_HEADERS = {
    "svix-id": "msg_123",
    "svix-timestamp": "1234567890",
    "svix-signature": "v1,test",
}


class FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSyncBudgetSession:
    def __init__(self, budgets: dict[str, dict[str, object]]) -> None:
        self.budgets = budgets
        self.commit_calls = 0

    def execute(self, _statement, params=None):
        tenant_id = str(params["tenant_id"])
        budget = self.budgets[tenant_id]
        budget["plan_tier"] = params["plan_tier"]
        budget["monthly_call_limit"] = params["monthly_call_limit"]
        budget["monthly_token_limit"] = params["monthly_token_limit"]
        budget["write_call_limit"] = params["write_call_limit"]
        budget["read_limit"] = params["read_limit"]
        budget["rate_limit_per_user_per_minute"] = params["rate_limit_per_user_per_minute"]
        budget["overage_policy"] = params["overage_policy"]
        budget["alert_threshold_pct"] = params["alert_threshold_pct"]
        budget["reset_at"] = "set"
        return None

    def commit(self) -> None:
        self.commit_calls += 1


class FakeAsyncSession:
    def __init__(self) -> None:
        self.tenants: dict[str, dict[str, object]] = {}
        self.budgets: dict[str, dict[str, object]] = {}
        self.commit_calls = 0
        self.plan_apply_calls = 0

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).lower().split())
        params = params or {}

        if "select id from tenants where clerk_org_id" in sql:
            org_id = str(params["org_id"])
            tenant = self.tenants.get(org_id)
            return FakeScalarResult(tenant["id"] if tenant else None)

        if "insert into tenants" in sql:
            org_id = str(params["clerk_org_id"])
            if org_id in self.tenants:
                return FakeScalarResult(None)
            tenant_id = str(uuid.uuid4())
            self.tenants[org_id] = {
                "id": tenant_id,
                "company_name": params["company_name"],
                "clerk_org_id": org_id,
                "is_active": True,
                "plan_tier": "free",
            }
            return FakeScalarResult(tenant_id)

        if "insert into tenant_budgets" in sql:
            tenant_id = str(params["tenant_id"])
            self.budgets.setdefault(
                tenant_id,
                {
                    "tenant_id": tenant_id,
                    "plan_tier": "free",
                    "monthly_call_limit": None,
                    "monthly_token_limit": None,
                    "write_call_limit": None,
                    "read_limit": None,
                    "rate_limit_per_user_per_minute": None,
                    "overage_policy": None,
                    "alert_threshold_pct": None,
                    "reset_at": None,
                },
            )
            return FakeScalarResult(None)

        if "update tenants set is_active = false" in sql:
            org_id = str(params["org_id"])
            if org_id in self.tenants:
                self.tenants[org_id]["is_active"] = False
            return FakeScalarResult(None)

        if "update tenants set company_name" in sql:
            org_id = str(params["org_id"])
            if org_id in self.tenants:
                self.tenants[org_id]["company_name"] = params["company_name"]
            return FakeScalarResult(None)

        raise AssertionError(f"Unexpected SQL in fake session: {statement}")

    async def commit(self) -> None:
        self.commit_calls += 1

    async def run_sync(self, fn):
        self.plan_apply_calls += 1
        return fn(FakeSyncBudgetSession(self.budgets))


class FakeWebhookService:
    def __init__(self, *, invalid_signature: bool = False) -> None:
        self.invalid_signature = invalid_signature
        self.process_calls = 0

    def _verify_svix_signature(self, *, payload: bytes, headers: dict[str, str]) -> None:
        if self.invalid_signature:
            raise APIError(status_code=401, code="WH_401", error="invalid_webhook_signature")

    async def verify_and_process(self, *, payload: bytes, headers: dict[str, str]) -> bool:
        self.process_calls += 1
        self._verify_svix_signature(payload=payload, headers=headers)
        return True


def build_test_app(
    *,
    session: FakeAsyncSession,
    invalid_signature: bool = False,
) -> FastAPI:
    app = FastAPI()
    webhook_service = FakeWebhookService(invalid_signature=invalid_signature)

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error,
                "code": exc.code,
                "request_id": get_request_id(request),
                "details": exc.details,
            },
        )

    async def override_db_session():
        yield session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_webhook_service] = lambda: webhook_service
    app.include_router(router)
    return app


def post_event(client: TestClient, *, event_type: str, data: dict[str, object]):
    return client.post(
        "/v1/webhooks/clerk",
        headers=SVIX_HEADERS,
        content=json.dumps({"type": event_type, "data": data}),
    )


def test_org_created_creates_tenant_and_budget() -> None:
    session = FakeAsyncSession()
    app = build_test_app(session=session)

    with TestClient(app) as client:
        response = post_event(
            client,
            event_type="organization.created",
            data={
                "id": "org_test_123",
                "name": "Acme AI",
                "slug": "acme-ai",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["received"] is True
    tenant = session.tenants["org_test_123"]
    budget = session.budgets[tenant["id"]]
    assert tenant["company_name"] == "Acme AI"
    assert tenant["is_active"] is True
    assert tenant["plan_tier"] == "free"
    assert budget["plan_tier"] == "free"
    assert budget["monthly_call_limit"] == 5_000
    assert budget["monthly_token_limit"] == 2_000_000
    assert budget["write_call_limit"] == 5_000
    assert budget["read_limit"] is None
    assert budget["rate_limit_per_user_per_minute"] == 3
    assert budget["reset_at"] == "set"


def test_org_created_idempotent() -> None:
    session = FakeAsyncSession()
    app = build_test_app(session=session)

    with TestClient(app) as client:
        first = post_event(
            client,
            event_type="organization.created",
            data={"id": "org_test_123", "name": "Acme AI", "slug": "acme-ai"},
        )
        second = post_event(
            client,
            event_type="organization.created",
            data={"id": "org_test_123", "name": "Acme AI", "slug": "acme-ai"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(session.tenants) == 1
    assert len(session.budgets) == 1


def test_org_deleted_deactivates_tenant() -> None:
    session = FakeAsyncSession()
    tenant_id = str(uuid.uuid4())
    session.tenants["org_test_123"] = {
        "id": tenant_id,
        "company_name": "Acme AI",
        "clerk_org_id": "org_test_123",
        "is_active": True,
        "plan_tier": "free",
    }
    app = build_test_app(session=session)

    with TestClient(app) as client:
        response = post_event(
            client,
            event_type="organization.deleted",
            data={"id": "org_test_123"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["received"] is True
    assert session.tenants["org_test_123"]["is_active"] is False


def test_org_updated_renames_tenant() -> None:
    session = FakeAsyncSession()
    tenant_id = str(uuid.uuid4())
    session.tenants["org_test_123"] = {
        "id": tenant_id,
        "company_name": "Old Name",
        "clerk_org_id": "org_test_123",
        "is_active": True,
        "plan_tier": "free",
    }
    app = build_test_app(session=session)

    with TestClient(app) as client:
        response = post_event(
            client,
            event_type="organization.updated",
            data={"id": "org_test_123", "name": "New Name", "slug": "new-name"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["received"] is True
    assert session.tenants["org_test_123"]["company_name"] == "New Name"


def test_org_created_invalid_signature_returns_400() -> None:
    session = FakeAsyncSession()
    app = build_test_app(session=session, invalid_signature=True)

    with TestClient(app) as client:
        response = post_event(
            client,
            event_type="organization.created",
            data={"id": "org_test_123", "name": "Acme AI", "slug": "acme-ai"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "WH_401"
    assert session.tenants == {}
    assert session.budgets == {}
