from __future__ import annotations

import json
import uuid
from datetime import UTC
from datetime import datetime

import pytest

from api.db.models import AgentApiKey
from api.db.models import GlobalAgent
from api.services.global_agent_service import GlobalAgentService


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class FakeExecuteResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True


class FakeSession:
    def __init__(self) -> None:
        self.agents: dict[uuid.UUID, GlobalAgent] = {}
        self.agent_api_keys: dict[uuid.UUID, AgentApiKey] = {}
        self.commit_calls = 0

    def add(self, instance) -> None:
        if isinstance(instance, GlobalAgent):
            self.agents[instance.id] = instance
        elif isinstance(instance, AgentApiKey):
            if instance.global_agent_id is None and getattr(instance, "global_agent", None) is not None:
                instance.global_agent_id = instance.global_agent.id
            self.agent_api_keys[instance.id] = instance

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, _instance) -> None:
        return None

    async def get(self, model, key):
        if model is GlobalAgent:
            return self.agents.get(key)
        return None

    async def execute(self, query):
        query_text = str(query).lower()
        if "from agent_api_keys" in query_text:
            prefix = None
            for criterion in getattr(query, "_where_criteria", ()):
                left = str(getattr(criterion, "left", ""))
                right = getattr(getattr(criterion, "right", None), "value", None)
                if "agent_api_keys.key_prefix" in left:
                    prefix = right
            rows = [
                key
                for key in self.agent_api_keys.values()
                if key.is_active and (prefix is None or key.key_prefix == prefix)
            ]
            rows.sort(key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.id), reverse=True)
            return FakeExecuteResult(rows)
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
async def test_register_creates_agent_and_returns_raw_key_once() -> None:
    session = FakeSession()
    cache_service = type("CacheService", (), {"client": FakeRedisClient(), "breaker": None})()
    service = GlobalAgentService(session=session, cache_service=cache_service)

    agent, raw_key = await service.register(
        tenant_id=str(uuid.uuid4()),
        name="Cross Agent",
        description="Shared memory agent",
        logo_url="https://example.com/logo.png",
        website_url="https://example.com",
        default_categories_requested=["fact", "goal"],
        redirect_uri="https://example.com/return",
    )

    assert agent.name == "Cross Agent"
    assert agent.redirect_uri == "https://example.com/return"
    assert raw_key.startswith("agent_sk_")
    assert len(session.agents) == 1
    assert len(session.agent_api_keys) == 1
    stored_key = next(iter(session.agent_api_keys.values()))
    assert stored_key.key_hash != raw_key
    assert stored_key.key_prefix == raw_key[:12]


@pytest.mark.asyncio
async def test_resolve_from_api_key_returns_agent_for_valid_key() -> None:
    session = FakeSession()
    cache_client = FakeRedisClient()
    cache_service = type("CacheService", (), {"client": cache_client, "breaker": None})()
    service = GlobalAgentService(session=session, cache_service=cache_service)
    agent, raw_key = await service.register(
        tenant_id=str(uuid.uuid4()),
        name="Resolve Agent",
        description=None,
        logo_url=None,
        website_url=None,
        default_categories_requested=["fact"],
        redirect_uri="https://example.com/return",
    )

    resolved = await service.resolve_from_api_key(raw_key)
    cache_key = f"agent_key:{raw_key[:12]}:agent_id"
    missing = await service.resolve_from_api_key("agent_sk_invalid")

    assert resolved is not None
    assert resolved.id == agent.id
    assert json.loads(cache_client.values[cache_key])["agent_id"] == str(agent.id)
    assert missing is None


@pytest.mark.asyncio
async def test_get_public_profile_hides_owner_tenant_id() -> None:
    session = FakeSession()
    service = GlobalAgentService(session=session)
    agent = GlobalAgent(
        id=uuid.uuid4(),
        owner_tenant_id=uuid.uuid4(),
        name="Public Agent",
        description="Visible profile",
        logo_url="https://example.com/logo.png",
        website_url="https://example.com",
        default_categories_requested=["preference", "fact"],
        redirect_uri="https://example.com/return",
        is_verified=True,
        is_public=True,
        created_at=datetime.now(UTC),
        is_active=True,
    )
    session.agents[agent.id] = agent

    profile = await service.get_public_profile(str(agent.id))

    assert profile is not None
    assert profile.name == "Public Agent"
    assert profile.default_categories_requested == ["preference", "fact"]
    assert not hasattr(profile, "owner_tenant_id")
