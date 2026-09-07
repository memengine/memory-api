from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from api.errors import APIError
from api.routers.mcp import issue_universal_capability


def test_capability_exchange_requires_verified_email_and_live_grant(monkeypatch) -> None:
    agent_id, user_id, grant_id = (uuid.uuid4() for _ in range(3))

    class FakeAgentService:
        def __init__(self, **_kwargs) -> None:
            pass

        async def resolve_from_api_key(self, raw_key: str):
            return SimpleNamespace(id=agent_id, is_active=True) if raw_key == "server-key" else None

    class FakeUUIService:
        def __init__(self, **_kwargs) -> None:
            pass

        async def resolve_by_email(self, email: str):
            return SimpleNamespace(id=user_id, is_active=True) if email == "user@example.com" else None

        async def get_grants(self, requested_user_id: str):
            assert requested_user_id == str(user_id)
            return [
                SimpleNamespace(
                    id=grant_id,
                    agent_id=agent_id,
                    access_type="read_write",
                    categories_allowed=["preference"],
                )
            ]

    monkeypatch.setattr("api.routers.mcp.GlobalAgentService", FakeAgentService)
    monkeypatch.setattr("api.routers.mcp.UUIService", FakeUUIService)
    monkeypatch.setattr("api.routers.mcp.issue_universal_mcp_capability", lambda **_kwargs: "capability")
    request = SimpleNamespace(
        headers={"x-memoryos-mcp-client": "public-v1"},
        state=SimpleNamespace(auth_email="user@example.com", auth_email_verified=True),
    )

    response = asyncio.run(
        issue_universal_capability(
            request=request,
            session=SimpleNamespace(),
            cache_service=SimpleNamespace(),
            x_memoryos_mcp_universal_agent_key="server-key",
        )
    )

    assert response["data"]["capability"] == "capability"
    assert response["data"]["access_type"] == "read_write"

    request.state.auth_email_verified = False
    with pytest.raises(APIError) as raised:
        asyncio.run(
            issue_universal_capability(
                request=request,
                session=SimpleNamespace(),
                cache_service=SimpleNamespace(),
                x_memoryos_mcp_universal_agent_key="server-key",
            )
        )
    assert raised.value.status_code == 403
