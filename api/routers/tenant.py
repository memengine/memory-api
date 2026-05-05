from __future__ import annotations

import base64
import calendar
import json
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Annotated

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from sqlalchemy import String
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.dependencies import DbSession
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_proxy_user_service
from api.dependencies import get_quota_manager
from api.db.models import ApiDeprecatedField
from api.db.models import CallQualityBlockedLayer
from api.db.models import CallQualityLog
from api.db.models import CrossUserConflict
from api.db.models import CrossUserConflictStatus
from api.db.models import Memory
from api.db.models import OveragePolicy
from api.db.models import ProxyUser
from api.db.models import TenantBudget
from api.db.models import TenantDeprecationUsage
from api.errors import APIError
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.schemas.conflict_schemas import CrossUserConflictData
from api.schemas.conflict_schemas import CrossUserConflictUpdateRequest
from api.schemas.conflict_schemas import CrossUserConflictsResponse
from api.schemas.responses import CursorPage
from api.schemas.responses import ProxyUserBlockData
from api.schemas.responses import ProxyUserBlockResponse
from api.schemas.responses import ProxyUserDeleteData
from api.schemas.responses import ProxyUserDeleteResponse
from api.schemas.tenant_schemas import BlockEvent
from api.schemas.tenant_schemas import CostSummary
from api.schemas.tenant_schemas import CostSummaryResponse
from api.schemas.tenant_schemas import ProxyUserDetail
from api.schemas.tenant_schemas import ProxyUserDetailResponse
from api.schemas.tenant_schemas import TenantDeprecationUsageEntry
from api.schemas.tenant_schemas import TenantDeprecationUsageResponse
from api.schemas.tenant_schemas import TenantMemoryAdditionPoint
from api.schemas.tenant_schemas import TenantMemoryAdditionsResponse
from api.schemas.tenant_schemas import TenantProxyUserData
from api.schemas.tenant_schemas import TenantQualityLogEntry
from api.schemas.tenant_schemas import TenantQualityLogResponse
from api.schemas.tenant_schemas import TenantSettingsData
from api.schemas.tenant_schemas import TenantSettingsPatchRequest
from api.schemas.tenant_schemas import TenantSettingsResponse
from api.schemas.tenant_schemas import TenantTestWebhookData
from api.schemas.tenant_schemas import TenantTestWebhookResponse
from api.schemas.tenant_schemas import TenantUsageData
from api.schemas.tenant_schemas import TenantUsageResponse
from api.schemas.tenant_schemas import TenantUsersListResponse
from api.services.proxy_user_service import ProxyUserService
from api.services.quota_manager import QuotaManager
from api.middleware.versioning import register_deprecated_field


router = APIRouter(prefix="/v1/tenant", tags=["tenant"])
DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET = datetime(2026, 10, 1, tzinfo=UTC)
DEPRECATED_PROXY_USER_STATS_FIELD_PATH = "GET /v1/tenant/users/{external_user_id}/stats response.data.user_id"
DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE = "https://docs.memoryos.io/migration/user-id-to-external-user-id"


def _encode_cursor(sort_at: datetime | None, row_id: uuid.UUID) -> str:
    payload = {
        "sort_at": sort_at.isoformat() if sort_at else None,
        "id": str(row_id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8"))
        sort_at_raw = payload.get("sort_at")
        row_id = uuid.UUID(payload["id"])
        return (datetime.fromisoformat(sort_at_raw) if sort_at_raw else None, row_id)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="invalid_cursor",
            details={"cursor": "malformed"},
        ) from exc


async def _load_tenant_budget(session: AsyncSession, tenant_id: str) -> TenantBudget | None:
    result = await session.execute(
        select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id))
    )
    return result.scalar_one_or_none()


