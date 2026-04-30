from __future__ import annotations

import base64
import calendar
import json
import uuid
from datetime import date
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy import Float
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.celery_app import celery_app
from api.db.cache import CacheService
from api.db.database import get_db_session
from api.db.models import ApiKey
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import CallQualityLog
from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import Memory
from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import ProxyUser
from api.db.models import QuotaMode
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.config.plan_limits import apply_plan_limits
from api.dependencies import get_cache_service
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.routers.common import get_request_id
from api.schemas.internal_schemas import AllTenantsResponse
from api.schemas.internal_schemas import AuditLogsResponse
from api.schemas.internal_schemas import BackfillJobResponse
from api.schemas.internal_schemas import CircuitStatus
from api.schemas.internal_schemas import CostSummaryResponse
from api.schemas.internal_schemas import CostSummaryTenant
from api.schemas.internal_schemas import DeadLetterDiscardResponse
from api.schemas.internal_schemas import InternalTenantRecord
from api.schemas.internal_schemas import QueueStatus
from api.schemas.internal_schemas import QualitySummary
from api.schemas.internal_schemas import RecentExtractionJob
from api.schemas.internal_schemas import SystemHealthResponse
from api.schemas.internal_schemas import TenantDetail
from api.schemas.internal_schemas import PlanChangeRequest
from api.schemas.internal_schemas import TenantBudgetRecord
from api.schemas.internal_schemas import TenantSummary
from api.schemas.tenant_schemas import TenantUsageData
from api.services.embedding_service import EmbeddingService
from api.services.llm_service import get_llm_provider_health
from api.services.quota_manager import QuotaManager
from api.tasks.backfill_tasks import run_backfill_proxy_user_ids
from api.tasks.extraction_tasks import EXTRACTION_TASK_NAME
from api.tasks.queue_router import ENTERPRISE_QUEUE
from api.tasks.queue_router import FREE_QUEUE
from api.tasks.queue_router import GROWTH_QUEUE
from api.tasks.queue_router import QueueRouter
from api.tasks.queue_router import STARTER_QUEUE


router = APIRouter(prefix="/v1/internal", tags=["internal"])

CIRCUIT_NAMES = ("redis", "gemini_embed", "gemini_extract", "qdrant", "postgres")
QUEUE_THRESHOLDS = {
    ENTERPRISE_QUEUE: 100,
    GROWTH_QUEUE: 200,
    STARTER_QUEUE: 500,
    FREE_QUEUE: 200,
}
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _result_all_rows(result) -> list[object]:
    if hasattr(result, "all"):
        return list(result.all())
    if hasattr(result, "scalars"):
        return list(result.scalars().all())
    return []


def _result_scalar_one_or_none(result):
    if hasattr(result, "scalar_one_or_none"):
        return result.scalar_one_or_none()
    if hasattr(result, "scalars"):
        rows = list(result.scalars().all())
        return rows[0] if rows else None
    return None


def _unpack_dead_letter_row(row) -> tuple[object | None, object]:
    if isinstance(row, tuple) and len(row) == 2:
        return row[0], row[1]
    if hasattr(row, "_mapping"):
        values = list(row._mapping.values())
        if len(values) >= 2:
            return values[0], values[1]
    if hasattr(row, "__getitem__"):
        try:
            return row[0], row[1]
        except Exception:
            pass
    return None, row


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _queue_jobs_key(queue_name: str) -> str:
    return f"queue_depth:{queue_name}:jobs"


def _cached_queue_depth_key(queue_name: str) -> str:
    return f"queue_depth:{queue_name}"


def _truncate_identifier(value: uuid.UUID | str | None, length: int = 8) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= length else f"{text[:length]}..."


def _truncate_text(value: str | None, length: int = 100) -> str | None:
    if value is None:
        return None
    return value if len(value) <= length else f"{value[:length]}..."


async def _reset_circuit_breaker(
    *,
    request: Request,
    cache_service: CacheService,
    circuit_name: str,
) -> CircuitStatus:
    if circuit_name not in CIRCUIT_NAMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="circuit_not_found")

    registry = getattr(request.app.state, "circuit_breakers", None) or CircuitBreakerRegistry.get_instance()
    breakers = getattr(registry, "_breakers", {})
    breaker = breakers.get(circuit_name)
    if breaker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="circuit_not_found")

    breaker._record_success()
    snapshot = breaker.snapshot()

    try:
        await cache_service.client.set(
            f"cb:{circuit_name}:state",
            json.dumps(snapshot),
        )
    except Exception:
        pass

    opened_at = float(snapshot.get("opened_at", 0.0) or 0.0)
    return CircuitStatus(
        name=circuit_name,
        state=str(snapshot.get("state", "CLOSED")),
        open_since=datetime.fromtimestamp(opened_at, tz=UTC) if opened_at > 0 else None,
        failure_count=int(snapshot.get("failure_count", 0) or 0),
    )


