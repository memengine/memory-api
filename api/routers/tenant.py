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
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from api.dependencies import DbSession
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_cache_service
from api.dependencies import get_proxy_user_service
from api.dependencies import get_quota_manager
from api.db.cache import CacheService
from api.db.models import ApiDeprecatedField
from api.db.models import ApiKey
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import CallQualityLog
from api.db.models import ClarificationQueue
from api.db.models import ClarificationQueueStatus
from api.db.models import CrossUserConflict
from api.db.models import CrossUserConflictStatus
from api.db.models import EdTechMemory
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import Memory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.db.models import MemorySourceEvent
from api.db.models import OrganisationDirectory
from api.db.models import OveragePolicy
from api.db.models import ProxyUser
from api.db.models import SharedContextSignal
from api.db.models import ServiceWriter
from api.db.models import SupportMemory
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.db.models import TenantDeprecationUsage
from api.errors import APIError
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.schemas.conflict_schemas import CrossUserConflictData
from api.schemas.conflict_schemas import CrossUserConflictUpdateRequest
from api.schemas.conflict_schemas import CrossUserConflictsResponse
from api.schemas.conflict_schemas import ConflictStatsData
from api.schemas.conflict_schemas import ConflictStatsResponse
from api.schemas.conflict_schemas import TenantConflictResolveData
from api.schemas.conflict_schemas import TenantConflictResolveRequest
from api.schemas.conflict_schemas import TenantConflictResolveResponse
from api.schemas.edtech_schemas import EnableEdTechSchemaData
from api.schemas.edtech_schemas import EnableEdTechSchemaResponse
from api.schemas.support_schemas import SupportCustomerSummary
from api.schemas.support_schemas import TenantSupportCustomersResponse
from api.schemas.support_schemas import TenantSupportStatsData
from api.schemas.support_schemas import TenantSupportStatsResponse
from api.schemas.support_schemas import TenantSupportTypeData
from api.schemas.support_schemas import TenantSupportTypePatchRequest
from api.schemas.support_schemas import TenantSupportTypeResponse
from api.schemas.responses import CursorPage
from api.schemas.provenance_schemas import ServiceWriterCreateRequest
from api.schemas.provenance_schemas import ServiceWriterData
from api.schemas.provenance_schemas import ServiceWriterListResponse
from api.schemas.provenance_schemas import ServiceWriterResponse
from api.schemas.provenance_schemas import ServiceWriterUpdateRequest
from api.schemas.provenance_schemas import MemorySourceEventData
from api.schemas.provenance_schemas import MemorySourceEventListResponse
from api.schemas.provenance_schemas import MemoryClaimData
from api.schemas.provenance_schemas import MemoryClaimListResponse
from api.schemas.provenance_schemas import MemoryClaimResponse
from api.schemas.provenance_schemas import MemoryClaimRevisionData
from api.schemas.responses import ProxyUserBlockData
from api.schemas.responses import ProxyUserBlockResponse
from api.schemas.responses import ProxyUserDeleteData
from api.schemas.responses import ProxyUserDeleteResponse
from api.schemas.tenant_schemas import BlockEvent
from api.schemas.tenant_schemas import CostSummary
from api.schemas.tenant_schemas import CostSummaryResponse
from api.schemas.tenant_schemas import PassportLinkTokenData
from api.schemas.tenant_schemas import PassportLinkTokenRequest
from api.schemas.tenant_schemas import PassportLinkTokenResponse
from api.schemas.tenant_schemas import OrganisationDirectoryRegisterData
from api.schemas.tenant_schemas import OrganisationDirectoryRegisterRequest
from api.schemas.tenant_schemas import OrganisationDirectoryRegisterResponse
from api.schemas.tenant_schemas import AvailableDomain
from api.schemas.tenant_schemas import StudentSummary
from api.schemas.tenant_schemas import TenantDomainSchemaData
from api.schemas.tenant_schemas import TenantDomainSchemaPatchRequest
from api.schemas.tenant_schemas import TenantDomainSchemaResponse
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
from api.schemas.tenant_schemas import TenantStudentsResponse
from api.schemas.tenant_schemas import TenantTestWebhookData
from api.schemas.tenant_schemas import TenantTestWebhookResponse
from api.schemas.tenant_schemas import TenantUsageData
from api.schemas.tenant_schemas import TenantUsageResponse
from api.schemas.tenant_schemas import TenantUsersListResponse
from api.services.proxy_user_service import ProxyUserService
from api.services.passport_link_service import PassportLinkService
from api.services.organisation_connection_service import OrganisationCredentialCipher
from api.services.quota_manager import QuotaManager
from api.services.conflict_resolution_service import apply_conflict_selection
from api.middleware.versioning import register_deprecated_field


router = APIRouter(prefix="/v1/tenant", tags=["tenant"])
DEPRECATED_PROXY_USER_STATS_FIELD_SUNSET = datetime(2026, 10, 1, tzinfo=UTC)
DEPRECATED_PROXY_USER_STATS_FIELD_PATH = (
    "GET /v1/tenant/users/{external_user_id}/stats response.data.user_id"
)
DEPRECATED_PROXY_USER_STATS_FIELD_GUIDE = (
    "https://docs.memoryos.io/migration/user-id-to-external-user-id"
)


AVAILABLE_DOMAIN_OPTIONS = [
    AvailableDomain(
        value=None,
        label="General Engine",
        description="Works for any AI product with generic facts, preferences, goals, procedures, relationships, and expertise.",
        status="available",
    ),
    AvailableDomain(
        value="edtech",
        label="EdTech Schema",
        description="Structured student memory for tutoring, exam prep, learning style, weak topics, and forgetting curves.",
        status="available",
    ),
    AvailableDomain(
        value="healthcare",
        label="HealthTech",
        description="Healthcare-specific memory schema.",
        status="coming_soon",
    ),
    AvailableDomain(
        value="agritech",
        label="AgriTech",
        description="Agriculture-specific memory schema.",
        status="coming_soon",
    ),
    AvailableDomain(
        value="hrtech",
        label="HR Tech",
        description="Hiring and workforce memory schema.",
        status="coming_soon",
    ),
    AvailableDomain(
        value="support",
        label="Customer Support Schema",
        description="Structured customer memory for support AI across SaaS, e-commerce, banking, travel, telecom, and more.",
        status="available",
    ),
]


