from __future__ import annotations

import asyncio
import os
import time
import uuid
from unittest.mock import MagicMock

import pytest
import redis.asyncio as redis

from api.db.cache import CacheService
from api.tasks.queue_router import STARTER_QUEUE, QueueRouter


@pytest.mark.asyncio
async def test_real_redis_enforces_queue_limit_and_job_ownership_under_contention() -> None:
    redis_url = os.getenv("PHASE26B_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("PHASE26B_TEST_REDIS_URL is required for Redis integration coverage")

    client = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    tenant_id = str(uuid.uuid4())
    plan_key = f"tenant:{tenant_id}:plan"
    depth_key = f"tenant_queue_depth:{tenant_id}:{STARTER_QUEUE}"
    breakdown_key = f"queue_depth:{STARTER_QUEUE}:tenant_breakdown"
    jobs_key = f"queue_depth:{STARTER_QUEUE}:jobs"
    metrics_key = f"queue_admission:{STARTER_QUEUE}:{time.strftime('%Y%m%d', time.gmtime())}"
    router = QueueRouter(
        session=MagicMock(),
        cache_service=CacheService(client=client),
    )
    job_ids = [f"load-{index}-{uuid.uuid4()}" for index in range(100)]
    try:
        await client.delete(metrics_key)
        await client.set(plan_key, "starter", ex=300)
        reservations = await asyncio.gather(*[
            router.reserve_extraction_slot(tenant_id=tenant_id, job_id=job_id)
            for job_id in job_ids
        ])
        accepted_ids = [
            job_id for job_id, reservation in zip(job_ids, reservations, strict=True)
            if reservation is not None
        ]

        assert len(accepted_ids) == 50
        assert int(await client.get(depth_key) or 0) == 50
        assert int(await client.hget(breakdown_key, tenant_id) or 0) == 50
        assert int(await client.hget(metrics_key, "attempted") or 0) == 100
        assert int(await client.hget(metrics_key, "full") or 0) == 50

        duplicate = await router.reserve_extraction_slot(
            tenant_id=tenant_id,
            job_id=accepted_ids[0],
        )
        assert duplicate is not None
        assert int(await client.get(depth_key) or 0) == 50

        await asyncio.gather(*[
            router.release_extraction_slot(
                tenant_id=tenant_id,
                queue_name=STARTER_QUEUE,
                job_id=job_id,
            )
            for job_id in accepted_ids
        ])
        await router.release_extraction_slot(
            tenant_id=tenant_id,
            queue_name=STARTER_QUEUE,
            job_id=accepted_ids[0],
        )

        assert await client.get(depth_key) is None
        assert await client.hget(breakdown_key, tenant_id) is None
    finally:
        pipe = client.pipeline()
        pipe.delete(plan_key, depth_key, metrics_key)
        pipe.hdel(breakdown_key, tenant_id)
        for job_id in job_ids:
            pipe.zrem(jobs_key, f"{tenant_id}:{job_id}")
        await pipe.execute()
        await client.aclose()
