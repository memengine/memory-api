from __future__ import annotations

import pytest

from api.db.cache import CacheService
from api.db.database import build_async_session_factory
from api.routers.internal import PROVENANCE_HEALTH_CACHE_KEY
from api.routers.internal import _get_provenance_health


@pytest.mark.asyncio
async def test_provenance_health_executes_against_postgres_and_redis() -> None:
    sessions = build_async_session_factory()
    cache = CacheService()
    await cache.client.delete(PROVENANCE_HEALTH_CACHE_KEY)

    async with sessions() as session:
        result = await _get_provenance_health(session, cache)

    assert result.memories_total >= result.memories_with_provenance >= 0
    assert 0 <= result.coverage_pct <= 100
    assert 0 <= result.tenant_coverage_pct <= 100
    assert 0 <= result.passport_coverage_pct <= 100
    assert result.tenant_claims_disputed >= 0
    assert result.passport_claims_disputed >= 0
    assert result.status in {"HEALTHY", "ATTENTION", "CRITICAL"}
    assert await cache.client.get(PROVENANCE_HEALTH_CACHE_KEY)