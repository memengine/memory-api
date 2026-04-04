from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.celery_app import celery_app
from api.db.cache import CacheService
from api.db.database import get_db_session
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.services.agent_service import AgentService
from api.services.api_key_service import ApiKeyService
from api.services.context_builder import ContextBuilder
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import QualityGateService
from api.services.budget_governor import BudgetGovernor
from api.services.quota_manager import QuotaManager
from api.services.retriever import RetrieverService
from api.services.user_service import UserService
from api.services.webhook_service import WebhookService


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_authenticated_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise APIError(status_code=401, code="AUTH_001", error="unauthorized")
    return str(user_id)


def get_authenticated_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise APIError(status_code=403, code="AUTH_403", error="tenant_auth_required")
    return str(tenant_id)


def get_cache_service(request: Request) -> CacheService:
    region_id = getattr(request.state, "region_id", None) or "IN1"
    region_pool = getattr(request.app.state, "region_pool", None)
    if region_pool is not None:
        return region_pool.get_cache_service(region_id)
    cache_service = getattr(request.app.state, "cache_service", None)
    if cache_service is None:
        cache_service = CacheService()
        request.app.state.cache_service = cache_service
    return cache_service


def get_qdrant_service(request: Request) -> QdrantService:
    region_id = getattr(request.state, "region_id", None) or "IN1"
    region_pool = getattr(request.app.state, "region_pool", None)
    if region_pool is not None:
        return QdrantService(client=region_pool.get_qdrant(region_id))
    qdrant_service = getattr(request.app.state, "qdrant_service", None)
    if qdrant_service is None:
        qdrant_service = QdrantService()
        request.app.state.qdrant_service = qdrant_service
    return qdrant_service


def get_context_builder() -> ContextBuilder:
    return ContextBuilder()


def get_proxy_user_service(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
) -> ProxyUserService:
    return ProxyUserService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
        region_id=getattr(request.state, "region_id", None) or "IN1",
    )


def get_quota_manager(
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> QuotaManager:
    return QuotaManager(
        session=session,
        cache_service=cache_service,
        dispatch_task=celery_app.send_task,
    )


def get_memory_service(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    quota_manager: Annotated[QuotaManager, Depends(get_quota_manager)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> MemoryService:
    return MemoryService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
        quota_manager=quota_manager,
        proxy_user_service=proxy_user_service,
        dispatch_task=celery_app.send_task,
        region_id=getattr(request.state, "region_id", None) or "IN1",
    )


def get_quality_gate_service(
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> QualityGateService:
    budget_governor = BudgetGovernor(
        session=session,
        dispatch_task=celery_app.send_task,
    )
    return QualityGateService(
        session=session,
        cache_service=cache_service,
        budget_governor=budget_governor,
    )


def get_retriever_service(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    quota_manager: Annotated[QuotaManager, Depends(get_quota_manager)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> RetrieverService:
    return RetrieverService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
        quota_manager=quota_manager,
        proxy_user_service=proxy_user_service,
        region_id=getattr(request.state, "region_id", None) or "IN1",
    )


def get_user_service(session: DbSession) -> UserService:
    return UserService(session=session)


def get_api_key_service(session: DbSession) -> ApiKeyService:
    return ApiKeyService(session=session)


def get_agent_service(session: DbSession) -> AgentService:
    return AgentService(session=session)


def get_webhook_service(session: DbSession) -> WebhookService:
    return WebhookService(session=session)
