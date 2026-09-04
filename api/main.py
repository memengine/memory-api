from __future__ import annotations

import logging
import os
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.benchmark_runtime_telemetry import BenchmarkRuntimeTelemetry
from api.benchmark_runtime_telemetry import runtime_telemetry_enabled
from api.db.cache import CacheService
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.region_pool import DEFAULT_REGION_ID
from api.infra.region_pool import RegionConnectionPool
from api.middleware.admin_auth import AdminAuthMiddleware
from api.middleware.auth import AuthMiddleware
from api.middleware.quota_envelope import QuotaEnvelopeMiddleware
from api.middleware.rate_limiter import RateLimiterMiddleware
from api.middleware.region import RegionMiddleware
from api.middleware.request_id import RequestContextMiddleware
from api.middleware.universal_auth import UniversalAuthMiddleware
from api.middleware.versioning import VersioningMiddleware
from api.routers import agents_router
from api.routers import api_keys_router
from api.routers import billing_router
from api.routers import internal_router
from api.routers import memories_router
from api.routers import tenant_router
from api.routers import uui_router
from api.routers import users_router
from api.routers import webhooks_router
from api.routers import razorpay_webhooks_router
from api.routers.universal import router as universal_router
from api.routers.common import get_request_id
from api.schemas.responses import ErrorResponse
from api.schemas.responses import HealthData
from api.schemas.responses import HealthResponse
from api.settings import get_settings


LOGGER = logging.getLogger("memoryos.main")


def _cors_allowed_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    origins = [origin.strip() for origin in configured if origin.strip()]
    if origins:
        return origins
    return ["http://localhost:3000", "http://localhost:3001", "http://localhost:3002"]


