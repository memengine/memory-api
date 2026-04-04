from __future__ import annotations

from typing import Any
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.db.database import SessionLocal
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.region_pool import DEFAULT_REGION_ID
from api.db.models import QuotaMode
from api.services.quota_manager import QuotaManager
from api.services.quota_manager import QuotaEnvelope


class QuotaEnvelopeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        envelope = QuotaEnvelope(
            mode=QuotaMode.full,
            budget_remaining_pct=1.0,
            reset_at=None,
        )

        tenant_id = getattr(request.state, "tenant_id", None)
        request_envelope = getattr(request.state, "quota_envelope", None)
        if isinstance(request_envelope, QuotaEnvelope):
            envelope = request_envelope
        elif tenant_id:
            region_id = getattr(request.state, "region_id", None) or DEFAULT_REGION_ID
            region_pool = getattr(request.app.state, "region_pool", None)
            cache_service = (
                region_pool.get_cache_service(region_id)
                if region_pool is not None
                else getattr(request.app.state, "cache_service", None)
            )
            quota_manager_factory = getattr(request.app.state, "quota_manager_factory", None)
            if cache_service is not None:
                try:
                    if region_pool is not None:
                        session_context = region_pool.get_db(region_id)
                    else:
                        session_context = SessionLocal()
                    async with session_context as session:
                        quota_manager = (
                            quota_manager_factory(session, cache_service)
                            if callable(quota_manager_factory)
                            else QuotaManager(session=session, cache_service=cache_service)
                        )
                        envelope = await quota_manager.get_quota_envelope(str(tenant_id))
                except Exception:
                    envelope = QuotaEnvelope(
                        mode=QuotaMode.full,
                        budget_remaining_pct=1.0,
                        reset_at=None,
                    )

        response.headers["X-MemoryOS-Quota-Mode"] = envelope.mode.value
        response.headers["X-MemoryOS-Budget-Remaining"] = f"{envelope.budget_remaining_pct:.4f}"
        response.headers["X-MemoryOS-Quota-Reset"] = envelope.reset_at.isoformat() if envelope.reset_at else ""
        try:
            registry = getattr(request.app.state, "circuit_breakers", None) or CircuitBreakerRegistry.get_instance()
            response.headers["X-MemoryOS-Circuit-Status"] = registry.overall_status_local()
        except Exception:
            response.headers["X-MemoryOS-Circuit-Status"] = "HEALTHY"
        return response
