from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import redis

from api.services.llm_service import LLMProvider, LLMService
from api.settings import get_settings


@pytest.mark.asyncio
async def test_real_redis_concurrent_admission_and_expired_owner_release(monkeypatch):
    url = os.getenv("PHASE26B_TEST_REDIS_URL")
    if not url:
        pytest.skip("Explicit test Redis URL required")
    client = redis.Redis.from_url(url)
    key = f"phase26b-test:{uuid.uuid4().hex}"

    class IsolatedStore:
        def eval(self, script, count, _key, *args):
            return client.eval(script, count, key, *args)

    monkeypatch.setenv("LLM_PROVIDER_CONCURRENCY_LIMITS", "openai=3")
    get_settings.cache_clear()
    service = LLMService(
        provider_clients={LLMProvider.OPENAI: object()},
        state_client=IsolatedStore(),
        require_provider=False,
        use_state_store=False,
    )
    service._state_client = IsolatedStore()
    try:
        slots = await asyncio.gather(*[
            service._acquire_provider_slot(LLMProvider.OPENAI) for _ in range(40)
        ])
        admitted = [slot for slot in slots if slot]
        assert len(admitted) == 3
        assert client.zcard(key) == 3
        expired = admitted[0]
        client.zadd(key, {expired: 0})
        replacement = await service._acquire_provider_slot(LLMProvider.OPENAI)
        assert replacement and replacement not in admitted
        await service._release_provider_slot(LLMProvider.OPENAI, expired)
        assert client.zcard(key) == 3
        assert await service._acquire_provider_slot(LLMProvider.OPENAI) is None
        await asyncio.gather(*[
            service._release_provider_slot(LLMProvider.OPENAI, slot)
            for slot in [*admitted, replacement]
        ])
        assert client.zcard(key) == 0
        # Rolling configuration changes must not expire a longer owner's lease.
        seconds, micros = client.time()
        now_ms = seconds * 1000 + micros // 1000
        client.zadd(key, {"long-owner": now_ms + 600_000})
        short_owner = await service._acquire_provider_slot(LLMProvider.OPENAI)
        assert short_owner
        assert client.pttl(key) > 590_000
        await service._release_provider_slot(LLMProvider.OPENAI, short_owner)
        assert client.zcard(key) == 1
    finally:
        client.delete(key)
        client.close()
        get_settings.cache_clear()
