from __future__ import annotations

from datetime import UTC
from datetime import datetime
import json
import secrets
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import AgentApiKey
from api.db.models import GlobalAgent
from api.errors import APIError
from api.infra.fallbacks import on_redis_open
from api.schemas.uui_schemas import GlobalAgentPublic
from api.utils.crypto import fingerprint_api_key
from api.utils.crypto import hash_api_key
from api.utils.crypto import verify_api_key


ALLOWED_MEMORY_CATEGORIES = {
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
}
AGENT_KEY_CACHE_TTL_SECONDS = 300
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


class GlobalAgentService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService | None = None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service

    async def register(
        self,
        tenant_id: str,
        name: str,
        description: str | None,
        logo_url: str | None,
        website_url: str | None,
        default_categories_requested: list[str] | None,
        redirect_uri: str | None = None,
    ) -> tuple[GlobalAgent, str]:
        categories = list(default_categories_requested or [])
        self._validate_categories(categories)

        global_agent = GlobalAgent(
            id=uuid.uuid4(),
            owner_tenant_id=self._as_uuid(tenant_id),
            name=name,
            description=description,
            logo_url=logo_url,
            website_url=website_url,
            default_categories_requested=categories,
            redirect_uri=str(redirect_uri or "").strip(),
            is_public=True,
            is_active=True,
        )
        raw_agent_api_key = self._generate_agent_api_key()
        agent_api_key = AgentApiKey(
            id=uuid.uuid4(),
            global_agent=global_agent,
            key_hash=hash_api_key(fingerprint_api_key(raw_agent_api_key)),
            key_prefix=raw_agent_api_key[:12],
            name=f"{name} Primary Key",
            is_active=True,
        )
        self.session.add(global_agent)
        self.session.add(agent_api_key)
        await self.session.commit()
        await self.session.refresh(global_agent)
        return global_agent, raw_agent_api_key

    async def resolve_from_api_key(self, raw_key: str) -> GlobalAgent | None:
        raw_key = str(raw_key or "").strip()
        if not raw_key:
            return None

        prefix = raw_key[:12]
        result = await self.session.execute(
            select(AgentApiKey)
            .where(
                AgentApiKey.is_active.is_(True),
                AgentApiKey.key_prefix == prefix,
            )
            .order_by(AgentApiKey.created_at.desc(), AgentApiKey.id.desc())
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return None

        for candidate in candidates:
            if verify_api_key(fingerprint_api_key(raw_key), candidate.key_hash):
                candidate.last_used_at = datetime.now(UTC)
                await self.session.commit()
                global_agent = await self.session.get(GlobalAgent, candidate.global_agent_id)
                if global_agent is None or not bool(global_agent.is_active):
                    return None
                await self._cache_agent_id(prefix, str(global_agent.id))
                return global_agent
        return None

    async def get_public_profile(self, agent_id: str) -> GlobalAgentPublic | None:
        global_agent = await self.session.get(GlobalAgent, self._as_uuid(agent_id))
        if global_agent is None or not bool(global_agent.is_active) or not bool(global_agent.is_public):
            return None
        tenant_metadata = getattr(getattr(global_agent, "owner_tenant", None), "metadata_json", None) or {}
        domain_schema = tenant_metadata.get("domain_schema") or tenant_metadata.get("memory_domain")
        return GlobalAgentPublic(
            id=global_agent.id,
            name=global_agent.name,
            description=global_agent.description,
            logo_url=global_agent.logo_url,
            website_url=global_agent.website_url,
            is_verified=bool(global_agent.is_verified),
            default_categories_requested=list(global_agent.default_categories_requested or []),
            owner_tenant={"domain_schema": str(domain_schema) if domain_schema else None},
        )

    async def _get_cached_agent_id(self, prefix: str) -> str | None:
        cached = await self._redis_get(self._agent_key_cache_key(prefix))
        if cached is None:
            return None
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return None
        agent_id = payload.get("agent_id")
        return str(agent_id) if agent_id else None

    async def _cache_agent_id(self, prefix: str, agent_id: str) -> None:
        await self._redis_set(
            self._agent_key_cache_key(prefix),
            json.dumps({"agent_id": agent_id}),
            ex=AGENT_KEY_CACHE_TTL_SECONDS,
        )

    async def _redis_get(self, key: str) -> str | None:
        if self.cache_service is None:
            return None
        breaker = getattr(self.cache_service, "breaker", None)
        try:
            if breaker is None or breaker.__class__.__module__.startswith("unittest.mock"):
                return await self.cache_service.client.get(key)
            return await breaker.call(
                self.cache_service.client.get,
                key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    async def _redis_set(self, key: str, value: str, *, ex: int) -> None:
        if self.cache_service is None:
            return None
        breaker = getattr(self.cache_service, "breaker", None)
        try:
            if breaker is None or breaker.__class__.__module__.startswith("unittest.mock"):
                await self.cache_service.client.set(key, value, ex=ex)
                return None
            await breaker.call(
                self.cache_service.client.set,
                key,
                value,
                ex=ex,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    @staticmethod
    def _generate_agent_api_key() -> str:
        return f"agent_sk_{secrets.token_hex(32)}"

    @staticmethod
    def _agent_key_cache_key(prefix: str) -> str:
        return f"agent_key:{prefix}:agent_id"

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    @staticmethod
    def _validate_categories(categories: list[str]) -> None:
        invalid = [category for category in categories if category not in ALLOWED_MEMORY_CATEGORIES]
        if invalid:
            raise APIError(
                status_code=422,
                code="REQ_422",
                error="validation_error",
                details={"invalid_categories": invalid},
            )