def _tenant_domain_schema(tenant: Tenant) -> str | None:
    domain_schema = (tenant.metadata_json or {}).get("domain_schema")
    return domain_schema if domain_schema in {"edtech", "support"} else None


def _domain_schema_data(tenant: Tenant) -> TenantDomainSchemaData:
    return TenantDomainSchemaData(
        domain_schema=_tenant_domain_schema(tenant),
        available_domains=AVAILABLE_DOMAIN_OPTIONS,
        support_type_configured=tenant.support_type_configured,
        support_type_mode=tenant.support_type_mode or "single",
        support_types_allowed=list(tenant.support_types_allowed or []),
    )


def _service_writer_data(writer: ServiceWriter) -> ServiceWriterData:
    return ServiceWriterData(
        id=str(writer.id),
        service_key=writer.service_key,
        display_name=writer.display_name,
        api_key_id=str(writer.api_key_id) if writer.api_key_id else None,
        authority_rules=dict(writer.authority_rules or {}),
        is_active=bool(writer.is_active),
        created_at=writer.created_at,
        updated_at=writer.updated_at,
    )


def _claim_revision_data(revision: MemoryClaimRevision) -> MemoryClaimRevisionData:
    source_event = revision.source_event
    writer = revision.source_writer or (source_event.writer if source_event else None)
    return MemoryClaimRevisionData(
        id=str(revision.id),
        memory_id=str(revision.memory_id) if revision.memory_id else None,
        source_event_id=str(revision.source_event_id)
        if revision.source_event_id
        else None,
        source_writer_id=str(revision.source_writer_id)
        if revision.source_writer_id
        else None,
        source_domain=revision.source_domain,
        source_domain_record_id=revision.source_domain_record_id,
        source_field=revision.source_field,
        source_service=(
            writer.display_name
            if writer is not None
            else (source_event.source_service if source_event is not None else None)
        ),
        source_event_key=(
            source_event.source_event_id if source_event is not None else None
        ),
        asserted_value=revision.asserted_value,
        status=revision.status,
        authority_priority=int(revision.authority_priority or 50),
        confidence_score=float(revision.confidence_score or 0.0),
        observed_at=revision.observed_at,
        evidence_refs=list(revision.evidence_refs or []),
        resolution_reason=revision.resolution_reason,
        schema_version=int(revision.schema_version or 1),
        processor_version=str(revision.processor_version or "legacy"),
        created_at=revision.created_at,
    )


def _claim_data(claim: MemoryClaim, external_user_id: str) -> MemoryClaimData:
    revisions = sorted(
        list(claim.revisions or []),
        key=lambda revision: revision.created_at,
        reverse=True,
    )
    return MemoryClaimData(
        id=str(claim.id),
        external_user_id=external_user_id,
        category=claim.category.value
        if hasattr(claim.category, "value")
        else str(claim.category),
        claim_fingerprint=claim.claim_fingerprint,
        subject_key=claim.subject_key,
        predicate_key=claim.predicate_key,
        scope=dict(claim.scope or {}),
        active_value=claim.active_value,
        status=claim.status,
        active_memory_id=str(claim.active_memory_id)
        if claim.active_memory_id
        else None,
        winning_revision_id=str(claim.winning_revision_id)
        if claim.winning_revision_id
        else None,
        authority_priority=int(claim.authority_priority or 50),
        confidence_score=float(claim.confidence_score or 0.0),
        observed_at=claim.observed_at,
        effective_at=claim.effective_at,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
        revisions=[_claim_revision_data(revision) for revision in revisions[:10]],
    )


def _require_dashboard_auth(request: Request) -> None:
    if getattr(request.state, "auth_method", None) != "clerk_jwt":
        raise APIError(
            status_code=403,
            code="AUTH_403",
            error="dashboard_auth_required",
        )


async def _validate_writer_api_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: str | None,
) -> uuid.UUID | None:
    if not api_key_id:
        return None
    try:
        key_uuid = uuid.UUID(api_key_id)
    except ValueError as exc:
        raise APIError(
            status_code=422, code="PROV_422", error="invalid_api_key_id"
        ) from exc
    api_key = await session.get(ApiKey, key_uuid)
    if api_key is None or str(api_key.tenant_id) != str(tenant_id):
        raise APIError(status_code=422, code="PROV_422", error="invalid_writer_api_key")
    return key_uuid


def _count_forgetting_risk(forgetting_stages: dict | None) -> int:
    if not forgetting_stages:
        return 0

    count = 0
    for value in forgetting_stages.values():
        if isinstance(value, dict):
            stage = value.get("stage")
        else:
            stage = value
        if stage in {"forgotten", "critical"}:
            count += 1
    return count


def _customer_tier(memory: SupportMemory) -> str | None:
    identity = memory.customer_identity or {}
    if not isinstance(identity, dict):
        return None
    value = identity.get("tier") or identity.get("customer_tier")
    return str(value) if value else None


