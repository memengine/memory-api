"""Synthetic HTTP regressions for validation error serialization; no services."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from api.schemas.requests import MemoryRetrieveRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "error_type", "message"),
    [
        ({"applicability": {"kind": "context"}, "property_keys": ["demo.style"]},
         "value_error", "context requires namespace and key; other kinds forbid them"),
        ({"applicability": {"kind": "global", "key": "stray"}, "property_keys": ["demo.style"]},
         "value_error", "only context applicability accepts namespace/key"),
        ({"applicability": {"kind": "unresolved"}, "property_keys": ["demo.style"]},
         "value_error", "reads require known applicability"),
        ({"applicability": {"kind": "global"}, "property_keys": ["demo.style", "demo.style"]},
         "value_error", "property_keys must be unique"),
        ({"property_keys": ["demo.style"]}, "missing", "Field required"),
        ({"applicability": {"kind": "global"}, "property_keys": 17},
         "tuple_type", "Input should be a valid tuple"),
    ],
)
async def test_invalid_typed_read_returns_serializable_422(context, error_type, message):
    # Importing main constructs its app; suppress telemetry initialization even
    # when a developer's local .env contains a Sentry DSN. No lifespan is run.
    with patch("sentry_sdk.init"):
        from api.main import _register_exception_handlers

    app = FastAPI()
    _register_exception_handlers(app)
    reached_route = False

    @app.post("/synthetic-read")
    async def read(body: MemoryRetrieveRequest):
        nonlocal reached_route
        reached_route = True
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/synthetic-read",
            json={"external_user_id": "synthetic-validation-only", "query": "read", "preference_context": context},
            headers={"x-request-id": "synthetic-validation-request"},
        )

    assert response.status_code == 422
    data = response.json()
    assert data.keys() == {"error", "code", "request_id", "details"}
    assert data["error"] == "validation_error"
    assert data["code"] == "REQ_422"
    assert data["request_id"] == "synthetic-validation-request"
    assert not reached_route
    detail = data["details"][0]
    assert detail["type"] == error_type
    assert detail["loc"][0] == "body"
    assert "input" in detail
    assert message in detail["msg"]
    if error_type == "value_error":
        assert detail["ctx"]["error"] == message
