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


def test_plans_returns_public_plan_ladder() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    assert response.status_code == 200
    plans = response.json()
    assert [plan["name"] for plan in plans] == ["free", "starter", "growth", "scale", "enterprise"]


def test_popular_plan_is_starter() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    plans = response.json()
    popular_plans = [plan for plan in plans if plan["is_popular"]]
    assert len(popular_plans) == 1
    assert popular_plans[0]["name"] == "starter"
    assert popular_plans[0]["badge"] == "Most Popular"


def test_all_plans_show_core_product_features() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    for plan in response.json():
        assert plan["features"]["quality_gate"] is True
        assert plan["features"]["domain_schemas"] is True
        assert plan["features"]["cross_agent"] is True
        assert plan["features"]["conflict_resolution"] is True
        assert plan["features"]["multi_service_writers"] is True
        assert "sla" not in plan["features"]


def test_free_plan_price_is_zero() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    free = next(plan for plan in response.json() if plan["name"] == "free")
    assert free["monthly_price_inr"] == 0
    assert free["annual_price_inr"] == 0
    assert free["monthly_price_usd"] == 0
    assert free["annual_price_usd"] == 0


def test_self_serve_plan_prices_match_public_pricing() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/billing/plans")

    plans = {plan["name"]: plan for plan in response.json()}
    assert plans["starter"]["monthly_price_inr"] == 1_800
    assert plans["starter"]["annual_price_inr"] == 18_000
    assert plans["growth"]["monthly_price_inr"] == 6_000
    assert plans["growth"]["annual_price_inr"] == 60_000
    assert plans["scale"]["monthly_price_inr"] == 18_000
    assert plans["scale"]["annual_price_inr"] == 180_000