def _encode_tenant_cursor(*, needs_attention_rank: int, last_api_call: datetime | None, tenant_id: uuid.UUID) -> str:
    payload = {
        "needs_attention_rank": needs_attention_rank,
        "last_api_call": last_api_call.isoformat() if last_api_call else None,
        "tenant_id": str(tenant_id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _decode_tenant_cursor(cursor: str) -> tuple[int, datetime | None, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8"))
        last_api_call_raw = payload.get("last_api_call")
        return (
            int(payload["needs_attention_rank"]),
            datetime.fromisoformat(last_api_call_raw) if last_api_call_raw else None,
            uuid.UUID(payload["tenant_id"]),
        )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor") from exc


def _quota_mode_expression():
    call_limit_reached = and_(
        TenantBudget.monthly_call_limit.is_not(None),
        TenantBudget.monthly_call_limit != 0,
        func.coalesce(TenantBudget.current_month_calls, 0) >= TenantBudget.monthly_call_limit,
    )
    token_limit_reached = and_(
        TenantBudget.monthly_token_limit.is_not(None),
        TenantBudget.monthly_token_limit != 0,
        func.coalesce(TenantBudget.current_month_tokens, 0) >= TenantBudget.monthly_token_limit,
    )
    write_limit_reached = and_(
        TenantBudget.write_call_limit.is_not(None),
        TenantBudget.write_call_limit != 0,
        func.coalesce(TenantBudget.write_calls, 0) >= TenantBudget.write_call_limit,
    )
    read_limit_remaining = or_(
        TenantBudget.read_limit.is_(None),
        TenantBudget.read_limit == 0,
        func.coalesce(TenantBudget.read_calls, 0) < TenantBudget.read_limit,
    )
    return case(
        (
            and_(
                TenantBudget.overage_policy == OveragePolicy.block,
                or_(call_limit_reached, token_limit_reached),
            ),
            literal(QuotaMode.blocked.value),
        ),
        (
            and_(
                TenantBudget.overage_policy == OveragePolicy.warn,
                call_limit_reached,
            ),
            literal(QuotaMode.passthrough.value),
        ),
        (
            and_(write_limit_reached, read_limit_remaining),
            literal(QuotaMode.degraded_retrieve.value),
        ),
        else_=literal(QuotaMode.full.value),
    )


async def _get_system_health(cache_service: CacheService) -> SystemHealthResponse:
    raw_circuit_states = await cache_service.client.mget([f"cb:{name}:state" for name in CIRCUIT_NAMES])
    circuits: list[CircuitStatus] = []
    for name, raw_value in zip(CIRCUIT_NAMES, raw_circuit_states, strict=False):
        payload: dict[str, Any] = {}
        if raw_value:
            try:
                payload = json.loads(raw_value)
            except json.JSONDecodeError:
                payload = {}
        opened_at = float(payload.get("opened_at", 0.0) or 0.0)
        circuits.append(
            CircuitStatus(
                name=name,
                state=str(payload.get("state", "CLOSED")),
                open_since=datetime.fromtimestamp(opened_at, tz=UTC) if opened_at > 0 else None,
                failure_count=int(payload.get("failure_count", 0) or 0),
            )
        )

    now_epoch = _utc_now().timestamp()
    queues: list[QueueStatus] = []
    for queue_name, threshold in QUEUE_THRESHOLDS.items():
        cached_depth = await cache_service.client.get(_cached_queue_depth_key(queue_name))
        depth = int(cached_depth or 0)
        oldest = await cache_service.client.zrange(_queue_jobs_key(queue_name), 0, 0, withscores=True)
        oldest_job_age_seconds = None
        if oldest:
            oldest_job_age_seconds = max(0, int(now_epoch - float(oldest[0][1])))

        queue_status = "NORMAL"
        if depth > threshold * 2:
            queue_status = "CRITICAL"
        elif depth > threshold:
            queue_status = "BACKLOG"

        queues.append(
            QueueStatus(
                name=queue_name,
                depth=depth,
                oldest_job_age_seconds=oldest_job_age_seconds,
                threshold=threshold,
                status=queue_status,
            )
        )

    llm_providers = get_llm_provider_health()
    postgres_open = any(item.name == "postgres" and item.state == "OPEN" for item in circuits)
    any_non_closed_circuit = any(item.state != "CLOSED" for item in circuits)
    any_non_closed_llm = any(str(item.get("state")) != "CLOSED" for item in llm_providers)
    any_critical_queue = any(item.status == "CRITICAL" for item in queues)
    any_backlog_queue = any(item.status == "BACKLOG" for item in queues)

    overall_status = "HEALTHY"
    if postgres_open or any_critical_queue:
        overall_status = "CRITICAL"
    elif any_non_closed_circuit or any_non_closed_llm or any_backlog_queue:
        overall_status = "DEGRADED"

    return SystemHealthResponse(
        circuits=circuits,
        llm_providers=llm_providers,
        queues=queues,
        overall_status=overall_status,
        generated_at=_utc_now(),
    )


async def _list_all_tenants(
    session: AsyncSession,
    *,
    cursor: str | None,
    limit: int,
) -> AllTenantsResponse:
    now = _utc_now()
    stale_cutoff = now - timedelta(days=7)

    memory_counts_subquery = (
        select(
            ProxyUser.tenant_id.label("tenant_id"),
            func.count(Memory.id).label("memory_count"),
        )
        .select_from(ProxyUser)
        .outerjoin(Memory, Memory.proxy_user_id == ProxyUser.id)
        .group_by(ProxyUser.tenant_id)
        .subquery()
    )
    active_users_subquery = (
        select(
            ProxyUser.tenant_id.label("tenant_id"),
            func.count(ProxyUser.id).label("active_users_7d"),
        )
        .where(ProxyUser.last_active_at > stale_cutoff)
        .group_by(ProxyUser.tenant_id)
        .subquery()
    )
    dead_jobs_subquery = (
        select(
            ExtractionJob.tenant_id.label("tenant_id"),
            func.count(ExtractionJob.id).label("dead_job_count"),
        )
        .where(ExtractionJob.status == ExtractionJobStatus.dead)
        .group_by(ExtractionJob.tenant_id)
        .subquery()
    )
    last_api_call_subquery = (
        select(
            ApiKey.tenant_id.label("tenant_id"),
            func.max(ApiKey.last_used_at).label("last_api_call"),
        )
        .group_by(ApiKey.tenant_id)
        .subquery()
    )

    quota_mode_expr = _quota_mode_expression()
    quota_pct_expr = case(
        (
            or_(TenantBudget.monthly_call_limit.is_(None), TenantBudget.monthly_call_limit == 0),
            literal(0.0),
        ),
        else_=cast(func.coalesce(TenantBudget.current_month_calls, 0), Float)
        / cast(TenantBudget.monthly_call_limit, Float),
    )
    dead_job_count_expr = func.coalesce(dead_jobs_subquery.c.dead_job_count, 0)
    last_api_call_expr = last_api_call_subquery.c.last_api_call
    needs_attention_rank = case(
        (
            or_(
                quota_mode_expr != literal(QuotaMode.full.value),
                dead_job_count_expr > 0,
                and_(last_api_call_expr.is_not(None), last_api_call_expr < stale_cutoff),
            ),
            1,
        ),
        else_=0,
    )
    last_api_call_sort = func.coalesce(last_api_call_expr, literal(EPOCH_UTC))

    stmt = (
        select(
            Tenant.id.label("tenant_id"),
            Tenant.company_name.label("company_name"),
            Tenant.plan_tier.label("plan_tier"),
            quota_mode_expr.label("quota_mode"),
            quota_pct_expr.label("quota_pct"),
            func.coalesce(memory_counts_subquery.c.memory_count, 0).label("memory_count"),
            func.coalesce(active_users_subquery.c.active_users_7d, 0).label("active_users_7d"),
            dead_job_count_expr.label("dead_job_count"),
            last_api_call_expr.label("last_api_call"),
            needs_attention_rank.label("needs_attention_rank"),
        )
        .select_from(Tenant)
        .outerjoin(TenantBudget, TenantBudget.tenant_id == Tenant.id)
        .outerjoin(memory_counts_subquery, memory_counts_subquery.c.tenant_id == Tenant.id)
        .outerjoin(active_users_subquery, active_users_subquery.c.tenant_id == Tenant.id)
        .outerjoin(dead_jobs_subquery, dead_jobs_subquery.c.tenant_id == Tenant.id)
        .outerjoin(last_api_call_subquery, last_api_call_subquery.c.tenant_id == Tenant.id)
    )

    if cursor:
        cursor_rank, cursor_last_api_call, cursor_tenant_id = _decode_tenant_cursor(cursor)
        cursor_last_api_sort = cursor_last_api_call or EPOCH_UTC
        stmt = stmt.where(
            or_(
                needs_attention_rank < cursor_rank,
                and_(needs_attention_rank == cursor_rank, last_api_call_sort < cursor_last_api_sort),
                and_(
                    needs_attention_rank == cursor_rank,
                    last_api_call_sort == cursor_last_api_sort,
                    Tenant.id < cursor_tenant_id,
                ),
            )
        )

    stmt = stmt.order_by(
        desc(needs_attention_rank),
        desc(last_api_call_expr).nullslast(),
        desc(Tenant.id),
    ).limit(limit + 1)

    rows = list((await session.execute(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        last_row = rows[limit - 1]
        next_cursor = _encode_tenant_cursor(
            needs_attention_rank=int(last_row.needs_attention_rank or 0),
            last_api_call=last_row.last_api_call,
            tenant_id=last_row.tenant_id,
        )
        rows = rows[:limit]

    return AllTenantsResponse(
        tenants=[
            TenantSummary(
                tenant_id=str(row.tenant_id),
                company_name=row.company_name,
                plan_tier=row.plan_tier.value if isinstance(row.plan_tier, PlanTier) else str(row.plan_tier),
                quota_mode=str(row.quota_mode),
                quota_pct=round(float(row.quota_pct or 0.0), 4),
                memory_count=int(row.memory_count or 0),
                active_users_7d=int(row.active_users_7d or 0),
                dead_job_count=int(row.dead_job_count or 0),
                last_api_call=row.last_api_call,
                needs_attention=bool(row.needs_attention_rank),
            )
            for row in rows
        ],
        next_cursor=next_cursor,
        limit=limit,
        generated_at=now,
    )


async def _get_internal_tenant_detail(
    session: AsyncSession,
    *,
    cache_service: CacheService,
    tenant_id: str,
) -> TenantDetail:
    tenant_uuid = uuid.UUID(tenant_id)
    tenant = await session.get(Tenant, tenant_uuid)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")

    tenant_budget = (
        await session.execute(select(TenantBudget).where(TenantBudget.tenant_id == tenant_uuid))
    ).scalar_one_or_none()
    envelope = await QuotaManager(session=session, cache_service=cache_service).get_quota_envelope(tenant_id)
    usage = TenantUsageData(
        calls_used=int(getattr(tenant_budget, "current_month_calls", 0) or 0),
        calls_limit=getattr(tenant_budget, "monthly_call_limit", None),
        tokens_used=int(getattr(tenant_budget, "current_month_tokens", 0) or 0),
        tokens_limit=getattr(tenant_budget, "monthly_token_limit", None),
        mode=envelope.mode.value,
        budget_remaining_pct=envelope.budget_remaining_pct,
        reset_at=envelope.reset_at,
        plan_tier=tenant.plan_tier.value,
    )

    recent_jobs = (
        await session.execute(
            select(ExtractionJob)
            .where(ExtractionJob.tenant_id == tenant_uuid)
            .order_by(ExtractionJob.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    now = _utc_now()
    week_start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    quality_summary_row = (
        await session.execute(
            select(
                func.count(CallQualityLog.id).label("total_calls"),
                func.coalesce(
                    func.sum(
                        case((cast(CallQualityLog.layer_blocked_at, String) != "NONE", 1), else_=0)
                    ),
                    0,
                ).label("blocked_calls"),
                func.coalesce(func.sum(case((cast(CallQualityLog.layer_blocked_at, String) == "L1", 1), else_=0)), 0).label("l1"),
                func.coalesce(func.sum(case((cast(CallQualityLog.layer_blocked_at, String) == "L2", 1), else_=0)), 0).label("l2"),
                func.coalesce(func.sum(case((cast(CallQualityLog.layer_blocked_at, String) == "L3", 1), else_=0)), 0).label("l3"),
                func.coalesce(func.sum(case((cast(CallQualityLog.layer_blocked_at, String) == "L4", 1), else_=0)), 0).label("l4"),
            ).where(
                CallQualityLog.tenant_id == tenant_uuid,
                CallQualityLog.created_at >= week_start,
            )
        )
    ).one()

    total_calls = int(quality_summary_row.total_calls or 0)
    blocked_calls = int(quality_summary_row.blocked_calls or 0)
    current_month_tokens = int(getattr(tenant_budget, "current_month_tokens", 0) or 0)

    return TenantDetail(
        tenant=InternalTenantRecord(
            tenant_id=str(tenant.id),
            company_name=tenant.company_name,
            plan_tier=tenant.plan_tier.value,
            created_at=tenant.created_at,
        ),
        usage=usage,
        recent_jobs=[
            RecentExtractionJob(
                id=str(job.id),
                status=job.status.value if isinstance(job.status, ExtractionJobStatus) else str(job.status),
                proxy_user_id=_truncate_identifier(job.proxy_user_id),
                created_at=job.created_at,
                processing_started_at=job.processing_started_at,
                completed_at=job.completed_at,
                attempts=int(job.attempts or 0),
                error=_truncate_text(job.error, 100),
            )
            for job in recent_jobs
        ],
        quality_summary=QualitySummary(
            total_calls=total_calls,
            blocked_calls=blocked_calls,
            block_rate=round((blocked_calls / total_calls), 4) if total_calls > 0 else 0.0,
            by_layer={
                "L1": int(quality_summary_row.l1 or 0),
                "L2": int(quality_summary_row.l2 or 0),
                "L3": int(quality_summary_row.l3 or 0),
                "L4": int(quality_summary_row.l4 or 0),
            },
        ),
        cost_estimate_mtd=round((current_month_tokens / 1_000_000) * 0.15, 4),
        cost_is_estimate=True,
    )


async def _get_system_cost_summary(session: AsyncSession) -> CostSummaryResponse:
    now = _utc_now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    totals_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(TenantBudget.current_month_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(TenantBudget.current_month_calls), 0).label("total_calls"),
            )
        )
    ).one()
    total_tokens_mtd = int(totals_row.total_tokens or 0)
    total_calls_mtd = int(totals_row.total_calls or 0)
    total_estimated_cost_usd = round((total_tokens_mtd / 1_000_000) * 0.15, 4)
    avg_cost_per_call = round(total_estimated_cost_usd / total_calls_mtd, 6) if total_calls_mtd > 0 else None

    top_rows = (
        await session.execute(
            select(
                Tenant.id.label("tenant_id"),
                Tenant.company_name.label("company_name"),
                func.coalesce(TenantBudget.current_month_tokens, 0).label("tokens"),
            )
            .select_from(Tenant)
            .outerjoin(TenantBudget, TenantBudget.tenant_id == Tenant.id)
            .order_by(desc(func.coalesce(TenantBudget.current_month_tokens, 0)), desc(Tenant.id))
            .limit(5)
        )
    ).all()

    blocked_row = (
        await session.execute(
            select(func.count(CallQualityLog.id).label("blocked_calls")).where(
                CallQualityLog.created_at >= func.date_trunc("month", func.now()),
                cast(CallQualityLog.layer_blocked_at, String) != "NONE",
            )
        )
    ).one()
    total_gate_blocks_mtd = int(blocked_row.blocked_calls or 0)

    return CostSummaryResponse(
        total_tokens_mtd=total_tokens_mtd,
        total_estimated_cost_usd=total_estimated_cost_usd,
        top_5_tenants_by_cost=[
            CostSummaryTenant(
                tenant_id=row.tenant_id,
                company_name=row.company_name,
                tokens=int(row.tokens or 0),
                estimated_cost_usd=round((int(row.tokens or 0) / 1_000_000) * 0.15, 4),
            )
            for row in top_rows
        ],
        avg_cost_per_call=avg_cost_per_call,
        total_gate_blocks_mtd=total_gate_blocks_mtd,
        estimated_savings_from_gate_usd=round(total_gate_blocks_mtd * (avg_cost_per_call or 0.0), 4),
        projected_month_cost_usd=round((total_estimated_cost_usd / now.day) * days_in_month, 4),
        cost_is_estimate=True,
    )


async def _list_backfill_jobs(session: AsyncSession) -> list[BackfillJobResponse]:
    result = await session.execute(
        text(
            """
            SELECT
                id,
                task_name,
                status,
                total_rows,
                processed_rows,
                started_at,
                completed_at,
                error
            FROM backfill_jobs
            ORDER BY started_at DESC
            """
        )
    )

    now = _utc_now()
    jobs: list[BackfillJobResponse] = []
    for row in result.mappings().all():
        total_rows = row["total_rows"]
        processed_rows = int(row["processed_rows"] or 0)
        pct_complete = None
        if total_rows:
            pct_complete = round((processed_rows / int(total_rows)) * 100, 4)

        eta_seconds = None
        if row["status"] == "running" and total_rows and processed_rows > 0 and row["started_at"] is not None:
            elapsed = max(0.0, (now - row["started_at"]).total_seconds())
            remaining = max(0, int(total_rows) - processed_rows)
            eta_seconds = int((elapsed / processed_rows) * remaining)

        jobs.append(
            BackfillJobResponse(
                id=row["id"],
                task_name=row["task_name"],
                status=row["status"],
                total_rows=total_rows,
                processed_rows=processed_rows,
                pct_complete=pct_complete,
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                error=row["error"],
                eta_seconds=eta_seconds,
            )
        )

    return jobs


async def _list_audit_logs(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    action: str | None,
    start_date: date | None,
    end_date: date | None,
    cursor: str | None,
    limit: int,
) -> AuditLogsResponse:
    now = _utc_now()
    if start_date is None and end_date is None:
        end_dt = now
        start_dt = now - timedelta(days=1)
        start_date_value = start_dt.date()
        end_date_value = end_dt.date()
    else:
        start_date_value = start_date or ((end_date or now.date()) - timedelta(days=1))
        end_date_value = end_date or now.date()
        start_dt = datetime.combine(start_date_value, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end_date_value, time.max, tzinfo=UTC)

    try:
        offset = max(0, int(cursor or "0"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor") from exc

    limit = max(1, min(limit, 200))

    where_clauses = [
        "al.created_at >= :start_dt",
        "al.created_at <= :end_dt",
    ]
    params: dict[str, Any] = {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "limit": limit,
        "offset": offset,
    }
    if tenant_id:
        where_clauses.append("pu.tenant_id = CAST(:tenant_id AS uuid)")
        params["tenant_id"] = tenant_id
    if action:
        where_clauses.append("CAST(al.action AS text) = :action")
        params["action"] = action

    where_sql = " AND ".join(where_clauses)
    total_count = int(
        (
            await session.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total_count
                    FROM audit_logs al
                    LEFT JOIN proxy_users pu ON al.proxy_user_id = pu.id
                    LEFT JOIN tenants t ON pu.tenant_id = t.id
                    WHERE {where_sql}
                    """
                ),
                params,
            )
        ).scalar_one()
        or 0
    )

    result = await session.execute(
        text(
            f"""
            SELECT
                al.id,
                pu.tenant_id,
                t.company_name,
                CAST(al.action AS text) AS action,
                al.memory_id,
                al.created_at,
                al.ip_address,
                al.old_value,
                al.new_value,
                al.metadata AS metadata_json
            FROM audit_logs al
            LEFT JOIN proxy_users pu ON al.proxy_user_id = pu.id
            LEFT JOIN tenants t ON pu.tenant_id = t.id
            WHERE {where_sql}
            ORDER BY al.created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )

    entries = [
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "company_name": row["company_name"],
            "action": row["action"],
            "memory_id": row["memory_id"],
            "created_at": row["created_at"],
            "ip_address": row["ip_address"],
            "old_value_summary": None if row["old_value"] is None else str(row["old_value"])[:100],
            "new_value_summary": None if row["new_value"] is None else str(row["new_value"])[:100],
            "metadata": row["metadata_json"] or None,
        }
        for row in result.mappings().all()
    ]

    next_cursor = str(offset + limit) if (offset + limit) < total_count else None
    return AuditLogsResponse(
        data=entries,
        next_cursor=next_cursor,
        total_count=total_count,
        start_date=start_date_value,
        end_date=end_date_value,
    )


async def _discard_dead_letter_job(
    session: AsyncSession,
    *,
    request: Request,
    job_id: str,
) -> DeadLetterDiscardResponse:
    job = await session.get(ExtractionJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead_letter_job_not_found")
    if job.status != ExtractionJobStatus.dead:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="dead_letter_job_not_dead")

    session.add(
        AuditLog(
            action=AuditAction.job_discarded,
            proxy_user_id=job.proxy_user_id,
            metadata_json={
                "job_id": str(job.id),
                "tenant_id": str(job.tenant_id),
                "discarded_by": "operator",
            },
            ip_address=request.client.host if request.client else None,
        )
    )
    await session.delete(job)
    await session.commit()
    return DeadLetterDiscardResponse(discarded=True, job_id=str(job.id))


def _tenant_budget_to_record(tenant_budget: TenantBudget) -> TenantBudgetRecord:
    return TenantBudgetRecord(
        id=str(tenant_budget.id),
        tenant_id=str(tenant_budget.tenant_id),
        plan_tier=tenant_budget.plan_tier.value,
        monthly_call_limit=tenant_budget.monthly_call_limit,
        monthly_token_limit=tenant_budget.monthly_token_limit,
        current_month_calls=int(tenant_budget.current_month_calls or 0),
        current_month_tokens=int(tenant_budget.current_month_tokens or 0),
        rate_limit_per_user_per_minute=tenant_budget.rate_limit_per_user_per_minute,
        overage_policy=tenant_budget.overage_policy.value,
        alert_threshold_pct=float(tenant_budget.alert_threshold_pct or 0.0),
        reset_at=tenant_budget.reset_at,
        created_at=tenant_budget.created_at,
        write_calls=int(tenant_budget.write_calls or 0),
        write_call_limit=tenant_budget.write_call_limit,
        read_calls=int(tenant_budget.read_calls or 0),
        read_limit=tenant_budget.read_limit,
        last_notified_mode=(
            tenant_budget.last_notified_mode.value if tenant_budget.last_notified_mode else None
        ),
        last_notified_pct=tenant_budget.last_notified_pct,
        alert_webhook_url=tenant_budget.alert_webhook_url,
        webhook_secret=tenant_budget.webhook_secret,
    )


@router.get("/system-health", response_model=SystemHealthResponse)
async def system_health(
    cache_service: CacheService = Depends(get_cache_service),
) -> SystemHealthResponse:
    return await _get_system_health(cache_service)


@router.post("/circuit/{circuit_name}/reset", response_model=CircuitStatus)
async def reset_circuit(
    circuit_name: str,
    request: Request,
    cache_service: CacheService = Depends(get_cache_service),
) -> CircuitStatus:
    return await _reset_circuit_breaker(
        request=request,
        cache_service=cache_service,
        circuit_name=circuit_name,
    )


@router.get("/all-tenants", response_model=AllTenantsResponse)
async def all_tenants(
    session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
    limit: int = 50,
) -> AllTenantsResponse:
    return await _list_all_tenants(session, cursor=cursor, limit=limit)


@router.get("/tenant/{tenant_id}", response_model=TenantDetail)
async def internal_tenant_detail(
    tenant_id: str,
    session: AsyncSession = Depends(get_db_session),
    cache_service: CacheService = Depends(get_cache_service),
) -> TenantDetail:
    return await _get_internal_tenant_detail(session, cache_service=cache_service, tenant_id=tenant_id)


@router.get("/cost-summary", response_model=CostSummaryResponse)
async def internal_cost_summary(
    session: AsyncSession = Depends(get_db_session),
) -> CostSummaryResponse:
    return await _get_system_cost_summary(session)


@router.patch("/tenants/{tenant_id}/plan", response_model=TenantBudgetRecord)
async def update_tenant_plan(
    tenant_id: str,
    payload: PlanChangeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TenantBudgetRecord:
    tenant_budget = (
        await session.execute(select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id)))
    ).scalar_one_or_none()
    if tenant_budget is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")

    await session.run_sync(
        lambda sync_session: apply_plan_limits(tenant_id, payload.plan_tier, sync_session)
    )
    await session.refresh(tenant_budget)
    return _tenant_budget_to_record(tenant_budget)


@router.delete("/dead-letter-jobs/{job_id}", response_model=DeadLetterDiscardResponse)
async def discard_dead_letter_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> DeadLetterDiscardResponse:
    return await _discard_dead_letter_job(session, request=request, job_id=job_id)


@router.get("/backfill-status", response_model=list[BackfillJobResponse])
async def backfill_status(
    session: AsyncSession = Depends(get_db_session),
) -> list[BackfillJobResponse]:
    return await _list_backfill_jobs(session)


@router.get("/audit-logs", response_model=AuditLogsResponse)
async def audit_logs(
    session: AsyncSession = Depends(get_db_session),
    tenant_id: str | None = None,
    action: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> AuditLogsResponse:
    return await _list_audit_logs(
        session,
        tenant_id=tenant_id,
        action=action,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        limit=limit,
    )


@router.get("/reembedding-status")
async def reembedding_status(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        text(
            """
            SELECT
                id,
                task_name,
                status,
                total_rows,
                processed_rows,
                pct_complete,
                eta_seconds,
                started_at,
                completed_at,
                error
            FROM backfill_jobs
            WHERE task_name LIKE 'reembed_tenant:%'
            ORDER BY started_at DESC
            """
        )
    )
    rows = []
    for row in result.mappings().all():
        rows.append(
            {
                "id": str(row["id"]),
                "task_name": row["task_name"],
                "status": row["status"],
                "total_rows": row["total_rows"],
                "processed_rows": row["processed_rows"],
                "pct_complete": float(row["pct_complete"] or 0),
                "eta_seconds": row["eta_seconds"],
                "started_at": None if row["started_at"] is None else row["started_at"].isoformat(),
                "completed_at": None if row["completed_at"] is None else row["completed_at"].isoformat(),
                "error": row["error"],
            }
        )

    return {
        "data": rows,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/lifecycle-report")
async def lifecycle_report(
    request: Request,
    cache_service: CacheService = Depends(get_cache_service),
) -> dict[str, object]:
    reports = await cache_service.get_lifecycle_reports()
    return {
        "data": reports,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/queue-depth")
async def queue_depth(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    cache_service: CacheService = Depends(get_cache_service),
) -> dict[str, object]:
    snapshot = await QueueRouter(session=session, cache_service=cache_service).inspect_all_queues()
    return {
        "data": snapshot,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/dead-letter-jobs")
async def dead_letter_jobs(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        select(DeadLetterJob, ExtractionJob)
        .join(ExtractionJob, DeadLetterJob.job_id == ExtractionJob.id)
        .where(ExtractionJob.status == ExtractionJobStatus.dead)
        .order_by(DeadLetterJob.created_at.desc())
        .limit(100)
    )
    rows = []
    for row in _result_all_rows(result):
        dead_letter, job = _unpack_dead_letter_row(row)
        rows.append(
            {
                "id": str(job.id),
                "tenant_id": str(job.tenant_id),
                "proxy_user_id": str(job.proxy_user_id),
                "external_user_id": job.external_user_id,
                "status": job.status.value,
                "attempts": int(job.attempts or 0),
                "queue_name": job.queue_name,
                "error": (dead_letter.error if dead_letter is not None else None) or job.error,
                "payload": (dead_letter.payload if dead_letter is not None else None) or job.payload,
                "queued_at": None
                if getattr(job, "created_at", None) is None and getattr(job, "queued_at", None) is None
                else (getattr(job, "created_at", None) or getattr(job, "queued_at", None)).isoformat(),
                "started_at": None
                if getattr(job, "processing_started_at", None) is None and getattr(job, "started_at", None) is None
                else (getattr(job, "processing_started_at", None) or getattr(job, "started_at", None)).isoformat(),
                "completed_at": None if job.completed_at is None else job.completed_at.isoformat(),
                "dead_lettered_at": None if job.dead_lettered_at is None else job.dead_lettered_at.isoformat(),
            }
        )
    return {
        "data": rows,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/dead-letter-jobs/{job_id}/retry")
async def retry_dead_letter_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    cache_service: CacheService = Depends(get_cache_service),
) -> dict[str, object]:
    job = await session.get(ExtractionJob, uuid.UUID(job_id))
    if job is None or job.status != ExtractionJobStatus.dead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead_letter_job_not_found")
    dead_letter = await session.execute(
        select(DeadLetterJob).where(DeadLetterJob.job_id == job.id)
    )
    dead_letter_row = _result_scalar_one_or_none(dead_letter)
    if dead_letter_row is not None and not isinstance(dead_letter_row, DeadLetterJob):
        dead_letter_row = None

    reservation = await QueueRouter(session=session, cache_service=cache_service).reserve_extraction_slot(
        tenant_id=str(job.tenant_id),
        job_id=str(job.id),
    )
    if reservation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tenant_queue_limit_reached")

    queue_name = reservation.queue_name
    payload = dict(job.payload or {})
    payload["queue_name"] = queue_name
    payload["plan_tier"] = reservation.plan_tier
    payload["job_id"] = str(job.id)

    job.status = ExtractionJobStatus.queued
    job.queue_name = queue_name
    job.payload = payload
    job.celery_task_id = None
    job.attempts = 0
    job.memories_created = 0
    job.error = None
    job.stale_after = None
    job.processing_started_at = None
    job.started_at = None
    job.completed_at = None
    job.dead_lettered_at = None
    job.updated_at = datetime.now(UTC)
    if dead_letter_row is not None:
        dead_letter_row.last_retried_at = datetime.now(UTC)
        await session.flush()
    await session.commit()

    celery_app.send_task(EXTRACTION_TASK_NAME, args=[payload], queue=queue_name)
    return {
        "data": {
            "job_id": str(job.id),
            "status": "queued",
            "queue_name": queue_name,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/embedding-models/activate/{model_id}")
async def activate_embedding_model(
    model_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    service = EmbeddingService(async_session=session)
    try:
        record = await service.set_active_model(model_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return {
        "data": {
            "id": record.id,
            "provider": record.provider,
            "model_name": record.model_name,
            "dimensions": record.dimensions,
            "qdrant_collection": record.qdrant_collection,
            "is_active": record.is_active,
            "deprecated_at": record.deprecated_at,
            "created_at": record.created_at,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/backfill/run/proxy-user-ids")
async def trigger_backfill_proxy_user_ids(
    request: Request,
    batch_size: int = 1000,
    sleep_between_batches_ms: int = 100,
) -> dict[str, object]:
    if batch_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="batch_size must be greater than zero",
        )
    if sleep_between_batches_ms < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sleep_between_batches_ms must be zero or greater",
        )

    task = run_backfill_proxy_user_ids.delay(
        batch_size=batch_size,
        sleep_between_batches_ms=sleep_between_batches_ms,
    )
    return {
        "data": {
            "task_name": "backfill_proxy_user_ids",
            "task_id": task.id,
            "status": "queued",
            "batch_size": batch_size,
            "sleep_between_batches_ms": sleep_between_batches_ms,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }
