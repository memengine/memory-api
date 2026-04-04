from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import DbSession
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_proxy_user_service
from api.dependencies import get_quota_manager
from api.db.models import ApiDeprecatedField
from api.db.models import CallQualityLog
from api.db.models import OveragePolicy
from api.db.models import ProxyUser
from api.db.models import TenantBudget
from api.db.models import TenantDeprecationUsage
from api.errors import APIError
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.schemas.responses import CursorPage
from api.schemas.responses import ProxyUserBlockData
from api.schemas.responses import ProxyUserBlockResponse
from api.schemas.responses import ProxyUserDeleteData
from api.schemas.responses import ProxyUserDeleteResponse
from api.schemas.responses import ProxyUserStatsData
from api.schemas.responses import ProxyUserStatsResponse
from api.schemas.tenant_schemas import TenantDeprecationUsageEntry
from api.schemas.tenant_schemas import TenantDeprecationUsageResponse
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
) -> tuple[list[ProxyUser], str | None, int]:
    stmt = select(ProxyUser).where(ProxyUser.tenant_id == uuid.UUID(tenant_id))
    count_stmt = select(ProxyUser.id).where(ProxyUser.tenant_id == uuid.UUID(tenant_id))

    if cursor:
        cursor_last_active_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                ProxyUser.last_active_at < cursor_last_active_at,
                (ProxyUser.last_active_at == cursor_last_active_at) & (ProxyUser.id < cursor_id),
            )
        )

    stmt = stmt.order_by(ProxyUser.last_active_at.desc(), ProxyUser.id.desc()).limit(limit + 1)
    items = list((await session.execute(stmt)).scalars().all())
    total = len((await session.execute(count_stmt)).scalars().all())

    next_cursor = None
    if len(items) > limit:
        last_item = items[limit - 1]
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
        "upgrade_url": "https://memoryos.io/upgrade",
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
            )
            for item in proxy_users
        ],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/users/{external_user_id}/stats", response_model=ProxyUserStatsResponse)
async def get_tenant_proxy_user_stats(
    request: Request,
    external_user_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> ProxyUserStatsResponse:
    """Fetch stats for a single tenant-scoped proxy user.

    Parameters: external user id path value. It is hashed inside the proxy-user service before DB lookup.
    Responses: memory count, last active time, and creation time.
    """
    stats = await proxy_user_service.get_stats(tenant_id=tenant_id, external_user_id=external_user_id)
    register_deprecated_field(
        request,
        field_path=DEPRECATED_PROXY_USER_STATS_FIELD_PATH,
        header_field_name="user_id",
        sunset_at=DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET,
        migration_guide_url=DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE,
        replacement_field="external_user_id",
    )
    return ProxyUserStatsResponse(
        data=ProxyUserStatsData(
            external_user_id=external_user_id,
            user_id=external_user_id,
            memory_count=stats.memory_count,
            last_active_at=stats.last_active_at,
            created_at=stats.created_at,
        ),
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