def _encode_cursor(sort_at: datetime | None, row_id: uuid.UUID) -> str:
    payload = {
        "sort_at": sort_at.isoformat() if sort_at else None,
        "id": str(row_id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        )
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


async def _load_tenant_budget(
    session: AsyncSession, tenant_id: str
) -> TenantBudget | None:
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
    count_stmt = select(func.count(ProxyUser.id)).where(
        ProxyUser.tenant_id == tenant_uuid
    )

    if cursor:
        cursor_last_active_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                ProxyUser.last_active_at < cursor_last_active_at,
                (ProxyUser.last_active_at == cursor_last_active_at)
                & (ProxyUser.id < cursor_id),
            )
        )

    stmt = stmt.order_by(ProxyUser.last_active_at.desc(), ProxyUser.id.desc()).limit(
        limit + 1
    )
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
    stmt = select(CallQualityLog).where(
        CallQualityLog.tenant_id == uuid.UUID(tenant_id)
    )
    count_stmt = select(CallQualityLog.id).where(
        CallQualityLog.tenant_id == uuid.UUID(tenant_id)
    )

    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                CallQualityLog.created_at < cursor_created_at,
                (CallQualityLog.created_at == cursor_created_at)
                & (CallQualityLog.id < cursor_id),
            )
        )

    stmt = stmt.order_by(
        CallQualityLog.created_at.desc(), CallQualityLog.id.desc()
    ).limit(limit + 1)
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
            func.concat(
                cast(CallQualityLog.tenant_id, String),
                literal(":"),
                CallQualityLog.external_user_id,
            ),
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
    external_user_id_hash = ProxyUserService.hash_external_user_id(
        tenant_id, external_user_id
    )
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
                            (
                                cast(CallQualityLog.layer_blocked_at, String) != "NONE",
                                1,
                            ),
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
                            (
                                cast(CallQualityLog.layer_blocked_at, String) != "NONE",
                                1,
                            ),
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
    gate_block_rate = (
        round((blocked_calls / total_calls), 4) if total_calls > 0 else 0.0
    )
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
    provenance_a = (
        dict(memory_a.metadata_json or {}).get("provenance", {})
        if memory_a is not None
        else {}
    )
    provenance_b = (
        dict(memory_b.metadata_json or {}).get("provenance", {})
        if memory_b is not None
        else {}
    )
    return CrossUserConflictData(
        id=str(conflict.id),
        tenant_id=str(conflict.tenant_id),
        entity_type=conflict.entity_type.value,
        entity_value_a=conflict.entity_value_a,
        entity_value_b=conflict.entity_value_b,
        user_a_memory_id=str(conflict.user_a_memory_id)
        if conflict.user_a_memory_id
        else None,
        user_b_memory_id=str(conflict.user_b_memory_id)
        if conflict.user_b_memory_id
        else None,
        user_a_id=(proxy_a.external_user_id if proxy_a is not None else None),
        user_b_id=(proxy_b.external_user_id if proxy_b is not None else None),
        memory_a_content=(memory_a.content if memory_a is not None else None),
        memory_b_content=(memory_b.content if memory_b is not None else None),
        source_service_a=provenance_a.get("service"),
        source_service_b=provenance_b.get("service"),
        memory_a_created_at=(memory_a.created_at if memory_a is not None else None),
        memory_b_created_at=(memory_b.created_at if memory_b is not None else None),
        detected_at=conflict.detected_at,
        status=conflict.status.value,
        auto_resolution=conflict.auto_resolution,
        auto_resolution_at=conflict.auto_resolution_at,
        resolved_at=conflict.resolved_at,
        resolution=conflict.resolution,
        resolution_path=conflict.resolution_path,
        resolved_by=conflict.resolved_by,
        resolution_reason=conflict.resolution_reason,
        requires_attention=bool(conflict.requires_attention),
    )


def _cross_user_conflict_dedupe_key(
    conflict: CrossUserConflict,
    *,
    include_status: bool = False,
) -> tuple[str, ...]:
    entity_type = (
        conflict.entity_type.value
        if hasattr(conflict.entity_type, "value")
        else str(conflict.entity_type)
    )
    memory_ids = sorted(
        str(memory_id)
        for memory_id in (conflict.user_a_memory_id, conflict.user_b_memory_id)
        if memory_id is not None
    )
    if len(memory_ids) < 2:
        memory_ids = sorted(
            [
                conflict.entity_value_a.lower().strip(),
                conflict.entity_value_b.lower().strip(),
            ]
        )

    parts: tuple[str, ...] = (entity_type, *memory_ids)
    if include_status:
        status = (
            conflict.status.value
            if hasattr(conflict.status, "value")
            else str(conflict.status)
        )
        parts = (*parts, status, conflict.resolution_path or "")
    return parts