async def _list_proxy_users(
    session: AsyncSession,
    *,
    tenant_id: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[tuple[ProxyUser, float | None]], str | None, int]:
    tenant_uuid = uuid.UUID(tenant_id)
    quality_cutoff = datetime.now(UTC) - timedelta(days=7)
    quality_hash = _quality_log_external_user_hash()
    quality_subquery = (
        select(
            quality_hash.label("external_user_id_hash"),
            func.avg(CallQualityLog.quality_score).label("quality_score_avg"),
        )
        .where(
            CallQualityLog.tenant_id == tenant_uuid,
            CallQualityLog.created_at > quality_cutoff,
        )
        .group_by(quality_hash)
        .subquery()
    )
    stmt = (
        select(ProxyUser, quality_subquery.c.quality_score_avg)
        .outerjoin(
            quality_subquery,
            quality_subquery.c.external_user_id_hash == ProxyUser.external_user_id_hash,
        )
        .where(ProxyUser.tenant_id == tenant_uuid)
    )
    count_stmt = select(func.count(ProxyUser.id)).where(ProxyUser.tenant_id == tenant_uuid)

    if cursor:
        cursor_last_active_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                ProxyUser.last_active_at < cursor_last_active_at,
                (ProxyUser.last_active_at == cursor_last_active_at) & (ProxyUser.id < cursor_id),
            )
        )

    stmt = stmt.order_by(ProxyUser.last_active_at.desc(), ProxyUser.id.desc()).limit(limit + 1)
    items = list((await session.execute(stmt)).all())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)

    next_cursor = None
    if len(items) > limit:
        last_item = items[limit - 1][0]
        next_cursor = _encode_cursor(last_item.last_active_at, last_item.id)
        items = items[:limit]
    return items, next_cursor, total


async def _list_quality_logs(
    session: AsyncSession,
    *,
    tenant_id: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[CallQualityLog], str | None, int]:
    stmt = select(CallQualityLog).where(CallQualityLog.tenant_id == uuid.UUID(tenant_id))
    count_stmt = select(CallQualityLog.id).where(CallQualityLog.tenant_id == uuid.UUID(tenant_id))

    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                CallQualityLog.created_at < cursor_created_at,
                (CallQualityLog.created_at == cursor_created_at) & (CallQualityLog.id < cursor_id),
            )
        )

    stmt = stmt.order_by(CallQualityLog.created_at.desc(), CallQualityLog.id.desc()).limit(limit + 1)
    items = list((await session.execute(stmt)).scalars().all())
    total = len((await session.execute(count_stmt)).scalars().all())

    next_cursor = None
    if len(items) > limit:
        last_item = items[limit - 1]
        next_cursor = _encode_cursor(last_item.created_at, last_item.id)
        items = items[:limit]
    return items, next_cursor, total


async def _update_tenant_budget_settings(
    session: AsyncSession,
    *,
    tenant_id: str,
    payload: TenantSettingsPatchRequest,
) -> TenantBudget:
    tenant_budget = await _load_tenant_budget(session, tenant_id)
    if tenant_budget is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_budget_not_found")

    if payload.alert_webhook_url is not None:
        tenant_budget.alert_webhook_url = payload.alert_webhook_url
    if payload.overage_policy is not None:
        tenant_budget.overage_policy = OveragePolicy(payload.overage_policy)

    await session.commit()
    await session.refresh(tenant_budget)
    return tenant_budget


