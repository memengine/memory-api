from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.cache import get_redis_url
from api.db.database import build_sync_session_factory
from api.db.models import PlanTier
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.services.webhook_event_service import WEBHOOK_EVENT_TASK_NAME


PLAN_CACHE_TTL_SECONDS = 300
QUEUE_DEPTH_CACHE_TTL_SECONDS = 15
FREE_QUEUE = "free-extraction"
STARTER_QUEUE = "starter-extraction"
GROWTH_QUEUE = "growth-extraction"
SCALE_QUEUE = "scale-extraction"
ENTERPRISE_QUEUE = "enterprise-extraction"
BACKGROUND_QUEUE = "celery"
REEMBEDDING_QUEUE = "reembedding"
DEAD_LETTER_QUEUE = "dead-letter"
ALL_QUEUES = (
    SCALE_QUEUE,
    ENTERPRISE_QUEUE,
    GROWTH_QUEUE,
    STARTER_QUEUE,
    FREE_QUEUE,
    BACKGROUND_QUEUE,
    REEMBEDDING_QUEUE,
    DEAD_LETTER_QUEUE,
)
PLAN_QUEUE_MAP = {
    PlanTier.enterprise.value: ENTERPRISE_QUEUE,
    PlanTier.scale.value: SCALE_QUEUE,
    PlanTier.growth.value: GROWTH_QUEUE,
    PlanTier.starter.value: STARTER_QUEUE,
    PlanTier.free.value: FREE_QUEUE,
}
PLAN_QUEUE_LIMITS = {
    PlanTier.enterprise.value: 1000,
    PlanTier.scale.value: 500,
    PlanTier.growth.value: 200,
    PlanTier.starter.value: 50,
    PlanTier.free.value: 10,
}
QUEUE_THROUGHPUT_PER_MINUTE = {
    SCALE_QUEUE: 8,
    ENTERPRISE_QUEUE: 8,
    GROWTH_QUEUE: 5,
    STARTER_QUEUE: 3,
    FREE_QUEUE: 1,
}


@dataclass(slots=True)
class QueueReservation:
    tenant_id: str
    queue_name: str
    plan_tier: str
    queue_limit: int


def _redis_sync_client() -> redis.Redis:
    return redis.Redis.from_url(
        get_redis_url(),
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
        retry_on_timeout=False,
    )


def _plan_cache_key(tenant_id: str) -> str:
    return f"tenant:{tenant_id}:plan"


def _tenant_queue_depth_key(tenant_id: str, queue_name: str) -> str:
    return f"tenant_queue_depth:{tenant_id}:{queue_name}"


def _queue_jobs_key(queue_name: str) -> str:
    return f"queue_depth:{queue_name}:jobs"


def _cached_queue_depth_key(queue_name: str) -> str:
    return f"queue_depth:{queue_name}"


def _queue_delay_state_key(tenant_id: str, queue_name: str) -> str:
    return f"queue_was_delayed:{tenant_id}:{queue_name}"


def _queue_tenant_breakdown_key(queue_name: str) -> str:
    return f"queue_depth:{queue_name}:tenant_breakdown"


def _queue_job_member(*, tenant_id: str, job_id: str) -> str:
    return f"{tenant_id}:{job_id}"


class QueueRouter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
    ) -> None:
        self.session = session
        self.cache_service = cache_service

    async def get_extraction_queue(self, tenant_id: str) -> str:
        plan_tier = await self._get_plan_tier(tenant_id)
        return PLAN_QUEUE_MAP.get(plan_tier, STARTER_QUEUE)

    async def reserve_extraction_slot(self, *, tenant_id: str, job_id: str) -> QueueReservation | None:
        plan_tier = await self._get_plan_tier(tenant_id)
        queue_name = PLAN_QUEUE_MAP.get(plan_tier, STARTER_QUEUE)
        queue_limit = PLAN_QUEUE_LIMITS.get(plan_tier, PLAN_QUEUE_LIMITS[PlanTier.starter.value])
        depth_key = _tenant_queue_depth_key(tenant_id, queue_name)
        jobs_key = _queue_jobs_key(queue_name)
        tenant_breakdown_key = _queue_tenant_breakdown_key(queue_name)
        member = _queue_job_member(tenant_id=tenant_id, job_id=job_id)
        now_score = float(time.time())

        pipe = self.cache_service.client.pipeline()
        while True:
            try:
                await pipe.watch(depth_key)
                current_depth_raw = await pipe.get(depth_key)
                current_depth = int(current_depth_raw or 0)
                if current_depth >= queue_limit:
                    await pipe.reset()
                    return None
                pipe.multi()
                pipe.incr(depth_key)
                pipe.expire(depth_key, PLAN_CACHE_TTL_SECONDS * 2)
                pipe.zadd(jobs_key, {member: now_score})
                pipe.expire(jobs_key, PLAN_CACHE_TTL_SECONDS * 2)
                pipe.hincrby(tenant_breakdown_key, tenant_id, 1)
                pipe.expire(tenant_breakdown_key, PLAN_CACHE_TTL_SECONDS * 2)
                await pipe.execute()
                return QueueReservation(
                    tenant_id=tenant_id,
                    queue_name=queue_name,
                    plan_tier=plan_tier,
                    queue_limit=queue_limit,
                )
            except Exception:
                try:
                    await pipe.reset()
                except Exception:
                    pass
                current_depth = await self._safe_int_get(depth_key)
                if current_depth >= queue_limit:
                    return None
                try:
                    await self.cache_service.client.incr(depth_key)
                    await self.cache_service.client.expire(depth_key, PLAN_CACHE_TTL_SECONDS * 2)
                    await self.cache_service.client.zadd(jobs_key, {member: now_score})
                    await self.cache_service.client.expire(jobs_key, PLAN_CACHE_TTL_SECONDS * 2)
                    await self.cache_service.client.hincrby(tenant_breakdown_key, tenant_id, 1)
                    await self.cache_service.client.expire(tenant_breakdown_key, PLAN_CACHE_TTL_SECONDS * 2)
                    return QueueReservation(
                        tenant_id=tenant_id,
                        queue_name=queue_name,
                        plan_tier=plan_tier,
                        queue_limit=queue_limit,
                    )
                except Exception:
                    return None

    async def release_extraction_slot(self, *, tenant_id: str, queue_name: str, job_id: str) -> None:
        member = _queue_job_member(tenant_id=tenant_id, job_id=job_id)
        try:
            await self.cache_service.client.decr(_tenant_queue_depth_key(tenant_id, queue_name))
            current_depth = await self._safe_int_get(_tenant_queue_depth_key(tenant_id, queue_name))
            if current_depth <= 0:
                await self.cache_service.client.delete(_tenant_queue_depth_key(tenant_id, queue_name))
            tenant_depth = await self.cache_service.client.hincrby(
                _queue_tenant_breakdown_key(queue_name),
                tenant_id,
                -1,
            )
            if int(tenant_depth) <= 0:
                await self.cache_service.client.hdel(_queue_tenant_breakdown_key(queue_name), tenant_id)
            await self.cache_service.client.zrem(_queue_jobs_key(queue_name), member)
        except Exception:
            return None

    async def inspect_all_queues(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        snapshot: dict[str, dict[str, Any]] = {}
        for queue_name in ALL_QUEUES:
            try:
                length = await self.cache_service.client.llen(queue_name)
            except Exception:
                length = 0
            try:
                oldest = await self.cache_service.client.zrange(_queue_jobs_key(queue_name), 0, 0, withscores=True)
            except Exception:
                oldest = []
            try:
                breakdown = await self.cache_service.client.hgetall(_queue_tenant_breakdown_key(queue_name))
            except Exception:
                breakdown = {}
            oldest_age_seconds = None
            if oldest:
                oldest_score = float(oldest[0][1])
                oldest_age_seconds = max(0, int(now - oldest_score))
            snapshot[queue_name] = {
                "length": int(length or 0),
                "oldest_job_age_seconds": oldest_age_seconds,
                "tenant_breakdown": {tenant_id: int(count) for tenant_id, count in breakdown.items()},
            }
        return snapshot

    async def _get_plan_tier(self, tenant_id: str) -> str:
        cache_key = _plan_cache_key(tenant_id)
        try:
            cached_plan = await self.cache_service.client.get(cache_key)
        except Exception:
            cached_plan = None

        if cached_plan:
            return str(cached_plan)

        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except (TypeError, ValueError):
            return PlanTier.starter.value
        tenant_budget = await self.session.execute(
            select(TenantBudget.plan_tier).where(TenantBudget.tenant_id == tenant_uuid)
        )
        plan_tier = tenant_budget.scalar_one_or_none()
        if plan_tier is None:
            tenant = await self.session.get(Tenant, tenant_uuid)
            plan_tier = PlanTier.starter if tenant is None else tenant.plan_tier

        plan_value = plan_tier.value if isinstance(plan_tier, PlanTier) else str(plan_tier)
        try:
            await self.cache_service.client.set(cache_key, plan_value, ex=PLAN_CACHE_TTL_SECONDS)
        except Exception:
            pass
        return plan_value

    async def _safe_int_get(self, key: str) -> int:
        try:
            raw_value = await self.cache_service.client.get(key)
        except Exception:
            return 0
        return int(raw_value or 0)


def release_extraction_slot_sync(*, tenant_id: str | None, queue_name: str | None, job_id: str | None) -> None:
    if not tenant_id or not queue_name or not job_id:
        return
    member = _queue_job_member(tenant_id=tenant_id, job_id=job_id)
    client = _redis_sync_client()
    depth_key = _tenant_queue_depth_key(tenant_id, queue_name)
    breakdown_key = _queue_tenant_breakdown_key(queue_name)
    jobs_key = _queue_jobs_key(queue_name)

    try:
        current_depth = client.decr(depth_key)
        if int(current_depth) <= 0:
            client.delete(depth_key)
        tenant_depth = client.hincrby(breakdown_key, tenant_id, -1)
        if int(tenant_depth) <= 0:
            client.hdel(breakdown_key, tenant_id)
        client.zrem(jobs_key, member)
    except Exception:
        return


def get_extraction_queue_sync(*, tenant_id: str, session_factory=None) -> str:
    cache_key = _plan_cache_key(tenant_id)
    client = _redis_sync_client()
    try:
        cached_plan = client.get(cache_key)
    except Exception:
        cached_plan = None
    if cached_plan:
        return PLAN_QUEUE_MAP.get(str(cached_plan), STARTER_QUEUE)

    session_factory = session_factory or build_sync_session_factory()
    session: Session = session_factory()
    try:
        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except (TypeError, ValueError):
            return STARTER_QUEUE
        plan_tier = session.execute(
            select(TenantBudget.plan_tier).where(TenantBudget.tenant_id == tenant_uuid)
        ).scalar_one_or_none()
        if plan_tier is None:
            tenant = session.get(Tenant, tenant_uuid)
            plan_tier = PlanTier.starter if tenant is None else tenant.plan_tier
        plan_value = plan_tier.value if isinstance(plan_tier, PlanTier) else str(plan_tier)
        try:
            client.set(cache_key, plan_value, ex=PLAN_CACHE_TTL_SECONDS)
        except Exception:
            pass
        return PLAN_QUEUE_MAP.get(plan_value, STARTER_QUEUE)
    finally:
        session.close()


def get_queue_depth(queue_name: str) -> int:
    client = None
    try:
        client = _redis_sync_client()
        cached = client.get(_cached_queue_depth_key(queue_name))
        if cached is not None:
            return int(cached)
    except Exception:
        client = None

    try:
        from api.celery_app import celery_app

        inspector = celery_app.control.inspect()
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        depth = _count_tasks_for_queue(active, queue_name) + _count_tasks_for_queue(reserved, queue_name)
        if client is not None:
            try:
                client.set(_cached_queue_depth_key(queue_name), str(depth), ex=QUEUE_DEPTH_CACHE_TTL_SECONDS)
            except Exception:
                pass
        return int(depth)
    except Exception:
        return 0


def get_processing_eta(tenant_id: str) -> int | None:
    queue_name = get_extraction_queue_sync(tenant_id=tenant_id)
    depth = get_queue_depth(queue_name)
    client = None
    try:
        client = _redis_sync_client()
    except Exception:
        client = None
    was_delayed = False
    state_key = _queue_delay_state_key(tenant_id, queue_name)
    if client is not None:
        try:
            was_delayed = client.get(state_key) is not None
        except Exception:
            was_delayed = False
    if depth <= 10:
        if was_delayed:
            _dispatch_processing_event(
                tenant_id=tenant_id,
                event="processing.recovered",
                data={"queue": queue_name},
            )
            if client is not None:
                try:
                    client.delete(state_key)
                except Exception:
                    pass
        return None
    throughput = QUEUE_THROUGHPUT_PER_MINUTE.get(queue_name)
    if not throughput:
        return None
    # This is an estimate for tenant UX, not a processing guarantee.
    eta = int(math.ceil(depth / throughput) * 60)
    if not was_delayed:
        _dispatch_processing_event(
            tenant_id=tenant_id,
            event="processing.delayed",
            data={"queue": queue_name, "eta_seconds": eta},
        )
    if client is not None:
        try:
            client.set(state_key, "1", ex=300)
        except Exception:
            pass
    return eta


def _count_tasks_for_queue(tasks_by_worker: dict[str, Any], queue_name: str) -> int:
    count = 0
    for tasks in tasks_by_worker.values():
        for task in tasks or []:
            if _task_queue_name(task) == queue_name:
                count += 1
    return count


def _task_queue_name(task: dict[str, Any]) -> str | None:
    delivery_info = task.get("delivery_info") or {}
    return (
        delivery_info.get("routing_key")
        or delivery_info.get("queue")
        or task.get("queue")
    )


def serialize_queue_snapshot(snapshot: dict[str, dict[str, Any]]) -> str:
    return json.dumps(snapshot, default=str)


def _dispatch_processing_event(*, tenant_id: str, event: str, data: dict[str, Any]) -> None:
    try:
        from api.celery_app import celery_app

        celery_app.send_task(
            WEBHOOK_EVENT_TASK_NAME,
            args=[tenant_id, event, data],
        )
    except Exception:
        return None


__all__ = [
    "ALL_QUEUES",
    "BACKGROUND_QUEUE",
    "DEAD_LETTER_QUEUE",
    "ENTERPRISE_QUEUE",
    "FREE_QUEUE",
    "GROWTH_QUEUE",
    "PLAN_QUEUE_LIMITS",
    "PLAN_QUEUE_MAP",
    "QueueReservation",
    "QueueRouter",
    "REEMBEDDING_QUEUE",
    "STARTER_QUEUE",
    "get_extraction_queue_sync",
    "get_processing_eta",
    "get_queue_depth",
    "release_extraction_slot_sync",
    "serialize_queue_snapshot",
]
