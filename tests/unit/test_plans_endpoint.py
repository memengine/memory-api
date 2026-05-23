from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.billing import router


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_plans_endpoint_requires_no_auth() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    assert response.status_code == 200


def test_plans_returns_4_plans() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    assert response.status_code == 200
    plans = response.json()
    assert [plan["name"] for plan in plans] == ["free", "starter", "growth", "enterprise"]


def test_popular_plan_is_starter() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    plans = response.json()
    popular_plans = [plan for plan in plans if plan["is_popular"]]
    assert len(popular_plans) == 1
    assert popular_plans[0]["name"] == "starter"
    assert popular_plans[0]["badge"] == "Most Popular"


def test_growth_has_domain_schemas_and_cross_agent() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    growth = next(plan for plan in response.json() if plan["name"] == "growth")
    assert growth["features"]["domain_schemas"] is True
    assert growth["features"]["cross_agent"] is True


def test_free_plan_price_is_zero() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    free = next(plan for plan in response.json() if plan["name"] == "free")
    assert free["monthly_price_inr"] == 0
    assert free["annual_price_inr"] == 0
    assert free["monthly_price_usd"] == 0
    assert free["annual_price_usd"] == 0