def _scrub_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Keep operational error telemetry from exporting customer data.

    MemoryOS may process conversations and identifiers. Error monitoring should
    receive only the minimum diagnostic shape, never request payloads, auth
    data, user details, breadcrumbs, arbitrary extras, or exception text that
    could echo a provider/request payload.
    """
    scrubbed = deepcopy(event)
    scrubbed.pop("user", None)
    scrubbed.pop("breadcrumbs", None)
    scrubbed.pop("extra", None)

    request = scrubbed.get("request")
    if isinstance(request, dict):
        method = request.get("method")
        scrubbed["request"] = {"method": method} if method else {}

    exception = scrubbed.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    value.pop("value", None)

    return scrubbed


def _dependency_status(*, breaker_state: str, service_available: bool = True) -> str:
    if not service_available:
        return "unavailable"
    if breaker_state == "OPEN":
        return "unavailable"
    if breaker_state == "HALF_OPEN":
        return "recovering"
    return "ok"


def _configure_sentry() -> None:
    settings = get_settings()
    sentry_dsn = settings.sentry_dsn.strip()
    if not sentry_dsn:
        return

    if sentry_sdk.Hub.current.client is not None:
        return

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=settings.app_env,
        release=settings.app_version,
        # Error monitoring must not receive request or user PII by default.
        # A deployment may opt in only after its privacy review explicitly permits it.
        send_default_pii=settings.sentry_send_default_pii,
        before_send=_scrub_sentry_event,
        integrations=[FastApiIntegration()],
    )


def _error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    error: str,
    details=None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=error,
        code=code,
        request_id=get_request_id(request),
        details=details,
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime_telemetry = None
    if runtime_telemetry_enabled(app_env=settings.app_env):
        runtime_telemetry = BenchmarkRuntimeTelemetry()
        await runtime_telemetry.start()
    app.state.circuit_breakers = CircuitBreakerRegistry.reset()
    universal_app = getattr(app.state, "universal_app", None)
    existing_cache_service = getattr(app.state, "cache_service", None)
    existing_qdrant_service = getattr(app.state, "qdrant_service", None)
    app.state.region_pool = None
    try:
        region_pool = RegionConnectionPool(app_env=settings.app_env)
        region_pool.initialize()
        app.state.region_pool = region_pool
        app.state.cache_service = existing_cache_service or region_pool.get_cache_service(DEFAULT_REGION_ID)
        if existing_qdrant_service is None:
            app.state.qdrant_service = QdrantService(client=region_pool.get_qdrant(DEFAULT_REGION_ID))
        else:
            app.state.qdrant_service = existing_qdrant_service
        if universal_app is not None:
            universal_app.state.region_pool = app.state.region_pool
            universal_app.state.cache_service = app.state.cache_service
            universal_app.state.qdrant_service = app.state.qdrant_service
    except Exception as exc:
        LOGGER.warning("RegionConnectionPool startup skipped: %s", exc)
        app.state.cache_service = existing_cache_service or CacheService()
        if existing_qdrant_service is None:
            try:
                app.state.qdrant_service = QdrantService()
            except Exception as qdrant_exc:
                LOGGER.warning("QdrantService startup skipped: %s", qdrant_exc)
                app.state.qdrant_service = None
        else:
            app.state.qdrant_service = existing_qdrant_service
        if universal_app is not None:
            universal_app.state.region_pool = app.state.region_pool
            universal_app.state.cache_service = app.state.cache_service
            universal_app.state.qdrant_service = app.state.qdrant_service
    try:
        yield
    finally:
        if runtime_telemetry is not None:
            await runtime_telemetry.stop()


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            error=exc.error,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=422,
            code="REQ_422",
            error="validation_error",
            details=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            request=request,
            status_code=exc.status_code,
            code="HTTP_ERROR",
            error=str(exc.detail),
            details=None,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled API exception", exc_info=exc)
        return _error_response(
            request=request,
            status_code=500,
            code="SRV_500",
            error="internal_server_error",
            details=None,
        )


def _build_universal_app() -> FastAPI:
    universal_app = FastAPI(
        title="MemoryOS Universal API",
        description="Cross-agent universal memory access",
        version=get_settings().app_version,
    )
    universal_app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-MemoryOS-UUI",
            "Idempotency-Key",
        ],
        expose_headers=[
            "X-MemoryOS-Processing",
        ],
        max_age=600,
    )
    _register_exception_handlers(universal_app)
    universal_app.include_router(universal_router)
    return universal_app


def create_app() -> FastAPI:
    _configure_sentry()
    settings = get_settings()
    app = FastAPI(
        title="MemoryOS API",
        description="Persistent memory infrastructure for AI agents",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.universal_app = _build_universal_app()
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(RateLimiterMiddleware)
    app.add_middleware(RegionMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(AdminAuthMiddleware)
    app.add_middleware(QuotaEnvelopeMiddleware)
    app.add_middleware(VersioningMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Admin-Secret",
            "X-MemoryOS-UUI",
            "X-MemoryOS-Quota-Mode",
            "X-MemoryOS-Budget-Remaining",
            "X-MemoryOS-Budget-Reset",
            "X-MemoryOS-Circuit-Status",
            "X-MemoryOS-Processing",
            "Idempotency-Key",
        ],
        expose_headers=[
            "X-MemoryOS-Quota-Mode",
            "X-MemoryOS-Budget-Remaining",
            "X-MemoryOS-Budget-Reset",
            "X-MemoryOS-Circuit-Status",
            "X-MemoryOS-Processing",
        ],
        max_age=600,
    )
    app.add_middleware(UniversalAuthMiddleware, universal_app=app.state.universal_app)
    _register_exception_handlers(app)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health_check(request: Request) -> HealthResponse:
        """Service health check for primary dependencies.

        Parameters: none.
        Responses: service status, dependency status summary, and API version.
        """
        registry = getattr(request.app.state, "circuit_breakers", None) or CircuitBreakerRegistry.get_instance()
        breaker_states = registry.get_health()
        overall_status = registry.overall_status()
        qdrant_status = _dependency_status(
            breaker_state=breaker_states.get("qdrant", "CLOSED"),
            service_available=getattr(request.app.state, "qdrant_service", None) is not None,
        )
        redis_status = _dependency_status(
            breaker_state=breaker_states.get("redis", "CLOSED"),
            service_available=getattr(request.app.state, "cache_service", None) is not None,
        )
        postgres_status = _dependency_status(
            breaker_state=breaker_states.get("postgres", "CLOSED"),
            service_available=True,
        )
        return HealthResponse(
            data=HealthData(
                status="ok" if overall_status == "HEALTHY" else overall_status.lower(),
                qdrant=qdrant_status,
                postgres=postgres_status,
                redis=redis_status,
                version=settings.app_version,
            ),
            request_id=get_request_id(request),
            timestamp=datetime.now(UTC),
        )

    @app.get("/v1/internal/circuit-health", tags=["internal"])
    async def circuit_health(request: Request) -> dict[str, object]:
        """Shared circuit breaker state for external dependencies.

        Parameters: none.
        Responses: circuit states for Redis, Gemini embed/extract, Qdrant, and PostgreSQL.
        """
        registry = getattr(request.app.state, "circuit_breakers", None) or CircuitBreakerRegistry.get_instance()
        return {
            "overall_status": registry.overall_status(),
            "breakers": registry.get_health(),
        }

    app.include_router(memories_router)
    app.include_router(internal_router)
    app.include_router(tenant_router)
    app.include_router(uui_router)
    app.include_router(users_router)
    app.include_router(api_keys_router)
    app.include_router(billing_router)
    app.include_router(agents_router)
    app.include_router(universal_router)
    app.include_router(webhooks_router)
    app.include_router(razorpay_webhooks_router)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Clerk JWT in the Authorization header as `Bearer <token>`.",
        }
        security_schemes["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "SDK API key in the Authorization header as `ApiKey <key>`.",
        }

        public_paths = {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/v1/billing/plans",
            "/v1/webhooks/clerk",
            "/v1/webhooks/razorpay",
        }
        for path, operations in schema.get("paths", {}).items():
            for operation in operations.values():
                if path in public_paths:
                    operation["security"] = []
                else:
                    operation["security"] = [{"BearerAuth": []}, {"ApiKeyAuth": []}]

        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app


app = create_app()