async def _send_test_webhook(webhook_url: str, tenant_id: str) -> tuple[bool, int]:
    payload = {
        "event": "quota_mode_changed",
        "tenant_id": tenant_id,
        "from_mode": "FULL",
        "to_mode": "FULL",
        "reset_at": None,
        "upgrade_url": os.getenv("BILLING_UPGRADE_URL", "").strip(),
        "test": True,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(webhook_url, json=payload)
        return response.is_success, response.status_code
    except httpx.HTTPError:
        return False, 0


async def _list_deprecation_usage(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> list[dict[str, object]]:
    result = await session.execute(
        select(
            TenantDeprecationUsage.field_path,
            TenantDeprecationUsage.last_used_at,
            ApiDeprecatedField.sunset_at,
            ApiDeprecatedField.migration_guide_url,
            ApiDeprecatedField.replacement_field,
        )
        .join(
            ApiDeprecatedField,
            (ApiDeprecatedField.api_version == TenantDeprecationUsage.api_version)
            & (ApiDeprecatedField.field_path == TenantDeprecationUsage.field_path),
        )
        .where(TenantDeprecationUsage.tenant_id == uuid.UUID(tenant_id))
        .order_by(TenantDeprecationUsage.last_used_at.desc())
    )
    return [dict(row._mapping) for row in result]


def _quality_log_external_user_hash():
    return func.encode(
        func.digest(
            func.concat(cast(CallQualityLog.tenant_id, String), literal(":"), CallQualityLog.external_user_id),
            literal("sha256"),
        ),
        literal("hex"),
    )


async def _get_proxy_user_detail(
    session: AsyncSession,
    *,
    tenant_id: str,
    external_user_id: str,
) -> ProxyUserDetail:
    tenant_uuid = uuid.UUID(tenant_id)
    external_user_id_hash = ProxyUserService.hash_external_user_id(tenant_id, external_user_id)
    proxy_user = (
        await session.execute(
            select(ProxyUser).where(
                ProxyUser.tenant_id == tenant_uuid,
                ProxyUser.external_user_id_hash == external_user_id_hash,
            )
        )
    ).scalar_one_or_none()
    if proxy_user is None:
        raise APIError(
            status_code=404,
            code="PRX_404",
            error="proxy_user_not_found",
            details={
                "tenant_id": tenant_id,
                "external_user_id_hash": external_user_id_hash,
            },
        )

    quality_cutoff = datetime.now(UTC) - timedelta(days=7)
    quality_hash = _quality_log_external_user_hash()
    quality_metrics = (
        await session.execute(
            select(
                func.avg(CallQualityLog.quality_score).label("quality_score_avg"),
                func.count(CallQualityLog.id).label("total_calls_7d"),
                func.coalesce(
                    func.sum(
                        case(
                            (cast(CallQualityLog.layer_blocked_at, String) != "NONE", 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("blocked_calls_7d"),
            ).where(
                CallQualityLog.tenant_id == tenant_uuid,
                quality_hash == external_user_id_hash,
                CallQualityLog.created_at > quality_cutoff,
            )
        )
    ).one()
    quality_score_avg = (
        float(quality_metrics.quality_score_avg)
        if quality_metrics.quality_score_avg is not None
        else None
    )
    total_calls_7d = int(quality_metrics.total_calls_7d or 0)
    blocked_calls_7d = int(quality_metrics.blocked_calls_7d or 0)

    block_rows = (
        await session.execute(
            select(
                CallQualityLog.created_at,
                CallQualityLog.layer_blocked_at,
                CallQualityLog.reason,
            )
            .where(
                CallQualityLog.tenant_id == tenant_uuid,
                quality_hash == external_user_id_hash,
                cast(CallQualityLog.layer_blocked_at, String) != "NONE",
            )
            .order_by(CallQualityLog.created_at.desc())
            .limit(50)
        )
    ).all()

    return ProxyUserDetail(
        external_user_id=external_user_id,
        user_id=external_user_id,
        memory_count=int(proxy_user.memory_count or 0),
        last_active_at=proxy_user.last_active_at,
        created_at=proxy_user.created_at,
        quality_score_avg=quality_score_avg,
        block_history=[
            BlockEvent(
                blocked_at=row.created_at,
                layer=row.layer_blocked_at.value,
                reason=row.reason,
            )
            for row in block_rows
        ],
        total_calls_7d=total_calls_7d,
        blocked_calls_7d=blocked_calls_7d,
    )


async def _get_cost_summary(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> CostSummary:
    tenant_budget = await _load_tenant_budget(session, tenant_id)
    if tenant_budget is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_budget_not_found")

    now = datetime.now(UTC)
    tenant_uuid = uuid.UUID(tenant_id)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    monthly_counts = (
        await session.execute(
            select(
                func.count(CallQualityLog.id).label("total_calls"),
                func.coalesce(
                    func.sum(
                        case(
                            (cast(CallQualityLog.layer_blocked_at, String) != "NONE", 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("blocked_calls"),
            ).where(
                CallQualityLog.tenant_id == tenant_uuid,
                CallQualityLog.created_at >= month_start,
            )
        )
    ).one()

    current_month_tokens = int(tenant_budget.current_month_tokens or 0)
    current_month_calls = int(tenant_budget.current_month_calls or 0)
    raw_estimated_cost_usd = (current_month_tokens / 1_000_000) * 0.15
    estimated_cost_usd = round(raw_estimated_cost_usd, 4)
    cost_per_call = (
        round(estimated_cost_usd / current_month_calls, 6)
        if current_month_calls > 0
        else None
    )
    total_calls = int(monthly_counts.total_calls or 0)
    blocked_calls = int(monthly_counts.blocked_calls or 0)
    gate_block_rate = round((blocked_calls / total_calls), 4) if total_calls > 0 else 0.0
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected_month_cost_usd = round((estimated_cost_usd / now.day) * days_in_month, 4)
    savings_from_gate_usd = (
        round(blocked_calls * cost_per_call, 4) if cost_per_call is not None else 0.0
    )

    return CostSummary(
        current_month_tokens=current_month_tokens,
        estimated_cost_usd=estimated_cost_usd,
        cost_per_call=cost_per_call,
        gate_block_rate=gate_block_rate,
        projected_month_cost_usd=projected_month_cost_usd,
        savings_from_gate_usd=savings_from_gate_usd,
        cost_is_estimate=True,
    )


async def _get_tenant_memory_additions(
    session: AsyncSession,
    *,
    tenant_id: str,
    limit: int,
) -> list[TenantMemoryAdditionPoint]:
    tenant_uuid = uuid.UUID(tenant_id)
    day_bucket = func.date_trunc("day", Memory.created_at)
    rows = (
        await session.execute(
            select(
                day_bucket.label("day"),
                func.count(Memory.id).label("count"),
            )
            .join(ProxyUser, ProxyUser.id == Memory.proxy_user_id)
            .where(ProxyUser.tenant_id == tenant_uuid)
            .group_by(day_bucket)
            .order_by(day_bucket.desc())
            .limit(limit)
        )
    ).all()

    return [
        TenantMemoryAdditionPoint(day=row.day, count=int(row.count or 0))
        for row in reversed(rows)
        if row.day is not None
    ]


def _cross_user_conflict_to_data(conflict: CrossUserConflict) -> CrossUserConflictData:
    memory_a = conflict.user_a_memory
    memory_b = conflict.user_b_memory
    proxy_a = memory_a.proxy_user if memory_a is not None else None
    proxy_b = memory_b.proxy_user if memory_b is not None else None
    return CrossUserConflictData(
        id=str(conflict.id),
        tenant_id=str(conflict.tenant_id),
        entity_type=conflict.entity_type.value,
        entity_value_a=conflict.entity_value_a,
        entity_value_b=conflict.entity_value_b,
        user_a_memory_id=str(conflict.user_a_memory_id) if conflict.user_a_memory_id else None,
        user_b_memory_id=str(conflict.user_b_memory_id) if conflict.user_b_memory_id else None,
        user_a_id=(proxy_a.external_user_id if proxy_a is not None else None),
        user_b_id=(proxy_b.external_user_id if proxy_b is not None else None),
        memory_a_content=(memory_a.content if memory_a is not None else None),
        memory_b_content=(memory_b.content if memory_b is not None else None),
        memory_a_created_at=(memory_a.created_at if memory_a is not None else None),
        memory_b_created_at=(memory_b.created_at if memory_b is not None else None),
        detected_at=conflict.detected_at,
        status=conflict.status.value,
    )


@router.get("/usage", response_model=TenantUsageResponse)
async def get_tenant_usage(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    quota_manager: Annotated[QuotaManager, Depends(get_quota_manager)],
) -> TenantUsageResponse:
    """Fetch current tenant budget usage and operating mode.

    Parameters: tenant is derived from API-key auth context.
    Responses: call/token usage, quota mode, budget remaining percent, and reset time.
    """
    tenant_budget = await _load_tenant_budget(session, tenant_id)
    envelope = await quota_manager.get_quota_envelope(tenant_id)
    request.state.quota_envelope = envelope

    if tenant_budget is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_budget_not_found")

    tenant_uuid = uuid.UUID(tenant_id)
    month_start = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cross_user_pending = int(
        (
            await session.execute(
                select(func.count(CrossUserConflict.id)).where(
                    CrossUserConflict.tenant_id == tenant_uuid,
                    CrossUserConflict.status == CrossUserConflictStatus.pending,
                )
            )
        ).scalar_one()
        or 0
    )
    conflicts_resolved_mtd = int(
        (
            await session.execute(
                select(func.count(CrossUserConflict.id)).where(
                    CrossUserConflict.tenant_id == tenant_uuid,
                    CrossUserConflict.status.in_(
                        [
                            CrossUserConflictStatus.resolved,
                            CrossUserConflictStatus.ignored,
                        ]
                    ),
                    CrossUserConflict.detected_at >= month_start,
                )
            )
        ).scalar_one()
        or 0
    )

    return TenantUsageResponse(
        data=TenantUsageData(
            calls_used=int(tenant_budget.current_month_calls or 0),
            calls_limit=tenant_budget.monthly_call_limit,
            tokens_used=int(tenant_budget.current_month_tokens or 0),
            tokens_limit=tenant_budget.monthly_token_limit,
            mode=envelope.mode.value,
            budget_remaining_pct=envelope.budget_remaining_pct,
            reset_at=envelope.reset_at,
            plan_tier=tenant_budget.plan_tier.value,
            conflicts_resolved_mtd=conflicts_resolved_mtd,
            cross_user_conflicts_pending=cross_user_pending,
            conflict_types_breakdown={
                "FACT_UPDATE": 0,
                "PREFERENCE_CHANGE": 0,
                "NEGATION": 0,
                "SKILL_PROGRESSION": 0,
                "NUMERIC_UPDATE": 0,
                "TEMPORAL_SHIFT": 0,
            },
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/users", response_model=TenantUsersListResponse)
async def list_tenant_proxy_users(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> TenantUsersListResponse:
    """List tenant-scoped proxy users with cursor pagination.

    Parameters: optional cursor and limit, capped at 100.
    Responses: proxy users, memory counts, last active timestamps, and next cursor.
    """
    proxy_users, next_cursor, total = await _list_proxy_users(
        session,
        tenant_id=tenant_id,
        cursor=cursor,
        limit=limit,
    )
    return TenantUsersListResponse(
        data=[
            TenantProxyUserData(
                external_user_id=item.external_user_id,
                memory_count=int(item.memory_count or 0),
                last_active_at=item.last_active_at,
                created_at=item.created_at,
                is_blocked=bool(item.is_blocked),
                quality_score_avg=(float(quality_score_avg) if quality_score_avg is not None else None),
            )
            for item, quality_score_avg in proxy_users
        ],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/users/{external_user_id}/stats", response_model=ProxyUserDetailResponse)
async def get_tenant_proxy_user_stats(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> ProxyUserDetailResponse:
    """Fetch stats for a single tenant-scoped proxy user.

    Parameters: external user id path value. It is hashed inside the proxy-user service before DB lookup.
    Responses: memory count, recent quality metrics, block history, and creation time.
    """
    detail = await _get_proxy_user_detail(
        session,
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )
    register_deprecated_field(
        request,
        field_path=DEPRECATED_PROXY_USER_STATS_FIELD_PATH,
        header_field_name="user_id",
        sunset_at=DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET,
        migration_guide_url=DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE,
        replacement_field="external_user_id",
    )
    return ProxyUserDetailResponse(
        data=detail,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/users/{external_user_id}", response_model=ProxyUserDeleteResponse)
async def delete_tenant_proxy_user(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserDeleteResponse:
    """Delete a tenant proxy user and all memories for GDPR cleanup.

    Parameters: external user id path value. It is hashed inside the proxy-user service before DB lookup.
    Responses: deletion status and count of removed memories.
    """
    memories_removed = await proxy_user_service.delete_all_memories(
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )
    return ProxyUserDeleteResponse(
        data=ProxyUserDeleteData(deleted=True, memories_removed=memories_removed),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/users/{external_user_id}/block", response_model=ProxyUserBlockResponse)
async def block_tenant_proxy_user(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserBlockResponse:
    """Block a tenant proxy user from new memory writes.

    Parameters: external user id path value. It is hashed inside the proxy-user service before DB lookup.
    Responses: whether the proxy user is now blocked.
    """
    blocked = await proxy_user_service.block(tenant_id=tenant_id, external_user_id=external_user_id)
    return ProxyUserBlockResponse(
        data=ProxyUserBlockData(blocked=blocked),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/quality-log", response_model=TenantQualityLogResponse)
async def list_tenant_quality_log(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> TenantQualityLogResponse:
    """List tenant-scoped call quality log entries with cursor pagination.

    Parameters: optional cursor and limit, capped at 100.
    Responses: blocked-layer log entries and next cursor for tenant analytics.
    """
    entries, next_cursor, total = await _list_quality_logs(
        session,
        tenant_id=tenant_id,
        cursor=cursor,
        limit=limit,
    )
    return TenantQualityLogResponse(
        data=[
            TenantQualityLogEntry(
                id=str(item.id),
                external_user_id=item.external_user_id,
                layer_blocked_at=item.layer_blocked_at.value,
                quality_score=float(item.quality_score or 0.0),
                semantic_similarity=item.semantic_similarity,
                created_at=item.created_at,
            )
            for item in entries
        ],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/memory-additions", response_model=TenantMemoryAdditionsResponse)
async def get_tenant_memory_additions(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    limit: int = Query(default=30, ge=1, le=90),
) -> TenantMemoryAdditionsResponse:
    """List tenant memory additions grouped by day for dashboard trends."""
    return TenantMemoryAdditionsResponse(
        data=await _get_tenant_memory_additions(session, tenant_id=tenant_id, limit=limit),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/shared-context-conflicts", response_model=CrossUserConflictsResponse)
async def get_tenant_shared_context_conflicts(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    include_resolved: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> CrossUserConflictsResponse:
    status_filter = (
        [CrossUserConflictStatus.pending]
        if not include_resolved
        else [
            CrossUserConflictStatus.pending,
            CrossUserConflictStatus.resolved,
            CrossUserConflictStatus.ignored,
        ]
    )
    conflicts = (
        await session.execute(
            select(CrossUserConflict)
            .options(
                selectinload(CrossUserConflict.user_a_memory).selectinload(Memory.proxy_user),
                selectinload(CrossUserConflict.user_b_memory).selectinload(Memory.proxy_user),
            )
            .where(
                CrossUserConflict.tenant_id == uuid.UUID(tenant_id),
                CrossUserConflict.status.in_(status_filter),
            )
            .order_by(CrossUserConflict.detected_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return CrossUserConflictsResponse(
        data=[_cross_user_conflict_to_data(conflict) for conflict in conflicts],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/shared-context-conflicts/{conflict_id}", response_model=CrossUserConflictsResponse)
async def update_tenant_shared_context_conflict(
    request: Request,
    conflict_id: str,
    payload: CrossUserConflictUpdateRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> CrossUserConflictsResponse:
    conflict = (
        await session.execute(
            select(CrossUserConflict)
            .options(
                selectinload(CrossUserConflict.user_a_memory).selectinload(Memory.proxy_user),
                selectinload(CrossUserConflict.user_b_memory).selectinload(Memory.proxy_user),
            )
            .where(
                CrossUserConflict.id == uuid.UUID(conflict_id),
                CrossUserConflict.tenant_id == uuid.UUID(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise APIError(status_code=404, code="CONFLICT_404", error="conflict_not_found")

    status = payload.status.lower()
    correct_user = (payload.correct_user or "").upper()
    if status == "ignored":
        conflict.status = CrossUserConflictStatus.ignored
    elif status == "resolved" and correct_user in {"A", "B"}:
        conflict.status = CrossUserConflictStatus.resolved
        memory_to_archive = conflict.user_b_memory if correct_user == "A" else conflict.user_a_memory
        if memory_to_archive is not None:
            memory_to_archive.is_archived = True
            memory_to_archive.updated_at = utc_now()
    else:
        raise APIError(status_code=400, code="CONFLICT_400", error="invalid_conflict_resolution")

    await session.commit()
    return CrossUserConflictsResponse(
        data=[_cross_user_conflict_to_data(conflict)],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/settings", response_model=TenantSettingsResponse)
async def update_tenant_settings(
    request: Request,
    payload: TenantSettingsPatchRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantSettingsResponse:
    """Update tenant alert and overage settings.

    Parameters: optional alert webhook URL and overage policy fields in the body.
    Responses: the updated tenant budget settings used by quota alerts and governance.
    """
    tenant_budget = await _update_tenant_budget_settings(
        session,
        tenant_id=tenant_id,
        payload=payload,
    )
    return TenantSettingsResponse(
        data=TenantSettingsData(
            alert_webhook_url=tenant_budget.alert_webhook_url,
            overage_policy=tenant_budget.overage_policy.value,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/test-webhook", response_model=TenantTestWebhookResponse)
async def test_tenant_webhook(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantTestWebhookResponse:
    """Send a test alert payload to the tenant webhook URL.

    Parameters: tenant is derived from API-key auth context.
    Responses: whether the delivery succeeded and the HTTP status code from the webhook target.
    """
    tenant_budget = await _load_tenant_budget(session, tenant_id)
    if tenant_budget is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_budget_not_found")
    if not tenant_budget.alert_webhook_url:
        raise APIError(status_code=400, code="TEN_400", error="alert_webhook_not_configured")

    delivered, status_code = await _send_test_webhook(tenant_budget.alert_webhook_url, tenant_id)
    return TenantTestWebhookResponse(
        data=TenantTestWebhookData(delivered=delivered, status_code=status_code),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/deprecation-usage", response_model=TenantDeprecationUsageResponse)
async def get_tenant_deprecation_usage(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantDeprecationUsageResponse:
    """List deprecated fields or endpoints this tenant still uses.

    Parameters: tenant is derived from API-key auth context.
    Responses: deprecated field usage, last seen timestamp, sunset date, and migration guide.
    """
    rows = await _list_deprecation_usage(session, tenant_id=tenant_id)
    return TenantDeprecationUsageResponse(
        data=[
            TenantDeprecationUsageEntry(
                field=str(row["field_path"]),
                last_used=row["last_used_at"],
                sunset_at=row["sunset_at"],
                migration_guide=str(row["migration_guide_url"]),
                replacement_field=(
                    str(row["replacement_field"]) if row.get("replacement_field") else None
                ),
            )
            for row in rows
        ],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/cost-summary", response_model=CostSummaryResponse)
async def get_tenant_cost_summary(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> CostSummaryResponse:
    """Summarize the tenant's current-month usage cost and quality-gate savings."""
    return CostSummaryResponse(
        data=await _get_cost_summary(session, tenant_id=tenant_id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
