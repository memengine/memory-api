from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.models import OrganisationDirectory
from api.db.models import Tenant
from api.db.models import UUIProxyLink
from api.db.models import VerifiedOrgConnection
from api.services.passport_link_service import PassportLinkService


class FakeScalars:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, *, agent=None, tenant=None, execute_values=None) -> None:
        self.agent = agent
        self.tenant = tenant
        self.execute_values = list(execute_values or [])
        self.added: list[object] = []
        self.flush = AsyncMock()

    async def get(self, model, _identifier):
        if model is Tenant:
            return self.tenant
        return self.agent

    async def execute(self, _statement):
        value = self.execute_values.pop(0) if self.execute_values else None
        return FakeScalars(value)

    def add(self, item) -> None:
        self.added.append(item)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True

    async def getdel(self, key):
        return self.values.pop(key, None)


@pytest.mark.asyncio
async def test_issue_and_consume_links_exact_proxy_user() -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    universal_user_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        owner_tenant_id=tenant_id,
        is_active=True,
    )
    proxy_user = SimpleNamespace(id=proxy_user_id)
    tenant = SimpleNamespace(id=tenant_id, company_name="Example Organisation")
    session = FakeSession(
        agent=agent,
        tenant=tenant,
        execute_values=[proxy_user, None, None, None],
    )
    redis = FakeRedis()
    service = PassportLinkService(
        session=session,  # type: ignore[arg-type]
        cache_service=SimpleNamespace(client=redis),  # type: ignore[arg-type]
    )

    issued = await service.issue(
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        external_user_id="customer_001",
    )
    link = await service.consume(
        token=issued.token,
        agent_id=str(agent_id),
        user_uui_id=str(universal_user_id),
    )

    assert issued.token.startswith("plink_")
    assert issued.expires_in_seconds == 900
    assert isinstance(link, UUIProxyLink)
    assert link.tenant_id == tenant_id
    assert link.proxy_user_id == proxy_user_id
    assert link.user_uui_id == universal_user_id
    assert any(isinstance(item, OrganisationDirectory) for item in session.added)
    assert any(item is link for item in session.added)
    assert any(isinstance(item, VerifiedOrgConnection) for item in session.added)


@pytest.mark.asyncio
async def test_link_token_is_one_time_use() -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    universal_user_id = uuid.uuid4()
    existing_link = UUIProxyLink(
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        user_uui_id=universal_user_id,
    )
    organisation = OrganisationDirectory(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        display_name="Example Organisation",
        category="other",
    )
    existing_connection = VerifiedOrgConnection(
        user_uui_id=universal_user_id,
        tenant_id=tenant_id,
        org_directory_id=organisation.id,
        connection_method="link_token",
    )
    session = FakeSession(
        execute_values=[organisation, existing_link, existing_connection],
    )
    redis = FakeRedis()
    token = "plink_one_time"
    redis.values[PassportLinkService._key(token)] = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "proxy_user_id": str(proxy_user_id),
        }
    )
    service = PassportLinkService(
        session=session,  # type: ignore[arg-type]
        cache_service=SimpleNamespace(client=redis),  # type: ignore[arg-type]
    )

    assert (
        await service.consume(
            token=token,
            agent_id=str(agent_id),
            user_uui_id=str(universal_user_id),
        )
        is existing_link
    )

    with pytest.raises(Exception) as exc_info:
        await service.consume(
            token=token,
            agent_id=str(agent_id),
            user_uui_id=str(universal_user_id),
        )
    assert "passport_link_expired_or_used" in str(exc_info.value)