def _dedupe_cross_user_conflicts(
    conflicts: list[CrossUserConflict],
    *,
    include_status: bool = False,
) -> list[CrossUserConflict]:
    seen: set[tuple[str, ...]] = set()
    unique: list[CrossUserConflict] = []
    for conflict in conflicts:
        key = _cross_user_conflict_dedupe_key(conflict, include_status=include_status)
        if key in seen:
            continue
        seen.add(key)
        unique.append(conflict)
    return unique


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
    cross_user_pending = 0
    conflicts_resolved_mtd = 0
    extraction_success_rate = 0.0
    nothing_to_extract_rate = 0.0
    if hasattr(session, "execute"):
        cross_user_pending = int(
            (
                await session.execute(
                    select(func.count(CrossUserConflict.id)).where(
                        CrossUserConflict.tenant_id == tenant_uuid,
                        CrossUserConflict.status == CrossUserConflictStatus.pending,
                        CrossUserConflict.requires_attention.is_(True),
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
        extraction_counts = (
            await session.execute(
                select(
                    func.count(ExtractionJob.id).label("completed_jobs"),
                    func.coalesce(
                        func.sum(
                            case((ExtractionJob.memories_created > 0, 1), else_=0)
                        ),
                        0,
                    ).label("jobs_with_memories"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    cast(
                                        ExtractionJob.result[
                                            "nothing_to_extract"
                                        ].astext,
                                        String,
                                    )
                                    == "true",
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("nothing_to_extract_jobs"),
                ).where(
                    ExtractionJob.tenant_id == tenant_uuid,
                    ExtractionJob.status == ExtractionJobStatus.completed,
                    ExtractionJob.completed_at >= month_start,
                )
            )
        ).one()
        completed_jobs = int(extraction_counts.completed_jobs or 0)
        jobs_with_memories = int(extraction_counts.jobs_with_memories or 0)
        nothing_to_extract_jobs = int(extraction_counts.nothing_to_extract_jobs or 0)
        extraction_success_rate = (
            round(jobs_with_memories / completed_jobs, 4) if completed_jobs > 0 else 0.0
        )
        nothing_to_extract_rate = (
            round(nothing_to_extract_jobs / completed_jobs, 4)
            if completed_jobs > 0
            else 0.0
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
            extraction_success_rate=extraction_success_rate,
            nothing_to_extract_rate=nothing_to_extract_rate,
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
                quality_score_avg=(
                    float(quality_score_avg) if quality_score_avg is not None else None
                ),
            )
            for item, quality_score_avg in proxy_users
        ],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/service-writers", response_model=ServiceWriterListResponse)
async def list_service_writers(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> ServiceWriterListResponse:
    _require_dashboard_auth(request)
    writers = (
        (
            await session.execute(
                select(ServiceWriter)
                .where(ServiceWriter.tenant_id == uuid.UUID(tenant_id))
                .order_by(ServiceWriter.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return ServiceWriterListResponse(
        data=[_service_writer_data(writer) for writer in writers],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


async def _repair_writer_attribution(
    session: AsyncSession,
    writer: ServiceWriter,
) -> tuple[int, int]:
    if not writer.is_active:
        return (0, 0)
    event_conditions = [
        MemorySourceEvent.tenant_id == writer.tenant_id,
        MemorySourceEvent.writer_id.is_(None),
        MemorySourceEvent.source_service == writer.service_key,
    ]
    if writer.api_key_id is not None:
        event_conditions.append(MemorySourceEvent.api_key_id == writer.api_key_id)
    event_ids = select(MemorySourceEvent.id).where(*event_conditions)
    revision_result = await session.execute(
        update(MemoryClaimRevision)
        .where(
            MemoryClaimRevision.source_writer_id.is_(None),
            MemoryClaimRevision.source_event_id.in_(event_ids),
        )
        .values(source_writer_id=writer.id)
    )
    event_result = await session.execute(
        update(MemorySourceEvent).where(*event_conditions).values(writer_id=writer.id)
    )
    return (int(event_result.rowcount or 0), int(revision_result.rowcount or 0))


@router.post("/service-writers", response_model=ServiceWriterResponse, status_code=201)
async def create_service_writer(
    request: Request,
    payload: ServiceWriterCreateRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> ServiceWriterResponse:
    _require_dashboard_auth(request)
    key_uuid = await _validate_writer_api_key(
        session,
        tenant_id=tenant_id,
        api_key_id=payload.api_key_id,
    )
    writer = ServiceWriter(
        tenant_id=uuid.UUID(tenant_id),
        api_key_id=key_uuid,
        service_key=payload.service_key,
        display_name=payload.display_name,
        authority_rules=payload.authority_rules.model_dump(),
    )
    session.add(writer)
    try:
        await session.flush()
        await _repair_writer_attribution(session, writer)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise APIError(
            status_code=409,
            code="PROV_409",
            error="service_writer_already_exists",
        ) from exc
    await session.refresh(writer)
    return ServiceWriterResponse(
        data=_service_writer_data(writer),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/service-writers/{writer_id}", response_model=ServiceWriterResponse)
async def update_service_writer(
    request: Request,
    writer_id: str,
    payload: ServiceWriterUpdateRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> ServiceWriterResponse:
    _require_dashboard_auth(request)
    try:
        writer_uuid = uuid.UUID(writer_id)
    except ValueError as exc:
        raise APIError(
            status_code=404, code="PROV_404", error="service_writer_not_found"
        ) from exc
    writer = (
        await session.execute(
            select(ServiceWriter).where(
                ServiceWriter.id == writer_uuid,
                ServiceWriter.tenant_id == uuid.UUID(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if writer is None:
        raise APIError(
            status_code=404, code="PROV_404", error="service_writer_not_found"
        )

    fields_set = payload.model_fields_set
    if "display_name" in fields_set:
        writer.display_name = str(payload.display_name)
    if "api_key_id" in fields_set:
        writer.api_key_id = await _validate_writer_api_key(
            session,
            tenant_id=tenant_id,
            api_key_id=payload.api_key_id,
        )
    if "authority_rules" in fields_set:
        writer.authority_rules = (
            payload.authority_rules.model_dump()
            if payload.authority_rules is not None
            else {}
        )
    if "is_active" in fields_set:
        writer.is_active = bool(payload.is_active)
    try:
        await session.flush()
        await _repair_writer_attribution(session, writer)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise APIError(
            status_code=409,
            code="PROV_409",
            error="api_key_already_bound_to_writer",
        ) from exc
    await session.refresh(writer)
    return ServiceWriterResponse(
        data=_service_writer_data(writer),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/source-events", response_model=MemorySourceEventListResponse)
async def list_memory_source_events(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    external_user_id: str | None = Query(default=None),
    source_service: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> MemorySourceEventListResponse:
    _require_dashboard_auth(request)
    statement = (
        select(
            MemorySourceEvent,
            ProxyUser.external_user_id,
            ExtractionJob.id.label("extraction_job_id"),
        )
        .join(ProxyUser, ProxyUser.id == MemorySourceEvent.proxy_user_id)
        .outerjoin(ExtractionJob, ExtractionJob.source_event_id == MemorySourceEvent.id)
        .where(MemorySourceEvent.tenant_id == uuid.UUID(tenant_id))
        .order_by(MemorySourceEvent.observed_at.desc(), MemorySourceEvent.id.desc())
        .limit(limit)
    )
    if external_user_id:
        statement = statement.where(ProxyUser.external_user_id == external_user_id)
    if source_service:
        statement = statement.where(MemorySourceEvent.source_service == source_service)
    rows = (await session.execute(statement)).all()
    return MemorySourceEventListResponse(
        data=[
            MemorySourceEventData(
                id=str(event.id),
                external_user_id=event_external_user_id,
                writer_id=str(event.writer_id) if event.writer_id else None,
                api_key_id=str(event.api_key_id) if event.api_key_id else None,
                source_service=event.source_service,
                source_event_id=event.source_event_id,
                observed_at=event.observed_at,
                received_at=event.received_at,
                payload_hash=event.payload_hash,
                scope=dict(event.scope or {}),
                evidence_refs=list(event.evidence_refs or []),
                processing_metadata=dict(event.processing_metadata or {}),
                extraction_job_id=(
                    str(extraction_job_id) if extraction_job_id else None
                ),
            )
            for event, event_external_user_id, extraction_job_id in rows
        ],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/claims", response_model=MemoryClaimListResponse)
async def list_memory_claims(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    external_user_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryClaimListResponse:
    _require_dashboard_auth(request)
    statement = (
        select(MemoryClaim, ProxyUser.external_user_id)
        .join(ProxyUser, ProxyUser.id == MemoryClaim.proxy_user_id)
        .where(MemoryClaim.tenant_id == uuid.UUID(tenant_id))
        .options(
            selectinload(MemoryClaim.revisions)
            .selectinload(MemoryClaimRevision.source_event)
            .selectinload(MemorySourceEvent.writer),
            selectinload(MemoryClaim.revisions).selectinload(
                MemoryClaimRevision.source_writer
            ),
        )
        .order_by(MemoryClaim.updated_at.desc(), MemoryClaim.id.desc())
        .limit(limit)
    )
    if external_user_id:
        statement = statement.where(ProxyUser.external_user_id == external_user_id)
    if status:
        statement = statement.where(MemoryClaim.status == status)
    if category:
        statement = statement.where(MemoryClaim.category == category)
    rows = (await session.execute(statement)).all()
    return MemoryClaimListResponse(
        data=[_claim_data(claim, external_user_id) for claim, external_user_id in rows],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/claims/{claim_id}", response_model=MemoryClaimResponse)
async def get_memory_claim(
    request: Request,
    claim_id: str,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> MemoryClaimResponse:
    _require_dashboard_auth(request)
    try:
        claim_uuid = uuid.UUID(claim_id)
    except ValueError as exc:
        raise APIError(
            status_code=404, code="CLAIM_404", error="claim_not_found"
        ) from exc
    row = (
        await session.execute(
            select(MemoryClaim, ProxyUser.external_user_id)
            .join(ProxyUser, ProxyUser.id == MemoryClaim.proxy_user_id)
            .where(
                MemoryClaim.id == claim_uuid,
                MemoryClaim.tenant_id == uuid.UUID(tenant_id),
            )
            .options(
                selectinload(MemoryClaim.revisions)
                .selectinload(MemoryClaimRevision.source_event)
                .selectinload(MemorySourceEvent.writer),
                selectinload(MemoryClaim.revisions).selectinload(
                    MemoryClaimRevision.source_writer
                ),
            )
        )
    ).one_or_none()
    if row is None:
        raise APIError(status_code=404, code="CLAIM_404", error="claim_not_found")
    claim, external_user_id = row
    return MemoryClaimResponse(
        data=_claim_data(claim, external_user_id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/domain-schema", response_model=TenantDomainSchemaResponse)
async def get_tenant_domain_schema(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantDomainSchemaResponse:
    tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")

    return TenantDomainSchemaResponse(
        data=_domain_schema_data(tenant),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/domain-schema", response_model=TenantDomainSchemaResponse)
async def update_tenant_domain_schema(
    request: Request,
    payload: TenantDomainSchemaPatchRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantDomainSchemaResponse:
    tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")

    if payload.domain_schema not in {None, "edtech", "support"}:
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="domain_schema_not_available",
            details={"domain_schema": payload.domain_schema},
        )

    metadata = dict(tenant.metadata_json or {})
    if payload.domain_schema == "edtech":
        metadata["domain_schema"] = "edtech"
        metadata["edtech_schema_enabled"] = True
    elif payload.domain_schema == "support":
        metadata["domain_schema"] = "support"
        metadata["edtech_schema_enabled"] = False
    else:
        metadata.pop("domain_schema", None)
        metadata["edtech_schema_enabled"] = False
        tenant.support_type_configured = None
        tenant.support_type_mode = "single"
        tenant.support_types_allowed = []
    tenant.metadata_json = metadata
    await session.commit()
    await session.refresh(tenant)

    return TenantDomainSchemaResponse(
        data=_domain_schema_data(tenant),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/support-type", response_model=TenantSupportTypeResponse)
async def update_tenant_support_type(
    request: Request,
    payload: TenantSupportTypePatchRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantSupportTypeResponse:
    tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
    if _tenant_domain_schema(tenant) != "support":
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="support_schema_required",
            details={
                "message": "Enable Customer Support schema before configuring support type."
            },
        )

    if payload.support_type_mode == "single" and payload.support_type is None:
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="support_type_required",
            details={"message": "single support mode requires support_type."},
        )
    if payload.support_type_mode == "multi" and not payload.support_types_allowed:
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="support_types_allowed_required",
            details={
                "message": "multi support mode requires at least one allowed support type."
            },
        )

    tenant.support_type_mode = payload.support_type_mode
    tenant.support_type_configured = (
        payload.support_type if payload.support_type_mode == "single" else None
    )
    tenant.support_types_allowed = list(dict.fromkeys(payload.support_types_allowed))
    await session.commit()
    await session.refresh(tenant)
    return TenantSupportTypeResponse(
        data=TenantSupportTypeData(
            support_type_configured=tenant.support_type_configured,
            support_type_mode=tenant.support_type_mode,
            support_types_allowed=list(tenant.support_types_allowed or []),
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/customers", response_model=TenantSupportCustomersResponse)
async def list_tenant_support_customers(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=50),
) -> TenantSupportCustomersResponse:
    tenant_uuid = uuid.UUID(str(tenant_id))
    tenant = await session.get(Tenant, tenant_uuid)
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
    if _tenant_domain_schema(tenant) != "support":
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="support_schema_required",
            details={
                "message": "Enable Customer Support schema to access customer support data."
            },
        )

    total_result = await session.execute(
        select(func.count(SupportMemory.id)).where(
            SupportMemory.tenant_id == tenant_uuid
        )
    )
    total = int(total_result.scalar_one() or 0)

    stmt = (
        select(SupportMemory, ProxyUser)
        .join(ProxyUser, ProxyUser.id == SupportMemory.proxy_user_id)
        .where(SupportMemory.tenant_id == tenant_uuid)
        .order_by(ProxyUser.last_active_at.desc(), SupportMemory.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        sort_at, row_id = _decode_cursor(cursor)
        if sort_at is None:
            stmt = stmt.where(SupportMemory.id < row_id)
        else:
            stmt = stmt.where(
                or_(
                    ProxyUser.last_active_at < sort_at,
                    (ProxyUser.last_active_at == sort_at) & (SupportMemory.id < row_id),
                )
            )

    result = await session.execute(stmt)
    rows = result.all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last_memory, last_user = page_rows[-1]
        next_cursor = _encode_cursor(last_user.last_active_at, last_memory.id)

    return TenantSupportCustomersResponse(
        data=[
            SupportCustomerSummary(
                external_user_id=user.external_user_id,
                customer_tier=_customer_tier(memory),
                support_type=memory.support_type,
                sentiment_pattern=memory.sentiment_pattern,
                open_issues_count=1 if memory.current_open_issue else 0,
                total_issues_lifetime=len(memory.issue_history or []),
                last_contact=user.last_active_at or memory.updated_at,
            )
            for memory, user in page_rows
        ],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/support-stats", response_model=TenantSupportStatsResponse)
async def get_tenant_support_stats(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantSupportStatsResponse:
    tenant_uuid = uuid.UUID(str(tenant_id))
    tenant = await session.get(Tenant, tenant_uuid)
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
    if _tenant_domain_schema(tenant) != "support":
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="support_schema_required",
            details={
                "message": "Enable Customer Support schema to access support stats."
            },
        )

    result = await session.execute(
        select(SupportMemory).where(SupportMemory.tenant_id == tenant_uuid)
    )
    memories = list(result.scalars().all())
    total = len(memories)
    sentiment_breakdown: dict[str, int] = {}
    support_type_distribution: dict[str, int] = {}
    total_issues = 0
    open_issues_count = 0
    high_risk_count = 0
    for memory in memories:
        sentiment = memory.sentiment_pattern or "unknown"
        sentiment_breakdown[sentiment] = sentiment_breakdown.get(sentiment, 0) + 1
        support_type = memory.support_type or "unknown"
        support_type_distribution[support_type] = (
            support_type_distribution.get(support_type, 0) + 1
        )
        total_issues += len(memory.issue_history or [])
        if memory.current_open_issue:
            open_issues_count += 1
        if memory.sentiment_pattern == "high_escalation_risk":
            high_risk_count += 1

    return TenantSupportStatsResponse(
        data=TenantSupportStatsData(
            total_customers_with_memory=total,
            open_issues_count=open_issues_count,
            high_escalation_risk_count=high_risk_count,
            sentiment_breakdown=sentiment_breakdown,
            support_type_distribution=support_type_distribution,
            avg_issues_per_customer=(round(total_issues / total, 2) if total else 0.0),
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/students", response_model=TenantStudentsResponse)
async def list_tenant_students(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=50),
) -> TenantStudentsResponse:
    tenant_uuid = uuid.UUID(str(tenant_id))
    tenant = await session.get(Tenant, tenant_uuid)
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
    if _tenant_domain_schema(tenant) != "edtech":
        raise APIError(
            status_code=400,
            code="TEN_400",
            error="edtech_schema_required",
            details={
                "message": "Enable EdTech schema to access student data",
            },
        )

    total_result = await session.execute(
        select(func.count(EdTechMemory.id)).where(EdTechMemory.tenant_id == tenant_uuid)
    )
    total = int(total_result.scalar_one() or 0)

    stmt = (
        select(EdTechMemory, ProxyUser)
        .join(ProxyUser, ProxyUser.id == EdTechMemory.proxy_user_id)
        .where(EdTechMemory.tenant_id == tenant_uuid)
        .order_by(ProxyUser.last_active_at.desc(), EdTechMemory.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        sort_at, row_id = _decode_cursor(cursor)
        if sort_at is None:
            stmt = stmt.where(EdTechMemory.id < row_id)
        else:
            stmt = stmt.where(
                or_(
                    ProxyUser.last_active_at < sort_at,
                    (ProxyUser.last_active_at == sort_at) & (EdTechMemory.id < row_id),
                )
            )

    result = await session.execute(stmt)
    rows = result.all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last_memory, last_user = page_rows[-1]
        next_cursor = _encode_cursor(last_user.last_active_at, last_memory.id)

    today = utc_now().date()
    data = []
    for memory, proxy_user in page_rows:
        days_to_exam = None
        if memory.exam_date:
            days_to_exam = (memory.exam_date - today).days
        data.append(
            StudentSummary(
                external_user_id=proxy_user.external_user_id,
                grade_level=memory.grade_level,
                board_or_curriculum=memory.board_or_curriculum,
                exam_name=memory.exam_name,
                exam_date=memory.exam_date,
                days_to_exam=days_to_exam,
                weak_topics_count=len(memory.weak_topics or []),
                forgetting_risk_count=_count_forgetting_risk(memory.forgetting_stages),
                last_session_at=proxy_user.last_active_at,
            )
        )

    return TenantStudentsResponse(
        data=data,
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
    blocked = await proxy_user_service.block(
        tenant_id=tenant_id, external_user_id=external_user_id
    )
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
                reason=getattr(item, "reason", None),
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
        data=await _get_tenant_memory_additions(
            session, tenant_id=tenant_id, limit=limit
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/conflict-stats", response_model=ConflictStatsResponse)
async def get_tenant_conflict_stats(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> ConflictStatsResponse:
    tenant_uuid = uuid.UUID(tenant_id)
    month_start = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_conflicts = (
        (
            await session.execute(
                select(CrossUserConflict)
                .where(
                    CrossUserConflict.tenant_id == tenant_uuid,
                    CrossUserConflict.detected_at >= month_start,
                )
                .order_by(CrossUserConflict.detected_at.desc())
            )
        )
        .scalars()
        .all()
    )
    open_conflicts = (
        (
            await session.execute(
                select(CrossUserConflict)
                .where(
                    CrossUserConflict.tenant_id == tenant_uuid,
                    CrossUserConflict.status.in_(
                        [
                            CrossUserConflictStatus.pending,
                            CrossUserConflictStatus.clarification_queued,
                        ]
                    ),
                )
                .order_by(CrossUserConflict.detected_at.desc())
            )
        )
        .scalars()
        .all()
    )
    month_unique = _dedupe_cross_user_conflicts(month_conflicts)
    open_unique = _dedupe_cross_user_conflicts(open_conflicts)

    total_detected = len(month_unique)
    requires_attention = sum(
        1
        for conflict in open_unique
        if conflict.requires_attention
        and conflict.status == CrossUserConflictStatus.pending
    )
    pending_user_session = sum(
        1
        for conflict in open_unique
        if conflict.resolution_path == "user_session"
        and conflict.status == CrossUserConflictStatus.clarification_queued
    )
    pending_tenant_review = sum(
        1
        for conflict in open_unique
        if conflict.resolution_path == "tenant_review"
        and conflict.status == CrossUserConflictStatus.pending
        and conflict.requires_attention
    )
    resolved_by_user_session_mtd = sum(
        1
        for conflict in month_unique
        if conflict.resolved_by == "user_session"
        and conflict.resolved_at is not None
        and conflict.resolved_at >= month_start
    )
    resolved_by_tenant_mtd = sum(
        1
        for conflict in month_unique
        if conflict.resolved_by == "tenant"
        and conflict.resolved_at is not None
        and conflict.resolved_at >= month_start
    )
    clarification_pending = pending_user_session
    clarification_conflict_ids = [
        conflict.id
        for conflict in open_unique
        if conflict.resolution_path == "user_session"
        and conflict.status == CrossUserConflictStatus.clarification_queued
    ]
    if clarification_conflict_ids:
        clarification_pending = int(
            (
                await session.execute(
                    select(func.count(ClarificationQueue.id)).where(
                        ClarificationQueue.tenant_id == tenant_uuid,
                        ClarificationQueue.status == ClarificationQueueStatus.pending,
                        ClarificationQueue.conflict_id.in_(clarification_conflict_ids),
                    )
                )
            ).scalar_one()
            or 0
        )
    breakdown = {
        "per_user_scoped": 0,
        "recency_weighted": 0,
        "confidence_weighted": 0,
        "clarification_queued": 0,
    }
    for conflict in month_unique:
        resolution = conflict.auto_resolution
        if resolution in breakdown:
            breakdown[str(resolution)] += 1

    auto_resolved = sum(breakdown.values())
    return ConflictStatsResponse(
        data=ConflictStatsData(
            total_detected_mtd=total_detected,
            auto_resolved_mtd=auto_resolved,
            auto_resolution_rate=(
                auto_resolved / total_detected if total_detected else 0.0
            ),
            resolution_breakdown=breakdown,
            requires_attention=requires_attention,
            clarifications_pending=clarification_pending,
            pending_user_session=pending_user_session,
            pending_tenant_review=pending_tenant_review,
            resolved_by_user_session_mtd=resolved_by_user_session_mtd,
            resolved_by_tenant_mtd=resolved_by_tenant_mtd,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post(
    "/directory/register",
    response_model=OrganisationDirectoryRegisterResponse,
)
async def register_tenant_organisation_directory(
    request: Request,
    payload: OrganisationDirectoryRegisterRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> OrganisationDirectoryRegisterResponse:
    tenant_uuid = uuid.UUID(tenant_id)
    existing = (
        await session.execute(
            select(OrganisationDirectory).where(
                OrganisationDirectory.tenant_id == tenant_uuid
            )
        )
    ).scalar_one_or_none()
    oauth_enabled = bool(payload.oauth_client_id)
    encrypted_secret = (
        OrganisationCredentialCipher.encrypt(payload.oauth_client_secret)
        if payload.oauth_client_secret
        else None
    )
    if existing is None:
        existing = OrganisationDirectory(tenant_id=tenant_uuid)
        session.add(existing)
    existing.display_name = payload.display_name.strip()
    existing.logo_url = payload.logo_url
    existing.website_url = payload.website_url
    existing.category = payload.category
    existing.oauth_enabled = oauth_enabled
    existing.oauth_client_id = payload.oauth_client_id
    if encrypted_secret is not None:
        existing.oauth_client_secret_ciphertext = encrypted_secret
    existing.oauth_authorization_url = payload.oauth_authorization_url
    existing.oauth_token_url = payload.oauth_token_url
    existing.oauth_userinfo_url = payload.oauth_userinfo_url
    existing.oauth_scopes = list(dict.fromkeys(payload.oauth_scopes))
    existing.link_token_enabled = payload.link_token_enabled
    existing.is_public = payload.is_public
    existing.is_verified = False
    await session.commit()
    await session.refresh(existing)
    return OrganisationDirectoryRegisterResponse(
        data=OrganisationDirectoryRegisterData(
            directory_id=existing.id,
            status="pending_review",
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/memory-passport/link-token", response_model=PassportLinkTokenResponse)
async def create_memory_passport_link_token(
    request: Request,
    payload: PassportLinkTokenRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> PassportLinkTokenResponse:
    issued = await PassportLinkService(
        session=session,
        cache_service=cache_service,
    ).issue(
        tenant_id=tenant_id,
        agent_id=str(payload.agent_id),
        external_user_id=payload.external_user_id,
    )
    return PassportLinkTokenResponse(
        data=PassportLinkTokenData(
            link_token=issued.token,
            expires_in_seconds=issued.expires_in_seconds,
        ),
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
            CrossUserConflictStatus.clarification_queued,
            CrossUserConflictStatus.resolved,
            CrossUserConflictStatus.ignored,
        ]
    )
    conflicts = (
        (
            await session.execute(
                select(CrossUserConflict)
                .options(
                    selectinload(CrossUserConflict.user_a_memory).selectinload(
                        Memory.proxy_user
                    ),
                    selectinload(CrossUserConflict.user_b_memory).selectinload(
                        Memory.proxy_user
                    ),
                )
                .where(
                    CrossUserConflict.tenant_id == uuid.UUID(tenant_id),
                    CrossUserConflict.status.in_(status_filter),
                )
                .order_by(CrossUserConflict.detected_at.desc())
                .limit(min(limit * 5, 2500))
            )
        )
        .scalars()
        .all()
    )
    conflicts = _dedupe_cross_user_conflicts(conflicts, include_status=True)[:limit]
    return CrossUserConflictsResponse(
        data=[_cross_user_conflict_to_data(conflict) for conflict in conflicts],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch(
    "/shared-context-conflicts/{conflict_id}", response_model=CrossUserConflictsResponse
)
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
                selectinload(CrossUserConflict.user_a_memory).selectinload(
                    Memory.proxy_user
                ),
                selectinload(CrossUserConflict.user_b_memory).selectinload(
                    Memory.proxy_user
                ),
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
    if status == "ignored":
        conflict.status = CrossUserConflictStatus.ignored
        conflict.requires_attention = False
        conflict.auto_resolution = conflict.auto_resolution or "marked_not_conflict"
        conflict.auto_resolution_at = utc_now()
    else:
        raise APIError(
            status_code=400, code="CONFLICT_400", error="invalid_conflict_resolution"
        )

    await session.commit()
    return CrossUserConflictsResponse(
        data=[_cross_user_conflict_to_data(conflict)],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post(
    "/conflicts/{conflict_id}/resolve", response_model=TenantConflictResolveResponse
)
async def resolve_tenant_conflict(
    request: Request,
    conflict_id: str,
    payload: TenantConflictResolveRequest,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> TenantConflictResolveResponse:
    conflict = (
        await session.execute(
            select(CrossUserConflict)
            .options(
                selectinload(CrossUserConflict.user_a_memory).selectinload(
                    Memory.proxy_user
                ),
                selectinload(CrossUserConflict.user_b_memory).selectinload(
                    Memory.proxy_user
                ),
            )
            .where(
                CrossUserConflict.id == uuid.UUID(conflict_id),
                CrossUserConflict.tenant_id == uuid.UUID(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise APIError(status_code=404, code="CONFLICT_404", error="conflict_not_found")
    if conflict.status in {
        CrossUserConflictStatus.resolved,
        CrossUserConflictStatus.ignored,
    }:
        raise APIError(
            status_code=409,
            code="CONFLICT_409",
            error="conflict_already_resolved",
        )
    if conflict.resolution_path not in {None, "tenant_review"}:
        raise APIError(
            status_code=400,
            code="CONFLICT_400",
            error="conflict_not_tenant_review",
        )

    correct_user = payload.correct_user
    if correct_user not in {"A", "B", "both_valid"}:
        raise APIError(
            status_code=400, code="CONFLICT_400", error="invalid_correct_user"
        )

    memory_a = conflict.user_a_memory
    memory_b = conflict.user_b_memory
    if memory_a is None or memory_b is None:
        raise APIError(
            status_code=400, code="CONFLICT_400", error="conflict_memory_missing"
        )

    reason = payload.reason or None
    action_taken = "both_valid_no_archive"
    archived_memory: Memory | None = None
    kept_memory: Memory | None = None
    if correct_user == "A":
        kept_memory = memory_a
        archived_memory = memory_b
        action_taken = "archived_user_b_memory"
    elif correct_user == "B":
        kept_memory = memory_b
        archived_memory = memory_a
        action_taken = "archived_user_a_memory"

    selection = "both" if correct_user == "both_valid" else correct_user
    transition_reason = reason or (
        "Tenant confirmed memory A."
        if correct_user == "A"
        else (
            "Tenant confirmed memory B."
            if correct_user == "B"
            else "Tenant confirmed both memories are valid."
        )
    )
    try:
        transition_action = await apply_conflict_selection(
            session,
            conflict=conflict,
            selection=selection,
            changed_by="operator",
            reason=transition_reason,
        )
    except ValueError as exc:
        raise APIError(
            status_code=400,
            code="CONFLICT_400",
            error=str(exc),
        ) from exc
    if transition_action != "memory_states_unchanged":
        action_taken = transition_action

    if archived_memory is not None and kept_memory is not None:
        archived_signal_rows = (
            (
                await session.execute(
                    select(SharedContextSignal).where(
                        SharedContextSignal.source_memory_id == archived_memory.id,
                        SharedContextSignal.tenant_id == uuid.UUID(tenant_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        kept_signal_rows = (
            (
                await session.execute(
                    select(SharedContextSignal).where(
                        SharedContextSignal.source_memory_id == kept_memory.id,
                        SharedContextSignal.tenant_id == uuid.UUID(tenant_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        for signal in archived_signal_rows:
            signal.is_superseded = True
        for signal in kept_signal_rows:
            signal.is_superseded = False

    clarification_rows = (
        (
            await session.execute(
                select(ClarificationQueue).where(
                    ClarificationQueue.conflict_id == conflict.id,
                    ClarificationQueue.status.in_(
                        [
                            ClarificationQueueStatus.pending,
                            ClarificationQueueStatus.triggered,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for clarification in clarification_rows:
        clarification.status = ClarificationQueueStatus.resolved

    now = utc_now()
    conflict.status = CrossUserConflictStatus.resolved
    conflict.resolution_path = "tenant_review"
    conflict.resolved_at = now
    conflict.resolved_by = "tenant"
    conflict.resolution = correct_user
    conflict.resolution_reason = reason
    conflict.requires_attention = False

    session.add(
        AuditLog(
            user_id=(archived_memory.user_id if archived_memory is not None else None),
            proxy_user_id=(
                archived_memory.proxy_user_id if archived_memory is not None else None
            ),
            action=AuditAction.conflict_resolved_by_tenant,
            memory_id=(archived_memory.id if archived_memory is not None else None),
            old_value={
                "conflict_id": str(conflict.id),
                "memory_a": memory_a.content,
                "memory_b": memory_b.content,
            },
            new_value={
                "correct_user": correct_user,
                "reason": reason,
                "action_taken": action_taken,
            },
            metadata_json={
                "conflict_id": str(conflict.id),
                "correct_user": correct_user,
                "reason": reason,
            },
        )
    )

    await session.commit()
    return TenantConflictResolveResponse(
        data=TenantConflictResolveData(
            resolved=True,
            conflict_id=str(conflict.id),
            action_taken=action_taken,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post(
    "/settings/enable-edtech-schema", response_model=EnableEdTechSchemaResponse
)
async def enable_edtech_schema(
    request: Request,
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
    session: DbSession,
) -> EnableEdTechSchemaResponse:
    tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None:
        raise APIError(status_code=404, code="TEN_404", error="tenant_not_found")
    metadata = dict(tenant.metadata_json or {})
    metadata["domain_schema"] = "edtech"
    metadata["edtech_schema_enabled"] = True
    tenant.metadata_json = metadata
    await session.commit()
    return EnableEdTechSchemaResponse(
        data=EnableEdTechSchemaData(
            enabled=True,
            effective_from="next add() call",
        ),
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
        raise APIError(
            status_code=400, code="TEN_400", error="alert_webhook_not_configured"
        )

    delivered, status_code = await _send_test_webhook(
        tenant_budget.alert_webhook_url, tenant_id
    )
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
                    str(row["replacement_field"])
                    if row.get("replacement_field")
                    else None
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
